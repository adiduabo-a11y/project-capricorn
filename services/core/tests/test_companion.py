"""Ambient commentary checks: privacy, cadence and event shape.

    uv run python services/core/tests/test_companion.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from capri_core.agent.prompt import (
    load_persona,
    looks_like_conversation,
    system_prompt,
)
from capri_core.agent.types import ChatResult
from capri_core.companion import (
    REGISTERS,
    ActivitySnapshot,
    Companion,
    activity_topic,
    app_label,
    load_chatter,
    select_register,
    step_label,
    upkeep_notice,
    _private,
)
from capri_core.config import CapriConfig
from capri_core.paths import packs_dir

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


class Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("type") == kind]


class Screen:
    def __init__(self) -> None:
        self.paused = False
        self.enabled = True
        self.activity = {
            "id": 1,
            "process": "Code.exe",
            "title": "app.py - capri - Visual Studio Code",
        }
        self.input_idle = 0.0

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "paused": self.paused}

    def input_idle_s(self) -> float:
        return self.input_idle

    def foreground(self) -> dict[str, Any]:
        return dict(self.activity)


class Brain:
    def __init__(self, *, available: bool = True) -> None:
        self.healthy = available
        self.calls = 0
        self.msgs: list = []

    async def available(self) -> bool:
        return self.healthy

    async def complete(self, msgs, **kwargs) -> ChatResult:
        self.calls += 1
        self.msgs = list(msgs)
        return ChatResult(text="Back in the editor. The capri repo is still winning.")


class SlowBrain(Brain):
    """Keeps a generation in flight long enough for a second one to start."""

    async def complete(self, msgs, **kwargs) -> ChatResult:
        self.calls += 1
        await asyncio.sleep(0.01)
        return ChatResult(text="Back in the editor. The capri repo is still winning.")


class GatedBrain(Brain):
    """Holds one generation open until the test lets it finish."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, msgs, **kwargs) -> ChatResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return ChatResult(text="Reading the parser now.")


class Voice:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stops = 0

    async def speak(self, turn_id: str, text: str, emit, **kwargs) -> dict[str, Any]:
        self.spoken.append(text)
        return {"segments": 1}

    async def stop(self) -> None:
        self.stops += 1


class SlowVoice(Voice):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def speak(self, turn_id: str, text: str, emit, **kwargs) -> dict[str, Any]:
        self.spoken.append(text)
        self.started.set()
        await self.release.wait()
        return {"segments": 1, "reason": "cancelled"}

    async def stop(self) -> None:
        self.stops += 1
        self.release.set()


class PreemptVoice(Voice):
    """Records whether the line was allowed to interrupt."""

    def __init__(self) -> None:
        super().__init__()
        self.preempt: list[bool] = []

    async def speak(self, turn_id: str, text: str, emit, **kwargs) -> dict[str, Any]:
        self.preempt.append(bool(kwargs.get("preempt", True)))
        return await super().speak(turn_id, text, emit, **kwargs)


def make_companion(
    screen: Screen,
    *,
    brain: Brain | None = None,
    voice: Voice | None = None,
    speak_allowed: list[bool] | None = None,
    active: list[int] | None = None,
    ready: list[bool] | None = None,
    recorded: list[tuple[str, str]] | None = None,
    config: CapriConfig | None = None,
    mic: list[bool] | None = None,
) -> tuple[Companion, Sink]:
    cfg = config or CapriConfig()
    cfg.companion.interval_s = 30
    cfg.companion.max_per_hour = 2
    # Commentary tests are about the window; chatter has its own test.
    cfg.companion.chatter = False
    sink = Sink()
    turns = active if active is not None else [0]
    shell = ready if ready is not None else [True]
    allowed = speak_allowed if speak_allowed is not None else [True]
    companion = Companion(
        cfg,
        screen,
        brain or Brain(),
        sink,
        voice=voice,
        speak_allowed=lambda: allowed[0],
        active_turns=lambda: turns[0],
        ready=lambda: shell[0],
        record=(
            (lambda turn_id, text: recorded.append((turn_id, text)))
            if recorded is not None
            else None
        ),
        mic_active=(lambda: mic[0]) if mic is not None else None,
        clock=lambda: 1000.0,
    )
    return companion, sink


