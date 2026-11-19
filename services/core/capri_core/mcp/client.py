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
import shutil
import subprocess
from collections import deque
from typing import Any, Mapping

from .. import __version__

log = logging.getLogger("capri.mcp")

LEGACY_PROTOCOL = "2025-11-25"
MODERN_PROTOCOL = "2026-07-28"
_CLIENT_INFO = {"name": "capri", "version": __version__}
# Schemas and tool results can be large; asyncio's default 64 KiB line limit
# would truncate a message mid-line and break the stream.
_BUFFER = 16 * 1024 * 1024

_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_SAFE_ENV_PREFIXES = ("XDG_", "NODE_", "NPM_", "UV_", "PYTHON", "VIRTUAL_ENV")


def _child_env(extra: Mapping[str, str]) -> dict[str, str]:
    """Build subprocess environment without leaking daemon secrets/API keys."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("CAPRI_SECRET_"):
            continue
        if upper in _SAFE_ENV_KEYS or upper.startswith(_SAFE_ENV_PREFIXES):
            env[key] = value
    env.update({str(k): str(v) for k, v in extra.items()})
    return env


class McpError(Exception):
    """A protocol-level failure: the server refused, or did not answer."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


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
        # The server's own words about a failure. A missing key or a traceback
        # arrives on stderr, not on the protocol, so the last few lines are kept
        # for the row that reports it.
        self._stderr_tail: deque[str] = deque(maxlen=8)
        self._next_id = 0
        self._modern = False
        self._send_lock = asyncio.Lock()
        self.server_info: dict[str, Any] = {}
        self.protocol = ""

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def modern(self) -> bool:
        return self._modern

    @property
    def stderr_tail(self) -> list[str]:
        """The last few lines the server printed on stderr, oldest first."""
        return list(self._stderr_tail)

    @property
    def exit_code(self) -> int | None:
        """How the process ended, or ``None`` while it is gone without a code."""
        return None if self._proc is None else self._proc.returncode

    async def start(self, timeout_s: float = 10.0) -> None:
        if self._proc is not None:
            return
        if not self._command.strip():
            raise McpError(f"{self.name}: no command configured")
        env = _child_env(self._env)
        command = self._resolve_command()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = await asyncio.create_subprocess_exec(
                command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._cwd,
                limit=_BUFFER,
                creationflags=flags,
            )
        except (OSError, ValueError) as exc:
            raise McpError(
                f"{self.name}: could not start {command!r}: {exc}"
            ) from exc

        self._reader = asyncio.create_task(self._read_stdout(), name=f"mcp:{self.name}:out")
        self._stderr = asyncio.create_task(self._read_stderr(), name=f"mcp:{self.name}:err")
        await self._handshake(timeout_s)

    def _resolve_command(self) -> str:
        """The command to spawn, with Windows shims resolved to their real file.

        CreateProcess does not apply PATHEXT, so a config saying ``npx`` — which
        is how almost every published MCP server is written — is not found even
        though ``npx.cmd`` is on PATH; it fails with WinError 2 and reads as the
        server being broken. ``shutil.which`` does apply PATHEXT, and Windows
        runs a batch file by its full path, so resolving is the whole fix. The
        cmd.exe wrapper is deliberately not used: /c re-parses the line and
        splits a path like ``C:\\Program Files\\nodejs\\npx.cmd``.
        """
        if os.name != "nt":
            return self._command
        resolved = shutil.which(self._command)
        # Not installed: keep the name the config asked for, so the error
        # names it rather than an empty path.
        return resolved or self._command

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
            if exc.code not in (-32601, -32602):
                raise
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

    async def settle_stderr(self, timeout_s: float = 0.25) -> None:
        """Give the stderr reader a moment to catch what the server said dying.

        A process that exits mid-handshake has already written its reason, but
        the reader is a task, so a message built immediately can be missing the
        line that matters. Bounded: a server still running never closes stderr.
        """
        task = self._stderr
        if task is None or task.done():
            return
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)

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
            if os.name == "nt" and proc.pid:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()

    # ------------------------------------------------------------------ transport

    async def _send(self, payload: Mapping[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        async with self._send_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.returncode is not None:
                raise McpError(f"{self.name}: not running")
            try:
                proc.stdin.write(encoded)
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
            raw_code = error.get("code") if isinstance(error, Mapping) else None
            code = raw_code if isinstance(raw_code, int) else None
            future.set_exception(McpError(f"{self.name}: {detail} ({raw_code})", code=code))
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
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text[:400])
                log.debug("%s stderr: %s", self.name, text[:400])
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
