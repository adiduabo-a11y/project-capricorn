"""End-to-end smoke test for the daemon.

Starts nothing itself — point it at a running `python -m capri_core` with the
scripted planner so the approval check is deterministic:

    CAPRI_FAKE_BRAIN=1 uv run python -m capri_core --port 8787
    uv run python services/core/tests/ws_smoke.py

Point both at the same throwaway CAPRI_HOME to keep the smoke out of the real
shard. A home without voice weights skips the speak/barge-in pair (reported as
a skip, not a pass). CAPRI_SMOKE_URL overrides the default port. The project
section opens a temporary folder and closes it again, restoring whatever was
already open.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from capri_core import paths

URL = os.environ.get("CAPRI_SMOKE_URL", "ws://127.0.0.1:8787")
FAILURES: list[str] = []
# Events seen while waiting for a response. workspace.changed is broadcast, so
# it arrives on the same socket as the reply and has to be kept, not dropped.
EVENTS: list[dict] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


async def rpc(ws, request_id: str, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("id") == request_id:
                return msg
            if msg.get("type"):
                EVENTS.append(msg)
    except ConnectionClosed:
        return {"id": request_id, "ok": False, "error": {"code": "closed"}}


def events_of(kind: str) -> list[dict]:
    return [event for event in EVENTS if event.get("type") == kind]


def workspace_event(reason: str) -> dict | None:
    for event in reversed(events_of("workspace.changed")):
        if event.get("reason") == reason:
            return event
    return None


async def drain_turn(ws, turn_id: str) -> dict | None:
    """Approve any card and wait for this turn's agent.done."""
    while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
        kind = event.get("type")
        if kind == "agent.confirm":
            # Answer without going through rpc(): a nested wait could swallow
            # this turn's done event.
            await ws.send(
                json.dumps(
                    {
                        "id": f"pc_{event['confirmId']}",
                        "method": "confirm.respond",
                        "params": {"confirmId": event["confirmId"], "approved": True},
                    }
                )
            )
            continue
        if kind == "agent.done" and event.get("turnId") == turn_id:
            return event
        if kind:
            EVENTS.append(event)


