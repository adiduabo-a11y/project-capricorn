"""Windows UIA observation.

The tree is the planner's view: one numbered line per control, the same
``element_id`` until the next observation, and no password values ever. The
screenshot is the fallback for surfaces without a useful tree; password
elements are painted out of it before it leaves this module.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import io
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Element:
    id: str
    role: str
    name: str
    automation_id: str
    bounds: tuple[int, int, int, int]
    enabled: bool = True
    password: bool = False
    value: str = ""
    depth: int = 0
    parent: str = ""
    handle: int = 0

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process: str
    minimized: bool = False


@dataclass
class Observation:
    title: str
    process: str
    elements: list[Element]
    digest: str
    window: tuple[int, int, int, int] = (0, 0, 0, 0)
    shot: bytes | None = None
    ocr: list[dict[str, Any]] = field(default_factory=list)

    def by_id(self, element_id: str) -> Element | None:
        for element in self.elements:
            if element.id == element_id:
                return element
        return None

    def find(self, text: str) -> Element | None:
        needle = text.strip().lower()
        if not needle:
            return None
        for element in self.elements:
            if needle in element.name.lower() or needle in element.automation_id.lower():
                return element
        return None

    def summary(self, limit: int = 120) -> str:
        lines = [
            f"window: {self.title or '(untitled)'} [{self.process or 'unknown'}]",
            f"hash: {self.digest[:10]}",
        ]
        if self.shot:
            lines.append(f"screenshot: {len(self.shot)} bytes")
        if self.ocr:
            lines.append(f"ocr: {len(self.ocr)} text box(es)")
        for element in self.elements[:limit]:
            flags = [f"at={element.bounds}"]
            if not element.enabled:
                flags.append("disabled")
            if element.password:
                flags.append("password")
            name = "(password)" if element.password else element.name
            value = ""
            if element.value and not element.password:
                value = f" value={element.value[:60]!r}"
            ident = f" id={element.automation_id}" if element.automation_id else ""
            lines.append(
                f"[{element.id}] {element.role} {name!r}{ident}{value} " + " ".join(flags)
            )
        if len(self.elements) > limit:
            lines.append(f"… {len(self.elements) - limit} more elements")
        return "\n".join(lines)


def digest(title: str, process: str, elements: Iterable[Element]) -> str:
    body = [title, process]
    for element in elements:
        body.append(
            repr(
                (
                    element.role,
                    element.name if not element.password else "",
                    element.automation_id,
                    element.bounds,
                    element.enabled,
                    element.password,
                    "" if element.password else element.value,
                )
            )
        )
    return hashlib.sha1("\n".join(body).encode("utf-8")).hexdigest()


def _process_name(hwnd: int) -> str:
    if not hwnd or os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return os.path.basename(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _window_title(hwnd: int) -> str:
    if not hwnd or os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        length = int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)))
        if length <= 0:
            return ""
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), title, length + 1)
        return title.value
    except Exception:
        return ""


_uia_thread = threading.local()


def _init_com() -> None:
    """Give this thread a COM apartment before it touches UI Automation.

    COM is per-thread, and the daemon reaches the backend from worker threads
    (``asyncio.to_thread``), so every one of them needs its own initialisation:
    uiautomation's own docs say InitializeUIAutomationInCurrentThread must be
    called first in a new thread. Without it every UIA call dies with
    CO_E_NOTINITIALIZED ("CoInitialize has not been called"), which is what the
    frozen sidecar used to report for observe, launch and act.
    """
    if getattr(_uia_thread, "ready", False):
        return
    import uiautomation

    try:
        uiautomation.InitializeUIAutomationInCurrentThread()
    except Exception:  # noqa: BLE001 - already in an apartment is fine
        # RPC_E_CHANGED_MODE arrives when something else already initialised
        # this thread; the UIA call that follows is the one worth reporting.
        pass
    _uia_thread.ready = True


class WindowsBackend:
    """UIA through ``uiautomation``; degrades to a named reason when absent."""

    def available(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import uiautomation  # noqa: F401
        except Exception:
            return False
        return True

    def reason(self) -> str:
        if os.name != "nt":
            return "screen control is Windows-only"
        try:
            import uiautomation  # noqa: F401
        except Exception:
            return "uiautomation is not installed: uv sync --package capri-core --extra screen"
        return ""

    def windows(self) -> list[WindowInfo]:
        """Visible, titled top-level windows in Z order (foreground first-ish)."""
        if not self.available():
            raise RuntimeError(self.reason())
        user32 = ctypes.windll.user32
        found: list[WindowInfo] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd))
                if length <= 0:
                    return True
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                handle = int(hwnd)
                found.append(
                    WindowInfo(
                        handle,
                        title.value,
                        _process_name(handle),
                        minimized=bool(user32.IsIconic(hwnd)),
                    )
                )
            except Exception:
                pass
            return True

        user32.EnumWindows(each, 0)
        return found

    def foreground(self) -> WindowInfo:
        """The current foreground window, without building a UIA tree."""
        if os.name != "nt":
            raise RuntimeError("screen control is Windows-only")
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            raise RuntimeError("no foreground window")
        return WindowInfo(
            hwnd,
            _window_title(hwnd),
            _process_name(hwnd),
            minimized=bool(user32.IsIconic(hwnd)),
        )

    def observe(self, max_elements: int = 300, max_depth: int = 8) -> Observation:
        if not self.available():
            raise RuntimeError(self.reason())
        import uiautomation as auto

        _init_com()
        control = auto.GetForegroundControl()
        if control is None:
            raise RuntimeError("no foreground window")
        try:
            top = control.GetTopLevelControl()
            if top is not None:
                control = top
        except Exception:
            pass

        title = str(getattr(control, "Name", "") or "")
        hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
        process = _process_name(hwnd)
        try:
            rect = control.BoundingRectangle
            window = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        except Exception:
            window = (0, 0, 0, 0)

        elements: list[Element] = []
        next_id = 1

        def add(node: Any, depth: int, parent: str) -> None:
            nonlocal next_id
            if len(elements) >= max_elements or depth > max_depth:
                return
            try:
                if bool(getattr(node, "IsOffscreen", False)):
                    return
            except Exception:
                pass
            try:
                role = str(node.ControlTypeName or "")
                name = str(node.Name or "")
                automation = str(node.AutomationId or "")
                password = bool(node.IsPassword)
                enabled = bool(node.IsEnabled)
                rect = node.BoundingRectangle
                bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            except Exception:
                return

            value = ""
            if not password:
                try:
                    value = str(node.GetValuePattern().Value or "")
                except Exception:
                    value = ""

            element_id = f"e{next_id}"
            next_id += 1
            elements.append(
                Element(
                    id=element_id,
                    role=role,
                    name="(password)" if password else name,
                    automation_id=automation,
                    bounds=bounds,
                    enabled=enabled,
                    password=password,
                    value=value,
                    depth=depth,
                    parent=parent,
                    handle=hwnd if depth == 0 else 0,
                )
            )

            try:
                children = node.GetChildren() or []
            except Exception:
                children = []
            for child in children:
                add(child, depth + 1, element_id)

        add(control, 0, "")
        return Observation(
            title=title,
            process=process,
            elements=elements,
            digest=digest(title, process, elements),
            window=window,
        )

    def capture(
        self,
        window: tuple[int, int, int, int],
        redact: Iterable[tuple[int, int, int, int]] = (),
        marks: Iterable[tuple[str, tuple[int, int, int, int]]] = (),
    ) -> bytes | None:
        """PNG of the window, downscaled to 1280 px wide, password boxes blacked out.

        ``marks`` draws set-of-marks labels beside each UIA element so a planner
        that cannot use the tree can still name a target by number.
        """
        if not self.available():
            return None
        try:
            import mss
            from PIL import Image, ImageDraw
        except Exception:
            return None

        left, top, right, bottom = window
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None
        try:
            with mss.mss() as screen:
                grab = screen.grab({"left": left, "top": top, "width": width, "height": height})
                image = Image.frombytes("RGB", grab.size, grab.bgra, "raw", "BGRX")
        except Exception:
            return None

        draw = ImageDraw.Draw(image)
        if self._redact_allowed():
            for box in redact:
                x0, y0, x1, y1 = box
                draw.rectangle(
                    (x0 - left, y0 - top, x1 - left, y1 - top),
                    fill=(0, 0, 0),
                )
        for element_id, box in marks:
            x0, y0, x1, y1 = box
            local = (x0 - left, y0 - top, x1 - left, y1 - top)
            draw.rectangle(local, outline=(62, 240, 160), width=2)
            label = str(element_id)
            tx, ty = max(0, local[0]), max(0, local[1] - 12)
            draw.rectangle((tx, ty, tx + 7 * len(label) + 4, ty + 12), fill=(8, 7, 12))
            draw.text((tx + 2, ty), label, fill=(255, 95, 162))

        if image.width > 1280:
            ratio = 1280 / image.width
            image = image.resize((1280, max(1, int(image.height * ratio))))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def _redact_allowed(self) -> bool:
        return True

    def ocr_reason(self) -> str:
        try:
            import winocr  # noqa: F401

            return ""
        except Exception:
            pass
        try:
            import pytesseract  # noqa: F401

            pytesseract.get_tesseract_version()
            return ""
        except Exception:
            return "OCR is not installed: uv sync --package capri-core --extra ocr"

    def ocr(self, png: bytes | None, *, language: str = "en") -> list[dict[str, Any]]:
        """Text boxes with window-relative pixel bounds, best-effort.

        Windows.Media.Ocr is the default: it ships with Windows 10/11, handles
        rendered text better than a generic engine, and needs no extra binary.
        A local Tesseract install is the fallback for unusual language packs.
        """
        reason = self.ocr_reason()
        if reason:
            raise RuntimeError(reason)
        if not png:
            return []
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(png)).convert("RGB")
        except Exception as exc:
            raise RuntimeError(f"OCR could not read the capture: {exc}") from exc

        try:
            import winocr  # type: ignore[import-not-found]

            result = winocr.recognize_pil_sync(image, language or "en")
            rows: list[dict[str, Any]] = []
            for line in result.get("lines", []):
                for word in line.get("words", []):
                    text = str(word.get("text") or "").strip()
                    rect = word.get("bounding_rect") or {}
                    if not text:
                        continue
                    left = int(rect.get("x", 0))
                    top = int(rect.get("y", 0))
                    width = int(rect.get("width", 0))
                    height = int(rect.get("height", 0))
                    rows.append(
                        {
                            "text": text,
                            "bounds": [left, top, left + width, top + height],
                            "confidence": 1.0,
                        }
                    )
            return rows[:200]
        except ImportError:
            pass
        except Exception as winrt_exc:
            # Fall through to Tesseract only when it is actually available.
            if importlib.util.find_spec("pytesseract") is None:
                raise RuntimeError(f"Windows OCR failed: {winrt_exc}") from winrt_exc

        try:
            import pytesseract  # type: ignore[import-not-found]

            data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        except Exception as exc:
            raise RuntimeError(f"OCR failed: {exc}") from exc

        rows = []
        for index, text in enumerate(data.get("text", [])):
            text = str(text or "").strip()
            try:
                confidence = float(data["conf"][index])
            except (KeyError, IndexError, TypeError, ValueError):
                confidence = 0.0
            if not text or confidence < 35:
                continue
            try:
                left = int(data["left"][index])
                top = int(data["top"][index])
                width = int(data["width"][index])
                height = int(data["height"][index])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            rows.append(
                {
                    "text": text,
                    "bounds": [left, top, left + width, top + height],
                    "confidence": round(confidence / 100.0, 3),
                }
            )
        return rows[:200]
