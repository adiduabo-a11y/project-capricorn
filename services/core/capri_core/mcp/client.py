"""Stdio MCP client: newline-delimited JSON-RPC over a child process.

Two protocol eras ship today. Servers on 2025-11-25 and earlier open with an
``initialize`` handshake and hold a session; servers on 2026-07-28 have no
handshake at all and carry the version and capabilities in ``_meta`` on every
request, rejecting ``initialize`` outright. This client speaks the modern form
when the server asks for it and the legacy handshake otherwise, which is what
the versioning spec expects of a client that has to work with both.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any, Mapping

from .. import __version__

log = logging.getLogger("capri.mcp")

LEGACY_PROTOCOL = "2025-11-25"
MODERN_PROTOCOL = "2026-07-28"
_CLIENT_INFO = {"name": "capri", "version": __version__}
# Schemas and tool results can be large; asyncio's default 64 KiB line limit
# would truncate a message mid-line and break the stream.
_BUFFER = 16 * 1024 * 1024


class McpError(Exception):
    """A protocol-level failure: the server refused, or did not answer."""


def flatten_result(result: Mapping[str, Any]) -> tuple[str, bool]:
    """Read a ``tools/call`` result as text, plus whether the server flagged it.

    MCP returns content blocks. Capri's observations are text, so images and
    audio become a short marker rather than being dropped silently, and
    structured content is serialised when there is no text to show.
    """
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if text:
                    parts.append(str(text))
            elif kind == "resource":
                resource = block.get("resource") or {}
                text = resource.get("text") if isinstance(resource, Mapping) else None
                parts.append(str(text) if text else f"[resource {resource.get('uri', '')}]")
            elif kind == "image":
                parts.append(f"[image {block.get('mimeType') or 'image'}]")
            elif kind == "audio":
                parts.append(f"[audio {block.get('mimeType') or 'audio'}]")
            elif kind:
                parts.append(f"[{kind}]")
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        structured = result.get("structuredContent")
        if structured is not None:
            try:
                text = json.dumps(structured, indent=2, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(structured)
    return text, bool(result.get("isError"))


class McpClient:
    """One server process, one request table, one reader."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str = "",
        name: str = "",
    ) -> None:
        self.name = name or command or "mcp"
        self._command = command
        self._args = [str(arg) for arg in (args or [])]
        self._env = dict(env or {})
        self._cwd = cwd or None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._modern = False
        self.server_info: dict[str, Any] = {}
        self.protocol = ""

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def modern(self) -> bool:
        return self._modern

    async def start(self, timeout_s: float = 10.0) -> None:
        if self._proc is not None:
            return
        if not self._command.strip():
            raise McpError(f"{self.name}: no command configured")
        env = {**os.environ, **self._env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._cwd,
                limit=_BUFFER,
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"{self.name}: could not start {self._command!r}: {exc}") from exc

        self._reader = asyncio.create_task(self._read_stdout(), name=f"mcp:{self.name}:out")
        self._stderr = asyncio.create_task(self._read_stderr(), name=f"mcp:{self.name}:err")
        await self._handshake(timeout_s)

    async def _handshake(self, timeout_s: float) -> None:
        try:
            result = await self.request(
                "initialize",
                {
                    "protocolVersion": LEGACY_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": dict(_CLIENT_INFO),
                },
                timeout_s=timeout_s,
            )
        except McpError as exc:
            # 2026-07-28 removed initialize on purpose. From here every request
            # carries the version and capabilities, and the server never opens
            # a session for us to close.
            log.debug("%s has no initialize (%s); using per-request metadata", self.name, exc)
            self._modern = True
            self.protocol = MODERN_PROTOCOL
            return
        if isinstance(result, Mapping):
            self.server_info = dict(result.get("serverInfo") or {})
            self.protocol = str(result.get("protocolVersion") or LEGACY_PROTOCOL)
        await self._send(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(f"{self.name}: closed"))
        self._pending.clear()
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader = None
        self._stderr = None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()

    # ------------------------------------------------------------------ transport

    async def _send(self, payload: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise McpError(f"{self.name}: not running")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise McpError(f"{self.name}: the connection closed while writing") from exc

    async def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    # The spec forbids anything else on stdout, but a server
                    # that logs there should not take the client down.
                    log.debug("%s wrote non-JSON on stdout: %s", self.name, text[:200])
                    continue
                if not isinstance(message, Mapping):
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: reader stopped", self.name)
        finally:
            self._fail_pending("the server closed its output")

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is None:
            log.debug("%s notification: %s", self.name, message.get("method"))
            return
        future = self._pending.get(message_id)
        if future is None or future.done():
            return
        error = message.get("error")
        if error:
            detail = error.get("message") if isinstance(error, Mapping) else str(error)
            code = error.get("code") if isinstance(error, Mapping) else None
            future.set_exception(McpError(f"{self.name}: {detail} ({code})"))
            return
        future.set_result(message.get("result"))

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(f"{self.name}: {reason}"))
        self._pending.clear()

    async def _read_stderr(self) -> None:
        """Drain stderr so a chatty server cannot fill the pipe and block."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                log.debug("%s stderr: %s", self.name, line.decode("utf-8", "replace").rstrip()[:400])
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------ jsonrpc

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout_s: float = 30.0
    ) -> Any:
        if not self.running:
            raise McpError(f"{self.name}: not running")
        body = dict(params or {})
        if self._modern:
            body.setdefault(
                "_meta",
                {
                    "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL,
                    "io.modelcontextprotocol/clientCapabilities": {},
                    "io.modelcontextprotocol/clientInfo": dict(_CLIENT_INFO),
                },
            )
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if body or params is not None:
            payload["params"] = body

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(payload)
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise McpError(f"{self.name}: {method} timed out after {timeout_s:g}s") from None
        finally:
            self._pending.pop(request_id, None)

    # ------------------------------------------------------------------ tools

    async def list_tools(self, *, timeout_s: float = 30.0) -> list[dict[str, Any]]:
        """Every advertised tool, following ``nextCursor`` across pages."""
        found: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(50):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params, timeout_s=timeout_s)
            if not isinstance(result, Mapping):
                raise McpError(f"{self.name}: tools/list returned {type(result).__name__}")
            tools = result.get("tools")
            if isinstance(tools, list):
                found.extend(tool for tool in tools if isinstance(tool, Mapping))
            cursor = str(result.get("nextCursor") or "")
            if not cursor:
                break
        return found

    async def call(
        self, name: str, arguments: Mapping[str, Any] | None = None, *, timeout_s: float = 30.0
    ) -> tuple[str, bool]:
        result = await self.request(
            "tools/call", {"name": name, "arguments": dict(arguments or {})}, timeout_s=timeout_s
        )
        if not isinstance(result, Mapping):
            raise McpError(f"{self.name}: {name} returned {type(result).__name__}")
        return flatten_result(result)
