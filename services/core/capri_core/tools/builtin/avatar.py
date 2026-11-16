"""Her body on the desktop.

The daemon is the mind; the sprite is drawn by the shell in its own window. This
tool turns "move to the left" into one event the shell acts on. When no shell is
listening it says so, because the failure mode this replaces was a cheerful
"moving the workspace left" and then nothing at all.
"""

from __future__ import annotations

import time

from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms

PLACE_SPEC = ToolSpec(
    name="avatar.place",
    description=(
        "Move your own body on the desktop — the sprite the user sees — to the "
        "left, right or centre of the screen. This is about where you are "
        "standing, never about the project."
    ),
    params={
        "type": "object",
        "properties": {
            "edge": {
                "type": "string",
                "enum": ["left", "right", "center"],
                "description": "Which side of the screen to stand on.",
            }
        },
        "required": ["edge"],
        "additionalProperties": False,
    },
    # Standing somewhere else changes nothing that cannot be changed back.
    tier=1,
    timeout_ms=5_000,
    max_calls_per_turn=4,
    mutates=True,
)

_SIDES = {"left": "the left", "right": "the right", "center": "the centre"}


async def avatar_place(ctx: ToolContext, *, edge: str) -> ToolResult:
    started = time.monotonic()
    side = _SIDES.get(str(edge).strip().lower())
    if side is None:
        return ToolResult.error("avatar.place: edge must be one of left, right, center")
    if ctx.emit is None:
        return ToolResult.error(
            "avatar.place: the desktop shell is not connected, so there is nowhere to stand"
        )
    await ctx.emit({"type": "avatar.place", "edge": str(edge).strip().lower(), "turnId": ctx.turn_id})
    return ToolResult(
        ok=True, output=f"Moved to {side} of the screen.", ms=elapsed_ms(started)
    )
