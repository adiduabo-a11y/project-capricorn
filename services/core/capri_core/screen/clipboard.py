"""Clipboard text, through the Win32 clipboard rather than a browser API.

Reading the clipboard is a privacy-sensitive operation, so the service only
allows it while the foreground window belongs to an allowlisted app and the
tool policy asks the user first. The clipboard is opened with a short retry:
another process can hold it for a few milliseconds at a time.

Every call gets explicit argtypes/restype. ctypes defaults a return value to
``c_int``, which truncates a clipboard or global-memory HANDLE on 64-bit
Windows, so the untagged calls failed with "int too long to convert" (and the
write path would have locked a bogus address).
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_libs = None


def available() -> bool:
    return IS_WINDOWS


def _libraries():
    """user32 and kernel32, with the clipboard and memory signatures pinned.

    The handles are 64-bit on x64, and a truncated one is either an overflow
    error or a pointer into nothing. Configured once per process on private
    ``WinDLL`` handles, so this module never changes the types another caller
    in the same process has already set on ``ctypes.windll``.
    """
    global _libs
    if _libs is not None:
        return _libs
    if not IS_WINDOWS:
        raise OSError("clipboard access is Windows-only")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    _libs = (user32, kernel32)
    return _libs


def _open_clipboard(user32, attempts: int = 8) -> None:
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.02)
    raise OSError("the clipboard is busy")


def get_text() -> str:
    if not IS_WINDOWS:
        raise OSError("clipboard access is Windows-only")
    user32, kernel32 = _libraries()
    _open_clipboard(user32)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_text(text: str) -> None:
    if not IS_WINDOWS:
        raise OSError("clipboard access is Windows-only")
    user32, kernel32 = _libraries()
    payload = (str(text) + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise OSError("could not allocate clipboard memory")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise OSError("could not lock clipboard memory")
    try:
        ctypes.memmove(pointer, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)
    _open_clipboard(user32)
    try:
        if not user32.EmptyClipboard():
            raise OSError("could not empty the clipboard")
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("could not set the clipboard")
        # Ownership passes to the system on success.
        handle = None
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)
