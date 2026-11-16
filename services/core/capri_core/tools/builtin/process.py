"""Closing an application, directly.

Screen control can focus a window and press keys, which is how "close Roblox
Studio" turned into a tour of its menu and then a dead end: the app is a process,
and a process can be asked to close. ``process.list`` finds the right name and
``process.close`` asks it to quit; ``force`` terminates it when it ignores that.

Both are for the whole machine rather than the workspace, so they are tier 2:
the person is asked before an app with unsaved work goes away.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Iterable

from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms

# Killing one of these leaves Windows in a state the user cannot get out of.
_PROTECTED = {
    "csrss",
    "wininit",
    "winlogon",
    "services",
    "lsass",
    "smss",
    "system",
    "registry",
    "memcompression",
    "svchost",
    "fontdrvhost",
    "dwm",
}
# Herself, and the app she lives in: a closed shell is not an answer.
_SELF = ("capri", "projectcapricorn", "capricore")

# Replaced in tests; the handler never shells out through anything else.
_tasklist_impl = None
_taskkill_impl = None
# How long a polite close gets before the tool admits it did not take.
_SETTLE_S = 4.0


LIST_SPEC = ToolSpec(
    name="process.list",
    description=(
        "List running processes as name and pid, so an app can be named exactly. "
        "Use this before process.close when the name is a guess."
    ),
    params={"type": "object", "properties": {}, "additionalProperties": False},
    tier=0,
    timeout_ms=10_000,
    max_calls_per_turn=4,
    observation="command",
)

CLOSE_SPEC = ToolSpec(
    name="process.close",
    mutates=True,
    description=(
        "Close an application by process name, e.g. 'roblox studio'. Asks the app "
        "to quit the way its X button does; force terminates it if it refuses. "
        "Closing an app is what this is for — do not drive its menu or send keys."
    ),
    params={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Process name or part of it, e.g. 'roblox studio' or 'chrome'.",
            },
            "force": {
                "type": "boolean",
                "description": "Terminate instead of asking politely. Unsaved work is lost.",
            },
            "all": {
                "type": "boolean",
                "description": "Close every match, not only a single unambiguous one.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    tier=2,
    timeout_ms=20_000,
    max_calls_per_turn=4,
    observation="command",
)


def _squash(text: str) -> str:
    return "".join(char for char in text.lower().removesuffix(".exe") if char.isalnum())


def matches(query: str, process: str) -> bool:
    """Substring match that ignores case, spaces, dots and the .exe suffix."""
    want = _squash(query)
    if not want:
        return False
    return want in _squash(process)


def refusable(process: str) -> str:
    """Why this process may not be closed by her, or an empty string."""
    name = _squash(process)
    if not name:
        return "an empty process name"
    if name in _PROTECTED:
        return f"{process} keeps Windows running"
    if any(part in name for part in _SELF):
        return f"{process} is Capri herself"
    return ""


def select(query: str, processes: Iterable[tuple[str, int]]) -> tuple[list[tuple[str, int]], str]:
    """Matching processes, plus why the match is unusable when it is."""
    found = [(name, pid) for name, pid in processes if matches(query, name)]
    if not found:
        return [], f"nothing is running whose name matches {query!r}"
    blocked = [refusable(name) for name, _pid in found]
    reasons = [reason for reason in blocked if reason]
    if reasons:
        return [], reasons[0]
    return found, ""


async def _tasklist() -> list[tuple[str, int]]:
    if _tasklist_impl is not None:
        return await _tasklist_impl()
    proc = await asyncio.create_subprocess_exec(
        "tasklist",
        "/FO",
        "CSV",
        "/NH",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out, _ = await proc.communicate()
    rows: list[tuple[str, int]] = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = [part.strip().strip('"') for part in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].strip('"')
        try:
            pid = int(parts[1].strip('"'))
        except ValueError:
            continue
        rows.append((name, pid))
    return rows


async def _taskkill(pid: int, force: bool) -> None:
    if _taskkill_impl is not None:
        await _taskkill_impl(pid, force)
        return
    command = ["taskkill", "/PID", str(pid)]
    if force:
        command.append("/F")
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    await proc.communicate()


async def process_list(ctx: ToolContext) -> ToolResult:
    started = time.monotonic()
    try:
        rows = await _tasklist()
    except Exception as exc:  # noqa: BLE001 - the tool reports, it never raises
        return ToolResult.error(f"process.list failed: {exc}")
    # One line per name, with every pid it owns.
    grouped: dict[str, list[int]] = {}
    for name, pid in rows:
        grouped.setdefault(name, []).append(pid)
    if not grouped:
        return ToolResult(ok=True, output="no processes came back", ms=elapsed_ms(started))
    lines = [
        f"{name} ({', '.join(str(pid) for pid in sorted(pids))})"
        for name, pids in sorted(grouped.items())
    ]
    return ToolResult(ok=True, output="\n".join(lines), ms=elapsed_ms(started))


async def process_close(
    ctx: ToolContext, *, name: str, force: bool = False, all: bool = False
) -> ToolResult:
    started = time.monotonic()
    query = str(name or "").strip()
    if not query:
        return ToolResult.error("process.close: name is required")
    try:
        rows = await _tasklist()
    except Exception as exc:  # noqa: BLE001
        return ToolResult.error(f"process.close failed to read the process list: {exc}")

    found, reason = select(query, rows)
    if not found:
        return ToolResult.error(f"process.close: {reason}")
    if len(found) > 1 and not all:
        listed = ", ".join(f"{proc} ({pid})" for proc, pid in found)
        return ToolResult.error(
            f"process.close: {len(found)} processes match {query!r} — {listed}. "
            "Pass all=true to close every one, or name one more precisely."
        )

    closed: list[str] = []
    for proc, pid in found:
        try:
            await _taskkill(pid, force)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"process.close: could not close {proc} ({pid}): {exc}")
        closed.append(f"{proc} ({pid})")

    # taskkill without /F only posts the close message, and an app can sit on a
    # save prompt. Saying "closed" without looking would be a lie.
    survivors: list[str] = []
    deadline = time.monotonic() + _SETTLE_S
    wanted = {pid for _proc, pid in found}
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        try:
            live = {pid for _proc, pid in await _tasklist()}
        except Exception:  # noqa: BLE001 - verification is best effort
            break
        if not (wanted & live):
            break
    else:
        survivors = [f"{proc} ({pid})" for proc, pid in found if pid in live]

    if survivors:
        return ToolResult(
            ok=False,
            output=(
                f"asked to close {', '.join(closed)}, but it is still running — likely "
                "sitting on a save prompt. Call process.close with force=true to "
                "terminate it."
            ),
            data={"closed": closed, "still": survivors},
            ms=elapsed_ms(started),
        )
    verb = "terminated" if force else "closed"
    return ToolResult(
        ok=True,
        output=f"{verb} {', '.join(closed)}.",
        data={"closed": closed},
        ms=elapsed_ms(started),
    )