async def test_labels_and_privacy() -> None:
    check(app_label("Code.exe") == "VS Code", "executable names get friendly labels")
    check(app_label("some-editor.exe") == "Some Editor", "unknown apps are readable")
    topic = activity_topic("app.py - capri - Visual Studio Code", "VS Code")
    check("app.py" in topic and "Visual Studio Code" not in topic, f"title suffix is removed: {topic}")
    check(_private({"process": "Capri.exe", "title": "Capri"}), "her own window is ignored")
    check(
        _private({"process": "chrome.exe", "title": "Sign in - Google Chrome"}),
        "sign-in windows are private",
    )
    check(
        not _private({"process": "Code.exe", "title": "app.py - Visual Studio Code"}),
        "editor windows are allowed",
    )


async def test_chatter_prompt_roles() -> None:
    """Her own lines reach the chatter model as hers, not as the person's."""
    brain = Brain()
    companion, _ = make_companion(Screen(), brain=brain)
    companion.context = lambda: [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "evening, read anything good"},
    ]
    await companion._generate(None, None, working=True, notice="")
    roles = [message.get("role") for message in brain.msgs]
    check(roles == ["system", "user", "assistant", "user"], f"transcript keeps its roles: {roles}")
    check(
        "evening, read anything good" in str(brain.msgs[2].get("content")),
        "her earlier line is on the assistant role",
    )
    check(
        all(
            "evening, read anything good" not in str(message.get("content"))
            for message in brain.msgs
            if message.get("role") == "user"
        ),
        "and is not inside the instruction",
    )


async def test_comment_and_speech() -> None:
    screen = Screen()
    voice = Voice()
    recorded: list[tuple[str, str]] = []
    companion, sink = make_companion(screen, voice=voice, recorded=recorded)

    result = await companion.tick(now=1000)
    comments = sink.of("companion.comment")
    check(result.get("skipped") is False, "first observation produces a comment")
    check(comments and comments[0]["text"] == "Back in the editor. The capri repo is still winning.", "model line is emitted as a companion comment")
    check(voice.spoken == [comments[0]["text"]], "the comment is spoken")
    check(recorded and recorded[0][1] == comments[0]["text"], "the comment is recorded for the next turn")
    states = [event.get("state") for event in sink.of("agent.state")]
    check(states == ["speaking", "idle"], f"speech brackets the comment with states: {states}")
    check(comments[0].get("app") == "VS Code", "the comment carries the app label")

    same = await companion.tick(now=1031)
    check(same.get("skipped") is True and same.get("reason") == "same activity", "unchanged activity stays quiet")

    screen.activity = {"id": 2, "process": "chrome.exe", "title": "Build log - Google Chrome"}
    next_result = await companion.tick(now=1062)
    check(next_result.get("skipped") is False, "a new activity gets a new comment")
    check(len(sink.of("companion.comment")) == 2, "comments are one per activity change")

    screen.activity = {"id": 3, "process": "notepad.exe", "title": "notes.txt - Notepad"}
    limited = await companion.tick(now=1093)
    check(limited.get("skipped") is True and limited.get("reason") == "hourly limit reached", "the hourly cap holds")


