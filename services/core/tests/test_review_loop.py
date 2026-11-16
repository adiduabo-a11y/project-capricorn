"""Auto review as the loop uses it: which calls skip the question.

    uv run python services/core/tests/test_review_loop.py

The reviewer is a stub, so this checks the wiring and the guards — tier three,
the veto list, a missing or slow model, a refusal — without a model, a database
or a turn.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from capri_core.agent.loop import TurnRunner
from capri_core.tools.policy import CONFIRM, Decision

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


class Stub:
    """Only what `_auto_review` touches."""

    def __init__(self, payload=None, *, delay: float = 0.0, raises: bool = False, available: bool = True, review: str = "auto") -> None:
        self.workspace = "C:\\work"
        self.conn = None
        self.workspace_id = None
        self.task_id = None
        self.traces: list[tuple[bool, str]] = []
        self.calls = 0
        self.request = ""
        self.msgs: list = []
        self._delay = delay
        self._raises = raises
        self._payload = payload if payload is not None else {}
        self.brain = self
        self._available = available
        self.config = SimpleNamespace(
            policy=SimpleNamespace(confirm=SimpleNamespace(review=review, review_timeout_s=0.05))
        )

    async def available(self) -> bool:
        return self._available

    async def args(self, spec, msgs):  # noqa: ANN001 - mirrors Brain.args
        self.calls += 1
        self.msgs = list(msgs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise RuntimeError("model unreachable")
        return self._payload, None

    async def _trace_review(self, turn_id, name, approved, reason):  # noqa: ANN001
        self.traces.append((approved, reason))


def confirm(tier: int = 2) -> Decision:
    return Decision(CONFIRM, tier, "arbitrary shell command")


def review(
    stub: Stub,
    *,
    tier: int = 2,
    args: dict | None = None,
    tool: str = "shell.run",
    request: str = "close roblox studio",
) -> bool:
    stub.request = request
    return asyncio.run(
        TurnRunner._auto_review(
            stub, "turn_1", tool, args or {"command": "rm -rf build"}, confirm(tier)
        )
    )


def main() -> int:
    print("approved")
    stub = Stub({"approve": True, "reversible": True, "blast": "build/", "reason": "a build artefact"})
    check(review(stub) is True, "a clean yes on both counts skips the question")
    check(stub.calls == 1, "the reviewer was asked once")
    check(stub.traces and stub.traces[-1][0] is True, "the decision is traced")
    check(
        'The person asked: "close roblox studio"' in str(stub.msgs[-1].get("content")),
        "the reviewer is told what was asked",
    )

    print("closing an app is reviewable")
    stub = Stub({"approve": True, "reversible": True, "reason": "an app can be reopened"})
    check(
        review(stub, tool="process.close", args={"name": "roblox studio"}) is True,
        "a named app close can be approved without asking",
    )
    check(
        "roblox studio" in str(stub.msgs[1].get("content")),
        "the reviewer sees which app it is",
    )
    stub = Stub({"approve": True, "reversible": True, "reason": "x"})
    check(
        review(stub, tool="process.close", args={"name": "roblox studio", "force": True})
        is False,
        "a forced close still asks, whatever the reviewer would say",
    )
    check(stub.calls == 0, "and is not even sent to the reviewer")

    print("refused")
    stub = Stub({"approve": True, "reversible": False, "reason": "loses work"})
    check(review(stub) is False, "no undo means it still asks")
    check(stub.traces and stub.traces[-1][0] is False, "and the refusal is traced")

    print("never reviewed")
    stub = Stub({"approve": True, "reversible": True, "reason": "x"})
    check(review(stub, args={"command": "rm -rf /"}) is False, "a vetoed command still asks")
    check(stub.calls == 0, "and the model is never asked about it")
    stub = Stub({"approve": True, "reversible": True, "reason": "x"})
    check(review(stub, tier=3) is False, "a tier-three call still asks")
    check(stub.calls == 0, "and is not sent to a model")
    stub = Stub({"approve": True, "reversible": True, "reason": "x"}, review="off")
    check(review(stub) is False, "confirm.review=off leaves every question in place")
    check(stub.calls == 0, "without a model call")

    print("failures ask")
    stub = Stub({"approve": True, "reversible": True, "reason": "x"}, raises=True)
    check(review(stub) is False, "a reviewer that errors leaves the question")
    stub = Stub({"approve": True, "reversible": True, "reason": "x"}, delay=0.4)
    check(review(stub) is False, "a reviewer that is too slow leaves the question")
    stub = Stub({"approve": True, "reversible": True, "reason": "x"}, available=False)
    check(review(stub) is False, "no model means the question stays")
    check(stub.calls == 0, "and it is not called when unavailable")
    stub = Stub("not an object")
    check(review(stub) is False, "a malformed answer leaves the question")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
