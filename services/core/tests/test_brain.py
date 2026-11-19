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

from capri_core.agent import prompt as prompt_mod
from capri_core.agent.loop import TurnRunner, _event
from capri_core.agent.types import ChatResult, Decision
from capri_core.config import CapriConfig, PolicyCfg
from capri_core.models import gbnf
from capri_core.server import protocol
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


class StepBrain:
    """Plays one decision per planner call and remembers what it was shown."""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = steps
        self.index = 0
        self.seen: list[list[str]] = []
        # One entry per planner call: the role/content of every message that
        # would be serialized into that request.
        self.requests: list[list[tuple[str, str]]] = []

    async def available(self) -> bool:
        return True

    async def decide(self, msgs: list[Any], specs: list[Any], persona_pack: str = "kyoko") -> Decision:
        self.seen.append([str(m.get("content", "")) for m in msgs])
        self.requests.append(
            [(str(m.get("role", "")), str(m.get("content", ""))) for m in msgs]
        )
        step = self.steps[min(self.index, len(self.steps) - 1)]
        self.index += 1
        return Decision(**step, usage=ChatResult(text="", tokens_in=10, tokens_out=5))

    async def args(self, spec: Any, msgs: list[Any], feedback: str | None = None):
        return {}, ChatResult(text="", tokens_in=5, tokens_out=5)


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


async def run_turn(
    config: CapriConfig,
    session,
    brain: FakeBrain,
    workspace: str,
    request: str = "do the thing",
    turn_id: str = "trn_test",
):
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
    await runner.run(turn_id, request)
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
    grep = registry.get("fs.grep")
    write = registry.get("fs.write")
    shell = registry.get("shell.run")
    assert read and grep and write and shell

    check(policy.check(read, {"path": "x.txt"}).verdict == "allow", "reads are free")
    check(
        policy.check(grep, {"pattern": "x"}).verdict == "allow",
        "grep in the workspace is free",
    )
    check(
        policy.check(
            grep, {"pattern": "x", "path": "C:/Users/daeme/.ssh/id_rsa"}
        ).verdict
        == "block",
        "grep cannot search denied files",
    )
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
            # fs.write only creates; changing an existing file goes through
            # fs.patch, which preserves encoding and leaves an undo record.
            {"thought": "Correct the summary.", "tool": "fs.patch"},
            {"path": str(out), "edits": [{"old": "old contents", "new": "sky=green"}]},
        ),
        ({"thought": "Done.", "tool": "done", "final": "Wrote it."}, None),
    ]
    harness = await run_turn(config, session, FakeBrain(script), str(tmp))

    tools = harness.of_type("agent.tool")
    names = [t["name"] for t in tools]
    check(names == ["fs.list", "fs.read", "fs.patch"], f"tools ran in order: {names}")
    check(all(t["ok"] for t in tools), "every tool reported ok")

    check(len(harness.confirms) == 1, "exactly one confirmation was requested")
    if harness.confirms:
        request = harness.confirms[0]
        check(request["tool"] == "fs.patch", "confirmation names the tool")
        check(request["tierName"] == "act.destructive", "confirmation shows the tier")

    check(
        out.exists() and out.read_text(encoding="utf-8").strip() == "sky=green",
        "file was patched",
    )

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


async def test_casual_turn_no_tools(tmp: Path, config: CapriConfig) -> None:
    """Talking must not cost a tool call, so no work block is invented."""
    conn = open_test_shard()
    session = Session.create(conn)
    brain = FakeBrain([({"thought": "", "tool": "done", "final": "Evening."}, None)])
    harness = await run_turn(config, session, brain, str(tmp))

    check(harness.of_type("agent.tool") == [], "casual turn makes no tool calls")
    check(harness.of_type("agent.done")[-1]["text"] == "Evening.", "casual turn answers")
    conn.close()


