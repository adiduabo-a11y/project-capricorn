"""WebSocket RPC server: the daemon's front door.

Loopback only, token-authenticated. Every durable write goes through the shard;
every turn is traced.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .. import __version__, paths
from ..agent import TurnRunner
from ..config import CapriConfig, load_config, save_config, schema, set_value
from ..models import Router
from ..store import db
from ..tools import Policy, builtin_registry
from . import protocol
from .session import Session

log = logging.getLogger("capri.server")

SENT = object()  # marker: handler already replied / is streaming
Handler = Callable[["Client", dict[str, Any]], Awaitable[Any]]


class RpcError(Exception):
    def __init__(self, code: str, message: str | None = None, fatal: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.fatal = fatal


def _now_ms() -> int:
    return int(time.time() * 1000)


class Client:
    """One connected shell."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.session: Session | None = None
        self.authed = False

    async def send(self, message: dict[str, Any]) -> None:
        try:
            await self.ws.send(protocol.encode(message))
        except ConnectionClosed:
            pass

    @property
    def peer(self) -> str:
        addr = getattr(self.ws, "remote_address", None)
        return f"{addr[0]}:{addr[1]}" if addr else "?"


class CapriServer:
    def __init__(self, config: CapriConfig, conn: sqlite3.Connection) -> None:
        self.config = config
        self.conn = conn
        self.clients: set[Client] = set()
        self.started_at = _now_ms()
        self.workspace = os.path.abspath(config.policy.workspace or os.getcwd())
        self.registry = builtin_registry()
        self.policy = Policy(config.policy, self.workspace)
        if os.environ.get("CAPRI_FAKE_BRAIN") == "1":
            from ..agent.fake import ScriptedBrain

            log.warning("scripted brain enabled (CAPRI_FAKE_BRAIN=1)")
            self.brain = ScriptedBrain(self.workspace)
        else:
            self.brain = Router(config.models)
        self.confirms: dict[str, asyncio.Future[bool]] = {}
        self.methods: dict[str, Handler] = {
            "hello": self._hello,
            "ping": self._ping,
            "turn.send": self._turn_send,
            "turn.cancel": self._turn_cancel,
            "confirm.respond": self._confirm_respond,
            "config.get": self._config_get,
            "config.set": self._config_set,
            "config.schema": self._config_schema,
            "trace.get": self._trace_get,
            "memory.list": self._memory_list,
            "memory.search": self._memory_search,
            "session.info": self._session_info,
            "tools.list": self._tools_list,
            "models.status": self._models_status,
        }

    # ------------------------------------------------------------------ lifecycle

    async def run(self, host: str | None = None, port: int | None = None) -> None:
        host = host or self.config.server.host
        port = port or self.config.server.port
        async with serve(self._handle, host, port, max_size=8 * 1024 * 1024):
            log.info("listening on ws://%s:%d", host, port)
            print(f"[core] listening ws://{host}:{port}", flush=True)
            await asyncio.Future()  # run until cancelled

    async def _handle(self, ws: ServerConnection) -> None:
        client = Client(ws)
        self.clients.add(client)
        log.info("client connected: %s", client.peer)
        try:
            async for raw in ws:
                await self._dispatch(client, raw)
        except ConnectionClosed:
            pass
        except Exception:  # never take the daemon down for one client
            log.exception("connection handler failed for %s", client.peer)
        finally:
            self.clients.discard(client)
            if client.session is not None:
                client.session.close()
            log.info("client disconnected: %s", client.peer)

    async def _dispatch(self, client: Client, raw: str | bytes) -> None:
        try:
            data = protocol.decode(raw)
        except (ValueError, json.JSONDecodeError):
            await client.send(protocol.err(None, "bad_request", "frame must be a JSON object"))
            return

        request_id = data.get("id")
        method = data.get("method")
        params = data.get("params") or {}

        if not isinstance(method, str) or not isinstance(params, dict):
            await client.send(protocol.err(request_id, "bad_request"))
            return

        if not client.authed and method != "hello":
            await client.send(protocol.err(request_id, "unauthorized", fatal=True))
            await client.ws.close(protocol.CLOSE_UNAUTHORIZED, "hello required")
            return

        handler = self.methods.get(method)
        if handler is None:
            await client.send(protocol.err(request_id, "unknown_method", f"no method {method!r}"))
            return

        try:
            result = await handler(client, params)
        except RpcError as exc:
            await client.send(
                protocol.err(request_id, exc.code, exc.message, fatal=exc.fatal)
            )
            if exc.fatal:
                code = (
                    protocol.CLOSE_UNAUTHORIZED
                    if exc.code == "unauthorized"
                    else protocol.CLOSE_BAD_REQUEST
                )
                await client.ws.close(code, (exc.message or exc.code)[:100])
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("method %s failed", method)
            await client.send(protocol.err(request_id, "internal"))
            return

        if result is not SENT:
            await client.send(protocol.ok(request_id, result))

    # ------------------------------------------------------------------ methods

    async def _hello(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        if self.config.server.require_token:
            supplied = str(params.get("token") or "")
            expected = paths.token(create=False)
            if not expected or not hmac.compare_digest(supplied, expected):
                raise RpcError("unauthorized", "bad token", fatal=True)

        client.authed = True
        if client.session is None:
            client.session = Session.create(self.conn, paths.DEFAULT_SUBJECT)
        session = client.session
        log.info("session %s open (episode %d)", session.id, session.episode_id)

        await client.send(
            protocol.event(
                "session.opened",
                sessionId=session.id,
                episodeId=session.episode_id,
                subject=session.subject,
            )
        )
        return {
            "version": __version__,
            "sessionId": session.id,
            "episodeId": session.episode_id,
            "subject": session.subject,
            "vectorBackend": db.get_meta(self.conn, "vector_backend", "unknown"),
            "config": self.config.model_dump(mode="json"),
        }

    async def _ping(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "t": _now_ms()}

    async def _session_info(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(client)
        return {
            "sessionId": session.id,
            "episodeId": session.episode_id,
            "subject": session.subject,
            "messages": session.recent_messages(50),
            "turns": [t for t, task in session.turns.items() if not task.done()],
        }

    async def _turn_send(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(client)
        text = str(params.get("text") or "").strip()
        if not text:
            raise RpcError("invalid_args", "text is required")
        if len(text) > 32_000:
            raise RpcError("invalid_args", "text too long")

        turn_id = f"trn_{uuid.uuid4().hex[:12]}"
        task = asyncio.create_task(self._run_turn(client, session, turn_id, text))
        session.turns[turn_id] = task
        task.add_done_callback(lambda _t, tid=turn_id: session.turns.pop(tid, None))
        return {"turnId": turn_id}

    async def _turn_cancel(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(client)
        turn_id = str(params.get("turnId") or "")
        task = session.turns.get(turn_id)
        if task is None:
            raise RpcError("not_found", f"no active turn {turn_id!r}")
        task.cancel()
        return {"cancelled": turn_id}

    async def _config_get(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return self.config.model_dump(mode="json")

    async def _config_set(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path")
        if not isinstance(path, str) or "value" not in params:
            raise RpcError("invalid_args", "path and value are required")
        try:
            updated = set_value(self.config, path, params["value"])
        except (KeyError, ValueError) as exc:
            raise RpcError("invalid_args", str(exc)) from exc
        try:
            save_config(updated)
        except OSError as exc:
            raise RpcError("internal", f"could not write config: {exc}") from exc
        self.config = updated
        db.trace(self.conn, span="config", name="config.set", payload={"path": path})
        return updated.model_dump(mode="json")

    async def _config_schema(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return schema()

    async def _trace_get(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 50)
        return {"traces": db.recent_traces(self.conn, limit)}

    async def _memory_list(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(params.get("limit") or 50), 500))
        rows = self.conn.execute(
            "SELECT id, kind, text, importance, confidence, created_at, accessed_at, access_count"
            " FROM memories WHERE subject = ? AND deleted_at IS NULL AND superseded_by IS NULL"
            " ORDER BY importance DESC, accessed_at DESC LIMIT ?",
            (paths.DEFAULT_SUBJECT, limit),
        ).fetchall()
        return {"items": [dict(row) for row in rows]}

    async def _memory_search(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("q") or "").strip()
        if not query:
            raise RpcError("invalid_args", "q is required")
        limit = max(1, min(int(params.get("k") or 10), 50))
        try:
            rows = self.conn.execute(
                "SELECT m.id, m.kind, m.text, m.importance, m.confidence"
                " FROM memory_fts f JOIN memories m ON m.id = f.rowid"
                " WHERE memory_fts MATCH ? AND m.deleted_at IS NULL AND m.superseded_by IS NULL"
                " ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.Error:
            rows = self.conn.execute(
                "SELECT id, kind, text, importance, confidence FROM memories"
                " WHERE text LIKE ? AND deleted_at IS NULL ORDER BY importance DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "mode": "fts"}

    # ------------------------------------------------------------------ turn loop

    def _require_session(self, client: Client) -> Session:
        if client.session is None:
            raise RpcError("unauthorized", "hello first")
        return client.session

    async def _run_turn(
        self, client: Client, session: Session, turn_id: str, text: str
    ) -> None:
        runner = TurnRunner(
            config=self.config,
            conn=self.conn,
            session=session,
            emit=client.send,
            confirm=lambda request: self._ask_user(client, request),
            brain=self.brain,
            registry=self.registry,
            policy=self.policy,
            workspace=self.workspace,
        )
        try:
            await runner.run(turn_id, text)
        except asyncio.CancelledError:
            log.info("turn %s cancelled", turn_id)
            raise
        except Exception:
            log.exception("turn %s failed", turn_id)
            await client.send(
                protocol.event("error", code="internal", message="turn failed", fatal=False)
            )
            await client.send(protocol.state("idle", "error"))

    async def _ask_user(self, client: Client, request: dict[str, Any]) -> bool:
        """Emit a confirmation card and wait for the shell's answer."""
        confirm_id = str(request["confirmId"])
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.confirms[confirm_id] = future
        await client.send(protocol.event("agent.confirm", **request))
        try:
            return await asyncio.wait_for(
                future, timeout=min(int(request.get("timeout") or 180), 3600)
            )
        except asyncio.TimeoutError:
            log.info("confirmation %s timed out", confirm_id)
            return False
        finally:
            self.confirms.pop(confirm_id, None)

    async def _confirm_respond(
        self, client: Client, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_session(client)
        confirm_id = str(params.get("confirmId") or "")
        future = self.confirms.get(confirm_id)
        if future is None or future.done():
            raise RpcError("not_found", f"no pending confirmation {confirm_id!r}")
        future.set_result(bool(params.get("approved")))
        return {"confirmId": confirm_id, "approved": bool(params.get("approved"))}

    async def _tools_list(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "tier": spec.tier,
                    "params": spec.params,
                }
                for spec in self.registry.specs()
            ],
            "workspace": self.workspace,
        }

    async def _models_status(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": self.config.models.endpoint,
            "online": await self.brain.available(),
            "planner": self.config.models.planner.name,
            "reflex": self.config.models.reflex.name,
            "structured": self.config.models.structured,
        }


async def run_server(config: CapriConfig | None = None, *, host: str | None = None,
                     port: int | None = None) -> None:
    cfg = config or load_config()
    paths.ensure_dirs()
    paths.token()  # make sure the shell can authenticate on first run
    conn = db.open_shard(paths.DEFAULT_SUBJECT)
    server = CapriServer(cfg, conn)
    try:
        await server.run(host, port)
    finally:
        await server.brain.aclose()
        conn.close()
