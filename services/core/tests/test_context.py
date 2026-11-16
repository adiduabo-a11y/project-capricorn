"""Context tests: observation budgets, ranged reads, grep, compaction, cancel.

No model, no network. The filesystem lives in a temp directory; the shard lives
under a temp ``CAPRI_HOME``. The cancellation test starts real processes and
kills them on purpose.

    uv run python services/core/tests/test_context.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from capri_core.agent import observe
from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import Attachment, ChatResult, Decision, Msg, wire_messages
from capri_core.config import CapriConfig, ObservationCfg, PolicyCfg
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, ToolContext, ToolResult, ToolSpec, builtin_registry
from capri_core.tools.builtin import fs, shell

FAILURES: list[str] = []
_CONNS: list[Any] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def open_test_shard() -> Any:
    conn = db.open_shard("test")
    _CONNS.append(conn)
    return conn


class ScriptedBrain:
    """Plays a fixed plan -> args script. Same shape as test_brain's fake."""

    def __init__(self, script: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> None:
        self.script = script
        self.index = 0
        self.seen: list[list[Msg]] = []

    async def available(self) -> bool:
        return True

    async def decide(self, msgs: list[Msg], specs: Any, persona_pack: str = "kyoko") -> Decision:
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
        return [e for e in self.events if e.get("type") == kind]


def make_runner(
    config: CapriConfig,
    session: Session,
    workspace: Path,
    *,
    brain: Any = None,
    registry: Any = None,
    harness: Harness | None = None,
) -> TurnRunner:
    harness = harness or Harness()
    return TurnRunner(
        config=config,
        conn=session.conn,
        session=session,
        emit=harness.emit,
        confirm=harness.confirm,
        brain=brain,
        registry=registry or builtin_registry(),
        policy=Policy(config.policy, str(workspace)),
        workspace=str(workspace),
    )


def context(workspace: Path) -> ToolContext:
    return ToolContext(conn=None, turn_id="trn_ctx", subject="default", workspace=str(workspace))


# --------------------------------------------------------------------------- read


async def test_ranged_read(tmp: Path) -> None:
    file = tmp / "long.txt"
    file.write_text(
        "\n".join(f"line {number}" for number in range(1, 301)) + "\n", encoding="utf-8"
    )

    result = await fs.fs_read(context(tmp), path=str(file), start_line=100, end_line=120)
    check(result.ok, "ranged read succeeds")
    check("100: line 100" in result.output, "ranged read starts at the requested line")
    check("120: line 120" in result.output, "ranged read ends at the requested line")
    check(
        "line 99" not in result.output and "line 121" not in result.output,
        "ranged read does not bleed",
    )
    check(
        result.data is not None
        and result.data["start_line"] == 100
        and result.data["end_line"] == 120
        and result.data["total_lines"] == 300,
        "read reports the range and the total",
    )
    check(
        result.data is not None and len(result.data["sha256"]) == 64,
        "read hashes the whole file",
    )
    check("continue with start_line=121" in result.output, "read names the next offset")

    empty = tmp / "empty.txt"
    empty.write_text("", encoding="utf-8")
    empty_result = await fs.fs_read(context(tmp), path=str(empty))
    check(
        empty_result.ok and "(empty file)" in empty_result.output,
        "reading an empty file is not an error",
    )

    binary = tmp / "latin1.txt"
    binary.write_bytes(b"caf\xe9\n")
    binary_result = await fs.fs_read(context(tmp), path=str(binary))
    check(
        binary_result.output.startswith("[not valid UTF-8"),
        "a file that is not UTF-8 is flagged at the top",
    )


async def test_oversized_read(tmp: Path) -> None:
    file = tmp / "huge-lines.txt"
    file.write_text(
        "\n".join(f"line {number}" for number in range(1, 1001)) + "\n", encoding="utf-8"
    )
    result = await fs.fs_read(context(tmp), path=str(file))
    check(result.ok, "default read succeeds")
    check("  400: line 400" in result.output, "default read returns 400 lines")
    check("line 401" not in result.output, "default read stops before 401")
    check("continue with start_line=401" in result.output, "oversized read has a usable pointer")
    check(
        result.data is not None and result.data["total_lines"] == 1000,
        "the total line count survives truncation",
    )


# --------------------------------------------------------------------------- grep


async def test_grep(tmp: Path) -> None:
    (tmp / "pkg").mkdir()
    (tmp / "pkg" / "a.py").write_text("needle one\nother\nneedle two\n", encoding="utf-8")
    (tmp / "pkg" / "b.txt").write_text("needle text\n", encoding="utf-8")
    (tmp / "node_modules").mkdir()
    (tmp / "node_modules" / "x.py").write_text("needle hidden\n", encoding="utf-8")
    (tmp / "ignored").mkdir()
    (tmp / "ignored" / "c.py").write_text("needle ignored\n", encoding="utf-8")
    (tmp / ".gitignore").write_text("ignored/\n", encoding="utf-8")

    result = await fs.fs_grep(context(tmp), pattern="needle")
    check(result.ok, "grep succeeds")
    check("a.py:1: needle one" in result.output, "grep returns path:line: text")
    check("a.py:3: needle two" in result.output, "grep finds every match in a file")
    check("b.txt" in result.output, "grep searches text files")
    check("node_modules" not in result.output, "grep skips excluded directories")
    check("ignored" not in result.output, "grep honours .gitignore at the root")

    filtered = await fs.fs_grep(context(tmp), pattern="needle", include="*.py")
    check(
        "a.py" in filtered.output and "b.txt" not in filtered.output,
        "grep respects include globs",
    )

    many = tmp / "many.txt"
    many.write_text("\n".join("needle" for _ in range(200)) + "\n", encoding="utf-8")
    capped = await fs.fs_grep(context(tmp), pattern="needle", path=str(many), max_matches=10)
    check(
        capped.data is not None
        and capped.data["matches"] == 10
        and capped.data["capped"] is True,
        "grep caps matches and says so",
    )
    check(len(capped.output.splitlines()) == 11, "capped grep counts only the matches it shows")

    outside = tmp.parent / "grep-outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.py").write_text("needle outside\n", encoding="utf-8")
    link = tmp / "linkdir"
    created = False
    try:
        if os.name == "nt":
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                text=True,
            )
            created = made.returncode == 0
        else:
            os.symlink(outside, link, target_is_directory=True)
            created = True
    except (OSError, NotImplementedError):
        created = False
    if created:
        linked = await fs.fs_grep(context(tmp), pattern="needle")
        check("secret.py" not in linked.output, "grep does not follow links out of the tree")
    else:
        print("  skip links (cannot create one here)")


