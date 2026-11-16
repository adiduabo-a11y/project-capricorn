"""Shell execution.

Deliberately blunt: one command string, one process, captured output. No PTY,
no streaming, no environment inheritance beyond the daemon's own. The policy
layer decides whether a given command may run automatically or needs the user.

Two things are not blunt:

- **The process tree dies with the call.** Killing the shell on Windows leaves
  the actual command running, which is how a cancelled test keeps burning CPU.
  ``taskkill /T`` (and a process group on POSIX) takes the children with it.
- **The result is structured.** Exit code, duration and the tail of the output
  travel in ``data``; the full text goes to an artifact in Milestone 3.
"""

from __future__ import annotations

import asyncio
import contextlib
import locale
import os
import signal
import subprocess
import time
from typing import Any

from ...work import store as work_store
from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms
from ..policy import resolve_path

MAX_OUTPUT_CHARS = 40_000
TAIL_CHARS = 2000
# A real test suite is slower than a one-line inspection. Only a command the
# detector read out of the project gets the longer default; anything else keeps
# the short one.
TEST_TIMEOUT_MS = 300_000
MAX_TIMEOUT_MS = 600_000


def _console_encoding() -> str:
    """The codepage a console child writes in when it is not emitting UTF-8."""
    if os.name == "nt":
        return "mbcs"
    return locale.getpreferredencoding(False) or "utf-8"


def _decode_text(raw: bytes) -> str:
    """Decode child output: UTF-8 first, then the console codepage.

    cmd.exe built-ins and git on a cp1252/cp866 console are not UTF-8, so a
    strict UTF-8 decode turns their output into replacement characters.
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode(_console_encoding())
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")


def _trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated at {limit} of {len(text)} chars]"


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Take the whole process tree down, then reap the shell."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), 10)
        except (OSError, asyncio.TimeoutError):
            pass
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


RUN_SPEC = ToolSpec(
    name="shell.run",
    mutates=True,
    description=(
        "Run a shell command and return its combined stdout/stderr. "
        "Use for inspecting the machine, running tests, or driving CLI tools."
    ),
    tier=2,
    timeout_ms=60_000,
    observation="command",
    params={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command line to execute."},
            "cwd": {"type": "string", "description": "Working directory."},
            "timeout_ms": {"type": "integer", "description": "Override the timeout."},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)


async def shell_run(
    ctx: ToolContext, *, command: str, cwd: str = "", timeout_ms: int = 0
) -> ToolResult:
    started = time.monotonic()
    line = str(command).strip()
    if not line:
        return ToolResult.error("shell.run: command is empty")
    if "\x00" in line:
        return ToolResult.error("shell.run: command contains a NUL byte")

    workdir = resolve_path(cwd, ctx.workspace) if cwd else ctx.workspace
    if not os.path.isdir(workdir):
        return ToolResult.error(f"shell.run: working directory does not exist: {workdir}")

    policy = getattr(ctx, "policy", None)
    detected = bool(policy.is_detected_command(line, workdir)) if policy else False
    default_ms = TEST_TIMEOUT_MS if detected else RUN_SPEC.timeout_ms
    timeout = (int(timeout_ms) or default_ms) / 1000
    timeout = max(1.0, min(timeout, MAX_TIMEOUT_MS / 1000))

    spawn: dict[str, object] = {}
    if os.name == "nt":
        spawn["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        spawn["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_shell(
            line,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            **spawn,
        )
    except (OSError, ValueError) as exc:
        return ToolResult.error(f"shell.run: could not start: {exc}")

    # The call can end three ways: the process exits, the timeout fires, or
    # the turn is cancelled. All three have to take the tree down, and the
    # cancel path has to be a *result* so the loop can close the turn cleanly.
    communication = asyncio.ensure_future(proc.communicate())
    stopped = asyncio.ensure_future(ctx.cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {communication, stopped},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        communication.cancel()
        stopped.cancel()
        await _kill_tree(proc)
        raise

    if stopped in done:
        communication.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await communication
        await _kill_tree(proc)
        return ToolResult(
            ok=False,
            output="shell.run: cancelled",
            data={
                "command": line,
                "cwd": workdir,
                "exit": None,
                "timeout": False,
                "cancelled": True,
                "duration_ms": elapsed_ms(started),
            },
            ms=elapsed_ms(started),
        )

    if communication not in done:
        stopped.cancel()
        communication.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await communication
        await _kill_tree(proc)
        return ToolResult(
            ok=False,
            output=f"shell.run: timed out after {timeout:.0f}s",
            data={
                "command": line,
                "cwd": workdir,
                "exit": None,
                "timeout": True,
                "cancelled": False,
                "duration_ms": elapsed_ms(started),
            },
            ms=elapsed_ms(started),
        )

    stopped.cancel()
    stdout, _ = communication.result()

    text = _trim(_decode_text(stdout))
    code = proc.returncode or 0
    body = text if text.strip() else "(no output)"

    # The full output belongs in an artifact; the observation carries a shaped
    # tail of it. A failing suite can be megabytes of noise.
    artifact: dict[str, Any] = {}
    if ctx.conn is not None:
        artifact = work_store.record_artifact(
            ctx.conn,
            turn_id=ctx.turn_id,
            task_id=ctx.task_id or None,
            kind="command",
            path=workdir,
            content=f"$ {line}\n{text}\nexit {code}\n",
        )

    return ToolResult(
        ok=code == 0,
        output=f"exit {code}\n{body}",
        data={
            "command": line,
            "cwd": workdir,
            "exit": code,
            "timeout": False,
            "duration_ms": elapsed_ms(started),
            "tail": text[-TAIL_CHARS:],
            "detected": detected,
            "artifact": artifact,
            "artifact_id": artifact.get("id", ""),
        },
        ms=elapsed_ms(started),
    )
