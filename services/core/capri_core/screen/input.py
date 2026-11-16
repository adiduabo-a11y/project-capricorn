"""Input injection through SendInput, never mouse_event.

Coordinates are virtual-desktop absolute, text is Unicode per code unit, and a
key chord is a real down/up sequence. The service checks panic and idle state
before calling any of this; this module only knows how to press and click.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

EXTENDED_KEYS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2D,
    0x2E,
    0x5B,
    0x5C,
}

BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
}
for _letter in "abcdefghijklmnopqrstuvwxyz":
    KEYS[_letter] = ord(_letter.upper())
for _digit in "0123456789":
    KEYS[_digit] = ord(_digit)
for _number in range(1, 25):
    KEYS[f"f{_number}"] = 0x6F + _number


def _send(inputs: list[INPUT]) -> None:
    if not IS_WINDOWS:
        raise OSError("input injection is Windows-only")
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput sent {sent} of {len(inputs)} events")


def _unicode(unit: int, flags: int) -> INPUT:
    # Unicode goes in wScan with wVk zero. Putting the unit in wVk makes
    # characters such as p act as virtual keys (p is F1, which opens Help).
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(0, unit, flags | KEYEVENTF_UNICODE, 0, 0),
    )


def _vk(code: int, flags: int = 0) -> INPUT:
    """A virtual-key event needs its scan code; SendInput ignores it otherwise."""
    scan = int(ctypes.windll.user32.MapVirtualKeyW(code, 0))
    extended = code in EXTENDED_KEYS
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(code, scan, flags, 0, 0))


def _virtual_screen() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    return (
        user32.GetSystemMetrics(76),
        user32.GetSystemMetrics(77),
        user32.GetSystemMetrics(78),
        user32.GetSystemMetrics(79),
    )


class WindowsInput:
    def move(self, x: int, y: int) -> None:
        left, top, width, height = _virtual_screen()
        nx = int(round((x - left) * 65535 / max(1, width - 1)))
        ny = int(round((y - top) * 65535 / max(1, height - 1)))
        _send(
            [
                INPUT(
                    type=INPUT_MOUSE,
                    mi=MOUSEINPUT(
                        nx,
                        ny,
                        0,
                        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                        0,
                        0,
                    ),
                )
            ]
        )

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        if button not in BUTTONS:
            raise ValueError(f"unknown mouse button {button!r}")
        down, up = BUTTONS[button]
        self.move(x, y)
        repeats = 2 if double else 1
        for index in range(repeats):
            _send(
                [
                    INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, down, 0, 0)),
                    INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, up, 0, 0)),
                ]
            )
            if index + 1 < repeats:
                time.sleep(0.06)

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        button: str = "left",
        duration: float = 0.35,
    ) -> None:
        if button not in BUTTONS:
            raise ValueError(f"unknown mouse button {button!r}")
        down, up = BUTTONS[button]
        self.move(x1, y1)
        _send([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, down, 0, 0))])
        steps = max(2, min(20, int(duration / 0.04)))
        try:
            for step in range(1, steps + 1):
                self.move(
                    int(round(x1 + (x2 - x1) * step / steps)),
                    int(round(y1 + (y2 - y1) * step / steps)),
                )
                time.sleep(max(0.0, duration / steps))
        finally:
            _send([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, up, 0, 0))])

    def scroll(self, amount: int, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            self.move(x, y)
        _send(
            [
                INPUT(
                    type=INPUT_MOUSE,
                    mi=MOUSEINPUT(0, 0, ctypes.c_ulong(int(amount) * 120).value, MOUSEEVENTF_WHEEL, 0, 0),
                )
            ]
        )

    def type_text(self, text: str) -> None:
        # SendInput inserts a whole burst, but editors such as Notepad drop
        # characters when the events arrive faster than they can process them.
        # 20 ms per character survived a full phrase here; 10 ms dropped letters.
        units = text.encode("utf-16-le")
        for index in range(0, len(units), 2):
            unit = int.from_bytes(units[index : index + 2], "little")
            _send([_unicode(unit, 0), _unicode(unit, KEYEVENTF_KEYUP)])
            if index + 2 < len(units):
                time.sleep(0.02)

    def key(self, chord: str) -> None:
        parts = [part.strip().lower() for part in str(chord).split("+") if part.strip()]
        if not parts:
            raise ValueError("empty key chord")
        codes: list[int] = []
        for part in parts:
            if part not in KEYS:
                raise ValueError(f"unknown key {part!r}")
            codes.append(KEYS[part])
        events = [_vk(code) for code in codes]
        events.extend(_vk(code, KEYEVENTF_KEYUP) for code in reversed(codes))
        _send(events)

    def focus_window(self, handle: int) -> None:
        if not IS_WINDOWS:
            raise OSError("windowing is Windows-only")
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        target = int(handle)
        hwnd = ctypes.c_void_p(target)

        def brought() -> bool:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            return user32.GetForegroundWindow() == target

        if brought():
            user32.SetFocus(hwnd)
            return

        # Windows refuses foreground changes from a background process. Attach
        # to the foreground thread's input queue, and to the target's own queue,
        # so SetForegroundWindow and SetFocus are permitted.
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = kernel32.GetCurrentThreadId()
        attach_foreground = bool(
            user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        attach_target = bool(
            user32.AttachThreadInput(current_thread, target_thread, True)
        )
        try:
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attach_target:
                user32.AttachThreadInput(current_thread, target_thread, False)
            if attach_foreground:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        if brought():
            user32.SetFocus(hwnd)
            return

        # SwitchToThisWindow is the shell's own activation path; do not tap Alt
        # here, because an Alt tap opens the target's menu and the next typed
        # characters become menu accelerators.
        user32.SwitchToThisWindow(hwnd, True)
        if user32.GetForegroundWindow() != target:
            raise OSError("could not bring the window to the foreground")