async def test_mic_route() -> None:
    screen = Screen()
    voice = Voice()
    allowed = [False]
    recorded: list[tuple[str, str]] = []
    companion, sink = make_companion(
        screen, voice=voice, speak_allowed=allowed, recorded=recorded
    )

    first = await companion.tick(now=1000)
    comment = sink.of("companion.comment")
    check(first.get("skipped") is False, "a mic route still allows the text comment")
    check(bool(comment), "the text comment is emitted")
    check(first.get("spoke") is False, "the mic route marks the line text-only")
    check(voice.spoken == [], "nothing is sent to speech while the route is on")
    check(not sink.of("agent.state"), "text-only does not announce a speaking state")
    check(bool(recorded), "a text-only line is still recorded")

    allowed[0] = True
    screen.activity = {"id": 2, "process": "chrome.exe", "title": "Build log - Google Chrome"}
    second = await companion.tick(now=1061)
    check(second.get("spoke") is True, "the route turning off restores speech")
    check(voice.spoken == [second.get("text")], "the next line is spoken")


async def test_mic_route_silences_in_flight() -> None:
    screen = Screen()
    voice = SlowVoice()
    companion, _ = make_companion(screen, voice=voice)
    check(await companion.silence() is False, "silence is a no-op when she is quiet")

    task = asyncio.create_task(companion.tick(now=1000))
    await asyncio.wait_for(voice.started.wait(), timeout=1)
    check(await companion.silence() is True, "silence stops the ambient line in flight")
    result = await task
    check(voice.stops == 1, "the voice stream is asked to stop")
    check(result.get("spoke") is False, "a cancelled line is not reported as spoken")


async def test_guards() -> None:
    screen = Screen()
    active = [0]
    ready = [True]
    companion, sink = make_companion(screen, active=active, ready=ready)

    active[0] = 1
    busy = await companion.tick(now=1000)
    check(busy.get("reason") == "active turn", "a running turn suppresses commentary")
    active[0] = 0
    ready[0] = False
    no_shell = await companion.tick(now=1001)
    check(no_shell.get("reason") == "no shell connected", "no open shell means no commentary")
    ready[0] = True
    screen.paused = True
    paused = await companion.tick(now=1002)
    check(paused.get("reason") == "screen control is paused", "pausing screen control silences her")
    screen.paused = False
    screen.activity = {"id": 4, "process": "chrome.exe", "title": "Password manager - Google Chrome"}
    private = await companion.tick(now=1003)
    check(private.get("reason") == "private window", "private windows are refused")
    screen.activity = {"id": 1, "process": "Code.exe", "title": "app.py - Visual Studio Code"}
    screen.input_idle = 600.0
    stale = await companion.tick(now=1004)
    check(stale.get("reason") == "no recent input", "an idle desk suppresses commentary")
    check(not sink.of("companion.comment"), "guards emit no comments")


async def test_interval_and_fallback() -> None:
    screen = Screen()
    voice = Voice()
    brain = Brain(available=False)
    companion, sink = make_companion(screen, brain=brain, voice=voice)

    first = await companion.tick(now=1000)
    check(first.get("skipped") is False, "an offline brain still allows a fallback comment")
    text = sink.of("companion.comment")[0]["text"]
    check("VS Code" in text or "app.py" in text, f"fallback names the activity: {text}")
    check(brain.calls == 0, "the model is not called when unavailable")

    too_soon = await companion.tick(now=1010)
    check(too_soon.get("reason") == "interval has not elapsed", "comments respect the interval")

    repeat = await companion.tick(now=1901)
    check(repeat.get("skipped") is False, "the same window gets a long-cooldown check-in")