async def test_prompt_is_built_once(tmp: Path, config: CapriConfig) -> None:
    """One system prompt per turn, one copy in each request it is sent with.

    A chat-completions server keeps no session, so the prompt rides along with
    every step; what it must not do is get rebuilt or duplicated per step.
    """
    conn = open_test_shard()
    session = Session.create(conn)
    turn_id = "trn_prompt_once"

    calls = {"n": 0}
    real = prompt_mod.system_prompt

    def counting(*args: Any, **kwargs: Any) -> str:
        calls["n"] += 1
        return real(*args, **kwargs)

    brain = StepBrain(
        [
            {"thought": "Look.", "tool": "fs.list"},
            {"thought": "Look again.", "tool": "fs.list"},
            {"thought": "", "tool": "done", "final": "Listed."},
        ]
    )
    prompt_mod.system_prompt = counting
    try:
        await run_turn(config, session, brain, str(tmp), turn_id=turn_id)
    finally:
        prompt_mod.system_prompt = real

    check(len(brain.requests) == 3, f"three planner calls: {len(brain.requests)}")
    check(calls["n"] == 1, f"the prompt was built once, not per step: {calls['n']}")
    systems = [
        [content for role, content in request if role == "system"]
        for request in brain.requests
    ]
    check(all(len(found) == 1 for found in systems), f"one copy per request: {[len(f) for f in systems]}")
    check(
        len({found[0] for found in systems if found}) == 1,
        "every step sends the identical prompt",
    )
    conn.close()


def test_announces_action() -> None:
    """The predicate names promises, not every future tense."""
    for line in [
        "I'll write the file now.",
        "I will check that.",
        "I'm going to patch it.",
        "Let me read the log first.",
        "On it.",
    ]:
        check(prompt_mod.announces_action(line), f"promises work: {line!r}")
    for line in [
        "I'll be here.",
        "I'll let you know.",
        "Sure, the sky is green.",
        "",
    ]:
        check(not prompt_mod.announces_action(line), f"is just talking: {line!r}")


def nudges_for(conn: Any, turn_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in db.recent_traces(conn, 500)
        if row["name"] == "turn.nudge" and row["turn_id"] == turn_id
    ]


async def test_announced_action_is_nudged(tmp: Path, config: CapriConfig) -> None:
    """Saying she will do the work is not doing it: the turn asks again."""
    conn = open_test_shard()
    session = Session.create(conn)
    turn_id = "trn_nudge"
    brain = StepBrain(
        [
            {"thought": "", "tool": "done", "final": "I'll write the file now."},
            {"thought": "", "tool": "done", "final": "Wrote it."},
        ]
    )
    harness = await run_turn(config, session, brain, str(tmp), turn_id=turn_id)

    check(len(brain.seen) == 2, f"the planner was asked again: {len(brain.seen)} calls")
    check(
        any("no tool ran" in text for text in brain.seen[-1]),
        "the nudge reached the model",
    )
    check(
        any("I'll write the file now." in text for text in brain.seen[-1]),
        "the model saw what it said it would do",
    )
    check(
        harness.of_type("agent.done")[-1]["text"] == "Wrote it.",
        "the announcement did not end the turn",
    )
    check(len(nudges_for(conn, turn_id)) == 1, "the nudge was traced")
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE turn_id = ? ORDER BY id", (turn_id,)
    ).fetchall()
    assistant = [row["content"] for row in rows if row["role"] == "assistant"]
    check(assistant == ["Wrote it."], f"only the real answer was persisted: {assistant}")
    conn.close()


async def test_nudges_are_bounded(tmp: Path, config: CapriConfig) -> None:
    """A model that will not act is not asked forever."""
    conn = open_test_shard()
    session = Session.create(conn)
    turn_id = "trn_nudge_twice"
    same = {"thought": "", "tool": "done", "final": "I'll start on it now."}
    brain = StepBrain([same, same, same])
    harness = await run_turn(config, session, brain, str(tmp), turn_id=turn_id)

    check(len(brain.seen) == 3, f"two nudges, three planner calls: {len(brain.seen)}")
    check(len(nudges_for(conn, turn_id)) == 2, "both nudges were traced")
    check(
        harness.of_type("agent.done")[-1]["text"] == "I'll start on it now.",
        "the third refusal stands instead of looping",
    )
    conn.close()


