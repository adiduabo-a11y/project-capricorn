"""A deterministic stand-in for the planner.

Used by the smoke test (and only then) so the full loop — tool call, policy
check, confirmation, observation, final answer — can be exercised with no model
and no GPU. Enabled with ``CAPRI_FAKE_BRAIN=1``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .. import paths
from .types import ChatResult, Decision, Msg

_USAGE = ChatResult(text="", model="scripted", tokens_in=42, tokens_out=17)


def _observations_this_turn(msgs: list[Msg]) -> int:
    count = 0
    for message in reversed(msgs):
        role = message.get("role")
        if role == "user" and not str(message.get("content", "")).startswith("[observation from "):
            break
        if str(message.get("content", "")).startswith("[observation from "):
            count += 1
    return count


class ScriptedBrain:
    """Two tool calls, one of them destructive, then an answer."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        # One fresh marker per daemon run: fs.write now refuses to overwrite,
        # and the smoke must not depend on leftover state from last time.
        self.marker = paths.capri_home() / "run" / f"smoke-{uuid.uuid4().hex[:8]}.txt"

    async def aclose(self) -> None:
        return None

    async def available(self) -> bool:
        return True

    async def extract(
        self, msgs: list[Msg], schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """One durable fact per scripted turn, enough to exercise the writer."""
        return {
            "items": [
                {
                    "kind": "fact",
                    "text": "The smoke test exercises the memory writer.",
                    "importance": 0.3,
                    "confidence": 0.9,
                    "entities": ["Capri"],
                }
            ]
        }

    async def decide(
        self, msgs: list[Msg], specs: list[Any], persona_pack: str = "kyoko"
    ) -> Decision:
        step = _observations_this_turn(msgs)
        if step == 0:
            return Decision(thought="See what's in front of me.", tool="fs.list", usage=_USAGE)
        if step == 1:
            return Decision(thought="Drop a marker.", tool="fs.write", usage=_USAGE)
        return Decision(
            thought="Done.",
            tool="done",
            final="Smoke complete. Two tools, one approval. Signal's clean.",
            usage=_USAGE,
        )

    async def args(
        self, spec: Any, msgs: list[Msg], feedback: str | None = None
    ) -> tuple[dict[str, Any], ChatResult]:
        if spec.name == "fs.list":
            return {"path": "."}, _USAGE
        if spec.name == "fs.write":
            return {
                "path": str(self.marker),
                "content": f"capri smoke {int(time.time())}\n",
            }, _USAGE
        return {}, _USAGE