async def test_chatter() -> None:
    screen = Screen()
    brain = Brain()
    voice = Voice()
    companion, sink = make_companion(screen, brain=brain, voice=voice)
    companion.config.companion.chatter = True
    companion.config.companion.chatter_interval_s = 120
    companion.config.companion.max_per_hour = 6

    first = await companion.tick(now=1000)
    check(first.get("kind") == "comment", "the first line is about the window")

    # No foreground window at all: a conversation does not need one.
    screen.activity = {"id": 0, "process": "", "title": ""}
    talk = await companion.tick(now=1300)
    check(talk.get("kind") == "talk", f"a due conversation is her own: {talk.get('kind')}")
    comments = sink.of("companion.comment")
    check(
        len(comments) == 2 and comments[-1].get("kind") == "talk",
        "the talk line is emitted as a conversation",
    )
    check(talk.get("app") == "", "a conversation names no app")
    check(bool(talk.get("spoke")), "a conversation is spoken like a comment")

    later = await companion.tick(now=1310)
    check(later.get("kind") != "talk", "chatter does not fire every tick")

    # Nobody at the keyboard, no conversation.
    companion._next_talk_at = 1300
    screen.input_idle = 600.0
    away = await companion.tick(now=1400)
    check(away.get("reason") == "no recent input", "an empty desk keeps her quiet")

    quiet_screen = Screen()
    quiet, quiet_sink = make_companion(quiet_screen)
    quiet.config.companion.chatter = False
    for stamp in (1000, 2000, 3000):
        await quiet.tick(now=stamp)
    kinds = [event.get("kind") for event in quiet_sink.of("companion.comment")]
    check(bool(kinds) and "talk" not in kinds, f"chatter off keeps her on the window: {kinds}")


def _at_hour(hour: int) -> float:
    """A timestamp whose local hour is `hour`, without depending on DST."""
    stamp = time.time()
    for _ in range(48):
        if time.localtime(stamp).tm_hour == hour:
            return stamp
        stamp -= 1800
    return stamp


async def test_chatter_register_and_prompt() -> None:
    persona_path = packs_dir() / "prompts" / "kyoko" / "system.md"
    persona = load_persona("kyoko")
    check(
        persona == persona_path.read_text(encoding="utf-8").strip(),
        "the work persona is the system.md bytes and nothing else",
    )
    chatter = load_chatter("kyoko")
    check("## Appetite" in chatter and "gremlin" in chatter, "the chatter pack is loaded")
    work_prompt = system_prompt([], "kyoko", profile="work")
    check("## Appetite" not in work_prompt, "appetite never reaches the work prompt")
    check("gremlin" not in work_prompt.lower(), "no register reaches the work prompt")
    check("Register for this line" not in work_prompt, "the work prompt has no register line")
    check(
        system_prompt([], "kyoko", profile="work") == work_prompt,
        "the work prompt is stable across builds",
    )

    # A greeting is not a job order. The prompt says so in the case and the
    # persona says so always; "what needs fixing" was her whole opener.
    check("## Company" in persona, "the persona has a companion-first section")
    check("## Where you are" in persona, "the persona says she stands on the desktop")
    check(
        "avatar.place" in persona and "workspace has no left side" in persona,
        "and that 'move to the left' is her body, not the project",
    )
    greeting = system_prompt([], "kyoko", profile="focus", request="hey capri")
    check("## Nothing to do yet" in greeting, "a greeting turn is told there is no request")
    check(
        "what needs fixing" in greeting,
        "and is told not to open by asking what needs fixing",
    )
    work_turn = system_prompt([], "kyoko", profile="focus", request="fix the failing build")
    check("## Nothing to do yet" not in work_turn, "a real request does not get that block")
    for text, expected in (
        ("hey", True),
        ("hello capri", True),
        ("how are you?", True),
        ("thanks!", True),
        ("what's up", True),
        ("", True),
        ("ok, now add a footer", False),
        ("fix the failing build", False),
        ("stop the server", False),
    ):
        check(looks_like_conversation(text) == expected, f"conversation? {text!r} -> {expected}")

    check(select_register(None, 14) == "analyst", "a plain hour is the analyst")
    check(
        select_register(ActivitySnapshot(last_failure="tests failed"), 14) == "gremlin",
        "a failing check leans gremlin",
    )
    check(select_register(None, 2) == "ghost", "a quiet late night is the ghost")
    check(select_register(None, 14, kind="test_failed") == "gremlin", "test failures are gremlin")
    check(select_register(None, 14, kind="build_finished") == "analyst", "a finished build is analyst")
    check(select_register(None, 14, kind="late_night") == "ghost", "late night is the ghost")

    screen = Screen()
    brain = Brain()
    companion, sink = make_companion(screen, brain=brain)
    companion.config.companion.chatter = True
    companion.config.companion.chatter_interval_s = 60
    companion.config.companion.max_per_hour = 6
    await companion.tick(now=1000)
    screen.activity = {"id": 0, "process": "", "title": ""}
    talk = await companion.tick(now=1200)
    check(talk.get("kind") == "talk", "a due conversation is still a talk")
    check(talk.get("register") in REGISTERS, f"a talk line carries a register: {talk.get('register')}")
    comments = sink.of("companion.comment")
    check(
        comments[-1].get("register") in REGISTERS,
        "the emitted event carries the register",
    )
    check(comments[0].get("register") in ("", None), "a comment about a window has no register")