# --------------------------------------------------------------------- observation


async def test_observation_budget(tmp: Path) -> None:
    cfg = ObservationCfg()
    check(cfg.file == 8000 and cfg.command == 6000, "observation defaults are per shape")
    check(
        builtin_registry().get("screen.observe").observation == "screen",
        "screen observations declare their shape",
    )

    big = tmp / "big.txt"
    big.write_text("\n".join("x" * 100 for _ in range(400)) + "\n", encoding="utf-8")
    result = await fs.fs_read(context(tmp), path=str(big))
    check(len(result.output) > 20_000, "the raw read is much larger than the budget")
    shown = observe.shape(result.output, fs.READ_SPEC, cfg)
    check(len(shown) <= 8100, "a 40 KB file is shaped to its 8 KB budget")
    check(len(shown) > 7000, "file observations get 8 KB, not the 1200 default")
    check(
        "continue with start_line=" in shown and "chars elided" not in shown,
        "file shaping keeps whole lines and a usable resume pointer",
    )

    wide = tmp / "wide.txt"
    wide.write_text("\n".join("w" * 70 for _ in range(300)) + "\n", encoding="utf-8")
    wide_result = await fs.fs_read(context(tmp), path=str(wide))
    wide_shown = observe.shape(wide_result.output, fs.READ_SPEC, cfg)
    match = re.search(r"continue with start_line=(\d+)", wide_shown)
    check(
        bool(match) and 1 < int(match.group(1)) < 300,
        "a truncated 300-line read points at a middle line",
    )

    command = "FIRST\n" + "m" * 20_000 + "\nLAST"
    shaped = observe.shape(command, shell.RUN_SPEC, cfg)
    check("FIRST" in shaped and "LAST" in shaped, "command output keeps its head and its tail")
    check("chars elided" in shaped, "command elision is marked")
    check(len(shaped) <= 6100, "command output is capped at 6 KB")

    small = observe.shape("fine", fs.READ_SPEC, cfg)
    check(small == "fine", "short observations are untouched")


