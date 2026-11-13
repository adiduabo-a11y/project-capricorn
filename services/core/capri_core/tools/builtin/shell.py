"""Shell execution.

Deliberately blunt: one command string, one process, captured output. No PTY,
no streaming, no environment inheritance beyond the daemon's own. The policy
layer decides whether a given command may run automatically or needs the user.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms
from ..policy import resolve_path

MAX_OUTPUT_CHARS = 40_000

RUN_SPEC = ToolSpec(
    name="shell.run",
    description=(
        "Run a shell command and return its combined stdout/stderr. "
        "Use for inspecting the machine, running tests, or driving CLI tools."
    ),
    tier=2,
    timeout_ms=60_000,
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


def _trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated at {limit} of {len(text)} chars]"


async def shell_run(
    ctx: ToolContext, *, command: str, cwd: str = "", timeout_ms: int = 0
) -> ToolResult:
    started = elapsed_ms_to_now()
    line = str(command).strip()
    if not line:
        return ToolResult.error("shell.run: command is empty")
    if "\x00" in line:
        return ToolResult.error("shell.run: command contains a NUL byte")

    workdir = resolve_path(cwd, ctx.workspace) if cwd else ctx.workspace
    if not os.path.isdir(workdir):
        return ToolResult.error(f"shell.run: working directory does not exist: {workdir}")

    timeout = (int(timeout_ms) or RUN_SPEC.timeout_ms) / 1000
    timeout = max(1.0, min(timeout, 300.0))

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = await asyncio.create_subprocess_shell(
            line,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except (OSError, ValueError) as exc:
        return ToolResult.error(f"shell.run: could not start: {exc}")

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(
            ok=False,
            output=f"shell.run: timed out after {timeout:.0f}s",
            data={"command": line, "cwd": workdir, "timeout": True},
            ms=elapsed_ms(started),
        )

    text = _trim(stdout.decode("utf-8", errors="replace"))
    code = proc.returncode or 0
    body = text if text.strip() else "(no output)"

    return ToolResult(
        ok=code == 0,
        output=f"exit {code}\n{body}",
        data={"command": line, "cwd": workdir, "exit": code},
        ms=elapsed_ms(started),
    )