async def test_reactions() -> None:
    screen = Screen()
    brain = Brain()
    companion, sink = make_companion(screen, brain=brain)
    companion.config.companion.max_per_hour = 10

    first = await companion.react("test_failed", "pytest tests/test_x.py", now=2000, hour=15)
    comments = sink.of("companion.comment")
    check(first.get("skipped") is False, f"a failing test gets a line: {first.get('reason')}")
    check(first.get("reaction") == "test_failed", "the line is marked as the reaction")
    check(first.get("kind") == "talk", "a reaction is a conversation, not a window comment")
    check(first.get("register") == "gremlin", "a failing test is gremlin")
    check(len(comments) == 1 and comments[0].get("reaction") == "test_failed", "one comment, marked")

    repeat = await companion.react("test_failed", "pytest tests/test_x.py", now=2100, hour=15)
    check(repeat.get("skipped") is True, "the same failure is not mentioned twice")
    check(len(sink.of("companion.comment")) == 1, "nothing was emitted the second time")

    other = await companion.react("test_failed", "pytest tests/test_y.py", now=3000, hour=15)
    check(other.get("skipped") is True and other.get("reason") == "cooldown", "the trigger cooldown holds")

    spaced = Screen()
    quiet, quiet_sink = make_companion(spaced)
    quiet.config.companion.max_per_hour = 10
    await quiet.react("build_finished", "npm run build", now=5000, hour=15)
    too_soon = await quiet.react("late_night", "", now=5060, hour=2)
    check(too_soon.get("reason") == "reaction spacing", "reactions keep five minutes apart")
    check(len(quiet_sink.of("companion.comment")) == 1, "the spaced reaction stays quiet")

    notes, _ = make_companion(Screen())
    notes.note_tool(
        name="shell.run",
        ok=False,
        ms=1200,
        args={"command": "uv run pytest tests/test_work.py"},
        output="1 failed, 2 passed",
    )
    check(notes.take_pending() == [("test_failed", "uv run pytest tests/test_work.py")], "a failing test command is queued")
    notes.note_tool(name="shell.run", ok=True, ms=1200, args={"command": "npm run build"}, output="built")
    check(not notes.take_pending(), "a fast build is not worth a line")
    notes.note_tool(name="shell.run", ok=True, ms=95_000, args={"command": "npm run build"}, output="bundled")
    check(notes.take_pending() == [("build_finished", "npm run build")], "a slow green build is queued")
    notes.note_tool(name="fs.read", ok=False, ms=10, args={"path": "x"}, output="error")
    check(not notes.take_pending(), "other tools never queue a reaction")

    busy, _ = make_companion(Screen(), active=[1])
    blocked = await busy.react("test_failed", "pytest x", now=2000, hour=15)
    check(blocked.get("reason") == "active turn", "a reaction never fires during a turn")
    muted, _ = make_companion(Screen(), mic=[True])
    muted_result = await muted.react("test_failed", "pytest x", now=2000, hour=15)
    check(muted_result.get("reason") == "the microphone route is active", "a mic route suppresses reactions")
    away = Screen()
    away.input_idle = 600.0
    idle, _ = make_companion(away)
    idle_result = await idle.react("test_failed", "pytest x", now=2000, hour=15)
    check(idle_result.get("reason") == "no recent input", "an empty desk suppresses reactions")