# ---------------------------------------------------------------------- compaction


async def test_context_guard(config: CapriConfig, tmp: Path) -> None:
    session = Session.create(open_test_shard())
    runner = make_runner(config, session, tmp)
    rules = "RULES " * 200
    messages = [Msg("system", rules), Msg("user", "the request")]
    for _ in range(8):
        messages.append(Msg("user", "[observation from fs.read] ok\n" + "o" * 3000))
    messages.append(Msg("user", "latest"))
    messages.append(Msg("user", "newest"))

    dropped, saved = runner._guard_context(messages, 2000)
    check(dropped > 0 and saved > 0, "compaction triggers over budget")
    check(messages[0]["content"] == rules, "compaction keeps the system prompt")
    check(messages[1]["content"] == "the request", "compaction keeps the first user message")
    check(
        messages[-2]["content"] == "latest" and messages[-1]["content"] == "newest",
        "compaction keeps the newest messages",
    )
    compacted = [m for m in messages[2:-2] if observe.COMPACT_MARK in str(m.get("content"))]
    check(bool(compacted), "old observation bodies become one-line summaries")
    check(observe.estimate_tokens(str(compacted[0]["content"])) < 200, "a summary is small")


async def test_compaction_event(config: CapriConfig, tmp: Path) -> None:
    original = config.agent.context_budget_tokens
    config.agent.context_budget_tokens = 1024
    try:
        big = tmp / "huge.txt"
        big.write_text("\n".join("y" * 100 for _ in range(400)) + "\n", encoding="utf-8")
        session = Session.create(open_test_shard())
        script: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for start in (1, 101, 201, 301):
            step = {"thought": "read", "tool": "fs.read"}
            script.append((step, {"path": str(big), "start_line": start, "end_line": start + 99}))
        script.append(({"thought": "done", "tool": "done", "final": "read it"}, None))

        brain = ScriptedBrain(script)
        harness = Harness()
        runner = make_runner(config, session, tmp, brain=brain, harness=harness)
        await runner.run("trn_compact", "do the thing")

        events = harness.of_type("context.compacted")
        check(len(events) == 1, "the compaction event fires once per turn")
        check(
            bool(events) and events[0]["droppedObservations"] >= 1,
            "the event counts what it dropped",
        )

        last = brain.seen[-1]
        check(last[0]["role"] == "system", "the last planner call still has the system prompt")
        check(last[1]["content"] == "do the thing", "the last planner call still has the request")
        check(
            any(observe.COMPACT_MARK in str(m.get("content")) for m in last[2:]),
            "later planner calls see the compacted history",
        )
    finally:
        config.agent.context_budget_tokens = original


# --------------------------------------------------------------------- attachments


def test_wire_messages() -> None:
    plain = Msg("user", "hello")
    wire = wire_messages([plain])
    check(wire[0] == {"role": "user", "content": "hello"}, "a plain message stays a plain dict")

    attachment = Attachment(data_url="data:image/png;base64,U0VDUkVU", label="frame")
    message = Msg("user", "a frame", attachments=[attachment])
    wire = wire_messages([message])
    blocks = wire[0]["content"]
    check(isinstance(blocks, list), "an attachment expands into content blocks")
    check(blocks[0] == {"type": "text", "text": "a frame"}, "the text block comes first")
    check(
        blocks[1]["type"] == "image_url"
        and blocks[1]["image_url"]["url"] == attachment.data_url,
        "the image block carries the data URL",
    )
    check(
        "U0VDUkVU" not in json.dumps(dict(message)),
        "attachment bytes never enter the message dict",
    )


CAPTURE_SPEC = ToolSpec(
    name="test.capture",
    description="Return one transient frame.",
    params={"type": "object", "properties": {}, "additionalProperties": False},
    tier=0,
)


