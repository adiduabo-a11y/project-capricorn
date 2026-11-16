"""Her body: placing the sprite where she was asked to stand.

    uv run python services/core/tests/test_avatar.py

The daemon cannot move a window, so the tool emits one event and the shell acts
on it. What is checked here is the contract: a benign tier, an enumerable edge,
a real event, and an honest refusal when no shell is listening.
"""

from __future__ import annotations

import asyncio
import sys

from capri_core.config import CapriConfig
from capri_core.tools import builtin_registry
from capri_core.tools.base import ToolContext
from capri_core.tools.builtin.avatar import PLACE_SPEC, avatar_place
from capri_core.tools.policy import Policy

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def context(events: list[dict] | None) -> ToolContext:
    async def emit(event: dict) -> None:
        assert events is not None
        events.append(event)

    return ToolContext(
        conn=None,
        turn_id="turn_9",
        subject="default",
        workspace=".",
        emit=emit if events is not None else None,
    )


async def main() -> int:
    print("manifest")
    check(PLACE_SPEC.name == "avatar.place", "the tool is avatar.place")
    check(PLACE_SPEC.tier == 1, "standing somewhere is benign, not a confirmation")
    check(PLACE_SPEC.mutates, "it changes what is on screen")
    check(
        PLACE_SPEC.params["properties"]["edge"]["enum"] == ["left", "right", "center"],
        "the edge is an enum, so the model cannot invent one",
    )
    check(PLACE_SPEC.params["required"] == ["edge"], "the edge is required")
    check("avatar.place" in builtin_registry().names(), "the daemon registers it")

    verdict = Policy(CapriConfig().policy, ".").check(PLACE_SPEC, {"edge": "left"})
    check(verdict.verdict == "allow", f"policy runs it without asking: {verdict.verdict}")

    print("placing")
    events: list[dict] = []
    result = await avatar_place(context(events), edge="left")
    check(result.ok, "a left placement succeeds")
    check("left" in result.output, f"and says where she went: {result.output!r}")
    check(
        events and events[0]["type"] == "avatar.place" and events[0]["edge"] == "left",
        f"the shell is asked to move her: {events[:1]}",
    )
    check(events and events[0]["turnId"] == "turn_9", "the event is attributed to the turn")

    for edge, word in (("right", "right"), ("center", "centre")):
        events = []
        result = await avatar_place(context(events), edge=edge)
        check(result.ok and word in result.output, f"{edge} placement works: {result.output!r}")

    events = []
    result = await avatar_place(context(events), edge="up")
    check(not result.ok and not events, "an unknown edge is refused, and nothing is emitted")

    result = await avatar_place(context(None), edge="left")
    check(not result.ok, "with no shell the tool fails instead of claiming success")
    check("shell" in result.output, f"and says why: {result.output!r}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