async def test_working_lines() -> None:
    screen = Screen()
    voice = PreemptVoice()
    companion, sink = make_companion(screen, voice=voice, active=[1])
    cfg = companion.config.companion
    cfg.max_per_hour = 10
    cfg.work_interval_s = 30
    cfg.work_max_per_turn = 2

    first = await companion.working(turn_id="trn_1", detail="reading a file", now=1000)
    lines = [
        event for event in sink.of("companion.comment") if event.get("kind") == "working"
    ]
    check(first.get("skipped") is False, "a running turn can be narrated")
    check(len(lines) == 1, "the working line is emitted as a companion comment")
    check(voice.spoken == [lines[0]["text"]], "the working line is spoken")
    check(lines[0].get("register") in REGISTERS, "a working line carries a register")
    check(
        voice.preempt == [False],
        f"a working line never preempts her answer: {voice.preempt}",
    )

    spacing = await companion.working(
        turn_id="trn_1", detail="running a command", now=1010
    )
    check(spacing.get("reason") == "work line spacing", "two steps in a row stay one line")

    second = await companion.working(
        turn_id="trn_1", detail="running a command", now=1040
    )
    check(second.get("skipped") is False, "the next step after the gap is allowed")
    third = await companion.working(turn_id="trn_1", detail="editing a file", now=1100)
    check(third.get("reason") == "turn line limit reached", "a turn has a line ceiling")

    fresh = await companion.working(turn_id="trn_2", detail="editing a file", now=1140)
    check(fresh.get("skipped") is False, "a new turn resets the per-turn ceiling")

    cfg.work_chatter = False
    off = await companion.working(turn_id="trn_3", detail="reading a file", now=2000)
    check(off.get("reason") == "work chatter is off", "the setting turns it off")
    cfg.work_chatter = True

    quiet, was = make_companion(Screen(), voice=Voice(), active=[1], mic=[True])
    quiet.config.companion.work_interval_s = 1
    denied = await quiet.working(turn_id="trn_m", detail="reading a file", now=1000)
    check(
        denied.get("reason") == "the microphone route is active",
        "the mic route keeps her quiet mid-task",
    )
    check(not was.of("companion.comment"), "nothing is said over a live microphone")

    away, _ = make_companion(Screen(), voice=Voice(), active=[1], ready=[False])
    noready = await away.working(turn_id="trn_r", detail="reading a file", now=1000)
    check(noready.get("reason") == "no shell connected", "no shell means no line")

    # Two tool calls can land inside one model call. Only one line may come of
    # it, so the throttle is reserved before the await.
    racer, race_sink = make_companion(Screen(), brain=SlowBrain(), active=[1])
    racer.config.companion.max_per_hour = 10
    await asyncio.gather(
        racer.working(turn_id="trn_race", detail="reading a file", now=3000),
        racer.working(turn_id="trn_race", detail="running a command", now=3000),
    )
    raced = [
        event for event in race_sink.of("companion.comment") if event.get("kind") == "working"
    ]
    check(len(raced) == 1, f"two steps at once still earn one line: {len(raced)}")

    check(step_label("fs.patch") == "editing a file", "a tool maps to a plain step name")
    check("did not work" in step_label("shell.run", False), "a failed step says so")
    check(step_label("memory.list") == "", "a tool not worth naming earns no line")
    snapshot = ActivitySnapshot(task="fix the parser", other_tasks=("the docs pass",))
    check(
        "Other open tasks: the docs pass" in "\n".join(snapshot.lines()),
        "another open task reaches the line's context",
    )


