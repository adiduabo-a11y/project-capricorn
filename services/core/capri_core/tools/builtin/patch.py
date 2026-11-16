"""``fs.patch`` and ``fs.undo``: the two calls that change a project.

The patch tool is deliberately narrow. It edits an existing text file by
exact-match replacement, records a backup first, refuses when the file changed
since this turn read it, and writes atomically. It never creates a file and
never rewrites one wholesale: that is what makes "small diffs" a property of
the tool rather than a hope about the model.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ...store import db
from ...work import patch as engine
from ...work import store as work_store
from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms
from ..policy import resolve_path

MAX_EDITS = 40

PATCH_SPEC = ToolSpec(
    name="fs.patch",
    mutates=True,
    description=(
        "Edit an existing text file by exact-match replacement. Give the text "
        "to find exactly as fs.read showed it, without the line numbers. Every "
        "edit applies or none do."
    ),
    tier=2,
    max_calls_per_turn=20,
    observation="summary",
    params={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to edit. Must already exist.",
            },
            "edits": {
                "type": "array",
                "description": "One or more exact-match replacements.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {
                            "type": "string",
                            "description": "Text that currently exists in the file.",
                        },
                        "new": {
                            "type": "string",
                            "description": "Replacement text.",
                        },
                        "count": {
                            "type": "integer",
                            "description": "How many times old must occur. Default 1.",
                        },
                    },
                    "required": ["old", "new"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    },
)

UNDO_SPEC = ToolSpec(
    name="fs.undo",
    mutates=True,
    description=(
        "Restore a file from the backup taken before an earlier edit. Refuses "
        "if the file changed after that edit."
    ),
    tier=2,
    max_calls_per_turn=8,
    observation="summary",
    params={
        "type": "object",
        "properties": {
            "backup_id": {
                "type": "string",
                "description": "A backup id from an earlier patch. Preferred.",
            },
            "path": {
                "type": "string",
                "description": "Otherwise, restore the latest backup for this file.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
)


def _display(path: str, workspace: str) -> str:
    try:
        return os.path.relpath(path, workspace)
    except ValueError:
        return path


def _parse_edits(raw: Any) -> tuple[list[engine.Edit], str]:
    if not isinstance(raw, list) or not raw:
        return [], "edits must be a non-empty array"
    if len(raw) > MAX_EDITS:
        return [], f"at most {MAX_EDITS} edits per call"
    parsed: list[engine.Edit] = []
    for index, entry in enumerate(raw):
        label = f"edit {index + 1}"
        if not isinstance(entry, dict):
            return [], f"{label} must be an object"
        old = entry.get("old")
        new = entry.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            return [], f"{label}: old and new must be strings"
        try:
            count = int(entry.get("count", 1))
        except (TypeError, ValueError):
            return [], f"{label}: count must be an integer"
        parsed.append(engine.Edit(old=old, new=new, count=count))
    return parsed, ""


async def fs_patch(
    ctx: ToolContext, *, path: str, edits: Any
) -> ToolResult:
    started = time.monotonic()
    target = resolve_path(path, ctx.workspace)
    file = Path(target)

    if not file.exists():
        return ToolResult.error(
            f"fs.patch: no such file: {_display(target, ctx.workspace)}. "
            "fs.patch edits existing files; use fs.write to create one."
        )
    if file.is_dir():
        return ToolResult.error(f"fs.patch: {_display(target, ctx.workspace)} is a directory")

    parsed, problem = _parse_edits(edits)
    if problem:
        return ToolResult.error(f"fs.patch: {problem}")

    try:
        raw = file.read_bytes()
    except OSError as exc:
        return ToolResult.error(f"fs.patch: {exc}")

    try:
        before = engine.decode_file(raw)
    except engine.PatchError as exc:
        return ToolResult.error(f"fs.patch: {exc.message}")

    before_hash = engine.sha256_text(before)
    recorded = ctx.reads.get(target) if ctx.reads is not None else None
    if recorded is not None and recorded != before_hash:
        # Server-side staleness check: the model cannot skip it, because it is
        # not the model's check. A save between read and patch lands here.
        head = "\n".join(
            f"{number:5d}: {line}"
            for number, line in enumerate(before.splitlines()[:5], 1)
        )
        return ToolResult(
            ok=False,
            output=(
                "fs.patch: the file changed since you read it. Read it again "
                "before patching.\n"
                f"current sha256 {before_hash[:12]}… ({len(raw)} bytes)\n{head}"
            ),
            data={"path": target, "stale": True},
            ms=elapsed_ms(started),
        )

    try:
        after, hunks, added, removed = engine.apply_edits(before, parsed)
    except engine.PatchError as exc:
        output = f"fs.patch: {exc.message}"
        if exc.diagnostics:
            output = f"{output}\n\n{exc.diagnostics}"
        return ToolResult(
            ok=False,
            output=output,
            data={"path": target, "code": exc.code},
            ms=elapsed_ms(started),
        )

    if after == before:
        return ToolResult.error("fs.patch: those edits leave the file unchanged")

    after_hash = engine.sha256_text(after)
    backup_id = ""
    if ctx.conn is not None:
        backup_id = work_store.record_backup(
            ctx.conn,
            path=target,
            before_text=before,
            before_hash=before_hash,
            after_hash=after_hash,
            task_id=ctx.task_id or None,
            turn_id=ctx.turn_id,
            added=added,
            removed=removed,
        )

    try:
        engine.write_atomic(target, engine.encode_file(after))
    except OSError as exc:
        return ToolResult.error(
            f"fs.patch: could not write {_display(target, ctx.workspace)}: {exc}. "
            "The original file is untouched."
        )

    if ctx.reads is not None:
        ctx.reads[target] = after_hash

    display = _display(target, ctx.workspace)
    artifact: dict[str, Any] = {}
    if ctx.conn is not None:
        artifact = work_store.record_artifact(
            ctx.conn,
            turn_id=ctx.turn_id,
            task_id=ctx.task_id or None,
            kind="diff",
            path=target,
            content=engine.unified_diff(display.replace(os.sep, "/"), before, after),
        )
        db.trace(
            ctx.conn,
            span="work",
            name="fs.patch",
            turn_id=ctx.turn_id,
            workspace_id=ctx.workspace_id or None,
            task_id=ctx.task_id or None,
            payload={
                "path": target,
                "hunks": hunks,
                "added": added,
                "removed": removed,
                "backup": backup_id,
                "artifact": artifact.get("id"),
            },
            ms=elapsed_ms(started),
        )

    lines = [
        f"patched {display} (+{added} -{removed}), {hunks} hunk"
        + ("" if hunks == 1 else "s")
    ]
    if backup_id:
        lines.append(f"backup {backup_id} (undo available)")
    return ToolResult(
        ok=True,
        output="\n".join(lines),
        data={
            "path": target,
            "sha256": after_hash,
            "lines_added": added,
            "lines_removed": removed,
            "backup_id": backup_id,
            "artifact": artifact,
            "hunks": hunks,
        },
        ms=elapsed_ms(started),
    )


async def fs_undo(
    ctx: ToolContext, *, backup_id: str = "", path: str = ""
) -> ToolResult:
    started = time.monotonic()
    if ctx.conn is None:
        return ToolResult.error("fs.undo: the shard is unavailable")

    target = resolve_path(path, ctx.workspace) if path else ""
    result = work_store.restore(
        ctx.conn,
        backup_id=str(backup_id or ""),
        path=target,
        turn_id=ctx.turn_id,
        task_id=ctx.task_id,
    )
    if not result.get("ok"):
        return ToolResult(
            ok=False,
            output=f"fs.undo: {result.get('output') or 'could not restore'}",
            ms=elapsed_ms(started),
        )

    data = dict(result.get("data") or {})
    restored_path = str(data.get("path") or target)
    if restored_path and ctx.reads is not None:
        ctx.reads[restored_path] = str(data.get("sha256") or "")
    if ctx.conn is not None:
        db.trace(
            ctx.conn,
            span="work",
            name="fs.undo",
            turn_id=ctx.turn_id,
            workspace_id=ctx.workspace_id or None,
            task_id=ctx.task_id or None,
            payload={"backup": data.get("backupId"), "path": restored_path},
            ms=elapsed_ms(started),
        )
    return ToolResult(
        ok=True,
        output=str(result.get("output") or "restored"),
        data=data,
        ms=elapsed_ms(started),
    )