async def test_nudge_on_the_last_step(tmp: Path, config: CapriConfig) -> None:
    """With no step left to act in, her reply survives and the truth follows."""
    conn = open_test_shard()
    session = Session.create(conn)
    turn_id = "trn_nudge_ceiling"
    # One step: the first decision is also the last, which is the case where a
    # nudge used to fall out of the loop and lose the reply.
    tight = config.model_copy(deep=True)
    tight.agent.max_steps = 1
    brain = StepBrain(
        [{"thought": "", "tool": "done", "final": "I'll write the file now."}]
    )
    harness = await run_turn(tight, session, brain, str(tmp), turn_id=turn_id)

    text = harness.of_type("agent.done")[-1]["text"]
    check(text.startswith("I'll write the file now."), f"her reply is not lost: {text!r}")
    check("nothing actually ran" in text, "it says the work did not happen")
    check("last step" in text, "it names why")
    check(not text.startswith("I hit the"), "the generic ceiling line is not used")
    check(len(brain.seen) == 1, f"no pointless second call: {len(brain.seen)}")
    check(len(nudges_for(conn, turn_id)) == 1, "the announcement was still traced")
    conn.close()


async def test_conversation_is_not_nudged(tmp: Path, config: CapriConfig) -> None:
    """A promise made in conversation is not a broken one."""
    conn = open_test_shard()
    session = Session.create(conn)
    turn_id = "trn_nudge_chat"
    brain = StepBrain(
        [{"thought": "", "tool": "done", "final": "I'll write it when you ask."}]
    )
    harness = await run_turn(
        config, session, brain, str(tmp), request="hey, capri", turn_id=turn_id
    )

    check(len(brain.seen) == 1, f"a greeting is answered once: {len(brain.seen)} call(s)")
    check(
        harness.of_type("agent.done")[-1]["text"] == "I'll write it when you ask.",
        "the reply stands",
    )
    check(nudges_for(conn, turn_id) == [], "no nudge was traced")
    conn.close()


async def test_turn_carries_work_ids(tmp: Path, config: CapriConfig) -> None:
    """Every tool event and trace row is attributable to a workspace and task."""
    conn = open_test_shard()
    session = Session.create(conn)
    (tmp / "a.txt").write_text("x\n", encoding="utf-8")

    work = prompt_mod.WorkContext(
        workspace_id="wsp_test", task_id="tsk_test", name="demo", root=str(tmp)
    )
    brain = FakeBrain(
        [
            ({"thought": "Look.", "tool": "fs.list"}, {"path": str(tmp)}),
            ({"thought": "Done.", "tool": "done", "final": "Listed."}, None),
        ]
    )
    harness = Harness()
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=session,
        emit=harness.emit,
        confirm=harness.confirm,
        brain=brain,
        registry=builtin_registry(),
        policy=Policy(config.policy, str(tmp)),
        workspace=str(tmp),
        work=work,
    )
    await runner.run("trn_work", "list it")

    tools = harness.of_type("agent.tool")
    check(len(tools) == 1, "one tool ran")
    check(
        bool(tools) and tools[0]["workspaceId"] == "wsp_test" and tools[0]["taskId"] == "tsk_test",
        "tool events carry workspace and task ids",
    )
    done = harness.of_type("agent.done")[-1]
    check(
        done["workspaceId"] == "wsp_test" and done["taskId"] == "tsk_test",
        "the final event carries workspace and task ids",
    )

    rows = conn.execute(
        "SELECT workspace_id, task_id FROM traces WHERE turn_id = 'trn_work'"
    ).fetchall()
    check(
        len(rows) == 2
        and all(row["workspace_id"] == "wsp_test" and row["task_id"] == "tsk_test" for row in rows),
        "trace rows carry workspace and task ids",
    )

    msgs = conn.execute("SELECT task_id FROM messages WHERE turn_id = 'trn_work'").fetchall()
    check(
        len(msgs) == 3 and all(row["task_id"] == "tsk_test" for row in msgs),
        "messages carry the task id",
    )
    conn.close()