async def test_working_race() -> None:
    """A line still being written when the answer starts must never be said."""

    # 1. The answer begins and the turn ends while the line is in flight.
    turns = [1]
    brain = GatedBrain()
    voice = PreemptVoice()
    companion, sink = make_companion(Screen(), brain=brain, voice=voice, active=turns)
    companion.config.companion.work_interval_s = 1
    companion.config.companion.max_per_hour = 10

    task = asyncio.create_task(
        companion.working(turn_id="trn_race", detail="reading a file", now=1000)
    )
    await asyncio.wait_for(brain.started.wait(), timeout=2)
    companion.note_answer("trn_race")
    turns[0] = 0
    brain.release.set()
    dropped = await asyncio.wait_for(task, timeout=2)

    check(dropped.get("skipped") is True, "a line that outlived the turn is dropped")
    check(
        dropped.get("reason") == "her answer has started",
        f"it says why: {dropped.get('reason')}",
    )
    check(not sink.of("companion.comment"), "nothing is said over the answer")
    check(voice.spoken == [], "the answer keeps the channel")
    check(voice.stops == 0, "the answer is never stopped to make room for it")

    # 2. Same race without the answer note: the turn simply finished first.
    ending = [1]
    late_brain = GatedBrain()
    ended_voice = PreemptVoice()
    ended, ended_sink = make_companion(
        Screen(), brain=late_brain, voice=ended_voice, active=ending
    )
    ended.config.companion.work_interval_s = 1
    task = asyncio.create_task(
        ended.working(turn_id="trn_end", detail="editing a file", now=2000)
    )
    await asyncio.wait_for(late_brain.started.wait(), timeout=2)
    ending[0] = 0
    late_brain.release.set()
    stale = await asyncio.wait_for(task, timeout=2)
    check(
        stale.get("reason") == "the turn finished while she was thinking",
        f"a finished turn drops it too: {stale.get('reason')}",
    )
    check(not ended_sink.of("companion.comment"), "no aside after the turn ended")
    check(ended_voice.spoken == [], "nothing is spoken after the turn ended")

    # 3. The turn is still running, but its answer has already begun.
    running, running_sink = make_companion(Screen(), voice=PreemptVoice(), active=[1])
    running.config.companion.work_interval_s = 1
    running.note_answer("trn_late")
    afterwards = await running.working(
        turn_id="trn_late", detail="running a command", now=3000
    )
    check(
        afterwards.get("reason") == "her answer has started",
        "an aside never lands once the answer has begun",
    )
    check(not running_sink.of("companion.comment"), "no line is emitted for it")

    # 4. The slot is given back, so a dropped line does not cost the next one.
    turns[0] = 1
    companion.note_answer("")
    later = await companion.working(
        turn_id="trn_next", detail="searching the project", now=4000
    )
    check(later.get("skipped") is False, "a new turn may still speak")


