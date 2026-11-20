"""Screen observation, action, and the settle/assert loop.

Everything that can change the desktop goes through here: the allowlist check,
the password and panic guards, the idle watch, and the verification that the
tree actually changed the way the caller expected. The backend and input
injector are injectable so the whole loop can be tested without a desktop.
"""

from __future__ import annotations

import base64
import dataclasses
import fnmatch
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from ..config import CapriConfig
from . import clipboard as clipboard_mod
from . import diff as pixel_diff
from .input import WindowsInput
from .uia import Element, Observation, WindowInfo, WindowsBackend
from .watch import Activity

DESTRUCTIVE_WORDS = (
    "delete",
    "remove",
    "send",
    "submit",
    "purchase",
    "buy",
    "pay",
    "uninstall",
    "format",
    "shutdown",
    "restart",
    "sign out",
    "log out",
)
SENSITIVE_WORDS = ("password", "passcode", "passphrase", "2fa", "verification code")


class ScreenError(RuntimeError):
    pass


class ScreenUnavailable(ScreenError):
    pass


class ScreenBlocked(ScreenError):
    pass


class ScreenPaused(ScreenError):
    pass


class ScreenVerifyError(ScreenError):
    def __init__(self, message: str, observation: Observation) -> None:
        super().__init__(message)
        self.observation = observation