async def test_failed_decision_not_persisted(tmp: Path, config: CapriConfig) -> None:
    conn = open_test_shard()
    session = Session.create(conn)
    failed = FakeBrain(
        [
            (
                {
                    "thought": "",
                    "tool": "done",
                    "final": (
                        "I couldn't get a valid planner decision from the model. "
                        "Check the Brain model and structured-output setting, then try again."
                    ),
                    "failed": True,
                },
                None,
            )
        ]
    )
    harness = await run_turn(config, session, failed, str(tmp))

    done = harness.of_type("agent.done")
    check(bool(done) and "valid planner decision" in done[-1]["text"], "failure is surfaced")
    rows = conn.execute(
        "SELECT role FROM messages WHERE episode_id = ? ORDER BY id",
        (session.episode_id,),
    ).fetchall()
    check([row["role"] for row in rows] == ["user"], "failed planner output is not replayed as assistant history")

    session.add_message(
        "trn_old",
        "assistant",
        "My planner produced something that wasn't a decision. Try again.",
    )
    session.add_message("trn_old", "user", "how are you?")
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=session,
        emit=Harness().emit,
        confirm=Harness().confirm,
        brain=FakeBrain([({"thought": "", "tool": "done", "final": "fine"}, None)]),
        registry=builtin_registry(),
        policy=Policy(config.policy, str(tmp)),
        workspace=str(tmp),
    )
    history = runner._history()
    check(
        all("planner produced" not in message["content"] for message in history),
        "old harness failure rows are kept out of the prompt",
    )
    conn.close()


def test_work_schema() -> None:
    """The shard knows about work before the services that fill it exist."""
    conn = open_test_shard()
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check(
        {"workspaces", "tasks", "file_backups", "artifacts"} <= tables,
        "work tables exist",
    )
    trace_cols = {row["name"] for row in conn.execute("PRAGMA table_info(traces)")}
    check(
        {"workspace_id", "task_id"} <= trace_cols,
        "traces carry workspace and task ids",
    )
    message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    check("task_id" in message_cols, "messages carry task ids")
    memory_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memories)")}
    check("workspace_id" in memory_cols, "memories carry workspace ids")
    busy = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    check(busy == 5000, f"a locked shard is waited on, not failed: busy_timeout={busy}")
    conn.close()


