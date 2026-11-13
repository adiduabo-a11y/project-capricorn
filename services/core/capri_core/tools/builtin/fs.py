"""Filesystem tools: read, list, write.

These do not enforce policy themselves — the loop checks every call before it
is dispatched. What they *do* enforce is bounds: size caps so a stray read of a
2 GB log cannot eat the context window.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms
from ..policy import resolve_path

elapsed_ms_to_now = time.monotonic

MAX_READ_BYTES = 400_000
MAX_WRITE_BYTES = 2_000_000
MAX_LIST_ENTRIES = 500

READ_SPEC = ToolSpec(
    name="fs.read",
    description="Read a UTF-8 text file and return its contents.",
    tier=0,
    params={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, absolute or workspace-relative."},
            "max_bytes": {"type": "integer", "description": "Truncate after this many bytes."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)

LIST_SPEC = ToolSpec(
    name="fs.list",
    description="List a directory: names, sizes and whether each entry is a directory.",
    tier=0,
    params={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)

WRITE_SPEC = ToolSpec(
    name="fs.write",
    description="Write text to a file, creating parent directories.",
    tier=2,
    params={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, absolute or workspace-relative."},
            "content": {"type": "string", "description": "Full text to write."},
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "description": "overwrite replaces the file; append adds to it.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)


def _display(path: str, workspace: str) -> str:
    try:
        return os.path.relpath(path, workspace)
    except ValueError:
        return path


async def fs_read(
    ctx: ToolContext, *, path: str, max_bytes: int = MAX_READ_BYTES
) -> ToolResult:
    started = elapsed_ms_to_now()
    target = resolve_path(path, ctx.workspace)
    file = Path(target)

    if not file.exists():
        return ToolResult.error(f"fs.read: no such file: {_display(target, ctx.workspace)}")
    if file.is_dir():
        return ToolResult.error(
            f"fs.read: {_display(target, ctx.workspace)} is a directory; use fs.list"
        )

    cap = min(max(1, int(max_bytes)), MAX_READ_BYTES)
    try:
        raw = file.read_bytes()
    except OSError as exc:
        return ToolResult.error(f"fs.read: {exc}")

    truncated = len(raw) > cap
    text = raw[:cap].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n[truncated at {cap} of {len(raw)} bytes]"

    return ToolResult(
        ok=True,
        output=text,
        data={"path": target, "bytes": len(raw), "truncated": truncated},
        ms=elapsed_ms(started),
    )


async def fs_list(ctx: ToolContext, *, path: str) -> ToolResult:
    started = elapsed_ms_to_now()
    target = resolve_path(path, ctx.workspace)
    directory = Path(target)

    if not directory.exists():
        return ToolResult.error(f"fs.list: no such directory: {_display(target, ctx.workspace)}")
    if not directory.is_dir():
        return ToolResult.error(f"fs.list: {_display(target, ctx.workspace)} is not a directory")

    lines: list[str] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return ToolResult.error(f"fs.list: {exc}")

    for entry in entries[:MAX_LIST_ENTRIES]:
        try:
            if entry.is_dir():
                lines.append(f"{entry.name}/")
            else:
                lines.append(f"{entry.name}  {entry.stat().st_size}B")
        except OSError:
            lines.append(f"{entry.name}  ?")
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"… {len(entries) - MAX_LIST_ENTRIES} more entries")

    return ToolResult(
        ok=True,
        output="\n".join(lines) or "(empty directory)",
        data={"path": target, "count": len(entries)},
        ms=elapsed_ms(started),
    )


async def fs_write(
    ctx: ToolContext, *, path: str, content: str, mode: str = "overwrite"
) -> ToolResult:
    started = elapsed_ms_to_now()
    target = resolve_path(path, ctx.workspace)
    data = str(content)

    if len(data.encode("utf-8")) > MAX_WRITE_BYTES:
        return ToolResult.error(f"fs.write: content exceeds {MAX_WRITE_BYTES} bytes")

    file = Path(target)
    if mode not in ("overwrite", "append"):
        return ToolResult.error(f"fs.write: mode must be overwrite or append, got {mode!r}")

    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with file.open("a", encoding="utf-8", newline="") as handle:
                handle.write(data)
        else:
            file.write_text(data, encoding="utf-8", newline="")
    except OSError as exc:
        return ToolResult.error(f"fs.write: {exc}")

    return ToolResult(
        ok=True,
        output=f"wrote {len(data.encode('utf-8'))} bytes to {_display(target, ctx.workspace)}"
        f" ({mode})",
        data={"path": target, "bytes": len(data.encode("utf-8")), "mode": mode},
        ms=elapsed_ms(started),
    )
