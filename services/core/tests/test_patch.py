"""The workbench: exact-match patching, backups, undo, artifacts, policy.

No model and no network beyond a local child process. Files live in a temp
project; the shard lives under a temp ``CAPRI_HOME``.

    uv run python services/core/tests/test_patch.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision, Msg
from capri_core.config import CapriConfig, PolicyCfg
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, ToolContext, builtin_registry
from capri_core.tools.builtin import fs, patch as patch_tool, shell
from capri_core.work import patch as engine
from capri_core.work import store as work_store
from capri_core.workspace.service import WorkspaceService

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


def make_config(root: Path, **policy: Any) -> CapriConfig:
    config = CapriConfig()
    config.policy = PolicyCfg(allow_paths=[str(root)], deny_paths=[], **policy)
    return config


def context(workspace: Path, conn: Any = None) -> ToolContext:
    return ToolContext(
        conn=conn,
        turn_id="trn_patch",
        subject="test",
        workspace=str(workspace),
        task_id="",
        workspace_id="",
    )


# --------------------------------------------------------------------------- matching


def test_matching(tmp: Path) -> None:
    text = "alpha\nbeta\ngamma\n"
    after, hunks, added, removed = engine.apply_edits(
        text, [engine.Edit("beta", "BETA")]
    )
    check(after == "alpha\nBETA\ngamma\n", "a single edit replaces the exact text")
    check((hunks, added, removed) == (1, 1, 1), "hunk and line counts are reported")

    after, hunks, _, _ = engine.apply_edits(
        text,
        [engine.Edit("alpha", "ALPHA"), engine.Edit("gamma", "GAMMA")],
    )
    check(
        after == "ALPHA\nbeta\nGAMMA\n" and hunks == 2,
        "multiple edits apply together",
    )

    code = ""
    try:
        engine.apply_edits(text, [engine.Edit("delta", "DELTA")])
    except engine.PatchError as exc:
        code = exc.code
    check(code == "no_match", "a missing old string is refused")

    code = ""
    try:
        engine.apply_edits("x\nx\n", [engine.Edit("x", "y", count=1)])
    except engine.PatchError as exc:
        code = exc.code
    check(code == "no_match", "an ambiguous edit is refused when count is 1")

    after, _, _, _ = engine.apply_edits("x\nx\n", [engine.Edit("x", "y", count=2)])
    check(after == "y\ny\n", "count=2 replaces both occurrences")

    code = ""
    try:
        engine.apply_edits(text, [engine.Edit("alpha", "a"), engine.Edit("nope", "n")])
    except engine.PatchError as exc:
        code = exc.code
    check(code == "no_match", "one bad edit refuses the whole set")

    code = ""
    try:
        engine.apply_edits(text, [engine.Edit("", "x")])
    except engine.PatchError as exc:
        code = exc.code
    check(code == "invalid_args", "an empty old string is refused")

    code = ""
    try:
        engine.apply_edits(text, [engine.Edit("alpha", "a", count=0)])
    except engine.PatchError as exc:
        code = exc.code
    check(code == "invalid_args", "count=0 is invalid")


def test_near_miss() -> None:
    text = "def parse(value):\n    return value\n\n\ndef render(node):\n    return node\n"
    try:
        engine.apply_edits(text, [engine.Edit("def parse(val):\n    return value", "x")])
    except engine.PatchError as exc:
        check("Closest regions" in exc.diagnostics, "a miss returns near-miss regions")
        check("def parse(value):" in exc.diagnostics, "the closest line is shown")
        check(
            re.search(r"\s+\d+: def parse", exc.diagnostics) is not None,
            "regions carry line numbers",
        )
        return
    check(False, "a near miss should have been refused")


def test_line_endings_and_bom(tmp: Path) -> None:
    target = tmp / "windows.py"
    original = "\ufeffline one\r\nline two\r\nline three\r\n"
    target.write_bytes(original.encode("utf-8"))

    raw = target.read_bytes()
    text = engine.decode_file(raw)
    check(text.startswith("\ufeff"), "the BOM survives decoding as a character")

    # The model saw the file through fs.read, which renders LF-ish text; give
    # it an LF-style old string on purpose.
    after, _, added, removed = engine.apply_edits(
        text, [engine.Edit("line two\n", "line 2\n")]
    )
    engine.write_atomic(target, engine.encode_file(after))
    written = target.read_bytes()

    check(written.startswith(b"\xef\xbb\xbf"), "the BOM is written back")
    check(b"\r\n" in written, "CRLF line endings are preserved")
    check(written.count(b"\r\n") == 3, "no line ending was doubled or converted")
    check(written.decode("utf-8") == "\ufeffline one\r\nline 2\r\nline three\r\n", "only the edit changed")
    check((added, removed) == (1, 1), "the line delta ignores the newline convention")

    binary = tmp / "blob.bin"
    binary.write_bytes(b"PK\x03\x04\x00\x00")
    code = ""
    try:
        engine.decode_file(binary.read_bytes())
    except engine.PatchError as exc:
        code = exc.code
    check(code == "binary", "a NUL-containing file is refused")


def test_atomicity(tmp: Path) -> None:
    target = tmp / "locked.txt"
    target.write_text("original\n", encoding="utf-8")

    real_replace = os.replace
    calls = {"n": 0}

    def flaky(source: str, destination: str) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("simulated editor lock")
        real_replace(source, destination)

    os.replace = flaky
    try:
        engine.write_atomic(target, b"new\n", attempts=5, delay=0.01)
    finally:
        os.replace = real_replace
    check(target.read_text(encoding="utf-8") == "new\n", "a transient lock is retried")
    check(calls["n"] == 3, "the write retries exactly as often as needed")

    target.write_text("keep me\n", encoding="utf-8")
    calls["n"] = 0

    def always_locked(source: str, destination: str) -> None:
        calls["n"] += 1
        raise PermissionError("still locked")

    os.replace = always_locked
    raised = False
    try:
        engine.write_atomic(target, b"nope\n", attempts=3, delay=0.01)
    except PermissionError:
        raised = True
    finally:
        os.replace = real_replace
    check(raised, "a permanent lock raises instead of pretending to write")
    check(calls["n"] == 3, "the retry count is bounded")
    check(
        target.read_text(encoding="utf-8") == "keep me\n",
        "the original file is untouched after a failed write",
    )


# --------------------------------------------------------------------------- backups


def test_backup_spill_and_prune(tmp: Path) -> None:
    conn = open_test_shard()
    big = "x" * (600 * 1024) + "\n"
    backup_id = work_store.record_backup(
        conn,
        path=str(tmp / "big.txt"),
        before_text=big,
        before_hash=engine.sha256_text(big),
        after_hash="a" * 64,
        turn_id="trn_spill",
    )
    row = conn.execute("SELECT * FROM file_backups WHERE id = ?", (backup_id,)).fetchone()
    check(row["before_text"] is None, "a large file does not go inline")
    check(bool(row["before_blob"]), "a large file spills to disk")
    check(work_store.backup_text(conn, backup_id) == big, "the spilled backup round-trips exactly")

    path = str(tmp / "many.txt")
    for index in range(25):
        work_store.record_backup(
            conn,
            path=path,
            before_text=f"v{index}\n",
            before_hash=engine.sha256_text(f"v{index}\n"),
            after_hash=engine.sha256_text(f"v{index + 1}\n"),
        )
    kept = conn.execute(
        "SELECT COUNT(*) AS n FROM file_backups WHERE path = ?", (path,)
    ).fetchone()["n"]
    check(kept <= work_store.MAX_BACKUPS_PER_FILE, "the per-file cap is enforced as rows arrive")

    old = conn.execute(
        "SELECT id FROM file_backups WHERE path = ? ORDER BY created_at ASC LIMIT 1", (path,)
    ).fetchone()
    conn.execute(
        "UPDATE file_backups SET created_at = ? WHERE id = ?",
        (int((time.time() - 40 * 86_400) * 1000), old["id"]),
    )
    removed = work_store.prune(conn, days=30)
    check(removed >= 1, "age pruning removes old backups")
    gone = conn.execute(
        "SELECT id FROM file_backups WHERE id = ?", (old["id"],)
    ).fetchone()
    check(gone is None, "the aged-out row is gone")


# --------------------------------------------------------------------------- tools


async def test_patch_tool(tmp: Path, config: CapriConfig) -> None:
    conn = open_test_shard()
    target = tmp / "app.py"
    target.write_text("def main():\n    print('hi')\n", encoding="utf-8")
    ctx = context(tmp, conn)

    read = await fs.fs_read(ctx, path=str(target))
    check(read.ok, "fs.read records the file for the turn")
    check(str(target) in ctx.reads, "the read hash is registered")

    result = await patch_tool.fs_patch(
        ctx,
        path=str(target),
        edits=[{"old": "print('hi')", "new": "print('hello')"}],
    )
    check(result.ok, "fs.patch applies an edit")
    check(
        target.read_text(encoding="utf-8") == "def main():\n    print('hello')\n",
        "the file on disk changed",
    )
    data = result.data or {}
    check(data.get("lines_added") == 1, "the result reports lines added")
    check(bool(data.get("backup_id")), "the result names the backup")
    check(bool((data.get("artifact") or {}).get("id")), "the patch produced a diff artifact")
    check("undo available" in result.output, "the output offers undo")
    check(ctx.reads[str(target)] == data.get("sha256"), "the read hash moves with the write")

    artifact = work_store.get_artifact(conn, (data.get("artifact") or {}).get("id"))
    check(bool(artifact) and "+    print('hello')" in str(artifact["content"]), "the artifact holds the diff")

    missing = await patch_tool.fs_patch(
        ctx, path=str(tmp / "nope.py"), edits=[{"old": "a", "new": "b"}]
    )
    check(not missing.ok and "no such file" in missing.output, "fs.patch does not create files")

    miss = await patch_tool.fs_patch(
        ctx, path=str(target), edits=[{"old": "print('goodbye')", "new": "x"}]
    )
    check(not miss.ok and "Closest regions" in miss.output, "a miss is refused with diagnostics")

    before = target.read_text(encoding="utf-8")
    partial = await patch_tool.fs_patch(
        ctx,
        path=str(target),
        edits=[
            {"old": "def main():", "new": "def entry():"},
            {"old": "does not exist", "new": "x"},
        ],
    )
    check(not partial.ok, "a partial edit set is refused")
    check(
        target.read_text(encoding="utf-8") == before,
        "no edit is written when any edit fails",
    )

    undo_ctx = context(tmp, conn)
    restored = await patch_tool.fs_undo(undo_ctx, backup_id=str(data.get("backup_id")))
    check(restored.ok, "fs.undo restores from a backup")
    check(
        target.read_text(encoding="utf-8") == "def main():\n    print('hi')\n",
        "the restored file matches the pre-patch text",
    )

    # fs.write only creates.
    existing = await fs.fs_write(ctx, path=str(target), content="clobber")
    check(not existing.ok and "already exists" in existing.output, "fs.write refuses to overwrite")
    fresh = await fs.fs_write(ctx, path=str(tmp / "new.py"), content="print('new')\n")
    check(fresh.ok and (tmp / "new.py").exists(), "fs.write creates a new file")
    check(bool((fresh.data or {}).get("created")), "the result marks the file as created")

    # The server-side staleness guard: an external save between read and patch.
    await fs.fs_read(ctx, path=str(target))
    target.write_text("def main():\n    print('edited elsewhere')\n", encoding="utf-8")
    stale = await patch_tool.fs_patch(
        ctx, path=str(target), edits=[{"old": "edited elsewhere", "new": "x"}]
    )
    check(not stale.ok and "changed since you read it" in stale.output, "a stale patch is refused")
    check((stale.data or {}).get("stale") is True, "the refusal is marked stale")


async def test_undo_conflict(tmp: Path) -> None:
    conn = open_test_shard()
    target = tmp / "conflict.txt"
    target.write_text("one\n", encoding="utf-8")
    ctx = context(tmp, conn)
    await fs.fs_read(ctx, path=str(target))
    result = await patch_tool.fs_patch(
        ctx, path=str(target), edits=[{"old": "one", "new": "two"}]
    )
    check(result.ok, "the first patch lands")
    backup_id = str((result.data or {}).get("backup_id"))

    target.write_text("three\n", encoding="utf-8")
    undo = await patch_tool.fs_undo(ctx, backup_id=backup_id)
    check(not undo.ok, "undo refuses after a later edit")
    check("changed after that edit" in undo.output, "the refusal explains why")
    check("+one" in undo.output or "-three" in undo.output, "the refusal offers a diff instead")
    check(target.read_text(encoding="utf-8") == "three\n", "the later edit is not stomped")


def test_patch_policy(tmp: Path, config: CapriConfig) -> None:
    workspace = tmp / "policy"
    workspace.mkdir()
    (workspace / "a.py").write_text("x\n", encoding="utf-8")

    policy = Policy(config.policy, str(workspace))
    policy.set_workspace(str(workspace), active=True, trusted=True)
    check(
        policy.check(patch_tool.PATCH_SPEC, {"path": "a.py", "edits": []}).verdict == "allow",
        "a patch inside a trusted workspace runs without a prompt",
    )
    policy.set_workspace(str(workspace), active=True, trusted=False)
    check(
        policy.check(patch_tool.PATCH_SPEC, {"path": "a.py", "edits": []}).verdict
        == "confirm",
        "a read-only restored workspace confirms a patch",
    )
    policy.set_workspace(str(workspace), active=True, trusted=True)
    outside = tmp / "outside.py"
    outside.write_text("x\n", encoding="utf-8")
    check(
        policy.check(patch_tool.PATCH_SPEC, {"path": str(outside), "edits": []}).verdict
        == "confirm",
        "a patch outside the workspace confirms",
    )
    check(
        policy.check(patch_tool.UNDO_SPEC, {"backup_id": "bk_1"}).verdict == "confirm",
        "undo always confirms",
    )

    careful = make_config(tmp, workspace_edits="confirm")
    confirm_policy = Policy(careful.policy, str(workspace))
    confirm_policy.set_workspace(str(workspace), active=True, trusted=True)
    check(
        confirm_policy.check(patch_tool.PATCH_SPEC, {"path": "a.py", "edits": []}).verdict
        == "confirm",
        "workspace_edits=confirm asks per patch",
    )


async def test_shell_artifact(tmp: Path, config: CapriConfig) -> None:
    conn = open_test_shard()
    ctx = context(tmp, conn)
    result = await shell.shell_run(
        ctx, command=f'"{sys.executable}" -c "print(\'suite ok\')"'
    )
    check(result.ok and "suite ok" in result.output, "shell.run returns the output")
    data = result.data or {}
    check(data.get("exit") == 0, "the result carries the exit code")
    check(bool(data.get("artifact_id")), "the full output became an artifact")
    artifact = work_store.get_artifact(conn, str(data.get("artifact_id")))
    check(
        bool(artifact) and "suite ok" in str(artifact["content"]),
        "the command artifact holds the transcript",
    )
    check(data.get("detected") is False, "an undetected command keeps the short timeout")


# --------------------------------------------------------------------------- loop


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
        return [e for e in self.events if e.get("type") == kind]


async def test_loop(tmp: Path, config: CapriConfig) -> None:
    project = tmp / "loop"
    project.mkdir()
    (project / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    test_file = project / "test_calc.py"
    test_file.write_text(
        "import subprocess, sys\n"
        "\n"
        "result = subprocess.run(\n"
        "    [sys.executable, '-c', 'from calc import add; assert add(2, 3) == 5'],\n"
        "    capture_output=True,\n"
        ")\n"
        "sys.exit(result.returncode)\n",
        encoding="utf-8",
    )

    conn = open_test_shard()
    session = Session.create(conn)
    script = [
        ({"thought": "What is this project?", "tool": "project.inspect"}, {}),
        ({"thought": "Find the function.", "tool": "fs.grep"}, {"pattern": "def add"}),
        ({"thought": "Read it.", "tool": "fs.read"}, {"path": str(project / "calc.py")}),
        (
            {"thought": "Fix the sign.", "tool": "fs.patch"},
            {"path": str(project / "calc.py"), "edits": [{"old": "a - b", "new": "a + b"}]},
        ),
        (
            {"thought": "Prove it.", "tool": "shell.run"},
            {"command": f'"{sys.executable}" "{test_file}"', "cwd": str(project)},
        ),
        ({"thought": "It passes.", "tool": "done", "final": "Fixed; the test passes."}, None),
    ]
    harness = Harness()
    registry = builtin_registry()
    policy = Policy(config.policy, str(project))
    policy.set_workspace(str(project), active=True, trusted=True)
    service = WorkspaceService(conn, config)
    service.open(str(project))
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=session,
        emit=harness.emit,
        confirm=harness.confirm,
        brain=ScriptedBrain(script),
        registry=registry,
        policy=policy,
        workspace=str(project),
        project=service,
    )
    await runner.run("trn_loop", "Fix the failing test.")

    tools = harness.of_type("agent.tool")
    names = [event["name"] for event in tools]
    check(
        names == ["project.inspect", "fs.grep", "fs.read", "fs.patch", "shell.run"],
        f"the workbench flow ran in order: {names}",
    )
    check(all(event["ok"] for event in tools), "every step succeeded")
    check(
        (project / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n",
        "the source was fixed",
    )
    patch_event = next(event for event in tools if event["name"] == "fs.patch")
    check(
        "patched" in patch_event["output"],
        "the patch observation names the file, not the whole diff",
    )
    artifacts = harness.of_type("agent.artifact")
    kinds = {event["kind"] for event in artifacts}
    check({"diff", "command"} <= kinds, f"artifacts were emitted: {sorted(kinds)}")
    check(
        all("id" in event for event in artifacts),
        "artifact events carry a reference, not megabytes",
    )
    check(len(harness.confirms) == 1, "only the shell step needed a confirmation")
    if harness.confirms:
        check(harness.confirms[0]["tool"] == "shell.run", "the confirmation names shell.run")

    done = harness.of_type("agent.done")
    check(bool(done) and "passes" in done[-1]["text"], "the turn reported the real result")

    backups = conn.execute("SELECT path FROM file_backups WHERE turn_id = ?", ("trn_loop",)).fetchall()
    check(bool(backups), "the patch left a backup for undo")


async def test_repeat_memory(tmp: Path, config: CapriConfig) -> None:
    """A repeat is refused, but verification after an edit is not a repeat."""
    project = tmp / "repeat"
    project.mkdir()
    source = project / "calc.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    conn = open_test_shard()
    session = Session.create(conn)
    edit = {"old": "a - b", "new": "a + b"}
    script = [
        ({"thought": "Read it.", "tool": "fs.read"}, {"path": str(source)}),
        ({"thought": "Read it again.", "tool": "fs.read"}, {"path": str(source)}),
        (
            {"thought": "Fix the sign.", "tool": "fs.patch"},
            {"path": str(source), "edits": [edit]},
        ),
        (
            {"thought": "Send the same edit again.", "tool": "fs.patch"},
            {"path": str(source), "edits": [edit]},
        ),
        ({"thought": "Verify the file.", "tool": "fs.read"}, {"path": str(source)}),
        ({"thought": "Done.", "tool": "done", "final": "Checked it."}, None),
    ]
    harness = Harness()
    policy = Policy(config.policy, str(project))
    policy.set_workspace(str(project), active=True, trusted=True)
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=session,
        emit=harness.emit,
        confirm=harness.confirm,
        brain=ScriptedBrain(script),
        registry=builtin_registry(),
        policy=policy,
        workspace=str(project),
    )
    await runner.run("trn_repeat", "Fix the sign and check it.")

    tools = harness.of_type("agent.tool")
    names = [event["name"] for event in tools]
    check(
        names == ["fs.read", "fs.read", "fs.patch", "fs.patch", "fs.read"],
        f"every attempted call was recorded: {names}",
    )
    oks = [bool(event["ok"]) for event in tools]
    check(
        oks == [True, False, True, False, True],
        f"repeats are refused, the edit and the verify are not: {oks}",
    )
    check(
        "nothing in between" in tools[1]["output"],
        "an identical read with no change in between is refused",
    )
    check(
        "changed something" in tools[3]["output"],
        "replaying a call that already changed the file is refused",
    )
    check(
        "a + b" in tools[4]["output"],
        "the read after the patch sees the edit",
    )


# --------------------------------------------------------------------------- main


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-patch-"))
    try:
        tmp = raw / "project"
        tmp.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")
        config = make_config(tmp)

        print("matching")
        test_matching(tmp)
        test_near_miss()
        print("encoding")
        test_line_endings_and_bom(tmp)
        print("atomicity")
        test_atomicity(tmp)
        print("backups")
        test_backup_spill_and_prune(tmp)
        print("tools")
        await test_patch_tool(tmp, config)
        await test_undo_conflict(tmp)
        print("policy")
        test_patch_policy(tmp, config)
        print("shell")
        await test_shell_artifact(tmp, config)
        print("loop")
        await test_loop(tmp, config)
        print("repeat")
        await test_repeat_memory(tmp, config)
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
