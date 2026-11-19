"""Her notes: the person's Obsidian vault, as three tools.

Search and read are observations; writing asks first, because a note is the
person's own writing and the vault is not a scratch directory. The vault folder
itself is a setting (Settings -> Notes), never an argument: she cannot point the
tools somewhere else, which is what keeps "add a note" from becoming a way to
edit anything on the disk.
"""

from __future__ import annotations

import time
from pathlib import Path

from ...notes import VaultError
from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms

# Left out of the prompt's tool index while no vault is set: a capability she
# cannot use is budget spent for nothing. They stay registered, so a call still
# answers with what to do about it instead of "unknown tool".
NAMES = ("note.search", "note.read", "note.write")

SEARCH_SPEC = ToolSpec(
    name="note.search",
    description=(
        "Search the person's Obsidian notes. Returns the matching notes with the "
        "passage that matched, so a note can be quoted rather than paraphrased."
    ),
    tier=0,
    max_calls_per_turn=12,
    observation="grep",
    params={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for, in words."},
            "limit": {
                "type": "integer",
                "description": "How many notes to return. Default 6, max 20.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)

READ_SPEC = ToolSpec(
    name="note.read",
    description=(
        "Read one note from the person's Obsidian vault by its vault-relative path, "
        "e.g. 'Projects/launch.md'. Returns the whole note."
    ),
    tier=0,
    max_calls_per_turn=12,
    observation="file",
    params={
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "Path inside the vault, including the .md suffix.",
            },
        },
        "required": ["note"],
        "additionalProperties": False,
    },
)

WRITE_SPEC = ToolSpec(
    name="note.write",
    tier=2,
    mutates=True,
    description=(
        "Add to the person's Obsidian vault: create a new note, append to one that "
        "exists, or rewrite it. Creating will not overwrite an existing note."
    ),
    max_calls_per_turn=8,
    params={
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "Path inside the vault, including the .md suffix.",
            },
            "content": {
                "type": "string",
                "description": "Markdown to write. Frontmatter, links and tags are yours to use.",
            },
            "mode": {
                "type": "string",
                "enum": ["create", "append", "replace"],
                "description": (
                    "create a new note (default), append to the end of one, or "
                    "replace its whole text."
                ),
            },
        },
        "required": ["note", "content"],
        "additionalProperties": False,
    },
)


def _missing(tool: str) -> ToolResult:
    return ToolResult.error(
        f"{tool}: no Obsidian vault is set up yet - ask the person to choose one in Settings -> Notes"
    )


async def note_search(ctx: ToolContext, *, query: str, limit: int = 6) -> ToolResult:
    started = time.monotonic()
    if ctx.notes is None:
        return _missing("note.search")
    try:
        hits = await ctx.notes.search(query, limit=max(1, min(int(limit or 6), 20)))
    except VaultError as exc:
        return ToolResult.error(f"note.search: {exc}")
    if not hits:
        return ToolResult(
            ok=True,
            output=f"No note in the vault matches {query!r}.",
            data={"results": []},
            ms=elapsed_ms(started),
        )
    lines = []
    for hit in hits:
        where = hit["note"] or hit["title"]
        heading = f" / {hit['heading']}" if hit.get("heading") else ""
        lines.append(f"- {where}{heading}: {hit['snippet']}")
    return ToolResult(
        ok=True,
        output="\n".join(lines),
        data={"results": hits, "query": query},
        ms=elapsed_ms(started),
    )


async def note_read(ctx: ToolContext, *, note: str) -> ToolResult:
    started = time.monotonic()
    if ctx.notes is None:
        return _missing("note.read")
    try:
        found = ctx.notes.read(note)
    except VaultError as exc:
        return ToolResult.error(f"note.read: {exc}")
    except OSError as exc:
        return ToolResult.error(f"note.read: could not read {note}: {exc}")
    return ToolResult(
        ok=True,
        output=f"{found['note']} ({found['lines']} lines)\n\n{found['text']}",
        data=found,
        ms=elapsed_ms(started),
    )


async def note_write(
    ctx: ToolContext, *, note: str, content: str, mode: str = "create"
) -> ToolResult:
    started = time.monotonic()
    if ctx.notes is None:
        return _missing("note.write")
    try:
        written = ctx.notes.write(note, content, mode=mode)
    except VaultError as exc:
        return ToolResult.error(f"note.write: {exc}")
    except OSError as exc:
        return ToolResult.error(f"note.write: could not write {note}: {exc}")
    path = written.pop("path", "")
    if path:
        await ctx.notes.index_note(Path(path))
    verb = {"create": "Created", "append": "Appended to", "replace": "Rewrote"}[written["mode"]]
    return ToolResult(
        ok=True,
        output=f"{verb} {written['note']} ({written['lines']} lines).",
        data=written,
        ms=elapsed_ms(started),
    )