async def capture(ctx: ToolContext, **kwargs: Any) -> ToolResult:
    return ToolResult(
        ok=True,
        output="captured a frame",
        attachments=[Attachment(data_url="data:image/png;base64,U0VDUkVU", label="frame")],
    )


async def test_attachment_not_persisted(config: CapriConfig, tmp: Path) -> None:
    conn = open_test_shard()
    session = Session.create(conn)
    registry = builtin_registry()
    registry.register(CAPTURE_SPEC, capture)
    brain = ScriptedBrain(
        [
            ({"thought": "look", "tool": "test.capture"}, {}),
            ({"thought": "done", "tool": "done", "final": "seen"}, None),
        ]
    )
    harness = Harness()
    runner = make_runner(config, session, tmp, brain=brain, registry=registry, harness=harness)
    await runner.run("trn_attach", "capture the screen")

    second = brain.seen[1]
    attached = [m for m in second if getattr(m, "attachments", ())]
    check(len(attached) == 1, "the observation message carries the attachment")
    check(
        attached and attached[0].attachments[0].label == "frame",
        "the attachment keeps its label",
    )

    rows = conn.execute("SELECT content FROM messages WHERE turn_id = 'trn_attach'").fetchall()
    check(bool(rows), "the turn persisted messages")
    check(
        all("U0VDUkVU" not in str(row["content"]) for row in rows),
        "persisted messages never receive attachment bytes",
    )
    traces = conn.execute("SELECT payload FROM traces WHERE turn_id = 'trn_attach'").fetchall()
    check(
        all("U0VDUkVU" not in str(row["payload"] or "") for row in traces),
        "traces never receive attachment bytes",
    )
    check("U0VDUkVU" not in json.dumps(harness.events), "events never receive attachment bytes")
    conn.close()


# --------------------------------------------------------------------- cancellation

PARENT_SCRIPT = """\
import subprocess
import sys
import time
from pathlib import Path

out = Path(sys.argv[1])
(out / "started.txt").write_text("1", encoding="utf-8")
subprocess.Popen([sys.executable, str(out / "grandchild.py"), str(out)])
time.sleep(30)
"""

GRANDCHILD_SCRIPT = """\
import sys
import time
from pathlib import Path

time.sleep(2.5)
Path(sys.argv[1], "survived.txt").write_text("1", encoding="utf-8")
"""


async def test_cancel_kills_tree(tmp: Path) -> None:
    work = tmp / "cancel"
    work.mkdir()
    (work / "parent.py").write_text(PARENT_SCRIPT, encoding="utf-8")
    (work / "grandchild.py").write_text(GRANDCHILD_SCRIPT, encoding="utf-8")
    command = f'"{sys.executable}" "{work / "parent.py"}" "{work}"'

    ctx = context(tmp)
    task = asyncio.create_task(shell.shell_run(ctx, command=command))
    started = work / "started.txt"
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.1)
    check(started.exists(), "the cancelled command actually started")

    await asyncio.sleep(1.0)
    ctx.cancel.set()
    result = await asyncio.wait_for(task, timeout=20)
    check(
        result.data is not None and result.data.get("cancelled") is True,
        "cancellation comes back as a structured result",
    )

    await asyncio.sleep(3.5)
    check(
        not (work / "survived.txt").exists(),
        "the grandchild process dies with the tree",
    )


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-context-"))
    try:
        tmp = raw / "workspace"
        tmp.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")
        config = CapriConfig()
        config.policy = PolicyCfg(
            allow_paths=[str(tmp)],
            deny_paths=["**/.ssh/**", "**/id_rsa", "C:/Windows/**"],
        )

        print("read: range")
        await test_ranged_read(tmp)
        print("read: default cap")
        await test_oversized_read(tmp)
        print("grep")
        await test_grep(tmp)
        print("observation budget")
        await test_observation_budget(tmp)
        print("compaction")
        await test_context_guard(config, tmp)
        print("compaction event")
        await test_compaction_event(config, tmp)
        print("attachments")
        test_wire_messages()
        await test_attachment_not_persisted(config, tmp)
        print("cancel")
        await test_cancel_kills_tree(tmp)
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
