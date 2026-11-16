"""Golden multi-step task runner.

Each JSON task provides fixtures, a planner script, and assertions. The runner
uses the real TurnRunner, built-in tools, policy gate and SQLite shard; only the
planner is scripted, so an eval needs no model or network.

    uv run python evals/run.py
    uv run python evals/run.py --task append-and-verify
"""

from __future__ import annotations

import argparse
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
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, builtin_registry

TASKS = Path(__file__).parent / "tasks"
USAGE = ChatResult(text="", model="eval", tokens_in=10, tokens_out=5)


class TaskBrain:
    """Plays the task's script in order; no heuristic branching."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.cursor = 0

    async def available(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def decide(self, msgs: list[Any], specs: list[Any], persona_pack: str = "kyoko") -> Decision:
        entry = self.script[min(self.cursor, len(self.script) - 1)]
        return Decision(
            thought=str(entry.get("thought") or ""),
            tool=str(entry.get("tool") or "done"),
            final=str(entry.get("final") or ""),
            usage=USAGE,
        )

    async def args(
        self, spec: Any, msgs: list[Any], feedback: str | None = None
    ) -> tuple[dict[str, Any], ChatResult]:
        entry = self.script[min(self.cursor, len(self.script) - 1)]
        self.cursor += 1
        return dict(entry.get("args") or {}), USAGE


class Harness:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.confirms: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def confirm(self, request: dict[str, Any]) -> bool:
        self.confirms.append(request)
        return True


def fail(message: str) -> None:
    raise AssertionError(message)


def tool_events(harness: Harness) -> list[dict[str, Any]]:
    return [event for event in harness.events if event.get("type") == "agent.tool"]


def done_text(harness: Harness) -> str:
    events = [event for event in harness.events if event.get("type") == "agent.done"]
    return str(events[-1].get("text") or "") if events else ""


def check_files(workspace: Path, expected: dict[str, Any]) -> None:
    for relative, want in expected.items():
        target = (workspace / relative).resolve()
        if workspace.resolve() not in target.parents and target != workspace.resolve():
            fail(f"fixture path escapes workspace: {relative}")
        if not target.exists():
            fail(f"expected file was not written: {relative}")
        text = target.read_text(encoding="utf-8")
        if isinstance(want, str):
            if text != want:
                fail(f"{relative} contents differ: {text!r}")
        elif isinstance(want, dict) and "contains" in want:
            if str(want["contains"]) not in text:
                fail(f"{relative} does not contain {want['contains']!r}")
        else:
            fail(f"bad expectation for {relative}")


async def run_task(task: dict[str, Any], *, keep: bool) -> dict[str, Any]:
    raw = Path(tempfile.mkdtemp(prefix=f"capri-eval-{task['id']}-"))
    workspace = raw / "workspace"
    workspace.mkdir()
    os.environ["CAPRI_HOME"] = str(raw / "home")
    conn = None
    try:
        for relative, content in (task.get("fixtures") or {}).items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")

        config = CapriConfig()
        config.agent.max_steps = max(len(task["script"]) + 2, 8)
        config.memory.extract_after_turn = False
        config.voice.enabled = False
        config.policy = PolicyCfg(
            workspace=str(workspace),
            allow_paths=[str(workspace)],
            deny_paths=["**/secret.txt", "C:/Windows/**"],
        )

        conn = db.open_shard("eval")
        session = Session.create(conn, "eval")
        harness = Harness()
        runner = TurnRunner(
            config=config,
            conn=conn,
            session=session,
            emit=harness.emit,
            confirm=harness.confirm,
            brain=TaskBrain(task["script"]),
            registry=builtin_registry(),
            policy=Policy(config.policy, str(workspace)),
            workspace=str(workspace),
        )
        await runner.run("trn_eval", str(task["prompt"]))
        session.close()

        expected = task.get("expect") or {}
        actual_tools = tool_events(harness)
        want_tools = expected.get("tools") or []
        if len(actual_tools) != len(want_tools):
            fail(f"expected {len(want_tools)} tools, got {len(actual_tools)}")
        for index, (actual, wanted) in enumerate(zip(actual_tools, want_tools)):
            if actual.get("name") != wanted.get("name"):
                fail(f"tool {index}: expected {wanted.get('name')}, got {actual.get('name')}")
            if bool(actual.get("ok")) is not bool(wanted.get("ok", True)):
                fail(
                    f"tool {index} ({actual.get('name')}) ok={actual.get('ok')}: "
                    f"{actual.get('output') or ''}"
                )
        final = done_text(harness)
        for phrase in expected.get("finalContains") or []:
            if str(phrase) not in final:
                fail(f"final reply is missing {phrase!r}: {final!r}")
        if "confirms" in expected and len(harness.confirms) != int(expected["confirms"]):
            fail(f"expected {expected['confirms']} confirmations, got {len(harness.confirms)}")
        check_files(workspace, expected.get("files") or {})
        return {
            "id": task["id"],
            "ok": True,
            "tools": len(actual_tools),
            "confirms": len(harness.confirms),
            "final": final,
        }
    finally:
        if conn is not None:
            conn.close()
        if keep:
            print(f"  .... kept {raw}")
        else:
            shutil.rmtree(raw, ignore_errors=True)


def load_tasks(only: str | None) -> list[dict[str, Any]]:
    found = []
    for path in sorted(TASKS.glob("*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        if only and task.get("id") != only:
            continue
        if not task.get("id") or not task.get("prompt") or not task.get("script"):
            raise ValueError(f"{path} needs id, prompt and script")
        found.append(task)
    return found


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run Capri's golden tasks")
    parser.add_argument("--task", help="run one task id")
    parser.add_argument("--keep", action="store_true", help="keep each temp workspace")
    args = parser.parse_args()

    tasks = load_tasks(args.task)
    if not tasks:
        print("no matching tasks")
        return 2
    failures = 0
    for task in tasks:
        try:
            result = await run_task(task, keep=args.keep)
        except Exception as exc:
            failures += 1
            print(f"FAIL {task['id']}: {exc}")
        else:
            print(
                f"ok   {result['id']}  {result['tools']} tools, "
                f"{result['confirms']} confirmations"
            )
    print()
    if failures:
        print(f"FAILED: {failures} of {len(tasks)} task(s)")
        return 1
    print(f"all {len(tasks)} task(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
