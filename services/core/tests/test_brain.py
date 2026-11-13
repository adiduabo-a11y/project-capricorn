"""Brain tests: grammar, validation, policy, and a full plan->act->observe turn.

No model, no network, no desktop. The fake brain plays a fixed script, so the
loop, the policy gate and the shard writes are all exercised deterministically.

    uv run python services/core/tests/test_brain.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision
from capri_core.config import CapriConfig, PolicyCfg
from capri_core.models import gbnf
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, builtin_registry
from capri_core.tools.schema import validate

FAILURES: list[str] = []
_CONNS: list[Any] = []


def open_test_shard() -> Any:
    conn = db.open_shard("test")
    _CONNS.append(conn)
    return conn


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


class FakeBrain:
    """Plays a fixed list of (decision, args) pairs."""

    def __init__(self, script: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> None:
        self.script = script
        self.index = 0

    async def available(self) -> bool:
        return True

    async def decide(self, msgs: list[Any], specs: list[Any], persona_pack: str = "kyoko") -> Decision:
        step, _ = self.script[min(self.index, len(self.script) - 1)]
        return Decision(**step, usage=ChatResult(text="", tokens_in=10, tokens_out=5))

    async def args(self, spec: Any, msgs: list[Any], feedback: str | None = None):
        _, args = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return dict(args or {}), ChatResult(text="", tokens_in=5, tokens_out=5)


class Harness:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.confirms: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def confirm(self, request: dict[str, Any]) -> bool:
        self.confirms.append(request)
        return True

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == kind]


async def run_turn(config: CapriConfig, session, brain: FakeBrain, workspace: str):
    harness = Harness()
    runner = TurnRunner(
        config=config,
        conn=session.conn,
        session=session,
        emit=harness.emit,
        confirm=harness.confirm,
        brain=brain,
        registry=builtin_registry(),
        policy=Policy(config.policy, workspace),
        workspace=workspace,
    )
    await runner.run("trn_test", "do the thing")
    return harness


# --------------------------------------------------------------------------- unit


def test_grammar() -> None:
    spec = builtin_registry().get("fs.read")
    assert spec is not None
    grammar = gbnf.from_schema(spec.params)
    check(grammar.startswith("root ::="), "schema grammar has a root rule")
    check('"path"' in grammar, "schema grammar mentions required keys")
    check("integer" in grammar, "schema grammar knows numbers")

    decision = gbnf.decision(["fs.read", "fs.write"])
    check('"done"' in decision and '"fs.read"' in decision, "decision grammar enumerates tools")


def test_validation() -> None:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    check(validate(schema, {"path": "a"}) == [], "valid args pass")
    check(bool(validate(schema, {})), "missing required is rejected")
    check(bool(validate(schema, {"path": 3})), "wrong type is rejected")
    check(bool(validate({"type": "string", "enum": ["a", "b"]}, "c")), "enum is enforced")


def test_policy(workspace: str, cfg: PolicyCfg) -> None:
    policy = Policy(cfg, workspace)
    registry = builtin_registry()

    read = registry.get("fs.read")
    write = registry.get("fs.write")
    shell = registry.get("shell.run")
    assert read and write and shell

    check(policy.check(read, {"path": "x.txt"}).verdict == "allow", "reads are free")
    check(
        policy.check(write, {"path": "new.txt", "content": "hi"}).verdict == "allow",
        "writing inside the workspace is allowed",
    )
    check(
        policy.check(write, {"path": "C:/Windows/System32/drivers/etc/hosts", "content": "x"}).verdict
        == "block",
        "denied roots are blocked",
    )
    check(
        policy.check(shell, {"command": "git status"}).verdict == "allow",
        "read-only shell is auto-allowed",
    )
    check(
        policy.check(shell, {"command": "python train.py"}).verdict == "confirm",
        "arbitrary shell needs confirmation",
    )
    check(
        policy.check(shell, {"command": "format c: /q"}).verdict == "block",
        "destructive shell is blocked",
    )
    check(
        policy.check(
            write, {"path": "C:/Users/daeme/.ssh/id_rsa", "content": "x"}
        ).verdict
        == "block",
        "ssh keys are blocked",
    )


# --------------------------------------------------------------------------- loop


async def test_three_step_turn(tmp: Path, config: CapriConfig) -> None:
    notes = tmp / "notes.txt"
    notes.write_text("the sky is green\n", encoding="utf-8")
    out = tmp / "out.txt"
    out.write_text("old contents\n", encoding="utf-8")

    conn = open_test_shard()
    session = Session.create(conn)

    script = [
        ({"thought": "Look first.", "tool": "fs.list"}, {"path": str(tmp)}),
        ({"thought": "Read the notes.", "tool": "fs.read"}, {"path": str(notes)}),
        (
            {"thought": "Write the summary.", "tool": "fs.write"},
            {"path": str(out), "content": "sky=green", "mode": "overwrite"},
        ),
        ({"thought": "Done.", "tool": "done", "final": "Wrote it."}, None),
    ]
    harness = await run_turn(config, session, FakeBrain(script), str(tmp))

    tools = harness.of_type("agent.tool")
    names = [t["name"] for t in tools]
    check(names == ["fs.list", "fs.read", "fs.write"], f"tools ran in order: {names}")
    check(all(t["ok"] for t in tools), "every tool reported ok")

    check(len(harness.confirms) == 1, "exactly one confirmation was requested")
    if harness.confirms:
        request = harness.confirms[0]
        check(request["tool"] == "fs.write", "confirmation names the tool")
        check(request["tierName"] == "act.destructive", "confirmation shows the tier")

    check(out.exists() and out.read_text(encoding="utf-8") == "sky=green", "file was written")

    done = harness.of_type("agent.done")
    check(bool(done) and done[-1]["text"] == "Wrote it.", "final answer streamed")
    check("sky is green" in json.dumps([e for e in harness.events]), "read content reached the agent")

    traces = db.recent_traces(conn, 20)
    spans = [row["span"] for row in traces]
    check(spans.count("tool") == 3, "each tool was traced")
    check("turn" in spans, "the turn was traced")

    rows = conn.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    roles = [row["role"] for row in rows]
    check(roles.count("tool") == 3, "observations were persisted")
    check(roles[-1] == "assistant", "final answer was persisted")
    conn.close()


async def test_blocked_tool(tmp: Path, config: CapriConfig) -> None:
    conn = open_test_shard()
    session = Session.create(conn)

    script = [
        ({"thought": "Clear it out.", "tool": "shell.run"}, {"command": "format c: /q"}),
        ({"thought": "No.", "tool": "done", "final": "Policy said no. Good."}, None),
    ]
    harness = await run_turn(config, session, FakeBrain(script), str(tmp))

    tools = harness.of_type("agent.tool")
    check(len(tools) == 1 and tools[0]["ok"] is False, "blocked call is reported as failed")
    check("blocked" in tools[0]["output"], "blocked reason is visible")
    check(harness.confirms == [], "blocked calls never ask the user")
    check(harness.of_type("agent.done")[-1]["text"].startswith("Policy"), "agent adapts")
    conn.close()


async def test_stall_guard(tmp: Path, config: CapriConfig) -> None:
    conn = open_test_shard()
    session = Session.create(conn)

    # One-entry script: the planner can only repeat itself, forever.
    stuck = FakeBrain([({"thought": "Look again.", "tool": "fs.list"}, {"path": str(tmp)})])
    harness = await run_turn(config, session, stuck, str(tmp))

    tools = harness.of_type("agent.tool")
    check(bool(tools) and tools[0]["ok"] is True, "the first call runs")
    refusals = [t for t in tools if not t["ok"]]
    check(len(refusals) == 3, f"repeats are refused ({len(refusals)})")
    check(all("skipped" in t["output"] for t in refusals), "refusal explains the repeat")
    check(
        harness.of_type("agent.done")[-1]["text"].startswith("I'm going in circles"),
        "the turn ends instead of burning the budget",
    )
    conn.close()


async def main() -> int:
    print("grammar")
    test_grammar()
    print("validation")
    test_validation()

    raw = Path(tempfile.mkdtemp(prefix="capri-brain-"))
    try:
        tmp = raw / "workspace"
        tmp.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")
        config = CapriConfig()
        config.policy = PolicyCfg(
            allow_paths=[str(tmp)],
            deny_paths=["**/.ssh/**", "**/id_rsa", "C:/Windows/**"],
        )

        print("policy")
        test_policy(str(tmp), config.policy)
        print("loop: 3 steps + confirmation")
        await test_three_step_turn(tmp, config)
        print("loop: blocked tool")
        await test_blocked_tool(tmp, config)
        print("loop: stall guard")
        await test_stall_guard(tmp, config)
    finally:
        for conn in _CONNS:
            try:
                conn.close()
            except Exception:
                pass
        shutil.rmtree(raw, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