async def check_project(ws) -> None:
    """project.* over the wire, plus the event and the turn context."""
    root = Path(tempfile.mkdtemp(prefix="capri-smoke-project-"))
    try:
        project = root / "demo"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (project / "package.json").write_text(
            json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8"
        )

        before = await rpc(ws, "p1", "project.current")
        previous = (before.get("result") or {}).get("workspace")
        check(bool(before.get("ok")), "project.current answers before one is chosen")
        if previous:
            # Somebody is already working in a folder. Do not leave them in a
            # throwaway directory: restore it at the end of this section.
            print(f"  .... restoring {previous['root']} when this section ends")

        opened = await rpc(ws, "p2", "project.open", {"path": str(project)})
        workspace = (opened.get("result") or {}).get("workspace") or {}
        check(
            bool(opened.get("ok")) and bool(workspace.get("root")),
            "project.open opens a folder",
        )
        opened_event = workspace_event("open")
        check(
            opened_event is not None
            and (opened_event.get("workspace") or {}).get("id") == workspace.get("id"),
            "workspace.changed (open) names the new project",
        )

        current = await rpc(ws, "p3", "project.current")
        check(
            ((current.get("result") or {}).get("workspace") or {}).get("id")
            == workspace.get("id"),
            "project.current returns the open project",
        )

        listing = await rpc(ws, "p4", "project.tree", {"depth": 2})
        paths = {entry["path"] for entry in (listing.get("result") or {}).get("entries", [])}
        check("src/main.py" in paths, "project.tree lists the project files")

        inspect = await rpc(ws, "p5", "project.inspect")
        commands = {
            item["command"] for item in (inspect.get("result") or {}).get("commands", [])
        }
        check("npm test" in commands, "project.inspect detects the test command")

        # A turn with a project open must carry its workspace id, and the
        # scripted brain's two calls must still land inside it.
        sent = await rpc(ws, "p6", "turn.send", {"text": "hello from the project"})
        project_turn = (sent.get("result") or {}).get("turnId", "")
        done = await drain_turn(ws, project_turn)
        check(done is not None, "a turn runs with a project open")
        check(
            (done or {}).get("workspaceId") == workspace.get("id"),
            "the turn carries the workspace id",
        )

        changes = await rpc(ws, "p7", "work.changes")
        check(isinstance((changes.get("result") or {}).get("items"), list), "work.changes")

        current = await rpc(ws, "t1", "task.current")
        current_task = (current.get("result") or {}).get("task") or {}
        check(bool(current_task.get("id")), "a work turn with a project open creates a task")
        check(
            (done or {}).get("taskId") == current_task.get("id"),
            "the turn and the task agree on the id",
        )
        check(
            current_task.get("status") in ("done", "paused"),
            f"the task recorded a status: {current_task.get('status')}",
        )
        check(bool(current_task.get("summary")), "the task kept a summary")
        check(bool(events_of("task.changed")), "task.changed is broadcast")

        recent = await rpc(ws, "t2", "task.recent", {"limit": 5})
        items = (recent.get("result") or {}).get("items", [])
        check(any(item["id"] == current_task.get("id") for item in items),
              "task.recent lists the task")

        # Continue starts a new turn on the same task, from the summary rather
        # than the transcript.
        continued = await rpc(ws, "t3", "task.continue", {"taskId": current_task.get("id")})
        check(bool(continued.get("ok")), f"task.continue starts a turn: {continued.get('error')}")
        resumed_turn = (continued.get("result") or {}).get("turnId", "")
        resumed_done = await drain_turn(ws, resumed_turn)
        check(resumed_done is not None, "the continuation turn finished")
        check(
            (resumed_done or {}).get("taskId") == current_task.get("id"),
            "the continuation kept the same task",
        )

        # A new chat must not inherit the task that was open: work asked for
        # there starts a fresh task, with none of the old summary or
        # observations behind it.
        await rpc(ws, "t5", "session.new")
        fresh = await rpc(ws, "t6", "turn.send", {"text": "start the new job"})
        fresh_turn = (fresh.get("result") or {}).get("turnId", "")
        fresh_task = (fresh.get("result") or {}).get("taskId", "")
        fresh_done = await drain_turn(ws, fresh_turn)
        check(fresh_done is not None, "a new chat runs a work turn")
        check(
            bool(fresh_task) and fresh_task != current_task.get("id"),
            f"a new chat starts detached from the open task: {fresh_task}",
        )
        check(
            (fresh_done or {}).get("taskId") == fresh_task,
            "the detached turn carries its own task",
        )

        # The turn's coroutine may still be cleaning up after agent.done.
        abandoned = await rpc(ws, "t4", "task.abandon", {"taskId": current_task.get("id")})
        for _ in range(20):
            if abandoned.get("ok"):
                break
            await asyncio.sleep(0.15)
            abandoned = await rpc(
                ws, "t4", "task.abandon", {"taskId": current_task.get("id")}
            )
        check(
            ((abandoned.get("result") or {}).get("task") or {}).get("status")
            == "abandoned",
            "task.abandon ends the task",
        )

        closed = await rpc(ws, "p8", "project.close")
        check((closed.get("result") or {}).get("closed") is True, "project.close closes it")
        closed_event = workspace_event("close")
        check(
            closed_event is not None and closed_event.get("workspace") is None,
            "workspace.changed (close) clears the project",
        )
        after = await rpc(ws, "p9", "project.current")
        check(
            bool(after.get("ok")) and (after.get("result") or {}).get("workspace") is None,
            "the project is gone after close",
        )

        missing = await rpc(ws, "p10", "project.open", {"path": str(root / "missing")})
        check(not missing.get("ok"), "project.open refuses a missing folder")
        no_tree = await rpc(ws, "p11", "project.tree")
        check(not no_tree.get("ok"), "project.tree refuses with nothing open")
        no_inspect = await rpc(ws, "p12", "project.inspect")
        check(not no_inspect.get("ok"), "project.inspect refuses with nothing open")

        # Last, so the refusals above still see an empty slot.
        if previous:
            restored = await rpc(ws, "p13", "project.open", {"path": previous["root"]})
            check(
                ((restored.get("result") or {}).get("workspace") or {}).get("id")
                == previous["id"],
                "the project that was already open is restored",
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def main() -> int:
    token = paths.token(create=False)
    if not token:
        print("no token at", paths.token_path())
        return 2

    async with connect(URL, max_size=8 * 1024 * 1024) as ws:
        hello = await rpc(ws, "r0", "hello", {"token": token})
        check(bool(hello.get("ok")), "hello accepted")
        if not hello.get("ok"):
            print(hello)
            return 1

        result = hello["result"]
        print(f"  .... version={result['version']} session={result['sessionId']}")
        print(f"  .... vector backend={result['vectorBackend']}")

        opened = events_of("session.opened")
        check(
            bool(opened) and opened[-1].get("reason") == "connect",
            "a connecting shell is not a new chat",
        )

        ping = await rpc(ws, "r1", "ping")
        check(ping["result"]["pong"] is True, "ping/pong")

        sent = await rpc(ws, "r2", "turn.send", {"text": "hello capri"})
        turn_id = sent["result"]["turnId"]
        check(bool(turn_id), "turn accepted")

        deltas: list[str] = []
        done = None
        states: list[str] = []
        confirms = 0
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if ev.get("type") == "agent.state":
                states.append(ev["state"])
            elif ev.get("type") == "agent.confirm":
                confirms += 1
                await rpc(
                    ws,
                    f"c{confirms}",
                    "confirm.respond",
                    {"confirmId": ev["confirmId"], "approved": True},
                )
            elif ev.get("type") == "agent.delta" and ev.get("turnId") == turn_id:
                deltas.append(ev["text"])
            elif ev.get("type") == "agent.done" and ev.get("turnId") == turn_id:
                done = ev
                break

        check(len(deltas) > 1, f"streamed {len(deltas)} deltas")
        check(done is not None, "turn completed")
        check(bool((done or {}).get("text", "").strip()), "turn produced a reply")
        check(states[:1] == ["thinking"], f"state order {states}")
        check(confirms == 1, f"asked for approval ({confirms})")
        if done:
            print(f"  .... {done['usage']}")

        traces = await rpc(ws, "r3", "trace.get", {"limit": 5})
        rows = traces["result"]["traces"]
        check(any(r["span"] == "turn" for r in rows), "turn traced")

        cfg = await rpc(ws, "r4", "config.get")
        check(cfg["result"]["server"]["port"] == 8787, "config.get")
        check(
            cfg["result"].get("screen", {}).get("vision") in ("auto", "on", "off"),
            "screen.vision is a switch",
        )
        check(
            cfg["result"].get("companion", {}).get("awareness")
            in ("off", "windows", "screen"),
            "companion.awareness is a level",
        )

        mem = await rpc(ws, "r5", "memory.list")
        check(isinstance(mem["result"]["items"], list), "memory.list")

        rags = await rpc(ws, "r5d", "rag.collections")
        check(isinstance(rags.get("result", {}).get("collections"), list), "rag.collections")

        found = await rpc(ws, "r5e", "rag.search", {"q": "anything at all"})
        check(isinstance(found.get("result", {}).get("items"), list), "rag.search")

        denied = await rpc(ws, "r5f", "rag.ingest", {"paths": ["C:/Windows"]})
        check(not denied.get("ok"), "rag.ingest refuses a denied path")

        tools = await rpc(ws, "r5b", "tools.list")
        names = [t["name"] for t in tools["result"]["tools"]]
        check(
            {
                "fs.read",
                "fs.write",
                "fs.patch",
                "fs.undo",
                "fs.grep",
                "project.inspect",
                "project.tree",
                "shell.run",
                "screen.windows",
                "screen.observe",
                "screen.focus",
                "screen.launch",
                "screen.clipboard",
                "screen.act",
            }
            <= set(names),
            f"tools: {names}",
        )

        screen = await rpc(ws, "r5g", "screen.status")
        check(
            bool(screen.get("ok")) and "enabled" in screen.get("result", {}),
            "screen.status",
        )

        windows = await rpc(ws, "r5h", "screen.windows")
        check(
            bool(windows.get("ok"))
            and isinstance(windows.get("result", {}).get("windows"), list),
            "screen.windows",
        )

        bad_focus = await rpc(ws, "r5i", "screen.focus", {"windowId": -1})
        check(not bad_focus.get("ok"), "screen.focus refuses a bad window id")

        bad_launch = await rpc(
            ws, "r5j", "screen.launch", {"app": "capri-not-allowlisted.exe"}
        )
        check(not bad_launch.get("ok"), "screen.launch refuses an unallowlisted app")

        paused = await rpc(ws, "r5k", "screen.pause", {"paused": True, "reason": "smoke"})
        check(
            paused.get("result", {}).get("paused") is True
            and paused.get("result", {}).get("presence") == "eyes closed",
            "screen.pause closes her eyes",
        )
        resumed = await rpc(ws, "r5l", "screen.pause", {"paused": False})
        check(resumed.get("result", {}).get("presence") == "eyes open", "screen.pause resumes")

        apps = await rpc(ws, "r5q", "screen.apps")
        check(
            bool(apps.get("ok"))
            and isinstance((apps.get("result") or {}).get("apps"), list),
            "screen.apps lists running processes",
        )
        rows = (apps.get("result") or {}).get("apps") or []
        check(
            all(set(row) == {"process", "windows", "allowed"} for row in rows),
            "the app picker never carries window titles",
        )
        check(
            any(row["allowed"] for row in rows) or not rows,
            "allowlisted processes are marked in the picker",
        )
        check(bool(events_of("screen.changed")), "screen.changed is broadcast on pause")

        # The app panel asks for work in a window; the flag has to reach the
        # turn without changing how the turn finishes.
        asked = await rpc(
            ws,
            "r5r",
            "turn.send",
            {"text": "look at this window and tell me what it says", "screen": True},
        )
        screen_turn = (asked.get("result") or {}).get("turnId", "")
        check(bool(screen_turn), "turn.send accepts a screen request")
        check(await drain_turn(ws, screen_turn) is not None, "a screen request finishes its turn")

        voice = await rpc(ws, "r5m", "voice.status")
        check(isinstance(voice.get("result", {}).get("stt"), dict), "voice.status reports STT")
        bad_audio = await rpc(ws, "r5n", "voice.listen", {"audio": "data:audio/wav;base64,"})
        check(not bad_audio.get("ok"), "voice.listen rejects empty audio")

        # A throwaway CAPRI_HOME has no voice weights. That is an environment
        # gap, not a regression, so say so instead of failing the suite.
        if (voice.get("result") or {}).get("available") is False:
            print("  skip voice.speak/barge_in: no voice engine in this home")
        else:
            # voice.speak returns before the stream ends, so a following
            # barge-in is not queued behind every sentence in the dispatcher.
            started = await rpc(
                ws,
                "r5o",
                "voice.speak",
                {"text": "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."},
            )
            check(
                started.get("result", {}).get("started") is True,
                "voice.speak starts a background stream",
            )
            await asyncio.sleep(0.15)
            barge = await rpc(ws, "r5p", "voice.barge_in")
            check(
                barge.get("result", {}).get("stopped") is True,
                "voice.barge_in interrupts an active stream",
            )

        models = await rpc(ws, "r5c", "models.status")
        check("online" in models["result"], "models.status")

        info = await rpc(ws, "r6", "session.info")
        check(len(info["result"]["messages"]) >= 2, "messages persisted")

        await check_project(ws)

        # Only an explicit new chat tells the shell to drop its transcript.
        before = await rpc(ws, "r7", "session.info")
        first_episode = (before.get("result") or {}).get("episodeId")
        new_chat = await rpc(ws, "r8", "session.new")
        check(bool(new_chat.get("ok")), "session.new opens a conversation")
        opened = events_of("session.opened")
        check(
            bool(opened) and opened[-1].get("reason") == "new",
            "a new chat says so on the event",
        )
        check(
            (new_chat.get("result") or {}).get("episodeId") != first_episode,
            "the new chat has a fresh episode",
        )

        # A turn that is still running when the reset lands must be named, so
        # the shell can ignore whatever it emits after the reset.
        midturn = await rpc(ws, "r9", "turn.send", {"text": "hello capri"})
        midturn_id = (midturn.get("result") or {}).get("turnId")
        reset = await rpc(ws, "r10", "session.new")
        check(
            bool(midturn_id)
            and midturn_id in ((reset.get("result") or {}).get("superseded") or []),
            f"the reset names the turn it superseded: {midturn_id}",
        )
        opened = events_of("session.opened")
        check(
            bool(midturn_id) and midturn_id in (opened[-1].get("superseded") or []),
            "the event carries the superseded turn too",
        )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1

    # bad token must be rejected on a fresh connection (server closes it)
    async with connect(URL) as ws2:
        bad = await rpc(ws2, "b1", "hello", {"token": "nope"})
        check(not bad.get("ok"), "bad token rejected")

    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
