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
        # The edit target is a fixture this brain lays down, not a file the turn
        # creates. Policy allows a *new* file inside an allowlisted root, so a
        # scripted fs.write into the run directory asked for no card at all;
        # patching an existing file outside the active workspace always does.
        # One fresh name per daemon run, so a rerun cannot depend on last time.
        self.marker = paths.run_dir() / f"smoke-{uuid.uuid4().hex[:8]}.txt"
        self.before = f"capri smoke {int(time.time())}\n"
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(self.before, encoding="utf-8")

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
            return Decision(thought="Edit the marker.", tool="fs.patch", usage=_USAGE)
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
        if spec.name == "fs.patch":
            return {
                "path": str(self.marker),
                "edits": [
                    {
                        "old": "capri smoke ",
                        "new": f"capri smoke approved {int(time.time())} ",
                    }
                ],
            }, _USAGE
        return {}, _USAGE
