"""The full contract of one tool, on request.

The system prompt carries a one-line index of every capability: name, arguments,
a sentence, and how the call gets approved. Everything else about a tool — the
rest of its description, what each argument means, which values an enum accepts
— lives here and costs nothing until she asks for it.
"""

from __future__ import annotations

import difflib
import time

from ..base import ToolContext, ToolResult, ToolSpec, describe_tool, elapsed_ms

DETAIL_SPEC = ToolSpec(
    name="tool.detail",
    description=(
        "Read one tool's full contract: every argument, what it does, and when it "
        "asks for approval. Call this when the catalogue line is not enough to use "
        "the tool safely."
    ),
    params={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A tool name exactly as the catalogue lists it.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    tier=0,
    timeout_ms=5_000,
    max_calls_per_turn=6,
    observation="default",
)


def make_detail(registry):
    """A handler bound to the live registry, so tools added later are visible."""

    async def detail(ctx: ToolContext, *, name: str) -> ToolResult:
        started = time.monotonic()
        wanted = str(name or "").strip()
        spec = registry.get(wanted)
        if spec is None:
            known = registry.names()
            close = difflib.get_close_matches(wanted, known, n=3, cutoff=0.4)
            hint = f" Did you mean {', '.join(close)}?" if close else ""
            return ToolResult.error(
                f"tool.detail: no tool named {wanted!r}.{hint} "
                f"Available: {', '.join(sorted(known))}"
            )
        return ToolResult(ok=True, output=describe_tool(spec), ms=elapsed_ms(started))

    return detail
