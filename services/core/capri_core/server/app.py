"""WebSocket RPC server: the daemon's front door.

Loopback only, token-authenticated. Every durable write goes through the shard;
every turn is traced.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .. import __version__, paths
from ..agent import TurnRunner, prompt
from ..blackwall import PROPOSE_SPEC, Blackwall
from ..companion import ActivitySnapshot, Companion, step_label
from ..config import CapriConfig, load_config, save_config, schema, set_value
from ..mcp import McpService
from ..memory import MemoryService
from ..models import Router
from ..notes import VaultService
from ..rag import RagService
from ..screen import ScreenService, observation_payload
from ..store import db
from ..store.shard import export_shard, import_shard
from ..tools import Policy, builtin_registry
from ..voice import VoiceService, build_engine
from ..work.tasks import Task, TaskService
from ..workspace import WorkspaceService
from . import protocol
from .routes_knowledge import KnowledgeRoutesMixin
from .routes_mcp import MCP_REGISTER_SPEC, McpRoutesMixin
from .routes_voice import VoiceRoutesMixin
from .routes_work import WorkRoutesMixin
from .rpc import Client, Handler, RpcError, _now_ms
from .scheduler import IdleScheduler
from .session import Session

log = logging.getLogger("capri.server")

# A turn only joins the open task when it reads like more of that work. These
# are conversation, not the next step: binding them to the task meant a casual
# line resumed an edit the user had moved on from. The same reading decides
# whether the turn's prompt says there is nothing to do yet.
_is_conversation = prompt.looks_like_conversation


class CapriServer(
    McpRoutesMixin,
    KnowledgeRoutesMixin,
    VoiceRoutesMixin,
    WorkRoutesMixin,
):
    def __init__(self, config: CapriConfig, conn: sqlite3.Connection) -> None:
        self.config = config
        self.conn = conn
        self.clients: set[Client] = set()
        self.started_at = _now_ms()
        self.workspace = os.path.abspath(config.policy.workspace or os.getcwd())
        self.registry = builtin_registry()
        self.screen = ScreenService(config, self.workspace)
        self.policy = Policy(config.policy, self.workspace)
        self.policy.screen = self.screen
        self.blackwall = Blackwall(self.registry, conn, self.policy)
        self.blackwall.load()
        # Configured MCP servers are started with the daemon; each tool they
        # advertise joins the same registry the built-ins live in.
        self.mcp = McpService(config, self.registry)
        try:
            self.registry.register(PROPOSE_SPEC, self.blackwall.propose_tool)
        except ValueError:
            # A legacy install could already own this name; load() rejects it
            # from new proposals, so keep the daemon bootable.
            log.exception("tool.propose was already registered")
        try:
            self.registry.register(MCP_REGISTER_SPEC, self._mcp_register)
        except ValueError:
            log.exception("mcp.register was already registered")
        if os.environ.get("CAPRI_FAKE_BRAIN") == "1":
            from ..agent.fake import ScriptedBrain

            log.warning("scripted brain enabled (CAPRI_FAKE_BRAIN=1)")
            self.brain = ScriptedBrain(self.workspace)
        else:
            self.brain = Router(config.models)
        self.blackwall.critic = self._review_tool
        self.memory = MemoryService(config, conn, self.brain, paths.DEFAULT_SUBJECT)
        self.rag = RagService(
            config,
            conn,
            self.brain,
            self.memory.embedder,
            workspace=self.workspace,
        )
        # Tellings in flight: fire-and-forget broadcasts of the vault pass, so
        # nothing here waits on a socket and an event is never lost to the GC.
        self._notes_events: set[asyncio.Task[Any]] = set()
        self.notes = VaultService(config, conn, self.rag, on_state=self._notes_state)
        # The project, if one was open when the daemon last stopped. Opening
        # one is a shell RPC; the model never gets this capability.
        self.workspaces = WorkspaceService(conn, config, busy=self._turns_busy)
        self.tasks = TaskService(conn)
        restored = self.workspaces.restore()
        if restored is not None:
            self._adopt_workspace(restored)
        self.voice = VoiceService(config, build_engine(config), conn=conn)
        self._voice_tasks: set[asyncio.Task[Any]] = set()
        self._reaction_tasks: set[asyncio.Task[Any]] = set()
        self._fake_brain = os.environ.get("CAPRI_FAKE_BRAIN") == "1"
        # bool for an ordinary card, a decision for one that carries tiers.
        self.confirms: dict[str, asyncio.Future[Any]] = {}
        self._profile_screen_pause = False
        self.scheduler = IdleScheduler(
            config,
            conn,
            paths.DEFAULT_SUBJECT,
            self.memory,
            self._active_episodes,
            self._active_turn_count,
            on_result=self._note_upkeep,
        )
        self.companion = Companion(
            config,
            self.screen,
            self.brain,
            self._broadcast_event,
            voice=self.voice,
            speak_allowed=self._ambient_speech_allowed,
            active_turns=self._active_turn_count,
            context=self._companion_context,
            ready=self._has_session,
            record=self._record_ambient,
            work=self._companion_snapshot,
            mic_active=self._mic_active,
        )
        self._apply_profile_pause(config.policy.profile)
        self.methods: dict[str, Handler] = {
            "hello": self._hello,
            "ping": self._ping,
            "turn.send": self._turn_send,
            "turn.cancel": self._turn_cancel,
            "confirm.respond": self._confirm_respond,
            "config.get": self._config_get,
            "config.set": self._config_set,
            "config.schema": self._config_schema,
            "mcp.servers": self._mcp_servers,
            "mcp.save": self._mcp_save,
            "mcp.try": self._mcp_try,
            "mcp.import": self._mcp_import,
            "mcp.remove": self._mcp_remove,
            "mcp.reload": self._mcp_reload,
            "trace.get": self._trace_get,
            "memory.list": self._memory_list,
            "memory.search": self._memory_search,
            "memory.write": self._memory_write,
            "memory.forget": self._memory_forget,
            "memory.merge": self._memory_merge,
            "memory.maintain": self._memory_maintain,
            "project.open": self._project_open,
            "project.current": self._project_current,
            "project.recent": self._project_recent,
            "project.close": self._project_close,
            "project.tree": self._project_tree,
            "project.inspect": self._project_inspect,
            "work.changes": self._work_changes,
            "work.artifact": self._work_artifact,
            "work.undo": self._work_undo,
            "task.current": self._task_current,
            "task.recent": self._task_recent,
            "task.continue": self._task_continue,
            "task.abandon": self._task_abandon,
            "notes.status": self._notes_status,
            "notes.set": self._notes_set,
            "notes.index": self._notes_index,
            "rag.ingest": self._rag_ingest,
            "rag.search": self._rag_search,
            "rag.collections": self._rag_collections,
            "rag.forget": self._rag_forget,
            "voice.speak": self._voice_speak,
            "voice.listen": self._voice_listen,
            "voice.wake": self._voice_wake,
            "voice.follow_up": self._voice_follow_up,
            "voice.warm": self._voice_warm,
            "voice.calibrate": self._voice_calibrate,
            "voice.stop": self._voice_stop,
            "voice.barge_in": self._voice_barge_in,
            "voice.status": self._voice_status,
            "mic.route": self._mic_route,
            "screen.status": self._screen_status,
            "screen.pause": self._screen_pause,
            "screen.windows": self._screen_windows,
            "screen.apps": self._screen_apps,
            "screen.observe": self._screen_observe,
            "screen.focus": self._screen_focus,
            "screen.launch": self._screen_launch,
            "screen.clipboard": self._screen_clipboard,
            "screen.act": self._screen_act,
            "session.history": self._session_history,
            "session.info": self._session_info,
            "session.new": self._session_new,
            "session.note": self._session_note,
            "tools.list": self._tools_list,
            "tools.stats": self._tools_stats,
            "tools.proposals": self._tools_proposals,
            "tools.installed": self._tools_installed,
            "tools.recipes": self._tools_recipes,
            "tools.recipe": self._tools_recipe,
            "tools.rollback": self._tools_rollback,
            "models.status": self._models_status,
            "scheduler.status": self._scheduler_status,
            "shard.export": self._shard_export,
            "shard.import": self._shard_import,
        }

    # ------------------------------------------------------------------ lifecycle

    async def run(self, host: str | None = None, port: int | None = None) -> None:
        host = host or self.config.server.host
        port = port or self.config.server.port
        self.scheduler.start()
        if not self._fake_brain:
            self.companion.start()
        # Her voice takes ~11 s to load (Pocket's base model plus her state).
        # Start that now, on a worker thread: the socket serves immediately and
        # her first reply after a restart does not open with that silence.
        # If model packages are missing, ensure_models downloads them first.
        async def _warm_voice() -> None:
            await self.voice.ensure_models()
            await self.voice.prewarm()

        speech_warm = asyncio.create_task(_warm_voice())
        origins: list[Any] = [
            None,
            "app://capri",
            re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"),
        ]
        try:
            async with serve(
                self._handle,
                host,
                port,
                origins=origins,
                max_size=16 * 1024 * 1024,
            ):
                log.info("listening on ws://%s:%d", host, port)
                print(f"[core] listening ws://{host}:{port}", flush=True)
                await asyncio.Future()  # run until cancelled
        finally:
            speech_warm.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await speech_warm
            for client in list(self.clients):
                if client.session is not None:
                    client.session.cancel_all()
            pending_tasks = [
                *list(self._voice_tasks),
                *list(self._reaction_tasks),
                *list(self._notes_events),
            ]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            await self.companion.stop()
            await self.scheduler.stop()

    async def _handle(self, ws: ServerConnection) -> None:
        client = Client(ws)
        self.clients.add(client)
        log.info("client connected: %s", client.peer)
        in_flight: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                task = asyncio.create_task(self._dispatch(client, raw))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        except ConnectionClosed:
            pass
        except Exception:  # never take the daemon down for one client
            log.exception("connection handler failed for %s", client.peer)
        finally:
            for task in list(in_flight):
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            was_mic_route = client.mic_route_active
            self.clients.discard(client)
            if client.session is not None:
                shared = any(
                    peer.session is not None
                    and peer.session.episode_id == client.session.episode_id
                    for peer in self.clients
                )
                if not shared:
                    client.session.close()
            if was_mic_route:
                await self.companion.silence()
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
            # A reconnect is the same conversation. Picking the episode back up
            # is what lets the panel rebuild its transcript from `session.info`
            # on a reload, and what keeps the planner's history across one.
            client.session = Session.resume(
                self.conn, paths.DEFAULT_SUBJECT
            ) or Session.create(self.conn, paths.DEFAULT_SUBJECT)
        session = client.session
        self.scheduler.touch()
        log.info("session %s open (episode %d)", session.id, session.episode_id)

        await client.send(
            protocol.event(
                "session.opened",
                sessionId=session.id,
                episodeId=session.episode_id,
                subject=session.subject,
                # A reconnect is not a new chat: the shell keeps what it shows.
                reason="connect",
            )
        )
        return {
            "version": __version__,
            "sessionId": session.id,
            "episodeId": session.episode_id,
            "subject": session.subject,
            "vectorBackend": db.get_meta(self.conn, "vector_backend", "unknown"),
            "workspace": self.workspaces.current(),
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
            # So the panel can say when the transcript it got is only the tail.
            "totalMessages": session.message_count(),
            "turns": [t for t, task in session.turns.items() if not task.done()],
            "workspace": self.workspaces.current(),
        }

    async def _session_history(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """One page further back in the conversation the panel is showing.

        `before` is the id of the oldest row the panel holds; the page is what
        sits above it, oldest first, so a rebuilt transcript can walk back past
        the window `session.info` hands out.
        """
        session = self._require_session(client)
        before = int(params.get("before") or 0)
        if before <= 0:
            raise RpcError("invalid_args", "before is required")
        limit = max(1, min(200, int(params.get("limit") or 50)))
        messages, has_more = session.messages_page(before, limit)
        return {"messages": messages, "hasMore": has_more}

    async def _session_note(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """Record a line she said outside a turn, without starting one.

        A bare wake gets a spoken acknowledgement, and the panel owns the audio.
        Without this the daemon's own history would not know she said it: the
        planner replays session messages, so "what did you just say?" had no
        answer. It is a message, not a turn - no task, no planner, no tokens.
        """
        session = self._require_session(client)
        text = " ".join(str(params.get("text") or "").split())
        if not text:
            raise RpcError("invalid_args", "text is required")
        if len(text) > 500:
            raise RpcError("invalid_args", "text too long")
        turn_id = str(params.get("turnId") or "").strip() or None
        recorded = session.add_note(text, turn_id)
        return {"turnId": recorded, "recorded": True}

    async def _session_new(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """Start a fresh conversation: a new episode, no transcript carried over.

        The old session is closed first, so any turn still running is cancelled
        and its episode gets an end time. Memory, documents and the open project
        are subject-level and survive: only the conversation starts over.
        """
        self._require_session(client)
        previous = client.session
        superseded: list[str] = []
        if previous is not None:
            # Captured before the close: these are the turns whose last events
            # can still be on the wire after this reset, and the shell must not
            # put them in the new conversation.
            superseded = list(previous.turns)
            # close() cancels whatever is still running and ends the episode.
            previous.close()
        session = Session.create(self.conn, paths.DEFAULT_SUBJECT)
        # A new chat starts with no task in hand, even when one is open in the
        # project. Work asked for here gets its own task.
        session.adopt_open_task = False
        client.session = session
        self.scheduler.touch()
        log.info("new chat: session %s (episode %d)", session.id, session.episode_id)
        await client.send(
            protocol.event(
                "session.opened",
                sessionId=session.id,
                episodeId=session.episode_id,
                subject=session.subject,
                reason="new",
                superseded=superseded,
            )
        )
        return {
            "sessionId": session.id,
            "episodeId": session.episode_id,
            "subject": session.subject,
            "previousSessionId": previous.id if previous is not None else "",
            "superseded": superseded,
        }

    async def _turn_send(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(client)
        if self._turns_busy():
            raise RpcError("busy", "a turn is already running")
        text = str(params.get("text") or "").strip()
        if not text:
            raise RpcError("invalid_args", "text is required")
        if len(text) > 32_000:
            raise RpcError("invalid_args", "text too long")

        # With a project open, every turn belongs to a task: the one already in
        # progress, or a new one titled from this request.
        task = self._task_for_turn(text, session)
        if task is not None:
            await self._broadcast_task(
                task, reason="start" if task.steps == 0 else "resume"
            )
        turn_id = await self._start_turn(
            client, session, text, task=task, screen=bool(params.get("screen"))
        )
        return {"turnId": turn_id, "taskId": task.id if task is not None else ""}

    async def _start_turn(
        self,
        client: Client,
        session: Session,
        text: str,
        *,
        task: Task | None = None,
        resume: bool = False,
        screen: bool = False,
    ) -> str:
        turn_id = f"trn_{uuid.uuid4().hex[:12]}"
        self.scheduler.touch()
        started = asyncio.create_task(
            self._run_turn(
                client, session, turn_id, text, task=task, resume=resume, screen=screen
            )
        )
        session.turns[turn_id] = started
        started.add_done_callback(lambda _t, tid=turn_id: session.turns.pop(tid, None))
        return turn_id

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
        self._reconfigure_all(updated)
        if path == "policy.profile":
            self._apply_profile_pause(updated.policy.profile)
            await self._broadcast_event(
                {"type": "screen.changed", "screen": self.screen.status()}
            )
        if path == "scheduler.enabled":
            if updated.scheduler.enabled:
                self.scheduler.start()
            else:
                await self.scheduler.stop()
        if path.startswith("companion."):
            await self.companion.stop()
            if updated.companion.enabled and not self._fake_brain:
                self.companion.start()
        db.trace(self.conn, span="config", name="config.set", payload={"path": path})
        return updated.model_dump(mode="json")

    def _reconfigure_all(self, updated: CapriConfig) -> None:
        """Push an updated config into every live subsystem without restarting."""
        self.config = updated
        # Policy and screen read their config live; rebinding those objects keeps
        # a settings change effective without restarting the daemon.
        self.policy.cfg = updated.policy
        self.screen.config = updated
        self.scheduler.config = updated
        self.memory.reconfigure(updated)
        self.rag.reconfigure(updated)
        self.notes.reconfigure(updated)
        self.workspaces.reconfigure(updated)
        self.mcp.config = updated.mcp
        self.voice.config = updated
        self.companion.config = updated
        if hasattr(self.brain, "reconfigure"):
            self.brain.reconfigure(updated.models)
        elif hasattr(self.brain, "cfg"):
            self.brain.cfg = updated.models

    async def _config_schema(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return schema()

    async def _trace_get(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 50)
        return {"traces": db.recent_traces(self.conn, limit)}

    # ------------------------------------------------------------------ screen

    async def _screen_status(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return self.screen.status()

    async def _screen_pause(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        if "paused" in params:
            paused = bool(params.get("paused"))
        else:
            paused = not bool(self.screen.status().get("paused"))
        reason = str(params.get("reason") or "paused from the shell")
        status = self.screen.pause(paused, reason)
        # Every shell shows the eyes; a pause from one has to reach the others
        # without waiting for a poll.
        await self._broadcast_event({"type": "screen.changed", "screen": status})
        return status

    async def _screen_apps(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        try:
            return await asyncio.to_thread(self.screen.running_apps)
        except Exception as exc:
            raise RpcError("screen_unavailable", str(exc)) from exc

    async def _screen_windows(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        try:
            return await asyncio.to_thread(self.screen.windows)
        except Exception as exc:
            raise RpcError("screen_unavailable", str(exc)) from exc

    async def _screen_focus(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        raw = params.get("windowId") or params.get("window_id")
        if raw is None:
            raise RpcError("invalid_args", "windowId is required")
        try:
            window_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise RpcError("invalid_args", "windowId must be an integer") from exc
        try:
            return await asyncio.to_thread(self.screen.focus, window_id)
        except Exception as exc:
            raise RpcError("screen_error", str(exc)) from exc

    async def _screen_launch(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        app = str(params.get("app") or "").strip()
        if not app:
            raise RpcError("invalid_args", "app is required")
        try:
            return await asyncio.to_thread(self.screen.launch, app)
        except Exception as exc:
            raise RpcError("screen_error", str(exc)) from exc

    async def _screen_observe(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        include_shot = bool(params.get("shot", True))
        marks = bool(params.get("marks", False))
        ocr = bool(params.get("ocr", False))
        try:
            observation = await asyncio.to_thread(
                self.screen.observe,
                include_shot=include_shot,
                marks=marks,
                ocr=ocr,
            )
        except Exception as exc:
            raise RpcError("screen_unavailable", str(exc)) from exc
        return observation_payload(observation, shot=include_shot)

    async def _screen_clipboard(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        mode = str(params.get("mode") or "").strip().lower()
        try:
            if mode == "read":
                return await asyncio.to_thread(self.screen.clipboard_read)
            if mode == "write":
                return await asyncio.to_thread(
                    self.screen.clipboard_write, str(params.get("text") or "")
                )
        except Exception as exc:
            raise RpcError("screen_error", str(exc)) from exc
        raise RpcError("invalid_args", "mode must be read or write")

    async def _screen_act(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        action = str(params.get("action") or "").strip()
        if not action:
            raise RpcError("invalid_args", "action is required")
        expect = params.get("expect")
        if expect is not None and not isinstance(expect, dict):
            raise RpcError("invalid_args", "expect must be an object")
        try:
            return await asyncio.to_thread(
                self.screen.act,
                action,
                element_id=str(params.get("elementId") or params.get("element_id") or ""),
                x=params.get("x"),
                y=params.get("y"),
                button=str(params.get("button") or "left"),
                text=str(params.get("text") or ""),
                keys=str(params.get("keys") or ""),
                amount=int(params.get("amount") or 0),
                x2=int(params["x2"]) if params.get("x2") is not None else None,
                y2=int(params["y2"]) if params.get("y2") is not None else None,
                expect=expect,
            )
        except Exception as exc:
            raise RpcError("screen_error", str(exc)) from exc


    # ------------------------------------------------------------------ turn loop

    def _require_session(self, client: Client) -> Session:
        if client.session is None:
            raise RpcError("unauthorized", "hello first")
        return client.session

    async def _run_turn(
        self,
        client: Client,
        session: Session,
        turn_id: str,
        text: str,
        *,
        task: Task | None = None,
        resume: bool = False,
        screen: bool = False,
    ) -> None:
        async def emit(event: dict[str, Any]) -> None:
            await client.send(event)
            # Her answer is starting. Any aside still being written for this
            # turn is now stale, and must not cut into the reply.
            if event.get("type") == "agent.state" and event.get("state") == "speaking":
                self.companion.note_answer(turn_id)
            if event.get("type") == "agent.step":
                tool = str(event.get("tool") or "")
                if tool and tool != "done":
                    self._note_work_line(turn_id, {"name": tool, "ok": True})
            # A reaction cannot fire mid-turn, so a finished tool call is only
            # noted here; the turn is asked for its line once it has settled.
            if event.get("type") == "agent.tool":
                args = event.get("args")
                self.companion.note_tool(
                    name=str(event.get("name") or ""),
                    ok=bool(event.get("ok")),
                    ms=int(event.get("ms") or 0),
                    args=args if isinstance(args, dict) else {},
                    output=str(event.get("output") or ""),
                )
                self._note_work_line(turn_id, event)

        runner = TurnRunner(
            config=self.config,
            conn=self.conn,
            session=session,
            emit=emit,
            confirm=lambda request: self._ask_user(client, request),
            choose=lambda request: self._ask_choice(client, request),
            brain=self.brain,
            registry=self.registry,
            policy=self.policy,
            workspace=self.workspace,
            work=self._work_context(task, resume=resume),
            project=self.workspaces,
            notes=self.notes,
            memory=self.memory,
            rag=self.rag,
            voice=self.voice,
            screen=self.screen,
            tasks=self.tasks,
            resume=resume,
            screen_task=screen,
        )
        try:
            await runner.run(turn_id, text)
            # The turn task is still counted as active here, so the flush is
            # detached: it runs after this coroutine returns.
            self._spawn_reaction_flush()
        except asyncio.CancelledError:
            log.info("turn %s cancelled", turn_id)
            raise
        except Exception:
            log.exception("turn %s failed", turn_id)
            await client.send(
                protocol.event("error", code="internal", message="turn failed", fatal=False)
            )
            await client.send(protocol.state("idle", "error"))
        finally:
            self.scheduler.touch()

    def _note_upkeep(self, result: dict[str, Any]) -> None:
        """A maintenance pass finished: ask for one short line about it.

        Called from the scheduler's own loop, so a task can be created here.
        Detached, like the working lines: housekeeping never waits on speech.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.companion.scheduled(result))
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    def _note_work_line(self, turn_id: str, event: dict[str, Any]) -> None:
        """Ask for one thinking-out-loud line about the step that just finished.

        Detached: the turn must never wait on a line, and the companion owns the
        throttling. Ten quick reads earn at most one line.
        """
        detail = step_label(str(event.get("name") or ""), bool(event.get("ok")))
        if not detail:
            return
        task = asyncio.create_task(
            self.companion.working(turn_id=turn_id, detail=detail)
        )
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    def _spawn_reaction_flush(self) -> None:
        task = asyncio.create_task(self._flush_reactions())
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    async def _flush_reactions(self) -> None:
        """Ask for one line about the tool events the turn queued.

        A reaction is refused while a turn is active, and this task is created
        from inside the turn, so it retries briefly until the turn counter has
        settled instead of losing the line.
        """
        pending = self.companion.take_pending()
        for attempt in range(3):
            retry: list[tuple[str, str]] = []
            for kind, detail in pending:
                try:
                    result = await self.companion.react(kind, detail)
                except Exception:
                    log.exception("companion reaction failed")
                    continue
                if result.get("skipped") and result.get("reason") == "active turn":
                    retry.append((kind, detail))
            pending = retry
            if not pending:
                return
            await asyncio.sleep(0.3 * (attempt + 1))

    async def _ask_user(
        self, client: Client, request: dict[str, Any], *, want: str = "bool"
    ) -> Any:
        """Emit a confirmation card and wait for the shell's answer.

        ``want`` is what the card is expected to carry back: a bare approve or
        deny, or a decision. Silence is never an approval, whatever the shape.
        """
        confirm_id = str(request["confirmId"])
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.confirms[confirm_id] = future
        await client.send(protocol.event("agent.confirm", **request))
        try:
            answer = await asyncio.wait_for(
                future, timeout=min(int(request.get("timeout") or 180), 3600)
            )
        except asyncio.TimeoutError:
            log.info("confirmation %s timed out", confirm_id)
            return False if want == "bool" else None
        finally:
            self.confirms.pop(confirm_id, None)
        if want == "bool" and isinstance(answer, dict):
            return bool(answer.get("approved"))
        return answer

    async def _ask_choice(
        self, client: Client, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A card whose answer is a decision: a yes plus the tiers behind it.

        The tool proposes the thing and its defaults; the person's answer is
        what gets acted on. No window means no card, and no card means no.
        """
        return await self._ask_user(client, request, want="payload")

    async def _confirm_respond(
        self, client: Client, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_session(client)
        confirm_id = str(params.get("confirmId") or "")
        future = self.confirms.get(confirm_id)
        if future is None or future.done():
            raise RpcError("not_found", f"no pending confirmation {confirm_id!r}")
        approved = bool(params.get("approved"))
        tiers = params.get("tiers")
        if "tiers" in params:
            # A decision card: the tiers the person chose travel with the yes.
            clean: dict[str, int] = {}
            if isinstance(tiers, dict):
                for name, raw in tiers.items():
                    try:
                        tier = int(raw)
                    except (TypeError, ValueError):
                        continue
                    clean[str(name)] = max(0, min(tier, 3))
            future.set_result({"approved": approved, "tiers": clean})
            return {"confirmId": confirm_id, "approved": approved, "tiers": clean}
        future.set_result(approved)
        return {"confirmId": confirm_id, "approved": approved}

    async def _models_status(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": self.config.models.endpoint,
            "online": await self.brain.available(),
            "model": self.config.models.model,
            "structured": self.config.models.structured,
            "embed": self.config.models.embed.name,
            "embedDim": self.config.models.embed.dim,
            "embedMode": self.config.memory.embed,
        }

    async def _scheduler_status(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return self.scheduler.status()

    def _active_episodes(self) -> set[int]:
        return {
            client.session.episode_id
            for client in self.clients
            if client.session is not None
        }

    def _active_turn_count(self) -> int:
        return sum(
            1
            for client in self.clients
            if client.session is not None
            for task in client.session.turns.values()
            if not task.done()
        )

    def _turns_busy(self) -> bool:
        return self._active_turn_count() > 0

    def _has_session(self) -> bool:
        return any(client.session is not None for client in self.clients)

    def _ambient_speech_allowed(self) -> bool:
        """Ambient lines may speak only when no shell has the mic route on."""
        return not self._mic_active()

    def _mic_active(self) -> bool:
        return any(client.mic_route_active for client in self.clients)

    def _companion_snapshot(self) -> ActivitySnapshot:
        """Names and statuses only, read from the shard at speak time."""
        active = self.workspaces.active
        state = "working" if self._turns_busy() else "idle"
        if active is None:
            return ActivitySnapshot(agent_state=state)

        task = self.tasks.open_for(active.id)
        recent = self.tasks.recent(active.id, limit=6)
        if task is None:
            task = recent[0] if recent else None
        # The rest of the board, so a working line can mention another task
        # without pretending it is the one at hand.
        others = tuple(
            item.title
            for item in recent
            if item.status in ("active", "paused")
            and (task is None or item.id != task.id)
        )
        return ActivitySnapshot(
            project=active.name,
            task=task.title if task is not None else "",
            task_status=task.status if task is not None else "",
            task_summary=str(task.summary or "") if task is not None else "",
            agent_state=state,
            last_failure=self._last_failure(active.id),
            other_tasks=others,
        )

    def _last_failure(self, workspace_id: str) -> str:
        """The most recent failed tool call, as one short line."""
        rows = self.conn.execute(
            "SELECT name, payload FROM traces WHERE span = 'tool' AND workspace_id = ?"
            " ORDER BY id DESC LIMIT 12",
            (workspace_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("ok", True):
                continue
            command = str((payload.get("args") or {}).get("command") or "")
            line = f"{row['name']} {command}".strip()
            return " ".join(line.split())[:120]
        return ""

    def _companion_context(self) -> list[dict[str, Any]]:
        for client in self.clients:
            if client.session is not None:
                return client.session.recent_messages(6)
        return []

    def _record_ambient(self, turn_id: str, text: str) -> None:
        for client in self.clients:
            if client.session is not None:
                client.session.add_message(turn_id, "assistant", text)
                return

    async def _broadcast_event(self, event: dict[str, Any]) -> None:
        for client in list(self.clients):
            if client.authed:
                await client.send(event)

    def _apply_profile_pause(self, profile: str) -> None:
        """Silent closes her eyes; leaving silent only undoes our own pause."""
        if profile == "silent":
            self.screen.pause(True, "profile: silent")
            self._profile_screen_pause = True
            return
        if self._profile_screen_pause:
            status = self.screen.status()
            if status.get("paused") and status.get("pauseReason") == "profile: silent":
                self.screen.resume()
            self._profile_screen_pause = False

    def _bundle_path(self, raw: Any, *, default_name: str) -> Path:
        text = str(raw or "").strip()
        if not text:
            return paths.shards_dir() / "exports" / default_name
        target = Path(text).expanduser()
        if not target.is_absolute():
            target = Path(self.workspace) / target
        return target

    async def _shard_export(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        raw_path = params.get("path")
        target = self._bundle_path(
            raw_path, default_name=f"capri-{stamp}.capri"
        )
        if str(raw_path or "").strip():
            verdict, reason = self.policy.classify_path(str(target))
            if verdict == "block":
                raise RpcError("invalid_args", f"{raw_path}: {reason}")
        try:
            result = await asyncio.to_thread(
                export_shard,
                self.conn,
                target,
                config=self.config,
                subject=paths.DEFAULT_SUBJECT,
            )
        except OSError as exc:
            raise RpcError("internal", f"could not write shard bundle: {exc}") from exc
        db.trace(self.conn, span="shard", name="shard.export", payload={"path": str(target)})
        return result

    async def _shard_import(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        if params.get("confirm") is not True:
            raise RpcError(
                "invalid_args",
                "import replaces the live shard; pass confirm: true to proceed",
            )
        raw_path = str(params.get("path") or "").strip()
        if not raw_path:
            raise RpcError("invalid_args", "path is required")
        for peer in self.clients:
            session = peer.session
            if session is not None and any(not task.done() for task in session.turns.values()):
                raise RpcError("invalid_args", "cannot import while a turn is running")
        target = self._bundle_path(raw_path, default_name="")
        verdict, reason = self.policy.classify_path(str(target))
        if verdict == "block":
            raise RpcError("invalid_args", f"{raw_path}: {reason}")
        scheduler_was_running = self.scheduler.status()["running"]
        await self.scheduler.stop()
        try:
            manifest = await asyncio.to_thread(
                import_shard,
                self.conn,
                target,
                config=self.config,
                subject=paths.DEFAULT_SUBJECT,
            )
        except ValueError as exc:
            if scheduler_was_running:
                self.scheduler.start()
            raise RpcError("invalid_args", str(exc)) from exc
        except OSError as exc:
            if scheduler_was_running:
                self.scheduler.start()
            raise RpcError("internal", f"could not read shard bundle: {exc}") from exc
        except Exception:
            if scheduler_was_running:
                self.scheduler.start()
            raise

        # Episodes from the old shard are gone. Every live session starts a
        # fresh episode rather than referring to a row that may not exist.
        for peer in self.clients:
            if peer.session is None:
                continue
            peer.session.cancel_all()
            peer.session = Session.create(self.conn, paths.DEFAULT_SUBJECT)
            await peer.send(
                protocol.event(
                    "session.opened",
                    sessionId=peer.session.id,
                    episodeId=peer.session.episode_id,
                    subject=peer.session.subject,
                    # The import replaced the shard, not the conversation the
                    # user was reading; the shell decides what to do with it.
                    reason="import",
                )
            )
        db.trace(self.conn, span="shard", name="shard.import", payload={"path": str(target)})
        if scheduler_was_running or self.config.scheduler.enabled:
            self.scheduler.start()
        return {
            "manifest": manifest,
            "sessionId": client.session.id if client.session else None,
            "episodeId": client.session.episode_id if client.session else None,
        }


async def run_server(config: CapriConfig | None = None, *, host: str | None = None,
                     port: int | None = None) -> None:
    cfg = config or load_config()
    paths.ensure_dirs()
    paths.token()  # make sure the shell can authenticate on first run
    conn = db.open_shard(paths.DEFAULT_SUBJECT, dim=cfg.models.embed.dim)
    server = CapriServer(cfg, conn)
    try:
        try:
            await server.mcp.start()
        except Exception:  # a broken server list must not stop the daemon
            log.exception("MCP startup failed; continuing without it")
        await server.run(host, port)
    finally:
        await server.mcp.aclose()
        await server.scheduler.stop()
        server.blackwall.close()
        await server.voice.aclose()
        await server.rag.aclose()
        await server.memory.aclose()
        await server.brain.aclose()
        conn.close()