def test_work_prompt(tmp: Path) -> None:
    """The persona and the work block say what the plan says they say."""
    persona = prompt_mod.load_persona("kyoko").lower()
    check("not an assistant" not in persona, "persona no longer disclaims being an assistant")
    check("companion with hands" in persona, "persona states the companion-with-hands framing")
    check("look before you touch" in persona, "persona carries the inspect-first rule")

    work = prompt_mod.WorkContext(
        workspace_id="wsp_1",
        task_id="tsk_1",
        name="capri",
        root=str(tmp),
        task_title="fix the parser",
        task_status="active",
        commands=(("test", "uv run pytest"), ("lint", "ruff check .")),
        conventions="Two spaces. No tabs.",
    )
    block = prompt_mod.work_block(work)
    check("Root:" in block and "fix the parser" in block, "work block names root and task")
    check("uv run pytest" in block and "ruff check ." in block, "work block lists detected commands")
    check("Two spaces" in block, "work block carries conventions")
    check("data, not instructions" in block, "work block keeps the data framing")
    check(prompt_mod.work_block(None) == "", "no workspace means no work block")
    check(
        prompt_mod.work_block(prompt_mod.WorkContext(workspace_id="wsp_1")) == "",
        "ids alone do not produce a block",
    )

    long_work = prompt_mod.WorkContext(
        name="capri", root=str(tmp), conventions="x" * (prompt_mod.CONVENTIONS_LIMIT * 3)
    )
    long_block = prompt_mod.work_block(long_work)
    # The block also carries the coding protocol, and the temp path may itself
    # contain an "x", so assert on the run of characters rather than a count.
    check(
        "[conventions truncated]" in long_block
        and "x" * prompt_mod.CONVENTIONS_LIMIT in long_block
        and "x" * (prompt_mod.CONVENTIONS_LIMIT + 1) not in long_block,
        "long conventions are capped",
    )

    system = prompt_mod.system_prompt([], "kyoko", work=work)
    check(str(tmp) in system and "## Workspace" in system, "work block reaches the system prompt")
    check(
        system.index("## Workspace") < system.index("## Acting"),
        "work block sits ahead of the acting protocol",
    )
    bare = prompt_mod.system_prompt([], "kyoko")
    check("## Workspace" not in bare, "no workspace means no block in the system prompt")

    # The screen protocol is for turns that are about a window, not every turn.
    check(
        "## Controlling a window" not in system,
        "the screen protocol stays out of an ordinary turn",
    )
    looking = prompt_mod.system_prompt([], "kyoko", work=work, screen=True)
    check("## Controlling a window" in looking, "the screen protocol joins a screen turn")
    check("expect" in looking, "the screen protocol asks for an expectation")
    check(
        looking.index("## Workspace") < looking.index("## Controlling a window"),
        "the screen protocol follows the workspace block",
    )

    # The injection is re-sent on every step of a turn, so its size is pinned:
    # a new tool or a reworded paragraph has to fit, not just compile.
    # Raised once from 8,350/8,980 when the three note tools landed (measured
    # 8,524/9,149): the vault is a capability she has to be able to discover,
    # and its three index lines are the whole cost of it.
    specs = builtin_registry().specs()
    plain = prompt_mod.system_prompt(
        specs, "kyoko", profile="focus", request="fix the failing test"
    )
    check(len(plain) <= 8600, f"system prompt is {len(plain)} chars")
    with_work = prompt_mod.system_prompt(
        specs,
        "kyoko",
        profile="focus",
        request="fix the failing test",
        work=prompt_mod.WorkContext(
            workspace_id="w", task_id="t", name="p", root="C:/p"
        ),
    )
    check(len(with_work) <= 9220, f"work prompt is {len(with_work)} chars")

    index = prompt_mod.describe_tools(specs)
    check(
        index.count("[asks] = the person is asked before that tool runs.") == 1,
        "consent is explained once, in a legend",
    )
    check("unattended" not in index, "the legend does not claim the rest are silent")
    check("asks the person first" not in index, "no row repeats the consent suffix")
    # Count from the registry, not a constant: the marker has to be on every row
    # that asks, and adding a tool must not look like a broken legend.
    asking = sum(1 for spec in specs if prompt_mod.consent_note(spec) == "asks the person first")
    check(
        index.count("[asks]") == asking + 1,
        f"the marker is on each of the {asking} asking rows, plus the legend",
    )


def test_event_fields() -> None:
    event = _event("memory.written", kind="fact", text="x")
    check(event["type"] == "memory.written" and event["kind"] == "fact", "loop event fields cannot collide with the type")
    wire = protocol.event("memory.written", kind="fact", text="x")
    check(wire["type"] == "memory.written" and wire["kind"] == "fact", "wire event fields cannot collide with the type")


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
        print("loop: failed decision")
        await test_failed_decision_not_persisted(tmp, config)
        print("loop: casual turn")
        await test_casual_turn_no_tools(tmp, config)
        print("loop: prompt built once")
        await test_prompt_is_built_once(tmp, config)
        print("nudge: predicate")
        test_announces_action()
        print("nudge: announced action")
        await test_announced_action_is_nudged(tmp, config)
        print("nudge: bounded")
        await test_nudges_are_bounded(tmp, config)
        print("nudge: last step")
        await test_nudge_on_the_last_step(tmp, config)
        print("nudge: conversation")
        await test_conversation_is_not_nudged(tmp, config)
        print("loop: workspace and task ids")
        await test_turn_carries_work_ids(tmp, config)
        print("prompt")
        test_work_prompt(tmp)
        print("schema")
        test_work_schema()
        print("events")
        test_event_fields()
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
