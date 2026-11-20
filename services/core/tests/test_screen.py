"""Screen tests: observation, guards, actions and the settle/assert loop.

No desktop is touched. The backend, input injector and input clock are fakes,
so the allowlist, password refusal, idle/panic pause and verification run
offline and deterministically.

    uv run python services/core/tests/test_screen.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from capri_core.agent.types import Decision
from capri_core.config import CapriConfig
from capri_core.screen import (
    Activity,
    Element,
    Observation,
    ScreenBlocked,
    ScreenPaused,
    ScreenService,
    ScreenVerifyError,
    WindowInfo,
    observation_payload,
)
from capri_core.screen import clipboard as clipboard_mod
from capri_core.screen import diff as pixel_diff
from capri_core.screen.uia import digest
from capri_core.tools import ToolContext
from capri_core.tools.builtin import screen as screen_tools
from capri_core.tools.policy import Policy

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def element(
    element_id: str,
    role: str,
    name: str,
    bounds: tuple[int, int, int, int],
    *,
    automation: str = "",
    password: bool = False,
    value: str = "",
    handle: int = 0,
) -> Element:
    return Element(
        id=element_id,
        role=role,
        name=name,
        automation_id=automation,
        bounds=bounds,
        password=password,
        value=value,
        handle=handle,
    )


def observation(
    *,
    title: str = "Untitled - Notepad",
    process: str = "notepad.exe",
    names: tuple[str, ...] = ("Body",),
    password: bool = False,
) -> Observation:
    elements = [
        element("e1", "WindowControl", title, (0, 0, 800, 600), handle=4242),
        element("e2", "ButtonControl", "Save", (700, 560, 780, 590), automation="SaveButton"),
    ]
    for index, name in enumerate(names, start=3):
        elements.append(
            element(
                f"e{index}",
                "EditControl",
                name,
                (20, 80, 760, 500),
                automation=f"Edit{index}",
                value="hello" if not password else "",
            )
        )
    if password:
        elements.append(
            element(
                "e9",
                "EditControl",
                "(password)",
                (20, 520, 400, 550),
                automation="PasswordBox",
                password=True,
            )
        )
    return Observation(
        title=title,
        process=process,
        elements=elements,
        digest=digest(title, process, elements),
        window=(0, 0, 800, 600),
    )


class FakeBackend:
    def __init__(
        self,
        frames: list[Observation],
        shot: bytes = b"png-bytes",
        windows: list[WindowInfo] | None = None,
        shots: list[bytes] | None = None,
    ) -> None:
        self.frames = list(frames)
        self.shot = shot
        self.shots = list(shots or [])
        self.window_list = list(windows or [])
        self.captures: list[tuple] = []

    def available(self) -> bool:
        return True

    def reason(self) -> str:
        return ""

    def observe(self, max_elements: int = 300, max_depth: int = 8) -> Observation:
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]

    def windows(self):
        return list(self.window_list)

    def capture(self, window, redact=(), marks=()):
        self.captures.append((window, list(redact), list(marks)))
        if self.shots:
            return self.shots.pop(0)
        return self.shot

    def ocr(self, png, *, language="en"):
        return [{"text": "canvas text", "bounds": [10, 20, 80, 40], "confidence": 1.0}]

    def ocr_reason(self):
        return ""


class FakeInput:
    def __init__(self, activity: "FakeActivity") -> None:
        self.activity = activity
        self.events: list[tuple] = []

    def click(self, x, y, button="left", double=False):
        self.events.append(("click", x, y, button, double))
        self.activity.tick += 1

    def move(self, x, y):
        self.events.append(("move", x, y))
        self.activity.tick += 1

    def scroll(self, amount, x=None, y=None):
        self.events.append(("scroll", amount, x, y))
        self.activity.tick += 1

    def type_text(self, text):
        self.events.append(("type", text))
        self.activity.tick += 1

    def key(self, chord):
        self.events.append(("key", chord))
        self.activity.tick += 1

    def drag(self, x1, y1, x2, y2, button="left"):
        self.events.append(("drag", x1, y1, x2, y2, button))
        self.activity.tick += 1

    def focus_window(self, handle):
        self.events.append(("focus", handle))


class FakeActivity(Activity):
    def __init__(self) -> None:
        self.tick = 0
        self.marked = 0
        self.panic_flag = False

    def mark(self) -> None:
        self.marked = self.tick

    def moved(self) -> bool:
        return self.tick != self.marked

    def panic(self) -> bool:
        return self.panic_flag

    def wait(self, seconds: float) -> None:
        return None


def service(
    config: CapriConfig,
    frames: list[Observation],
    *,
    activity: FakeActivity | None = None,
    windows: list[WindowInfo] | None = None,
    launcher=None,
    shots: list[bytes] | None = None,
) -> tuple[ScreenService, FakeBackend, FakeInput]:
    backend = FakeBackend(frames, windows=windows, shots=shots)
    activity = activity or FakeActivity()
    input = FakeInput(activity)
    return (
        ScreenService(
            config,
            "C:/work",
            backend=backend,
            input=input,
            activity=activity,
            launcher=launcher,
            poll_s=0,
        ),
        backend,
        input,
    )


def test_observation(config: CapriConfig) -> None:
    first = observation(password=True)
    svc, backend, _ = service(config, [first])
    seen = svc.observe(include_shot=True)
    text = seen.summary()
    check("[e2] ButtonControl 'Save'" in text, "observation numbers controls")
    check("(password)" in text, "password control is marked")
    check("hello" not in text.split("(password)")[-1], "password value is never shown")
    check(backend.captures and backend.captures[0][1] == [(20, 520, 400, 550)], "password box is redacted")
    payload = observation_payload(seen, shot=True)
    check(payload["shotUrl"].startswith("data:image/png;base64,"), "screenshot is a data URL")
    check(payload["elements"][0]["handle"] == 4242, "window handle rides along")
    check(seen.find("save") is not None, "find matches names case-insensitively")

    marked, marked_backend, _ = service(config, [observation()])
    seen_marks = marked.observe(include_shot=True, marks=True, ocr=True)
    check(bool(seen_marks.ocr), "OCR returns text boxes")
    check("canvas text" in observation_payload(seen_marks)["ocr"][0]["text"], "OCR rides in the payload")
    check(bool(marked_backend.captures[-1][2]), "set-of-marks capture carries element boxes")
    check(seen_marks.shot == b"png-bytes", "set-of-marks still returns a screenshot")


def test_click_and_verify(config: CapriConfig) -> None:
    before = observation()
    after = observation(title="Untitled - Notepad*")
    svc, _, input = service(config, [before, before, after])
    svc.observe()
    result = svc.act("click", element_id="e2", expect={"changed": True, "title": "Notepad"})
    check(result["changed"] is True, "click reports the changed tree")
    check(input.events and input.events[0][0] == "click", "click reached the injector")
    check(input.events[0][1:3] == (740, 575), f"click used the element centre: {input.events[0]}")


def test_guards(config: CapriConfig) -> None:
    svc, _, input = service(config, [observation(password=True)])
    svc.observe()
    try:
        svc.act("click", element_id="e9")
        check(False, "password field refused")
    except ScreenBlocked as exc:
        check("password" in str(exc), "password field refused")

    cmd, _, _ = service(config, [observation(process="cmd.exe")])
    cmd.observe()
    try:
        cmd.act("click", element_id="e2")
        check(False, "non-allowlisted app refused")
    except ScreenBlocked as exc:
        check("screen_allow_apps" in str(exc), "non-allowlisted app refused")

    activity = FakeActivity()
    paused, _, paused_input = service(config, [observation()], activity=activity)
    paused.observe()
    activity.tick += 1
    try:
        paused.act("click", element_id="e2")
        check(False, "user input pauses the task")
    except ScreenPaused as exc:
        check("you moved" in str(exc), "user input pauses the task")
    check(not paused_input.events, "no input injected after the user took over")

    activity = FakeActivity()
    activity.panic_flag = True
    panic, _, panic_input = service(config, [observation()], activity=activity)
    panic.observe()
    try:
        panic.act("click", element_id="e2")
        check(False, "panic key aborts")
    except ScreenPaused as exc:
        check("panic" in str(exc), "panic key aborts")
    check(not panic_input.events, "no input injected while panic is held")


def test_typing_and_verification(config: CapriConfig) -> None:
    before = observation(names=("Body",))
    after = observation(names=("Body",), title="Untitled - Notepad*")
    svc, _, input = service(config, [before, before, after])
    svc.observe()
    result = svc.act("type_text", element_id="e3", text="a longer line " * 6)
    kinds = [event[0] for event in input.events]
    check(kinds[0] == "click" and "type" in kinds, "type_text focuses the element then types")
    check(len([event for event in input.events if event[0] == "type"]) >= 2, "typing is chunked")
    check(result["changed"] is True, "typing verifies a change")

    unchanged, _, _ = service(config, [observation()])
    unchanged.observe()
    try:
        unchanged.act("click", element_id="e2", expect={"changed": True})
        check(False, "a missing change fails verification")
    except ScreenVerifyError as exc:
        check("did not change" in str(exc), "a missing change fails verification")

    title, _, _ = service(config, [observation()])
    title.observe()
    try:
        title.act("click", element_id="e2", expect={"title": "Firefox"})
        check(False, "wrong title fails verification")
    except ScreenVerifyError as exc:
        check("title" in str(exc), "wrong title fails verification")


def test_pause_clipboard(config: CapriConfig) -> None:
    svc, _, input = service(config, [observation()])
    status = svc.pause(True, "tray test")
    check(status["paused"] and status["presence"] == "eyes closed", "pause is visible in status")
    try:
        svc.act("click", element_id="e2")
        check(False, "paused screen refuses input")
    except ScreenPaused as exc:
        check("tray test" in str(exc), "paused screen refuses input")
    check(not input.events, "no input injected while paused")
    check(svc.pause(False)["presence"] == "eyes open", "resume restores presence")

    try:
        svc.act("click", x=999, y=999)
        check(False, "out-of-window coordinate click refused")
    except ScreenBlocked:
        check(True, "out-of-window coordinate click refused")
    try:
        svc.act("key", keys="win+r")
        check(False, "Win shortcut chord refused")
    except ScreenBlocked:
        check(True, "Win shortcut chord refused")

    unallowed_clip, _, _ = service(config, [observation(process="keepass.exe")])
    try:
        unallowed_clip.clipboard_read()
        check(False, "clipboard read refuses non-allowlisted foreground window")
    except ScreenBlocked:
        check(True, "clipboard read refuses non-allowlisted foreground window")

    original_get = clipboard_mod.get_text
    original_set = clipboard_mod.set_text
    try:
        clipboard_mod.get_text = lambda: "clip text"
        clipboard_mod.set_text = lambda text: None
        read = svc.clipboard_read()
        check(read["text"] == "clip text", "clipboard read is returned")
        wrote = svc.clipboard_write("replacement")
        check(wrote["length"] == len("replacement"), "clipboard write is accepted")
    finally:
        clipboard_mod.get_text = original_get
        clipboard_mod.set_text = original_set


def test_drag_and_pixels(config: CapriConfig) -> None:
    svc, backend, input = service(config, [observation()])
    svc.observe()
    result = svc.act("drag", x=10, y=20, x2=200, y2=120)
    check(input.events[-1] == ("drag", 10, 20, 200, 120, "left"), "drag reaches the injector")
    check(result["action"] == "drag", "drag result is reported")

    import struct
    import zlib

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    def png(color: tuple[int, int, int]) -> bytes:
        # Build a minimal valid 20x20 RGB PNG using stdlib so tests run even without Pillow.
        row = b"\x00" + bytes(color) * 20
        ihdr = struct.pack(">IIBBBBB", 20, 20, 8, 2, 0, 0, 0)
        idat = zlib.compress(row * 20)
        return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")

    check(pixel_diff.changed(png((0, 0, 0)), png((255, 255, 255))), "pixel diff sees a real change")
    check(not pixel_diff.changed(png((0, 0, 0)), png((0, 0, 0))), "pixel diff ignores identical images")

    visual, _, _ = service(config, [observation()], shots=[png((0, 0, 0)), png((255, 255, 255))])
    visual.observe()
    result = visual.act("click", element_id="e2", expect={"changed": True})
    check(result["changedBy"] == "pixels", "pixel change satisfies the verify loop")

    retrying, _, retry_input = service(config, [observation()])
    retrying.observe()
    try:
        retrying.act("click", element_id="e2", expect={"changed": True})
        check(False, "retry eventually fails")
    except ScreenVerifyError:
        check(True, "retry eventually fails")
    check(
        len([event for event in retry_input.events if event[0] == "click"]) == 3,
        "an unchanged click is retried twice (three attempts)",
    )


def test_policy(config: CapriConfig) -> None:
    policy = Policy(config.policy, "C:/work")
    seen = observation()
    seen.elements.append(element("e10", "ButtonControl", "Delete note", (700, 500, 780, 540)))
    policy.screen = type("Screen", (), {"last": seen})()
    decision = policy.check(
        screen_tools.ACT_SPEC,
        {"action": "click", "element_id": "e10", "target_name": "Save"},
    )
    check(decision.verdict == "confirm" and decision.tier == 2, "destructive target asks first")
    benign = policy.check(
        screen_tools.ACT_SPEC, {"action": "click", "element_id": "e2", "target_name": "Save"}
    )
    check(benign.verdict == "allow" and benign.tier == 1, "benign target is allowed")
    fallback = policy.check(
        screen_tools.ACT_SPEC, {"action": "click", "target_name": "Delete note"}
    )
    check(fallback.verdict == "confirm", "a target name alone can still escalate")
    observed = policy.check(screen_tools.OBSERVE_SPEC, {})
    check(observed.verdict == "allow", "observation is free")
    launch = policy.check(screen_tools.LAUNCH_SPEC, {"app": "notepad.exe"})
    check(launch.verdict == "allow", "allowlisted launch is benign")
    blocked_launch = policy.check(screen_tools.LAUNCH_SPEC, {"app": "cmd.exe"})
    check(blocked_launch.verdict == "block", "unallowlisted launch is blocked")
    blocked_path = policy.check(
        screen_tools.LAUNCH_SPEC, {"app": "C:\\Windows\\notepad.exe"}
    )
    check(blocked_path.verdict == "block", "launch paths are blocked")
    focus = policy.check(screen_tools.FOCUS_SPEC, {"window_id": 4242})
    check(focus.verdict == "allow", "focus is benign")
    clipboard_read = policy.check(screen_tools.CLIPBOARD_SPEC, {"mode": "read"})
    check(clipboard_read.verdict == "confirm", "clipboard read asks first")
    clipboard_write = policy.check(screen_tools.CLIPBOARD_SPEC, {"mode": "write"})
    check(clipboard_write.verdict == "allow", "clipboard write is benign")


def test_windows_and_focus(config: CapriConfig) -> None:
    notepad = WindowInfo(4242, "Untitled - Notepad", "Notepad.exe")
    command = WindowInfo(77, "Command Prompt", "cmd.exe")
    svc, _, input = service(config, [observation()], windows=[notepad, command])
    listed = svc.windows()
    check(len(listed["windows"]) == 1, "window list filters to the allowlist")
    check(listed["windows"][0]["id"] == 4242, "window list keeps the handle")

    result = svc.focus(4242)
    check(input.events[-1] == ("focus", 4242), "focus brought the window forward")
    check(result["process"].lower() == "notepad.exe", "focus observed the target")

    blocked, _, blocked_input = service(config, [observation()], windows=[notepad, command])
    try:
        blocked.focus(77)
        check(False, "focus refuses an unallowlisted window")
    except ScreenBlocked:
        check(True, "focus refuses an unallowlisted window")
    check(not blocked_input.events, "no focus call for an unallowlisted window")

    gone, _, gone_input = service(config, [observation()], windows=[notepad])
    try:
        gone.focus(999)
        check(False, "focus refuses a stale window id")
    except ScreenBlocked as exc:
        check("gone" in str(exc), "focus refuses a stale window id")
    check(not gone_input.events, "no focus call for a stale id")


def test_launch(config: CapriConfig) -> None:
    notepad = WindowInfo(4242, "Untitled - Notepad", "Notepad.exe")
    launched: list[str] = []

    def launch_it(app: str) -> None:
        launched.append(app)
        backend.window_list.append(notepad)

    svc, backend, input = service(
        config, [observation()], windows=[], launcher=launch_it
    )
    result = svc.launch("notepad.exe")
    check(launched == ["notepad.exe"], "launch started the allowlisted app")
    check(result["launched"] is True, "launch reports a new window")
    check(input.events[-1] == ("focus", 4242), "launch focused the new window")

    existing, _, existing_input = service(
        config, [observation()], windows=[notepad], launcher=lambda app: launched.append(app)
    )
    before = len(launched)
    result = existing.launch("Notepad.exe")
    check(len(launched) == before, "launch reuses an existing window")
    check(result["launched"] is False, "existing app is not launched twice")
    check(existing_input.events[-1] == ("focus", 4242), "existing app is focused")

    refused, _, refused_input = service(config, [observation()], windows=[], launcher=launch_it)
    try:
        refused.launch("cmd.exe")
        check(False, "launch refuses an unallowlisted app")
    except ScreenBlocked:
        check(True, "launch refuses an unallowlisted app")
    check(not refused_input.events, "no focus call after a refused launch")
    try:
        refused.launch("C:\\Windows\\notepad.exe")
        check(False, "launch refuses a path")
    except ScreenBlocked:
        check(True, "launch refuses a path")


def test_activity() -> None:
    tick = [10]
    activity = Activity(last_input=lambda: tick[0], panic=lambda: False)
    activity.mark()
    check(not activity.moved(), "same input tick is idle")
    tick[0] = 11
    check(activity.moved(), "a new input tick is user activity")
    aged = Activity(last_input=lambda: 1000, tick=lambda: 4000, panic=lambda: False)
    check(aged.idle_seconds() == 3.0, "input age uses the last-input clock")
    unknown = Activity(last_input=lambda: 0, tick=lambda: 4000, panic=lambda: False)
    check(unknown.idle_seconds() == float("inf"), "unknown input age is treated as idle")
    panicked = Activity(last_input=lambda: 0, panic=lambda: True)
    check(panicked.panic(), "panic reader is honoured")


def test_tool(config: CapriConfig) -> None:
    notepad = WindowInfo(4242, "Untitled - Notepad", "Notepad.exe")
    svc, _, _ = service(config, [observation()], windows=[notepad])
    ctx = ToolContext(conn=None, turn_id="t", subject="s", workspace="C:/work", screen=svc)  # type: ignore[arg-type]
    result = asyncio.run(screen_tools.screen_observe(ctx))
    check(result.ok and "ButtonControl" in result.output, "screen.observe tool returns the tree")
    acted = asyncio.run(
        screen_tools.screen_act(ctx, action="click", element_id="e2")
    )
    check(acted.ok, "screen.act tool completes")
    listed = asyncio.run(screen_tools.screen_windows(ctx))
    check(listed.ok and "Notepad" in listed.output, "screen.windows tool returns the list")
    focused = asyncio.run(screen_tools.screen_focus(ctx, window_id=4242))
    check(focused.ok, "screen.focus tool completes")

    launch_svc, backend, _ = service(
        config,
        [observation()],
        windows=[],
        launcher=lambda app: backend.window_list.append(notepad),
    )
    launch_ctx = ToolContext(
        conn=None, turn_id="t", subject="s", workspace="C:/work", screen=launch_svc
    )  # type: ignore[arg-type]
    launched = asyncio.run(screen_tools.screen_launch(launch_ctx, app="notepad.exe"))
    check(launched.ok and "launched" in launched.output, "screen.launch tool completes")


def test_running_apps(config: CapriConfig) -> None:
    windows = [
        WindowInfo(1, "app.py - capri", "code.exe"),
        WindowInfo(2, "other.py - capri", "Code.exe"),
        WindowInfo(3, "Untitled - Notepad", "notepad.exe"),
        WindowInfo(4, "a song", "spotify.exe"),
        WindowInfo(5, "junk", ""),
    ]
    svc, _, _ = service(config, [observation()], windows=windows)
    result = svc.running_apps()
    apps = {row["process"]: row for row in result["apps"]}
    check(
        set(apps) == {"code.exe", "notepad.exe", "spotify.exe"},
        "one row per process, blank processes skipped",
    )
    check(apps["code.exe"]["windows"] == 2, "windows are counted per process")
    check(apps["code.exe"]["allowed"] is True, "an allowlisted process is marked")
    check(apps["spotify.exe"]["allowed"] is False, "an untrusted process is not")
    check(
        all(set(row) == {"process", "windows", "allowed"} for row in result["apps"]),
        "the picker carries names, not window titles",
    )
    check(
        result["allowApps"] == list(config.policy.screen_allow_apps),
        "the picker returns the allowlist it checked against",
    )


def test_blocked_apps() -> None:
    cfg = CapriConfig()
    cfg.policy.screen_allow_apps = []
    cfg.policy.screen_block_apps = ["cmd.exe", "powershell*"]
    policy = Policy(cfg.policy, "C:/work")

    # Allowed by default when not in block list
    allowed_launch = policy.check(screen_tools.LAUNCH_SPEC, {"app": "notepad.exe"})
    check(allowed_launch.verdict == "allow", "unblocked app is allowed by default")
    discord_launch = policy.check(screen_tools.LAUNCH_SPEC, {"app": "discord.exe"})
    check(discord_launch.verdict == "allow", "discord is allowed by default")

    # Blocked apps
    cmd_launch = policy.check(screen_tools.LAUNCH_SPEC, {"app": "cmd.exe"})
    check(cmd_launch.verdict == "block", "blocked app is blocked by policy")
    ps_launch = policy.check(screen_tools.LAUNCH_SPEC, {"app": "powershell.exe"})
    check(ps_launch.verdict == "block", "wildcard-blocked app is blocked")

    # Service-level checks
    svc, _, _ = service(cfg, [observation(process="notepad.exe")])
    check(svc._process_allowed("notepad.exe") is True, "service allows unblocked app")
    check(svc._process_allowed("discord.exe") is True, "service allows discord by default")
    check(svc._process_allowed("cmd.exe") is False, "service disallows blocked app")
    check(svc._process_allowed("powershell.exe") is False, "service disallows wildcard blocked app")

    try:
        svc._allowed_process("cmd.exe")
        check(False, "_allowed_process raises on blocked app")
    except ScreenBlocked as exc:
        check("screen_block_apps" in str(exc), "_allowed_process raises ScreenBlocked for blocked app")


def test_default_allow_apps() -> None:
    cfg = CapriConfig()
    cfg.policy.screen_allow_apps = []
    cfg.policy.screen_block_apps = []
    policy = Policy(cfg.policy, "C:/work")

    check(
        policy.check(screen_tools.LAUNCH_SPEC, {"app": "notepad.exe"}).verdict == "allow",
        "notepad allowed by default",
    )
    check(
        policy.check(screen_tools.LAUNCH_SPEC, {"app": "discord.exe"}).verdict == "allow",
        "discord allowed by default",
    )
    check(
        policy.check(screen_tools.LAUNCH_SPEC, {"app": "cmd.exe"}).verdict == "allow",
        "cmd allowed by default when no blocklist",
    )

    svc, _, _ = service(cfg, [observation()])
    check(svc._process_allowed("notepad.exe") is True, "all apps allowed when neither list set")
    check(svc._process_allowed("discord.exe") is True, "discord allowed when neither list set")


def test_launcher_resolution() -> None:
    from capri_core.screen.service import resolve_windows_app

    # Test resolution of discord
    discord_res = resolve_windows_app("discord.exe")
    check(discord_res is not None, "discord.exe resolves on Windows")
    discord_bare_res = resolve_windows_app("discord")
    check(discord_bare_res is not None, "bare discord resolves on Windows")

    # Test resolution of notepad
    notepad_res = resolve_windows_app("notepad.exe")
    check(notepad_res is not None, "notepad.exe resolves via which/PATH")


def main() -> int:
    config = CapriConfig()
    config.policy.screen_allow_apps = [
        "chrome.exe",
        "code.exe",
        "explorer.exe",
        "notepad.exe",
    ]
    config.screen.settle_ms = 300
    config.screen.max_elements = 100
    tests = [
        ("observation", lambda: test_observation(config)),
        ("click", lambda: test_click_and_verify(config)),
        ("guards", lambda: test_guards(config)),
        ("typing", lambda: test_typing_and_verification(config)),
        ("windows", lambda: test_windows_and_focus(config)),
        ("launch", lambda: test_launch(config)),
        ("pause/clipboard", lambda: test_pause_clipboard(config)),
        ("drag/pixels", lambda: test_drag_and_pixels(config)),
        ("policy", lambda: test_policy(config)),
        ("apps", lambda: test_running_apps(config)),
        ("activity", test_activity),
        ("tool", lambda: test_tool(config)),
        ("blocked_apps", test_blocked_apps),
        ("default_allow", test_default_allow_apps),
        ("launcher_resolution", test_launcher_resolution),
    ]
    for name, run in tests:
        print(name)
        run()
    if FAILURES:
        print(f"\n{len(FAILURES)} failures")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