async def test_upkeep_line() -> None:
    """A scheduled maintenance pass gets one short line, paced like the work."""
    turns = [0]
    voice = PreemptVoice()
    companion, sink = make_companion(Screen(), voice=voice, active=turns)
    cfg = companion.config.companion
    cfg.max_per_hour = 10
    cfg.work_interval_s = 30

    first = await companion.scheduled(
        {"journaled": 1, "prunedMemories": 3, "prunedTraces": 9}, now=1000
    )
    lines = [
        event for event in sink.of("companion.comment") if event.get("kind") == "upkeep"
    ]
    check(first.get("skipped") is False, "a maintenance pass gets a line")
    check(len(lines) == 1, "the upkeep line is emitted as a companion comment")
    check(voice.spoken == [lines[0]["text"]], "the upkeep line is spoken")
    check(voice.preempt == [False], "an upkeep line never preempts a reply")
    check(lines[0].get("register") in REGISTERS, "an upkeep line carries a register")

    spacing = await companion.scheduled({"prunedTraces": 2}, now=1010)
    check(
        spacing.get("reason") == "work line spacing",
        "upkeep shares the working-line spacing",
    )

    # The slot is shared in both directions: a working line inside the same gap
    # stays quiet too.
    turns[0] = 1
    held = await companion.working(turn_id="trn_shared", detail="reading a file", now=1020)
    check(held.get("reason") == "work line spacing", "a working line waits behind upkeep")
    turns[0] = 0

    cfg.work_chatter = False
    off = await companion.scheduled({"prunedTraces": 1}, now=2000)
    check(off.get("reason") == "work chatter is off", "the same switch turns upkeep off")
    cfg.work_chatter = True

    # A turn can start while the line is being written; then it is dropped.
    racing = [0]
    brain = GatedBrain()
    busy, busy_sink = make_companion(
        Screen(), brain=brain, voice=PreemptVoice(), active=racing
    )
    busy.config.companion.work_interval_s = 1
    task = asyncio.create_task(busy.scheduled({"prunedTraces": 4}, now=3000))
    await asyncio.wait_for(brain.started.wait(), timeout=2)
    racing[0] = 1
    brain.release.set()
    dropped = await asyncio.wait_for(task, timeout=2)
    check(
        dropped.get("reason") == "a turn started while she was thinking",
        f"a turn that starts mid-line drops it: {dropped.get('reason')}",
    )
    check(not busy_sink.of("companion.comment"), "nothing is said over the turn")

    check(
        upkeep_notice({"prunedTraces": 9}) == "memory upkeep: 9 traces trimmed",
        "the notice is counts only",
    )
    check(
        upkeep_notice({"journaled": 2, "prunedMemories": 4})
        == "memory upkeep: 2 episodes filed, 4 memories forgotten",
        "several counts read as one line",
    )
    check(
        upkeep_notice({}) == "a maintenance pass that had nothing to do",
        "an empty pass says so",
    )


async def test_late_night() -> None:
    stamp = _at_hour(2)
    screen = Screen()
    # No foreground window: the middle of the night is not about a window.
    screen.activity = {"id": 0, "process": "", "title": ""}
    companion, sink = make_companion(screen)
    companion.config.companion.max_per_hour = 10

    first = await companion.tick(now=stamp)
    check(first.get("reaction") == "late_night", f"2 a.m. earns one line: {first.get('reason')}")
    check(first.get("register") == "ghost", "the late-night line is the ghost")
    reactions = [event for event in sink.of("companion.comment") if event.get("reaction")]
    check(len(reactions) == 1, "one late-night reaction was emitted")

    again = await companion.tick(now=stamp + 120)
    check(again.get("reaction") is not True, "the same night does not repeat it")
    reactions = [event for event in sink.of("companion.comment") if event.get("reaction")]
    check(len(reactions) == 1, "no second late-night line")

    next_night = await companion.tick(now=stamp + 86_400)
    check(next_night.get("reaction") == "late_night", "the next night is a new night")


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-companion-"))
    try:
        print("labels and privacy")
        await test_labels_and_privacy()
        await test_chatter_prompt_roles()
        print("comment and speech")
        await test_comment_and_speech()
        print("chatter")
        await test_chatter()
        print("chatter registers and prompt boundary")
        await test_chatter_register_and_prompt()
        print("reactions")
        await test_reactions()
        print("working lines")
        await test_working_lines()
        print("working race with the answer")
        await test_working_race()
        print("upkeep lines")
        await test_upkeep_line()
        print("late night")
        await test_late_night()
        print("mic route")
        await test_mic_route()
        await test_mic_route_silences_in_flight()
        print("guards")
        await test_guards()
        print("interval and fallback")
        await test_interval_and_fallback()
    finally:
        shutil.rmtree(raw, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))