def resolve_windows_app(app: str) -> tuple[str, list[str]] | str | None:
    """Intelligently resolve a Windows app name to a path or executable invocation.

    Checks:
    1. PATH (shutil.which)
    2. Registry App Paths (HKCU and HKLM)
    3. %LOCALAPPDATA% (Update.exe --processStart, direct .exe, app-* subdirectories, Programs folder)
    4. Start Menu shortcuts (.lnk) in APPDATA and ProgramData
    5. %ProgramFiles% and %ProgramFiles(x86)%
    """
    if os.name != "nt":
        return None
    raw = str(app or "").strip()
    if not raw:
        return None
    stem = raw[:-4] if raw.lower().endswith(".exe") else raw
    exe_name = f"{stem}.exe"

    # 1. PATH via shutil.which
    found = shutil.which(raw) or shutil.which(exe_name)
    if found:
        return found

    # 2. Registry App Paths
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (exe_name, stem):
                key_path = f"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{sub}"
                try:
                    with winreg.OpenKey(root, key_path) as key:
                        val, _ = winreg.QueryValueEx(key, "")
                        if val:
                            cleaned = str(val).strip('"').strip()
                            if os.path.exists(cleaned):
                                return cleaned
                except OSError:
                    pass
    except ImportError:
        pass

    # 3. %LOCALAPPDATA% (especially Discord and Squirrel apps)
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata and os.path.exists(localappdata):
        for folder_name in (stem, stem.capitalize(), stem.upper()):
            candidate_dir = os.path.join(localappdata, folder_name)
            if not os.path.isdir(candidate_dir):
                continue
            update_exe = os.path.join(candidate_dir, "Update.exe")
            if os.path.exists(update_exe):
                return (update_exe, ["--processStart", exe_name])
            direct_exe = os.path.join(candidate_dir, exe_name)
            if os.path.exists(direct_exe):
                return direct_exe
            try:
                for sub in os.listdir(candidate_dir):
                    if sub.startswith("app-"):
                        nested_exe = os.path.join(candidate_dir, sub, exe_name)
                        if os.path.exists(nested_exe):
                            return nested_exe
            except OSError:
                pass

        programs_dir = os.path.join(localappdata, "Programs")
        if os.path.exists(programs_dir):
            for folder_name in (stem, stem.capitalize()):
                cand = os.path.join(programs_dir, folder_name, exe_name)
                if os.path.exists(cand):
                    return cand

    # 4. Start Menu shortcuts (.lnk)
    start_menu_roots = [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    fuzzy_lnk: str | None = None
    for menu_root in start_menu_roots:
        if not os.path.exists(menu_root):
            continue
        try:
            for root_dir, _, files in os.walk(menu_root):
                for f in files:
                    if f.lower() == f"{stem.lower()}.lnk":
                        return os.path.join(root_dir, f)
                    if (
                        fuzzy_lnk is None
                        and f.lower().startswith(stem.lower())
                        and f.lower().endswith(".lnk")
                    ):
                        fuzzy_lnk = os.path.join(root_dir, f)
        except OSError:
            pass
    if fuzzy_lnk:
        return fuzzy_lnk

    # 5. ProgramFiles / ProgramFiles(x86)
    for pf_var in ("ProgramFiles", "ProgramFiles(x86)"):
        pf = os.environ.get(pf_var, "")
        if pf and os.path.exists(pf):
            for folder_name in (stem, stem.capitalize()):
                cand = os.path.join(pf, folder_name, exe_name)
                if os.path.exists(cand):
                    return cand

    return None


def _default_launcher(app: str) -> None:
    if os.name != "nt":
        raise ScreenUnavailable("launching apps is Windows-only")

    resolved = resolve_windows_app(app)
    if isinstance(resolved, tuple):
        exe, args = resolved
        try:
            subprocess.Popen([exe, *args])
            return
        except Exception as exc:
            raise ScreenError(f"could not launch {app} via {exe}: {exc}") from exc
    if isinstance(resolved, str):
        try:
            os.startfile(resolved)  # type: ignore[attr-defined]
            return
        except OSError:
            pass

    # Try direct os.startfile with the raw app / exe name
    try:
        os.startfile(app)  # type: ignore[attr-defined]
        return
    except OSError:
        pass

    stem = app[:-4] if app.lower().endswith(".exe") else app
    if stem != app:
        try:
            os.startfile(stem)  # type: ignore[attr-defined]
            return
        except OSError:
            pass

    # Fallback to PowerShell Start-Process
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Start-Process '{stem}'"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        raise ScreenError(f"could not launch {app}: {exc}") from exc


def observation_payload(observation: Observation, *, shot: bool = False) -> dict[str, Any]:
    elements = []
    for element in observation.elements:
        row = dataclasses.asdict(element)
        if row.get("password"):
            row["value"] = ""
        elements.append(row)
    payload: dict[str, Any] = {
        "title": observation.title,
        "process": observation.process,
        "hash": observation.digest,
        "window": list(observation.window),
        "elements": elements,
        "summary": observation.summary(),
    }
    if observation.ocr:
        payload["ocr"] = observation.ocr
    if shot and observation.shot:
        encoded = base64.b64encode(observation.shot).decode("ascii")
        payload["shotUrl"] = f"data:image/png;base64,{encoded}"
    return payload


class ScreenService:
    def __init__(
        self,
        config: CapriConfig,
        workspace: str,
        *,
        backend: Any = None,
        input: Any = None,
        activity: Activity | None = None,
        launcher: Callable[[str], None] | None = None,
        poll_s: float = 0.12,
    ) -> None:
        self.config = config
        self.workspace = workspace
        self.backend = backend or WindowsBackend()
        self.input = input or WindowsInput()
        self.activity = activity or Activity()
        self.launcher = launcher or _default_launcher
        self.poll_s = poll_s
        self.last: Observation | None = None
        self._lock = threading.Lock()
        self._pause_lock = threading.Lock()
        self._paused = False
        self._pause_reason = ""

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        ocr_reason = ""
        if hasattr(self.backend, "ocr_reason"):
            ocr_reason = self.backend.ocr_reason()
        return {
            "enabled": self.config.screen.enabled,
            "available": self.backend.available(),
            "reason": self.backend.reason(),
            "allowApps": list(getattr(self.config.policy, "screen_allow_apps", ())),
            "blockApps": list(getattr(self.config.policy, "screen_block_apps", ())),
            "window": self.last.title if self.last else None,
            "process": self.last.process if self.last else None,
            "paused": self._paused,
            "pauseReason": self._pause_reason,
            "presence": "eyes closed" if self._paused else "eyes open",
            "clipboard": clipboard_mod.available(),
            "ocr": not ocr_reason,
            "ocrReason": ocr_reason,
        }

    def pause(self, paused: bool = True, reason: str = "paused from the shell") -> dict[str, Any]:
        """Tray/global pause. Observing remains possible; touching does not."""
        with self._pause_lock:
            self._paused = bool(paused)
            self._pause_reason = "" if not paused else (reason or "paused")
        return self.status()

    def resume(self) -> dict[str, Any]:
        return self.pause(False)

    def toggle_pause(self) -> dict[str, Any]:
        return self.pause(not self._paused)

    # ------------------------------------------------------------------ windows

    def windows(self) -> dict[str, Any]:
        """Only windows whose process is in policy.screen_allow_apps."""
        with self._lock:
            self._require_available()
            found = self.backend.windows()
            rows = [
                {
                    "id": window.handle,
                    "title": window.title,
                    "process": window.process,
                    "minimized": bool(getattr(window, "minimized", False)),
                }
                for window in found
                if self._process_allowed(window.process)
            ]
            # Listing is the start of a deliberate screen task, so the idle
            # guard should measure input from here, not from an old command.
            self.activity.mark()
            return {
                "windows": rows,
                "allowApps": list(getattr(self.config.policy, "screen_allow_apps", ())),
                "blockApps": list(getattr(self.config.policy, "screen_block_apps", ())),
            }

    def running_apps(self) -> dict[str, Any]:
        """Every visible window's process name, allowlisted or not.

        Names only, no titles: granting trust for an app should not require
        reading what is open inside it. This is the picker's list, not an
        observation.
        """
        with self._lock:
            self._require_available()
            counts: dict[str, int] = {}
            for window in self.backend.windows():
                process = str(window.process or "").strip().lower()
                if not process:
                    continue
                counts[process] = counts.get(process, 0) + 1
            return {
                "apps": [
                    {
                        "process": name,
                        "windows": count,
                        "allowed": self._process_allowed(name),
                    }
                    for name, count in sorted(counts.items())
                ],
                "allowApps": list(getattr(self.config.policy, "screen_allow_apps", ())),
                "blockApps": list(getattr(self.config.policy, "screen_block_apps", ())),
            }

    def input_idle_s(self) -> float:
        """Seconds since the user's last keyboard or mouse input."""
        return float(self.activity.idle_seconds())

    def foreground(self) -> dict[str, Any]:
        """Passive foreground read used by ambient commentary.

        This deliberately does not require the UIA extras or the action
        allowlist: it reads only the top-level window title and process name,
        never its contents. A commentary window is not a deliberate screen
        task, so it also does not touch the input-activity clock.
        """
        with self._lock:
            if not self.config.screen.enabled:
                raise ScreenUnavailable("screen control is disabled in config")
            reader = getattr(self.backend, "foreground", None)
            if reader is None:
                raise ScreenUnavailable("foreground activity is unavailable")
            try:
                window = reader()
            except Exception as exc:
                raise ScreenUnavailable(f"could not read the foreground window: {exc}") from exc
            return {
                "id": window.handle,
                "title": window.title,
                "process": window.process,
                "minimized": bool(getattr(window, "minimized", False)),
            }

    def focus(self, window_id: int) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            window = self._find_window(window_id)
            self._allowed_process(window.process)
            return self._focus_window(window)

    def launch(self, app: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
        name = str(app or "").strip()
        if not name or os.path.basename(name).lower() != name.lower() or "/" in name or "\\" in name:
            raise ScreenBlocked("launch takes an application name such as notepad.exe, not a path")
        self._allowed_process(name)

        with self._lock:
            self._require_available()
            stem = name[:-4].lower() if name.lower().endswith(".exe") else name.lower()
            exe = f"{stem}.exe"
            existing = [
                window
                for window in self.backend.windows()
                if window.process.lower() in (name.lower(), stem, exe)
            ]
            self.activity.mark()  # launching is the deliberate start of the task
            if existing:
                result = self._focus_window(existing[0], watch_idle=False)
                result["launched"] = False
                result["app"] = name
                return result
            try:
                self.launcher(name)
            except Exception as exc:
                raise ScreenError(f"could not launch {name}: {exc}") from exc

            deadline = time.monotonic() + max(1.0, min(float(timeout_s), 30.0))
            window: WindowInfo | None = None
            while time.monotonic() < deadline:
                if self._paused:
                    raise ScreenPaused(
                        self._pause_reason or "screen control is paused; resume it from the tray"
                    )
                if self.activity.panic():
                    raise ScreenPaused("panic key held; input injection stopped")
                matches = [
                    item
                    for item in self.backend.windows()
                    if item.process.lower() in (name.lower(), stem, exe)
                ]
                if matches:
                    window = matches[0]
                    break
                time.sleep(0.2)
            if window is None:
                raise ScreenError(f"{name} launched but did not open a window")

            # Starting an app can disturb the shell's input clock (and the
            # process launch is our own action, not the user taking over).
            self.activity.mark()
            result = self._focus_window(window, watch_idle=False)
            result["launched"] = True
            result["app"] = name
            return result

    # ------------------------------------------------------------------ observe

    def observe(
        self,
        *,
        include_shot: bool = False,
        marks: bool = False,
        ocr: bool = False,
    ) -> Observation:
        with self._lock:
            return self._observe(include_shot=include_shot, marks=marks, ocr=ocr)

    def _observe(
        self,
        *,
        include_shot: bool,
        mark: bool = True,
        marks: bool = False,
        ocr: bool = False,
    ) -> Observation:
        self._require_available()
        if mark:
            self.activity.mark()
        observation = self.backend.observe(
            max_elements=self.config.screen.max_elements,
            max_depth=self.config.screen.max_depth,
        )
        if (include_shot or ocr) and self.config.screen.capture:
            redact: Iterable[tuple[int, int, int, int]] = ()
            if self.config.screen.redact_passwords:
                redact = [element.bounds for element in observation.elements if element.password]
            try:
                plain: bytes | None = None
                if ocr or (include_shot and not marks):
                    plain = self.backend.capture(observation.window, redact)
                if ocr and plain:
                    try:
                        observation.ocr = self.backend.ocr(
                            plain, language=self.config.screen.ocr_language
                        )
                    except Exception:
                        observation.ocr = []
                if include_shot:
                    if marks:
                        try:
                            observation.shot = self.backend.capture(
                                observation.window,
                                redact,
                                [(element.id, element.bounds) for element in observation.elements],
                            )
                        except Exception:
                            observation.shot = plain
                    else:
                        observation.shot = plain
            except Exception:
                observation.shot = None
        self.last = observation
        return observation

    def ambient_text(self, max_chars: int = 400) -> str:
        """OCR text of the foreground window, for screen-level awareness.

        Reads and captures without touching the input clock: awareness is not a
        deliberate screen task, so it must not reset the idle guard the user's
        own input is measured against.
        """
        with self._lock:
            self._require_available()
            try:
                observation = self._observe(include_shot=False, mark=False, ocr=True)
                self._allowed(observation)
            except ScreenError:
                return ""
            rows = observation.ocr or []
            parts = [str(row.get("text") or "").strip() for row in rows]
            return " ".join(part for part in parts if part)[:max_chars]

    def _capture(self, observation: Observation) -> bytes | None:
        if not self.config.screen.capture:
            return None
        redact: Iterable[tuple[int, int, int, int]] = ()
        if self.config.screen.redact_passwords:
            redact = [element.bounds for element in observation.elements if element.password]
        try:
            return self.backend.capture(observation.window, redact)
        except Exception:
            return None

    # ------------------------------------------------------------------ clipboard

    def clipboard_read(self) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            observation = self._observe(include_shot=False, mark=False)
            self._allowed(observation)
            # Reading the clipboard injects nothing, so a mouse move is not a
            # reason to refuse it. Mark, then check: pause and panic still veto,
            # while the latched input clock stops vetoing a read-only step.
            self.activity.mark()
            self._guard()
            try:
                text = clipboard_mod.get_text()
            except Exception as exc:
                raise ScreenError(f"could not read the clipboard: {exc}") from exc
            self.activity.mark()
            return {"ok": True, "text": text[:20_000], "length": len(text)}

    def clipboard_write(self, text: str) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            observation = self._observe(include_shot=False, mark=False)
            self._allowed(observation)
            # Same contract as the read: replacing the clipboard is not input
            # injection, so it is not the user's takeover to veto.
            self.activity.mark()
            self._guard()
            value = str(text or "")
            if len(value) > 20_000:
                raise ScreenBlocked("clipboard text is too long")
            try:
                clipboard_mod.set_text(value)
            except Exception as exc:
                raise ScreenError(f"could not write the clipboard: {exc}") from exc
            self.activity.mark()
            return {"ok": True, "length": len(value)}

    # ------------------------------------------------------------------ act

    def act(
        self,
        action: str,
        *,
        element_id: str = "",
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        text: str = "",
        keys: str = "",
        amount: int = 0,
        x2: int | None = None,
        y2: int | None = None,
        expect: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        with self._lock:
            # Re-read the foreground window. A stale observation is how an
            # agent types into the wrong app after the user switches windows.
            observation = self._observe(include_shot=False, mark=False)
            self._allowed(observation)
            self._guard()

            element: Element | None = None
            if element_id:
                element = observation.by_id(str(element_id))
                if element is None:
                    raise ScreenBlocked(
                        f"{element_id} is not in the latest observation; observe again before acting"
                    )
                if element.password:
                    raise ScreenBlocked("refusing to act on a password field")
                if self._sensitive(element):
                    raise ScreenBlocked("refusing to act on a credential field")

            if action in ("click", "double_click", "move", "drag") and element is None:
                if x is None or y is None:
                    raise ScreenBlocked(f"{action} needs element_id or x and y")
                self._check_point(observation, x, y)
            if action == "drag":
                if x2 is None or y2 is None:
                    raise ScreenBlocked("drag needs x2 and y2 as the destination")
                self._check_point(observation, x2, y2)
            if action == "type_text" and not text:
                raise ScreenBlocked("type_text needs text")
            if action == "key":
                self._check_key_chord(keys)

            before = observation.digest
            if element is not None and element_id:
                x, y = element.center
            if action == "drag" and element is None and (x2 is None or y2 is None):
                raise ScreenBlocked("drag needs x2 and y2 as the destination")

            wants_visual = bool(expect and (expect.get("changed") or expect.get("pixels")))
            wants_diff = wants_visual and self.config.screen.pixel_diff
            before_png = self._capture(observation) if wants_diff else None
            retry_safe = action in ("click", "double_click", "scroll", "move")
            attempts_allowed = (
                1 + (self.config.screen.retries if retry_safe and wants_visual else 0)
            )
            last_error: ScreenVerifyError | None = None

            for attempt in range(1, attempts_allowed + 1):
                self._guard()
                self._perform(
                    action,
                    element=element,
                    x=x,
                    y=y,
                    x2=x2,
                    y2=y2,
                    button=button,
                    text=text,
                    keys=keys,
                    amount=amount,
                )
                self.activity.mark()
                after = self._settle()
                if action == "focus_window":
                    # Focus is only useful if it actually landed in an app she may
                    # touch; otherwise the next action would be aimed at a stranger.
                    self._allowed(after)
                changed_uia = after.digest != before
                after_png = self._capture(after) if before_png else None
                changed_pixels = pixel_diff.changed(
                    before_png,
                    after_png,
                    self.config.screen.pixel_threshold,
                )
                changed = changed_uia or changed_pixels
                changed_by = "uia" if changed_uia else ("pixels" if changed_pixels else "none")
                self.last = after
                try:
                    self._assert(after, expect or {}, changed, changed_pixels)
                except ScreenVerifyError as exc:
                    last_error = exc
                    if attempt < attempts_allowed and not changed:
                        # A click that produced neither a tree nor a pixel change
                        # gets two more chances; typing is never retried because
                        # a duplicate line is worse than a failed step.
                        self.activity.wait(0.15)
                        continue
                    raise

                result: dict[str, Any] = {
                    "ok": True,
                    "action": action,
                    "changed": changed,
                    "changedBy": changed_by,
                    "attempts": attempt,
                    "title": after.title,
                    "process": after.process,
                    "hash": after.digest,
                    "summary": after.summary(limit=40),
                }
                return result

            if last_error is not None:
                raise last_error
            raise ScreenVerifyError("the action produced no result", self.last or observation)

    def _perform(
        self,
        action: str,
        *,
        element: Element | None,
        x: int | None,
        y: int | None,
        x2: int | None,
        y2: int | None,
        button: str,
        text: str,
        keys: str,
        amount: int,
    ) -> None:
        if action == "click":
            self.input.click(int(x), int(y), button=button, double=False)  # type: ignore[arg-type]
        elif action == "double_click":
            self.input.click(int(x), int(y), button=button, double=True)  # type: ignore[arg-type]
        elif action == "move":
            self.input.move(int(x), int(y))  # type: ignore[arg-type]
        elif action == "drag":
            self.input.drag(int(x), int(y), int(x2), int(y2), button=button)  # type: ignore[arg-type]
        elif action == "scroll":
            self.input.scroll(int(amount), x, y)
        elif action == "type_text":
            if element is not None:
                self.input.click(element.center[0], element.center[1], button="left")
                # Our own click is not the user taking over.
                self.activity.mark()
            self._type_in_chunks(str(text))
        elif action == "key":
            if not keys:
                raise ScreenBlocked("key needs a chord such as ctrl+s")
            self.input.key(str(keys))
        elif action == "focus_window":
            if element is None or not element.handle:
                raise ScreenBlocked("focus_window needs the window element from an observation")
            self.input.focus_window(element.handle)
        else:
            raise ScreenBlocked(f"unsupported screen action {action!r}")

    # ------------------------------------------------------------------ guards

    def _require_available(self) -> None:
        if not self.config.screen.enabled:
            raise ScreenUnavailable("screen control is disabled in config")
        if not self.backend.available():
            raise ScreenUnavailable(self.backend.reason())

    def _block_patterns(self) -> list[str]:
        return [pattern.lower() for pattern in getattr(self.config.policy, "screen_block_apps", ())]

    def _allow_patterns(self) -> list[str]:
        return [pattern.lower() for pattern in getattr(self.config.policy, "screen_allow_apps", ())]

    def _process_allowed(self, process: str) -> bool:
        candidate = (process or "").strip().lower()
        if not candidate:
            return False
        stem = candidate[:-4] if candidate.endswith(".exe") else candidate
        exe = f"{stem}.exe"
        candidates = (candidate, stem, exe)

        block_patterns = self._block_patterns()
        if any(
            any(fnmatch.fnmatch(c, pattern) for pattern in block_patterns)
            for c in candidates
        ):
            return False

        allow_patterns = self._allow_patterns()
        if allow_patterns:
            return any(
                any(fnmatch.fnmatch(c, pattern) for pattern in allow_patterns)
                for c in candidates
            )

        return True

    def _allowed_process(self, process: str) -> None:
        candidate = (process or "").strip().lower()
        if not candidate:
            raise ScreenBlocked("cannot verify which app that window belongs to; screen control stays off")
        stem = candidate[:-4] if candidate.endswith(".exe") else candidate
        exe = f"{stem}.exe"
        candidates = (candidate, stem, exe)

        block_patterns = self._block_patterns()
        if any(
            any(fnmatch.fnmatch(c, pattern) for pattern in block_patterns)
            for c in candidates
        ):
            raise ScreenBlocked(
                f"{candidate} is in policy.screen_block_apps; screen control is blocked"
            )

        allow_patterns = self._allow_patterns()
        if allow_patterns and not any(
            any(fnmatch.fnmatch(c, pattern) for pattern in allow_patterns)
            for c in candidates
        ):
            raise ScreenBlocked(
                f"{candidate} is not in policy.screen_allow_apps; add it before I can touch it"
            )

    def _allowed(self, observation: Observation) -> None:
        self._allowed_process(observation.process)

    def _find_window(self, window_id: int) -> WindowInfo:
        try:
            handle = int(window_id)
        except (TypeError, ValueError) as exc:
            raise ScreenBlocked("window_id must be one of the ids from screen.windows") from exc
        if handle <= 0:
            raise ScreenBlocked("window_id must be one of the ids from screen.windows")
        for window in self.backend.windows():
            if window.handle == handle:
                return window
        raise ScreenBlocked("that window is gone; call screen.windows again")

    def _focus_window(self, window: WindowInfo, *, watch_idle: bool = True) -> dict[str, Any]:
        self._guard()
        try:
            self.input.focus_window(window.handle)
        except Exception as exc:
            raise ScreenBlocked(f"could not focus {window.title!r}: {exc}") from exc
        # Activating a freshly launched app can land on the input clock a
        # moment after SetForegroundWindow returns. Let that settle, then take
        # the baseline so it is not mistaken for the user taking over.
        self.activity.wait(0.15)
        self.activity.mark()
        # Bringing a window forward can move the shell's input clock while the
        # app is starting; panic still aborts, but startup noise is not a
        # user takeover. The next real action re-arms the idle guard.
        after = self._settle(watch_idle=watch_idle)
        self._allowed(after)
        if after.process.lower() != window.process.lower():
            raise ScreenBlocked(
                f"focus landed on {after.process or 'an unknown app'}, not {window.process}"
            )
        self.last = after
        return {
            "ok": True,
            "action": "focus_window",
            "changed": False,
            "window_id": window.handle,
            "title": after.title,
            "process": after.process,
            "hash": after.digest,
            "summary": after.summary(limit=40),
        }

    @staticmethod
    def _check_point(observation: Observation, x: int, y: int) -> None:
        left, top, right, bottom = observation.window
        if right <= left or bottom <= top:
            return
        if not (left <= int(x) <= right and top <= int(y) <= bottom):
            raise ScreenBlocked(
                f"coordinates ({x}, {y}) lie outside the allowlisted window {observation.window}"
            )

    @staticmethod
    def _check_key_chord(keys: str) -> None:
        parts = {part.strip().lower() for part in str(keys or "").split("+") if part.strip()}
        if not parts:
            return
        if parts & {"win", "lwin", "rwin", "meta", "super"}:
            raise ScreenBlocked("OS shortcut keys (Win) are blocked")
        if ("alt" in parts and "tab" in parts) or (
            "ctrl" in parts and "alt" in parts and ("del" in parts or "delete" in parts)
        ) or ("ctrl" in parts and "shift" in parts and "esc" in parts):
            raise ScreenBlocked(f"OS task-switch chord {keys!r} is blocked")

    @staticmethod
    def _sensitive(element: Element) -> bool:
        name = f"{element.name} {element.automation_id}".lower()
        return any(word in name for word in SENSITIVE_WORDS)

    def _guard(self) -> None:
        if self._paused:
            raise ScreenPaused(
                self._pause_reason or "screen control is paused; resume it from the tray"
            )
        if self.activity.panic():
            raise ScreenPaused("panic key held; input injection stopped")
        if self.activity.moved():
            raise ScreenPaused("you moved the mouse or typed; I stopped and left the desktop to you")

    def _type_in_chunks(self, text: str) -> None:
        # Small chunks keep the panic key and the idle watch between keystrokes.
        for start in range(0, len(text), 32):
            self._guard()
            self.input.type_text(text[start : start + 32])
            self.activity.mark()
            if start + 32 < len(text):
                self.activity.wait(0.02)

    def _settle(self, *, watch_idle: bool = True) -> Observation:
        deadline = time.monotonic() + self.config.screen.settle_ms / 1000
        stable = 0
        previous = ""
        latest: Observation | None = None
        while time.monotonic() < deadline:
            if self._paused:
                raise ScreenPaused(
                    self._pause_reason or "screen control is paused; resume it from the tray"
                )
            if self.activity.panic():
                raise ScreenPaused("panic key held; input injection stopped")
            if watch_idle and self.activity.moved():
                raise ScreenPaused("you moved the mouse or typed; I stopped and left the desktop to you")
            latest = self._observe(include_shot=False, mark=False)
            if latest.digest == previous:
                stable += 1
                if stable >= 2:
                    return latest
            else:
                stable = 0
                previous = latest.digest
            self.activity.wait(self.poll_s)
        if latest is None:
            latest = self._observe(include_shot=False, mark=False)
        return latest

    @staticmethod
    def _assert(
        observation: Observation,
        expect: dict[str, Any],
        changed: bool,
        pixels_changed: bool = False,
    ) -> None:
        title = str(expect.get("title") or "").strip()
        if title and title.lower() not in observation.title.lower():
            raise ScreenVerifyError(f"window title does not contain {title!r}", observation)
        name = str(expect.get("element") or "").strip()
        if name and observation.find(name) is None:
            raise ScreenVerifyError(f"no element matching {name!r} after the action", observation)
        if expect.get("pixels") and not pixels_changed:
            raise ScreenVerifyError("the pixels did not change", observation)
        if expect.get("changed") and not changed:
            raise ScreenVerifyError("the window state did not change", observation)
