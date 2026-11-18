"""Task continuity: one task per thread of work, paused at a ceiling.

No model and no network beyond a local child process. Every task lives in a
temp shard under a temp ``CAPRI_HOME``.

    uv run python services/core/tests/test_tasks.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

from capri_core.agent import prompt
from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision, Msg
from capri_core.config import CapriConfig, PolicyCfg
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, builtin_registry
from capri_core.work.tasks import TaskService, title_for
from capri_core.workspace.service import WorkspaceService

FAILURES: list[str] = []
_CONNS: list[Any] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def open_shard() -> Any:
    conn = db.open_shard("test")
    _CONNS.append(conn)
    return conn


def make_config(project: Path, **agent: Any) -> CapriConfig:
    config = CapriConfig()
    config.policy = PolicyCfg(allow_paths=[str(project)], deny_paths=[])
    for key, value in agent.items():
        setattr(config.agent, key, value)
    return config


class ScriptedBrain:
    def __init__(self, script: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> None:
        self.script = script
        self.index = 0
        self.seen: list[list[Msg]] = []

    async def available(self) -> bool:
        return True

    async def decide(
        self, msgs: list[Msg], specs: Any, persona_pack: str = "kyoko"
    ) -> Decision:
        self.seen.append(list(msgs))
        step, _ = self.script[min(self.index, len(self.script) - 1)]
        return Decision(**step, usage=ChatResult(text="", tokens_in=1, tokens_out=1))

    async def args(self, spec: Any, msgs: list[Msg], feedback: str | None = None):
        _, args = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return dict(args or {}), ChatResult(text="", tokens_in=1, tokens_out=1)


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
        return [event for event in self.events if event.get("type") == kind]


def make_runner(
    *,
    conn: Any,
    config: CapriConfig,
    project: Path,
    brain: ScriptedBrain,
    harness: Harness,
    tasks: TaskService,
    task: Any,
    resume: bool = False,
    observations: tuple[str, ...] = (),
    session: Any | None = None,
) -> TurnRunner:
    policy = Policy(config.policy, str(project))
    policy.set_workspace(str(project), active=True, trusted=True)
    work = prompt.WorkContext(
        workspace_id=task.workspace_id,
        task_id=task.id,
        name=project.name,
        root=str(project),
        task_title=task.title,
        task_status=task.status,
        task_summary=task.summary,
        task_observations=observations,
    )
    return TurnRunner(
        config=config,
        conn=conn,
        session=session or Session.create(conn),
        emit=harness.emit,
        confirm=harness.confirm,
        brain=brain,
        registry=builtin_registry(),
        policy=policy,
        workspace=str(project),
        work=work,
        tasks=tasks,
        resume=resume,
    )


# --------------------------------------------------------------------------- service


def test_titles_and_service(tmp: Path) -> None:
    conn = open_shard()
    service = WorkspaceService(conn, CapriConfig())
    project = tmp / "service"
    project.mkdir()
    space = service.open(str(project))
    tasks = TaskService(conn)

    check(title_for("  fix   the \n parser  ") == "fix the parser", "a title is the request, collapsed")
    long_title = title_for("word " * 40)
    check(long_title.endswith("…") and len(long_title) <= 61, "a long request is cut at a word")
    check(title_for("") == "", "an empty request has an empty title")

    task = tasks.create(space.id, "Fix the parser\nplease", title="Fix the parser")
    check(task.status == "active" and task.steps == 0, "a task starts active with no steps")
    check(task.title == "Fix the parser", "an explicit title wins")
    check(tasks.get(task.id) is not None, "the task is readable by id")
    check(tasks.open_for(space.id).id == task.id, "the open task is the active one")

    tasks.add_step(task.id, 2)
    check(tasks.get(task.id).steps == 2, "steps accumulate")
    tasks.touch(task.id, last_turn_id="trn_a")
    check(tasks.get(task.id).last_turn_id == "trn_a", "the last turn id is recorded")

    paused = tasks.pause(task.id, "halfway")
    check(paused.status == "paused" and paused.summary == "halfway", "a task can pause with a summary")
    check(tasks.open_for(space.id).id == task.id, "a paused task is still the one to continue")
    check(tasks.resume(task.id).status == "active", "resume puts it back to work")

    done = tasks.finish(task.id, result="passed", summary="all green")
    check(done.status == "done" and done.result == "passed", "finish records the outcome")
    check(tasks.open_for(space.id) is None, "a finished task is no longer open")
    check(tasks.recent(space.id)[0].id == task.id, "a finished task stays in history")
    check(tasks.recent("wsp_missing") == [], "history is scoped to a workspace")

    second = tasks.create(space.id, "Second job")
    abandoned = tasks.abandon(second.id)
    check(abandoned.status == "abandoned", "abandon is its own status")
    check(tasks.open_for(space.id) is None, "an abandoned task is not open")
    check(tasks.get(second.id) is not None, "an abandoned task keeps its record")

    check(tasks.summarize("tsk_missing") == "", "a missing task summarises to nothing")


def test_conversation_gate() -> None:
    """A greeting or a correction must not be bound to the open task."""
    from capri_core.server.app import _is_conversation

    check(_is_conversation("Having fun Capri?"), "a greeting is conversation")
    check(_is_conversation("thanks!"), "a thank-you is conversation")
    check(_is_conversation("I didn't ask you to update index.html"),
          "a correction is conversation")
    check(_is_conversation("why did you change index.html?"), "a complaint is conversation")
    check(_is_conversation("stop"), "stop is conversation")
    check(not _is_conversation("Add a footer to the hero section"), "an instruction is work")
    check(not _is_conversation("ok, now add a footer"),
          "a pleasantry prefix does not hide an instruction")
    check(not _is_conversation("continue"), "an explicit continue is work")


def test_summary_and_observations(tmp: Path) -> None:
    conn = open_shard()
    service = WorkspaceService(conn, CapriConfig())
    project = tmp / "summary"
    project.mkdir()
    space = service.open(str(project))
    tasks = TaskService(conn)
    task = tasks.create(space.id, "Change two files and run a check")
    now = int(time.time() * 1000)
    for index, (name, added, removed) in enumerate(
        [("src/one.py", 4, 1), ("tests/two.py", 2, 0)]
    ):
        conn.execute(
            "INSERT INTO file_backups (id, task_id, turn_id, path, before_hash,"
            " after_hash, lines_added, lines_removed, created_at)"
            " VALUES (?, ?, 'trn_s', ?, 'a', 'b', ?, ?, ?)",
            (f"bkp_{index}", task.id, str(project / name), added, removed, now + index),
        )
    db.trace(
        conn,
        span="tool",
        name="shell.run",
        task_id=task.id,
        payload={"ok": True, "args": {"command": "pytest -q"}},
    )
    db.trace(
        conn,
        span="tool",
        name="shell.run",
        task_id=task.id,
        payload={"ok": False, "args": {"command": "ruff check ."}},
    )
    tasks.add_step(task.id, 5)

    summary = tasks.summarize(task.id)
    check("Changed 2 file(s) (+6 -1)" in summary, "the summary counts the diff")
    check("one.py" in summary and "two.py" in summary, "the summary names the files")
    check("pytest -q (ok)" in summary, "a command that passed is listed")
    check("ruff check . (failed)" in summary, "a command that failed is listed")
    check("Steps so far: 5" in summary, "the summary counts steps")
    check("Ran: " in summary and summary.index("pytest") < summary.index("ruff"), "commands are oldest first")

    empty = tasks.create(space.id, "Nothing yet")
    check("Changed nothing yet" in tasks.summarize(empty.id), "an untouched task says so")

    session = Session.create(conn)
    for text in ("first observation", "second observation", "third observation", "fourth"):
        session.add_message("trn_s", "tool", text, task_id=task.id)
    session.add_message("trn_s", "tool", "another task", task_id=empty.id)
    seen = tasks.observations(task.id)
    check(len(seen) == 3, "at most three observations come back")
    check(seen[-1] == "fourth", "the newest observation is last")
    check(seen[0] == "second observation", "only the last three are kept")
    check(all("another task" != item for item in seen), "observations are scoped to the task")


# --------------------------------------------------------------------------- loop


async def test_ceiling_pauses(tmp: Path, config: CapriConfig) -> None:
    project = tmp / "ceiling"
    project.mkdir()
    (project / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (project / "two.py").write_text("TWO = 2\n", encoding="utf-8")

    conn = open_shard()
    service = WorkspaceService(conn, CapriConfig())
    space = service.open(str(project))
    tasks = TaskService(conn)
    task = tasks.create(space.id, "Read both files and tell me what they do")

    config = make_config(project, max_steps=2)
    brain = ScriptedBrain(
        [
            ({"thought": "Read the first.", "tool": "fs.read"}, {"path": str(project / "one.py")}),
            ({"thought": "Read the second.", "tool": "fs.read"}, {"path": str(project / "two.py")}),
            ({"thought": "Answer.", "tool": "done", "final": "Two constants."}, None),
        ]
    )
    harness = Harness()
    runner = make_runner(
        conn=conn, config=config, project=project, brain=brain,
        harness=harness, tasks=tasks, task=task,
    )
    await runner.run("trn_ceiling", "Read both files and tell me what they do")

    stored = conn.execute(
        "SELECT tool_name, tool_args, tool_result, tool_ok, tool_tier, tool_ms"
        " FROM messages WHERE role = 'tool' AND turn_id = 'trn_ceiling' ORDER BY id"
    ).fetchall()
    check(len(stored) == 2, f"both tool steps are stored: {len(stored)}")
    check(stored[0]["tool_name"] == "fs.read", "a stored step keeps the tool's name")
    check(
        json.loads(str(stored[0]["tool_args"]))["path"] == str(project / "one.py"),
        "and the arguments it was called with",
    )
    check(bool(stored[0]["tool_result"]), "and what it returned")
    check(stored[0]["tool_ok"] == 1 and stored[0]["tool_tier"] is not None
          and stored[0]["tool_ms"] >= 0, "and that it succeeded, at a tier, with a duration")

    paused = tasks.get(task.id)
    check(paused.status == "paused", "the step ceiling pauses the task instead of finishing it")
    check(paused.result == "none", "a paused task is not marked passed or failed")
    check(paused.steps == 2, "the task counted the steps it ran")
    check("Steps so far: 2" in paused.summary, "the summary records where it stopped")
    check(runner.paused_reason == "step ceiling", "the loop knows why it stopped")

    done = harness.of_type("agent.done")
    check(bool(done), "the turn still ends with a reply")
    check("ceiling" in done[-1]["text"], "the reply says it hit the ceiling")
    check(done[-1]["taskId"] == task.id, "agent.done carries the task id")

    changed = harness.of_type("task.changed")
    check(bool(changed), "a task.changed event is emitted")
    check(changed[-1]["task"]["status"] == "paused", "the event carries the paused task")
    kinds = [event["type"] for event in harness.events]
    check(
        kinds.index("task.changed") < kinds.index("agent.done"),
        "the task update reaches the shell before done",
    )

    tool_events = harness.of_type("agent.tool")
    check(
        all(event.get("taskId") == task.id for event in tool_events),
        "tool events carry the task id",
    )
    stored = conn.execute(
        "SELECT role, task_id FROM messages WHERE turn_id = 'trn_ceiling'"
    ).fetchall()
    check(bool(stored) and all(row["task_id"] == task.id for row in stored),
          "messages are stamped with the task id")
    traced = conn.execute(
        "SELECT task_id FROM traces WHERE turn_id = 'trn_ceiling'"
    ).fetchall()
    check(bool(traced) and all(row["task_id"] == task.id for row in traced),
          "traces are stamped with the task id")

    # A second turn with a message attached puts the same task back to work.
    reopened = tasks.resume(tasks.open_for(space.id).id)
    check(reopened.status == "active", "the next turn picks the paused task back up")


async def test_completion_and_resume(tmp: Path, config: CapriConfig) -> None:
    project = tmp / "complete"
    project.mkdir()
    source = project / "calc.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    test_file = project / "test_calc.py"
    test_file.write_text(
        "import subprocess, sys\n"
        "result = subprocess.run(\n"
        "    [sys.executable, '-c', 'from calc import add; assert add(2, 3) == 5'],\n"
        "    capture_output=True,\n"
        ")\n"
        "sys.exit(result.returncode)\n",
        encoding="utf-8",
    )

    conn = open_shard()
    service = WorkspaceService(conn, CapriConfig())
    space = service.open(str(project))
    tasks = TaskService(conn)
    task = tasks.create(space.id, "Fix the failing test.")

    brain = ScriptedBrain(
        [
            ({"thought": "Read it.", "tool": "fs.read"}, {"path": str(source)}),
            (
                {"thought": "Fix the sign.", "tool": "fs.patch"},
                {"path": str(source), "edits": [{"old": "a - b", "new": "a + b"}]},
            ),
            (
                {"thought": "Prove it.", "tool": "shell.run"},
                {"command": f'"{sys.executable}" "{test_file}"', "cwd": str(project)},
            ),
            ({"thought": "It passes.", "tool": "done", "final": "Fixed; the test passes."}, None),
        ]
    )
    harness = Harness()
    runner = make_runner(
        conn=conn, config=config, project=project, brain=brain,
        harness=harness, tasks=tasks, task=task,
    )
    await runner.run("trn_done", "Fix the failing test.")

    finished = tasks.get(task.id)
    check(finished.status == "done", "a turn that answers finishes the task")
    check(finished.result == "passed", "the last command decides the result")
    check("Changed 1 file(s)" in finished.summary, "the summary keeps the change")
    check("(ok)" in finished.summary and "test_calc" in finished.summary,
          "the summary keeps the test that passed")
    check(finished.steps == 3, "every tool call is counted")

    changes = conn.execute(
        "SELECT task_id FROM file_backups WHERE turn_id = 'trn_done'"
    ).fetchall()
    check(bool(changes) and changes[0]["task_id"] == task.id,
          "the backup belongs to the task")

    done = harness.of_type("agent.done")[-1]
    check(done["text"] == "Fixed; the test passes.", "the reply is the model's")

    # Continue: the summary and the last observations, never the transcript.
    observations = tuple(tasks.observations(task.id))
    check(len(observations) == 3, "the task remembers its last three observations")
    resumed_task = tasks.resume(task.id)
    before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]

    resume_brain = ScriptedBrain(
        [({"thought": "Nothing left.", "tool": "done", "final": "Already fixed."}, None)]
    )
    resume_harness = Harness()
    resumed = make_runner(
        conn=conn,
        config=config,
        project=project,
        brain=resume_brain,
        harness=resume_harness,
        tasks=tasks,
        task=resumed_task,
        resume=True,
        observations=observations,
        # The daemon keeps one session across turns; a continuation is the same
        # conversation, so the record plus that conversation is what it sees.
        session=runner.session,
    )
    await resumed.run("trn_resume", "Continue the task.")

    history = resume_brain.seen[0]
    check(len(history) >= 3, "a continuation carries the conversation, not just a note")
    check(history[-1]["role"] == "user" and "unfinished" in history[-1]["content"],
          "the continuation note asks for the next step, after the transcript")
    transcript = [str(m["content"]) for m in history[1:-1]]
    check(any("Fix the failing test." in part for part in transcript),
          "the request that started the task stays visible")
    check(any("Fixed; the test passes." in part for part in transcript),
          "her last reply stays visible")
    system = str(history[0]["content"])
    check("What this task has done so far:" in system, "the summary is in the prompt")
    check("Ran:" in system and "test_calc" in system, "the prompt carries the command that ran")
    check("Where it stopped" in system, "the prompt says where it stopped")
    check("trn_done" not in system, "the prompt does not replay the transcript")
    check("Fix the failing test." not in system or "Task:" in system,
          "the earlier request is not replayed as conversation")
    check(len(observations) <= 3 and all(o for o in observations),
          "the observations are the real ones")

    after = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    check(after == before + 2, "a continuation adds only its own two messages")
    check(tasks.get(task.id).status == "done", "the continuation finishes the task again")
    check(resume_harness.of_type("agent.done")[-1]["taskId"] == task.id,
          "the continuation keeps the same task id")


def test_abandon_and_restart(tmp: Path) -> None:
    conn = open_shard()
    service = WorkspaceService(conn, CapriConfig())
    project = tmp / "abandon"
    project.mkdir()
    (project / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
    space = service.open(str(project))
    tasks = TaskService(conn)
    task = tasks.create(space.id, "An experiment")
    session = Session.create(conn)
    session.add_message("trn_x", "user", "An experiment", task_id=task.id)
    session.add_message("trn_x", "tool", "read keep.py", task_id=task.id)
    conn.execute(
        "INSERT INTO file_backups (id, task_id, turn_id, path, before_hash,"
        " after_hash, lines_added, lines_removed, created_at)"
        " VALUES ('bkp_ab', ?, 'trn_x', ?, 'a', 'b', 1, 0, ?)",
        (task.id, str(project / "keep.py"), int(time.time() * 1000)),
    )

    abandoned = tasks.abandon(task.id)
    check(abandoned.status == "abandoned", "abandoning ends the task")
    check(
        conn.execute("SELECT COUNT(*) AS n FROM messages WHERE task_id = ?", (task.id,)).fetchone()["n"]
        == 2,
        "abandoning leaves the messages alone",
    )
    check(
        conn.execute("SELECT COUNT(*) AS n FROM file_backups WHERE task_id = ?", (task.id,)).fetchone()["n"]
        == 1,
        "abandoning leaves the backups alone",
    )
    check((project / "keep.py").read_text(encoding="utf-8") == "KEEP = 1\n",
          "abandoning leaves the files alone")
    check(tasks.open_for(space.id) is None, "the next turn starts a fresh task")
    check(tasks.recent(space.id)[0].status == "abandoned", "the abandoned task stays in history")

    # A restart is a new connection to the same shard.
    for leftover in _CONNS:
        try:
            leftover.close()
        except Exception:
            pass
    _CONNS.clear()
    revived = open_shard()
    again = TaskService(revived)
    restored = again.get(task.id)
    check(restored is not None and restored.status == "abandoned",
          "task history survives a restart")
    check(again.recent(space.id)[0].id == task.id, "recent tasks survive a restart")


def test_session_resume() -> None:
    """A reconnect picks the conversation back up instead of starting blank."""
    conn = open_shard()
    first = Session.create(conn)
    check(Session.resume(conn).episode_id == first.episode_id, "an open episode is resumed")

    first.add_message("trn_1", "user", "hey capri")
    first.add_note("Yeah? I'm here.", "wake_1")
    first.close()
    again = Session.resume(conn)
    check(again is not None and again.episode_id == first.episode_id,
          "a disconnected episode is resumed, not replaced")
    check([row["content"] for row in again.recent_messages(5)] == ["hey capri", "Yeah? I'm here."],
          "the transcript is still there for the panel to rebuild from")
    row = conn.execute("SELECT ended_at FROM episodes WHERE id = ?", (first.episode_id,)).fetchone()
    check(row["ended_at"] is None, "and it is open again, so retention leaves it alone")

    newer = Session.create(conn)
    check(Session.resume(conn).episode_id == newer.episode_id, "a new chat is what the next connect gets")
    check(Session.resume(conn).recent_messages(5) == [], "which starts empty")


def test_tool_rows() -> None:
    """A tool row keeps what the panel needs to rebuild its chip."""
    conn = open_shard()
    session = Session.create(conn)
    session.add_message(
        "trn_tool",
        "tool",
        "[observed] fs.read] ok\nhello",
        tool_name="fs.read",
        tool_args='{"path": "a.txt"}',
        tool_result="hello",
        tool_ok=False,
        tool_tier=2,
        tool_ms=37,
    )
    row = session.recent_messages(5)[-1]
    check(row["tool_name"] == "fs.read", "the tool name is stored")
    check(json.loads(str(row["tool_args"])) == {"path": "a.txt"}, "its arguments are stored")
    check(row["tool_result"] == "hello", "and what it returned")
    check(row["content"] == "[observed] fs.read] ok\nhello", "beside the planner's observation")
    check((row["tool_ok"], row["tool_tier"], row["tool_ms"]) == (0, 2, 37),
          f"and how it went, at what tier, for how long: {row['tool_ok']}")
    columns = {info["name"] for info in conn.execute("PRAGMA table_info(messages)")}
    check({"tool_ok", "tool_tier", "tool_ms"} <= columns, "the shard carries the new columns")
    check(session.message_count() == 1, "the session counts every stored row")
    for index in range(130):
        session.add_message("trn_tool", "user", f"line {index}")
    window = session.recent_messages(50)
    check(len(window) == 50, "the panel's window is bounded")
    check(session.message_count() == 131, "and the count still says how much is held")

    page, more = session.messages_page(int(window[0]["id"]), 50)
    check(len(page) == 50 and more is True, "a page walks back from the oldest row held")
    check(page[-1]["id"] == int(window[0]["id"]) - 1, "and stops just above it")
    check([row["id"] for row in page] == sorted(row["id"] for row in page),
          "a page reads oldest first")
    rest, more = session.messages_page(int(page[0]["id"]), 50)
    check(rest[0]["tool_tier"] == 2 and rest[0]["tool_ms"] == 37,
          "an older page carries the same outcome as the newest window")
    check(len(rest) == 31 and more is False, f"the start of the conversation ends it: {len(rest)}")
    check(rest[-1]["id"] == int(page[0]["id"]) - 1, "with no row repeated between pages")


async def test_history_rpc() -> None:
    """`session.history` pages back from the cursor the panel holds."""
    from capri_core.server.app import CapriServer, RpcError

    conn = open_shard()
    server = CapriServer.__new__(CapriServer)
    session = Session.create(conn)
    for index in range(3):
        session.add_message("trn_h", "user", f"line {index}")
    client = types.SimpleNamespace(session=session)

    window = session.recent_messages(2)
    page = await server._session_history(client, {"before": int(window[0]["id"]), "limit": 2})
    check(len(page["messages"]) == 1 and page["hasMore"] is False,
          f"the page holds what is above the cursor: {len(page['messages'])}")
    check(page["messages"][0]["content"] == "line 0", "and reads oldest first")

    raised = ""
    try:
        await server._session_history(client, {})
    except RpcError as exc:
        raised = exc.code
    check(raised == "invalid_args", f"a page without a cursor is refused: {raised}")


def test_session_note() -> None:
    """A spoken acknowledgement is conversation, not a turn."""
    conn = open_shard()
    session = Session.create(conn)
    session.add_message("trn_1", "user", "hey capri")

    turn = session.add_note("Yeah? I'm here.", "wake_1")
    check(turn == "wake_1", "the shell's id is kept")
    check(session.turns == {}, "a note does not start a turn")
    rows = session.recent_messages(10)
    check([row["role"] for row in rows] == ["user", "assistant"], "the note is an assistant line")
    check(rows[-1]["content"] == "Yeah? I'm here.", "and it is what she said")

    generated = session.add_note("Hm?")
    check(generated.startswith("note_"), f"a note without an id gets one: {generated}")
    check(session.turns == {}, "still no turn")


async def test_session_note_rpc() -> None:
    """The RPC writes through the same path the panel calls."""
    from capri_core.server.app import CapriServer, RpcError

    conn = open_shard()
    server = CapriServer.__new__(CapriServer)
    client = types.SimpleNamespace(session=Session.create(conn))

    result = await server._session_note(client, {"text": "  Hm?  ", "turnId": "wake_9"})
    check(result == {"turnId": "wake_9", "recorded": True}, f"the note is recorded: {result}")
    rows = client.session.recent_messages(5)
    check(rows[-1]["content"] == "Hm?", "the line is stored, whitespace collapsed")
    check(client.session.turns == {}, "no turn is registered")

    raised = ""
    try:
        await server._session_note(client, {"text": "   "})
    except RpcError as exc:
        raised = exc.code
    check(raised == "invalid_args", f"an empty note is refused: {raised}")


# --------------------------------------------------------------------------- main


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-tasks-"))
    try:
        tmp = raw / "project"
        tmp.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")
        config = make_config(tmp)

        print("service")
        test_titles_and_service(tmp)
        print("conversation")
        test_conversation_gate()
        print("session note")
        test_session_note()
        await test_session_note_rpc()
        print("session resume")
        test_session_resume()
        print("tool rows")
        test_tool_rows()
        print("history paging")
        await test_history_rpc()
        print("summary")
        test_summary_and_observations(tmp)
        print("ceiling")
        await test_ceiling_pauses(tmp, config)
        print("completion")
        await test_completion_and_resume(tmp, config)
        print("abandon")
        test_abandon_and_restart(tmp)
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
