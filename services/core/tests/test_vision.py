"""Visual context and companion awareness.

One half asks whether a redacted screenshot reaches the model, and whether it
can ever reach the shard. The other asks what an ambient line is allowed to
know about the work.

    uv run python services/core/tests/test_vision.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision, Msg, wire_messages
from capri_core.companion import ActivitySnapshot, Companion
from capri_core.config import CapriConfig, PolicyCfg
from capri_core.models.vision import asks_to_look, model_sees_images, vision_enabled
from capri_core.screen.service import ScreenService
from capri_core.screen.uia import Element, Observation
from capri_core.screen.watch import Activity
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, ToolContext, builtin_registry
from capri_core.tools.base import ScreenState
from capri_core.tools.builtin.screen import screen_act, screen_observe

FAILURES: list[str] = []
_CONNS: list[Any] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def observation(**kwargs: Any) -> Observation:
    elements = [
        Element("e1", "Window", "Notepad", "", (0, 0, 800, 600), handle=4242),
        Element("e2", "Edit", "text area", "editor", (10, 40, 780, 560)),
        Element(
            "e3",
            "Edit",
            "password",
            "passwordBox",
            (20, 520, 400, 550),
            password=True,
            value="hunter2",
        ),
    ]
    defaults: dict[str, Any] = {
        "title": kwargs.pop("title", "Untitled - Notepad"),
        "process": "notepad.exe",
        "elements": elements,
        "digest": "a" * 40,
        "window": (0, 0, 800, 600),
    }
    defaults.update(kwargs)
    return Observation(**defaults)


class FakeBackend:
    def __init__(self, frames: list[Observation] | None = None) -> None:
        self.frames = list(frames or [observation()])
        self.captures: list[tuple] = []
        # Successive screenshots, so a test can change the pixels under an
        # unchanged tree (which is what a canvas does).
        self.shots: list[bytes] = []
        self.shot = b"png-bytes"
        self.ocr_text = "sign in to the dashboard"

    def available(self) -> bool:
        return True

    def reason(self) -> str:
        return ""

    def observe(self, max_elements: int = 300, max_depth: int = 8) -> Observation:
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]

    def windows(self):
        return []

    def capture(self, window, redact=(), marks=()):
        self.captures.append((window, list(redact), list(marks)))
        if self.shots:
            return self.shots.pop(0)
        return self.shot

    def ocr(self, png, *, language="en"):
        return [{"text": self.ocr_text, "bounds": [1, 2, 3, 4], "confidence": 1.0}]

    def ocr_reason(self):
        return ""

    def foreground(self):
        from capri_core.screen.uia import WindowInfo

        return WindowInfo(4242, "Untitled - Notepad", "notepad.exe")


class FakeInput:
    def click(self, *a, **k): ...
    def move(self, *a, **k): ...
    def scroll(self, *a, **k): ...
    def drag(self, *a, **k): ...
    def type_text(self, *a, **k): ...
    def key(self, *a, **k): ...
    def focus_window(self, *a, **k): ...


class FakeActivity(Activity):
    def __init__(self) -> None:
        self.tick = 0
        self.marked = 0

    def mark(self) -> None:
        self.marked = self.tick

    def moved(self) -> bool:
        return self.tick != self.marked

    def panic(self) -> bool:
        return False

    def wait(self, seconds: float) -> None:
        return None


def make_screen(config: CapriConfig, project: Path) -> tuple[ScreenService, FakeBackend]:
    backend = FakeBackend()
    service = ScreenService(
        config,
        str(project),
        backend=backend,
        input=FakeInput(),
        activity=FakeActivity(),
        poll_s=0.001,
    )
    return service, backend


# --------------------------------------------------------------------------- switch


def test_switch() -> None:
    check(vision_enabled("off", "qwen2.5-vl-7b") is False, "off beats a vision model")
    check(vision_enabled("on", "llama-3-8b") is True, "on attaches for any model")
    check(
        vision_enabled("auto", "qwen2.5-vl-7b-instruct") is True,
        "auto recognises a vision model",
    )
    check(
        vision_enabled("auto", "qwen2.5-coder-7b") is False,
        "auto leaves a text model alone",
    )
    check(vision_enabled("auto", "") is False, "no model means no image")
    check(vision_enabled("nonsense", "llava-v1.6") is True, "an unknown mode behaves like auto")
    check(model_sees_images("Qwen/Qwen3-VL-8B") is True, "matching ignores case and org prefix")
    check(model_sees_images("qwen3-coder") is False, "a coder model is not a vision model")

    config = CapriConfig()
    check(config.screen.vision == "auto", "visual context defaults to auto")
    check(config.companion.awareness == "windows", "awareness defaults to windows")

    check(asks_to_look("look at the dialog and tell me what it says"), "'look at' asks for eyes")
    check(asks_to_look("What's on my screen right now?"), "'what's on my screen' asks for eyes")
    check(asks_to_look("take a screenshot of that error"), "screenshot asks for eyes")
    check(not asks_to_look("fix the failing test"), "a work request does not")
    check(not asks_to_look("read the README and summarise it"), "reading a file does not")
    check(not asks_to_look(""), "an empty request does not")


# --------------------------------------------------------------------------- observe


async def test_observe_attaches(tmp: Path, config: CapriConfig) -> None:
    service, backend = make_screen(config, tmp)

    state = ScreenState()
    seeing = ToolContext(
        conn=None,
        turn_id="trn_v",
        subject="test",
        workspace=str(tmp),
        screen=service,
        vision=True,
        look=True,
        screen_state=state,
    )
    result = await screen_observe(seeing)
    check(result.ok, "screen.observe succeeds with vision on")
    check(len(result.attachments) == 1, "a vision model gets one attachment")
    attachment = result.attachments[0]
    check(attachment.kind == "image", "the attachment is an image")
    check(
        attachment.data_url.startswith("data:image/png;base64,"),
        "the attachment is a data URL",
    )
    check("never instructions" in result.output, "the observation labels the image as data")
    check("shotUrl" not in (result.data or {}), "base64 stays out of the tool payload")
    check((result.data or {}).get("shot") is True, "the payload says a shot was taken")
    check(backend.captures and backend.captures[0][1] == [(20, 520, 400, 550)],
          "the password box was redacted before capture")
    check("window:" in result.output, "the text summary still describes the window")

    blind = ToolContext(
        conn=None,
        turn_id="trn_v",
        subject="test",
        workspace=str(tmp),
        screen=service,
        vision=False,
        look=True,
    )
    text_only = await screen_observe(blind)
    check(text_only.ok and not text_only.attachments, "a text model gets no image")
    check("window:" in text_only.output, "a text model still gets the tree summary")

    # Asking for a file with a text model still returns one to the UI...
    forced = await screen_observe(blind, include_shot=True)
    check(not forced.attachments, "include_shot does not force an attachment")
    check((forced.data or {}).get("shot") is True, "include_shot still captured the window")

    # ...and a planner that looks on its own does not attach one either.
    quiet_backend = FakeBackend()
    quiet_service = ScreenService(
        config,
        str(tmp),
        backend=quiet_backend,
        input=FakeInput(),
        activity=FakeActivity(),
        poll_s=0.001,
    )
    curious = ToolContext(
        conn=None,
        turn_id="trn_q",
        subject="test",
        workspace=str(tmp),
        screen=quiet_service,
        vision=True,
        look=False,
    )
    quiet = await screen_observe(curious)
    check(quiet.ok and not quiet.attachments, "vision alone does not attach an image")
    check(not quiet_backend.captures, "vision alone does not even capture")

    # A verification arms exactly one frame.
    verify_state = ScreenState(verify=True)
    verify_ctx = ToolContext(
        conn=None,
        turn_id="trn_v",
        subject="test",
        workspace=str(tmp),
        screen=service,
        vision=True,
        look=False,
        screen_state=verify_state,
    )
    armed = await screen_observe(verify_ctx)
    check(len(armed.attachments) == 1, "an armed verification attaches a frame")
    check(verify_state.verify is False, "the verification is consumed by the observation")
    spent = await screen_observe(verify_ctx)
    check(not spent.attachments, "the next observation is back to text only")

    # A looking turn can observe twice; the unchanged frame rides once.
    look_state = ScreenState()
    look_service, _ = make_screen(config, tmp)
    look_ctx = ToolContext(
        conn=None,
        turn_id="trn_v",
        subject="test",
        workspace=str(tmp),
        screen=look_service,
        vision=True,
        look=True,
        screen_state=look_state,
    )
    first_look = await screen_observe(look_ctx)
    check(len(first_look.attachments) == 1, "the first look attaches the frame")
    second_look = await screen_observe(look_ctx)
    check(not second_look.attachments, "the same unchanged frame is not attached twice")
    check("same frame" in second_look.output, "the observation says why it was skipped")
    check(len(look_state.frames) == 1, "the turn remembers the frame it sent")

    # A different picture is a different frame, even with a new tree.
    moved_service, moved_backend = make_screen(config, tmp)
    moved_backend.frames = [observation(digest="c" * 40)]
    moved_backend.shots = [b"moved-png"]
    moved_ctx = ToolContext(
        conn=None,
        turn_id="trn_v",
        subject="test",
        workspace=str(tmp),
        screen=moved_service,
        vision=True,
        look=True,
        screen_state=look_state,
    )
    moved = await screen_observe(moved_ctx)
    check(len(moved.attachments) == 1, "a changed frame is attached")

    # The same tree with new pixels is a new frame. A canvas never changes its
    # accessibility digest, and that is exactly what vision is for.
    canvas_service, canvas_backend = make_screen(config, tmp)
    canvas_backend.shots = [b"canvas-one", b"canvas-two"]
    canvas_state = ScreenState()
    canvas_ctx = ToolContext(
        conn=None,
        turn_id="trn_v",
        subject="test",
        workspace=str(tmp),
        screen=canvas_service,
        vision=True,
        look=True,
        screen_state=canvas_state,
    )
    canvas_first = await screen_observe(canvas_ctx)
    check(len(canvas_first.attachments) == 1, "the first canvas frame is attached")
    canvas_second = await screen_observe(canvas_ctx)
    check(
        len(canvas_second.attachments) == 1,
        "a new canvas frame with the same tree is attached",
    )
    check(
        canvas_second.attachments[0].data_url != canvas_first.attachments[0].data_url,
        "the second canvas frame is the new image",
    )
    check(len(canvas_state.frames) == 2, "both pixel hashes are remembered")

    # ...and the image is an image block for the model, not text.
    wire = wire_messages([Msg("user", "seen", attachments=list(result.attachments))])
    blocks = wire[0]["content"]
    check(isinstance(blocks, list) and blocks[1]["type"] == "image_url",
          "the attachment becomes an image_url block on the wire")
    check(wire_messages([Msg("user", "plain")])[0] == {"role": "user", "content": "plain"},
          "a message without attachments serialises as before")


class ActingService:
    """Enough of ScreenService.act to test how the tool arms a verification."""

    def act(self, action: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "action": action,
            "changed": True,
            "changedBy": "uia",
            "attempts": 1,
            "title": "Untitled - Notepad",
            "process": "notepad.exe",
            "hash": "b" * 40,
            "summary": "window: Untitled - Notepad",
        }


async def test_act_arms_verification(tmp: Path) -> None:
    state = ScreenState()
    ctx = ToolContext(
        conn=None,
        turn_id="trn_act",
        subject="test",
        workspace=str(tmp),
        screen=ActingService(),
        vision=True,
        look=False,
        screen_state=state,
    )
    await screen_act(ctx, action="click", element_id="e1", expect={"changed": True})
    check(state.verify is True, "an action with an expectation arms a verification")

    state.verify = False
    await screen_act(ctx, action="click", element_id="e1")
    check(state.verify is False, "an action without an expectation does not")


class ScriptedBrain:
    def __init__(self, script: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> None:
        self.script = script
        self.index = 0
        self.seen: list[list[Msg]] = []

    async def available(self) -> bool:
        return True

    async def decide(self, msgs, specs, persona_pack: str = "kyoko") -> Decision:
        self.seen.append(list(msgs))
        step, _ = self.script[min(self.index, len(self.script) - 1)]
        return Decision(**step, usage=ChatResult(text="", tokens_in=1, tokens_out=1))

    async def args(self, spec, msgs, feedback: str | None = None):
        _, args = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return dict(args or {}), ChatResult(text="", tokens_in=1, tokens_out=1)


async def test_quiet_turn(tmp: Path, config: CapriConfig) -> None:
    """A work request that happens to look at the screen attaches nothing."""
    project = tmp / "quiet"
    project.mkdir()
    service, _ = make_screen(config, project)
    config.screen.vision = "on"

    conn = db.open_shard("test")
    _CONNS.append(conn)
    brain = ScriptedBrain(
        [
            ({"thought": "Look.", "tool": "screen.observe"}, {}),
            ({"thought": "Done.", "tool": "done", "final": "Notepad."}, None),
        ]
    )

    async def emit(event: dict[str, Any]) -> None: ...

    async def confirm(request: dict[str, Any]) -> bool:
        return True

    policy = Policy(config.policy, str(project))
    policy.set_workspace(str(project), active=True, trusted=True)
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=Session.create(conn),
        emit=emit,
        confirm=confirm,
        brain=brain,
        registry=builtin_registry(),
        policy=policy,
        workspace=str(project),
        screen=service,
    )
    await runner.run("trn_quiet", "Fix the failing test in calc.py.")
    check(runner.look is False, "a work request does not ask for eyes")
    attached = [msg for msg in brain.seen[1] if getattr(msg, "attachments", ())]
    check(not attached, "so the observation carries no image")

    looking = ScriptedBrain(
        [
            ({"thought": "Look.", "tool": "screen.observe"}, {}),
            ({"thought": "Done.", "tool": "done", "final": "Notepad."}, None),
        ]
    )
    runner2 = TurnRunner(
        config=config,
        conn=conn,
        session=Session.create(conn),
        emit=emit,
        confirm=confirm,
        brain=looking,
        registry=builtin_registry(),
        policy=policy,
        workspace=str(project),
        screen=service,
    )
    await runner2.run("trn_look", "Look at the window and tell me what it says.")
    check(runner2.look is True, "a looking request does ask for eyes")
    attached = [msg for msg in looking.seen[1] if getattr(msg, "attachments", ())]
    check(len(attached) == 1, "so the observation carries the image")


async def test_nothing_persisted(tmp: Path, config: CapriConfig) -> None:
    project = tmp / "persist"
    project.mkdir()
    service, _ = make_screen(config, project)
    config.screen.vision = "on"

    conn = db.open_shard("test")
    _CONNS.append(conn)
    session = Session.create(conn)
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def confirm(request: dict[str, Any]) -> bool:
        return True

    brain = ScriptedBrain(
        [
            ({"thought": "Look at the window.", "tool": "screen.observe"}, {}),
            ({"thought": "That is all.", "tool": "done", "final": "Notepad, with a password box."}, None),
        ]
    )
    policy = Policy(config.policy, str(project))
    policy.set_workspace(str(project), active=True, trusted=True)
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=session,
        emit=emit,
        confirm=confirm,
        brain=brain,
        registry=builtin_registry(),
        policy=policy,
        workspace=str(project),
        screen=service,
    )
    await runner.run("trn_vision", "Look at the window and tell me what you see.")
    check(runner.vision is True, "the turn resolved vision from the model")

    second_call = brain.seen[1]
    attached = [msg for msg in second_call if getattr(msg, "attachments", ())]
    check(len(attached) == 1, "the loop handed the image to the next planner call")
    check(attached[0].attachments[0].data_url.startswith("data:image/png"),
          "the image reached the model as a data URL")

    def leaks(text: str) -> bool:
        return "base64" in text or "iVBOR" in text or "png-bytes" in text

    messages = conn.execute("SELECT content FROM messages").fetchall()
    check(bool(messages) and not any(leaks(row["content"] or "") for row in messages),
          "no screenshot bytes reached the messages table")
    traces = conn.execute("SELECT payload FROM traces").fetchall()
    check(not any(leaks(row["payload"] or "") for row in traces),
          "no screenshot bytes reached the traces table")
    artifacts = conn.execute("SELECT content FROM artifacts").fetchall()
    check(not any(leaks(row["content"] or "") for row in artifacts),
          "no screenshot bytes reached the artifacts table")
    check(any("screenshot attached" in (row["content"] or "") for row in messages),
          "the observation text records that an image was attached")
    check(any(event.get("type") == "agent.tool" and "screenshot attached" in event.get("output", "")
              for event in events),
          "the tool event carries the text, not the image")


# --------------------------------------------------------------------------- awareness


class AmbientScreen:
    def __init__(self) -> None:
        self.paused = False
        self.enabled = True
        self.ocr_text = "sign in to the dashboard"

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "paused": self.paused}

    def input_idle_s(self) -> float:
        return 0.0

    def foreground(self) -> dict[str, Any]:
        return {
            "id": 1,
            "process": "Code.exe",
            "title": "app.py - capri - Visual Studio Code",
        }

    def ambient_text(self, max_chars: int = 400) -> str:
        return self.ocr_text[:max_chars]


class Brain:
    def __init__(self, reply: str = "The test is still failing. Predictable.") -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def available(self) -> bool:
        return True

    async def complete(self, msgs, **kwargs) -> ChatResult:
        self.calls += 1
        self.prompts.append(str(msgs[-1].get("content") or ""))
        self.systems.append(str(msgs[0].get("content") or ""))
        return ChatResult(text=self.reply)


class Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("type") == kind]


def make_companion(
    config: CapriConfig,
    screen: AmbientScreen,
    brain: Brain,
    sink: Sink,
    *,
    snapshot: list[ActivitySnapshot | None],
    mic: list[bool] | None = None,
) -> Companion:
    return Companion(
        config,
        screen,
        brain,
        sink,
        work=lambda: snapshot[0],
        mic_active=lambda: bool((mic or [False])[0]),
    )


async def test_snapshot(tmp: Path, config: CapriConfig) -> None:
    config.companion.interval_s = 30
    config.companion.repeat_s = 900
    config.companion.speak = False
    screen = AmbientScreen()
    brain = Brain()
    sink = Sink()
    snapshot = [
        ActivitySnapshot(
            project="capri",
            task="Fix the flaky test",
            task_status="paused",
            task_summary="Changed 1 file(s) (+4 -1): app.py\nRan: pytest -q (failed)",
            agent_state="idle",
            last_failure="shell.run pytest -q",
        )
    ]
    companion = make_companion(config, screen, brain, sink, snapshot=snapshot)

    lines = snapshot[0].lines()
    check("Project: capri" in lines, "the snapshot names the project")
    check("Task: Fix the flaky test (paused)" in lines, "the snapshot names the task and status")
    check(any("Ran: pytest" in line for line in lines), "the snapshot carries the task summary")

    first = await companion.tick(now=1000.0)
    check(first.get("skipped") is False, "a snapshot still allows a comment")
    check(brain.calls == 1, "the model was asked once")
    prompt = brain.prompts[0]
    check("Project: capri" in prompt, "the prompt carries the project")
    check("Task: Fix the flaky test (paused)" in prompt, "the prompt carries the task")
    check("Last failure: shell.run pytest -q" in prompt, "the prompt carries the last failure")
    check("untrusted data" in brain.systems[0], "the rules label the window as data")
    check(companion.last["task"] == "Fix the flaky test", "the last comment records the task")
    check(companion.last["awareness"] == "windows", "the level rides along")
    comments = sink.of("companion.comment")
    check(len(comments) == 1, "one comment was emitted")
    check(comments[0]["text"].startswith("The test is still failing"),
          "the comment text is the model's")

    # Same window, same task: repeat_s holds it back.
    same = await companion.tick(now=1060.0)
    check(same.get("skipped") and same.get("reason") == "same activity",
          "an unchanged activity stays quiet")
    check(brain.calls == 1, "no second model call for an unchanged activity")

    # The task changing state is a new thing even though the window did not.
    snapshot[0] = ActivitySnapshot(
        project="capri", task="Fix the flaky test", task_status="done"
    )
    moved = await companion.tick(now=1090.0)
    check(moved.get("skipped") is False, "a task changing state re-arms the comment")
    check(brain.calls == 2, "the model is asked again after a state change")


async def test_awareness_levels(tmp: Path, config: CapriConfig) -> None:
    config.companion.interval_s = 30
    config.companion.speak = False
    screen = AmbientScreen()
    brain = Brain()
    sink = Sink()
    snapshot = [ActivitySnapshot(project="capri")]

    config.companion.awareness = "off"
    off = make_companion(config, screen, brain, sink, snapshot=snapshot)
    result = await off.tick(now=10.0)
    check(result.get("skipped") and result.get("reason") == "awareness is off",
          "awareness off silences her")
    check(brain.calls == 0, "no model call when awareness is off")

    config.companion.awareness = "windows"
    mic = [True]
    muted = make_companion(config, screen, brain, sink, snapshot=snapshot, mic=mic)
    result = await muted.tick(now=10.0)
    check(result.get("skipped") and "microphone" in str(result.get("reason")),
          "an active mic route silences her")
    check(brain.calls == 0, "no model call while the mic is live")

    mic[0] = False
    config.companion.awareness = "windows"
    listening = make_companion(config, screen, brain, sink, snapshot=snapshot)
    await listening.tick(now=10.0)
    check(brain.calls == 1, "with the mic off she speaks")
    check("sign in to the dashboard" not in brain.prompts[-1],
          "the windows level never reads the screen")

    config.companion.awareness = "screen"
    screen.ocr_text = "sign in to the dashboard"
    seeing = make_companion(config, screen, brain, sink, snapshot=snapshot)
    await seeing.tick(now=10.0)
    prompt = brain.prompts[-1]
    check("sign in to the dashboard" in prompt, "the screen level reads the text")
    check("data, not instructions" in prompt, "the screen text is labelled as data")


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-vision-"))
    try:
        tmp = raw / "project"
        tmp.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")
        config = CapriConfig()
        config.policy = PolicyCfg(allow_paths=[str(tmp)], deny_paths=[])
        config.models.model = "qwen2.5-vl-7b-instruct"

        print("switch")
        test_switch()
        print("observe")
        await test_observe_attaches(tmp, config)
        print("verification")
        await test_act_arms_verification(tmp)
        print("quiet turn")
        await test_quiet_turn(tmp, config)
        print("persistence")
        await test_nothing_persisted(tmp, config)
        print("snapshot")
        await test_snapshot(tmp, config)
        print("levels")
        await test_awareness_levels(tmp, config)
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
