"""Who has the keyboard, and is the panic key down?

Both are read from Windows directly so they work while another app has focus.
Tests inject the readers, so the policy is exercisable without a desktop.
"""

from __future__ import annotations

import ctypes
import os
import time
from typing import Callable

IS_WINDOWS = os.name == "nt"

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B

PANIC_KEYS = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_ESCAPE)


def last_input_tick() -> int:
    if not IS_WINDOWS:
        return 0
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO), dwTime=0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return int(info.dwTime)


def now_tick() -> int:
    """The same 32-bit millisecond clock GetLastInputInfo reports."""
    if not IS_WINDOWS:
        return 0
    return int(ctypes.windll.kernel32.GetTickCount())


def panic_down() -> bool:
    if not IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32
    return all(bool(user32.GetAsyncKeyState(key) & 0x8000) for key in PANIC_KEYS)


class Activity:
    """The user's input clock for one screen task."""

    def __init__(
        self,
        *,
        last_input: Callable[[], int] = last_input_tick,
        tick: Callable[[], int] = now_tick,
        panic: Callable[[], bool] = panic_down,
    ) -> None:
        self._last_input = last_input
        self._tick = tick
        self._panic = panic
        self._mark = last_input()

    def mark(self) -> None:
        self._mark = self._last_input()

    def moved(self) -> bool:
        return self._last_input() != self._mark

    def idle_seconds(self) -> float:
        """Seconds since the last keyboard or mouse input, or infinity if unknown."""
        last = self._last_input()
        now = self._tick()
        if not last or not now:
            return float("inf")
        # GetTickCount and LASTINPUTINFO.dwTime are both 32-bit and wrap at
        # 49.7 days; the mask makes a wrap look like a small elapsed time.
        return ((now - last) & 0xFFFFFFFF) / 1000.0

    def panic(self) -> bool:
        return self._panic()

    def wait(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))
