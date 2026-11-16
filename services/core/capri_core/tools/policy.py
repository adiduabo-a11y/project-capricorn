"""Permission tiers.

The rule that keeps this honest: a tool declares what it *can* do, but the
policy decides what this call is allowed to do *right now*, based on the actual
arguments. ``fs.write`` to a new file in the workspace is not the same act as
overwriting your thesis.

Trust has three layers, in precedence order:

1. deny paths - always block, read or write;
2. the active workspace - opening it grants write trust;
3. allow paths - ambient read roots whose writes still confirm.

Paths are canonicalized before comparison, so a junction cannot move a file out
of the workspace after the check.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from ..config import PolicyCfg
from ..pathsafe import denied, matches_deny, resolve, within
from .base import ToolSpec

Verdict = Literal["allow", "confirm", "block"]

TIER_NAMES = {
    0: "observe",
    1: "act.benign",
    2: "act.destructive",
    3: "act.blocked",
    4: "blackwall",
}

# Commands that only read. Anything else gets a confirmation prompt.
_BENIGN_PREFIXES = (
    "dir",
    "type",
    "where",
    "echo",
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git show",
    "git ls-files",
    "git rev-parse",
    "python --version",
    "node --version",
    "uv --version",
    "tasklist",
    "whoami",
    "hostname",
    "rg",
)

# Never, regardless of approvals. Matched against the first token(s) of every
# command in the line, not as a substring: ``dotnet format`` and
# ``git diff --format=%H`` are legitimate commands.
_BLOCKED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("format",),
    ("diskpart",),
    ("bcdedit",),
    ("shutdown",),
    ("reg", "delete"),
    ("net", "user"),
    ("cipher", "/w"),
    ("vssadmin", "delete"),
    ("wevtutil", "cl"),
    ("rd", "/s"),
    ("rmdir", "/s"),
    ("del", "/s"),
    ("del", "/f"),
)
_DESTRUCTIVE_DELETE = ("del", "rd", "rmdir")

# Read-only prefixes that still have a flag which writes or executes.
_BENIGN_FLAG_DENIES: dict[str, frozenset[str]] = {
    "git diff": frozenset({"--output"}),
    "git log": frozenset({"--output"}),
    "git show": frozenset({"--output"}),
    "rg": frozenset({"--pre", "--pre-glob"}),
}

# ``git branch`` mutates the ref store; only its listing forms are read-only.
_GIT_BRANCH_WRITES = frozenset(
    {
        "-d",
        "-m",
        "-c",
        "-f",
        "-u",
        "--delete",
        "--move",
        "--copy",
        "--force",
        "--set-upstream-to",
        "--unset-upstream",
        "--edit-description",
        "--track",
        "--no-track",
        "--create-reflog",
        "--quiet",
    }
)
_GIT_BRANCH_LISTING = frozenset(
    {
        "-a",
        "-r",
        "-v",
        "-l",
        "--all",
        "--remotes",
        "--verbose",
        "--list",
        "--contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--sort",
        "--format",
        "--show-current",
    }
)

# Targets that swallow output instead of writing a file.
_DISCARD_TARGETS = frozenset({"nul", "/dev/null", "$null", "/dev/stdout", "/dev/stderr"})

_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    tier: int
    reason: str


ALLOW = "allow"
CONFIRM = "confirm"
BLOCK = "block"


def _normcase(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def resolve_path(raw: str, workspace: str) -> str:
    """Canonical absolute path. Relative paths hang off the workspace."""
    return resolve(raw, workspace)


def _within(child: str, root: str) -> bool:
    return within(child, root)


def _matches_glob(path: str, pattern: str) -> bool:
    return matches_deny(path, pattern)


def _segments(command: str) -> list[str]:
    """Split a command line on ``;``, ``&``, ``&&``, ``||`` and pipes, quote-aware.

    A single ``&`` chains commands in cmd.exe and backgrounds them in POSIX
    shells, so it separates commands for policy purposes too. ``2>&1`` is a
    file-descriptor duplication, not a separator.
    """
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            buf.append(char)
            index += 1
            continue
        pair = command[index : index + 2]
        if pair in ("&&", "||"):
            out.append("".join(buf))
            buf = []
            index += 2
            continue
        if char == "&":
            previous = command[index - 1] if index else ""
            following = command[index + 1] if index + 1 < len(command) else ""
            if previous == ">" or (
                previous.isdigit() and (following.isdigit() or following == "-")
            ):
                buf.append(char)  # fd duplication: 2>&1, >&2
                index += 1
                continue
            out.append("".join(buf))
            buf = []
            index += 1
            continue
        if char in ";|\n\r":
            out.append("".join(buf))
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    out.append("".join(buf))
    return [part.strip() for part in out if part.strip()]


def _tokens(segment: str) -> list[str]:
    values: list[str] = []
    for raw in _TOKEN.findall(segment):
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        values.append(raw)
    return values


def _strip_wrapper(tokens: list[str]) -> list[str]:
    """Look past ``cmd /c`` and ``powershell -Command``, which are just runners."""
    while tokens:
        first = tokens[0].lower()
        if first == "&":
            tokens = tokens[1:]
            continue
        if first in ("cmd", "cmd.exe") and len(tokens) > 1 and tokens[1].lower() in (
            "/c",
            "/k",
        ):
            tokens = tokens[2:]
            continue
        if first in (
            "powershell",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
        ) and len(tokens) > 1 and tokens[1].lower() in ("-command", "-c"):
            tokens = tokens[2:]
            continue
        break
    return tokens


def _blocked_segment(segment: str) -> bool:
    tokens = _strip_wrapper(_tokens(segment))
    if not tokens:
        return False
    lowered = [token.lower() for token in tokens]
    for pattern in _BLOCKED_COMMANDS:
        if tuple(lowered[: len(pattern)]) == pattern:
            return True
    # Flags after other flags still mean the same recursive/forced delete.
    if lowered[0] in _DESTRUCTIVE_DELETE and any(
        token in ("/f", "/s") for token in lowered[1:]
    ):
        return True
    return False


def _command_blocked(command: str) -> bool:
    return any(_blocked_segment(segment) for segment in _segments(command))


def _starts_command(lowered: str, prefix: str) -> bool:
    return (
        lowered == prefix
        or lowered.startswith(prefix + " ")
        or lowered.startswith(prefix + "\t")
    )


def _flag_name(token: str) -> str:
    return token.split("=", 1)[0].strip().lower()


def _redirect_writes(segment: str) -> bool:
    """True when an unquoted redirection sends output to a real file.

    ``>nul``/``>/dev/null`` and fd duplications such as ``2>&1`` do not write,
    but ``echo hi > notes.txt`` must not pass as a read-only inspection.
    """
    quote = ""
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char != ">":
            index += 1
            continue
        rest = segment[index + 1 :].lstrip()
        if rest.startswith("&"):  # fd duplication: >&2
            index += 1
            continue
        if rest.startswith(">"):
            rest = rest[1:].lstrip()
        found = _TOKEN.findall(rest)
        target = found[0].strip("'\"") if found else ""
        if target and target.lower() not in _DISCARD_TARGETS:
            return True
        if not target:
            return True  # a dangling redirect is not a read-only command
        index += 1
    return False


def _git_branch_listing(tokens: list[str]) -> bool:
    """``git branch`` is read-only only when it lists branches."""
    rest = tokens[2:]
    flags = {_flag_name(token) for token in rest if token.startswith("-")}
    if flags & _GIT_BRANCH_WRITES:
        return False
    words = [token for token in rest if not token.startswith("-")]
    if not words:
        return True
    # A bare word creates (or renames) a branch; only listing filters take one.
    return bool(flags & _GIT_BRANCH_LISTING)


def _benign_args(prefix: str, tokens: list[str]) -> bool:
    """Reject the flags that turn an otherwise read-only command into a write."""
    flags = {_flag_name(token) for token in tokens[1:] if token.startswith("-")}
    denied = _BENIGN_FLAG_DENIES.get(prefix)
    if denied and flags & denied:
        return False
    if prefix == "git branch":
        return _git_branch_listing(tokens)
    return True


def _benign_segment(segment: str) -> bool:
    stripped = segment.strip()
    if not stripped:
        return True
    tokens = _strip_wrapper(_tokens(stripped))
    if not tokens:
        return False
    lowered = " ".join(token.lower() for token in tokens)
    prefix = next((item for item in _BENIGN_PREFIXES if _starts_command(lowered, item)), "")
    if not prefix:
        return False
    if _redirect_writes(stripped):
        return False  # the redirect target is a file write, not an inspection
    return _benign_args(prefix, tokens)


def _command_benign(command: str) -> bool:
    segments = _segments(command)
    return bool(segments) and all(_benign_segment(segment) for segment in segments)


class Policy:
    def __init__(self, cfg: PolicyCfg, workspace: str) -> None:
        self.cfg = cfg
        self.workspace = resolve(workspace)
        # Set by the daemon once the screen service exists, so an element's
        # visible name can lend its tier to the call.
        self.screen = None
        # True only when a project was opened (or restored) in this process.
        # A bare ``policy.workspace`` is a path default, not a trust grant.
        self.workspace_active = False
        self.workspace_trusted = True
        self._detected_root = ""
        self._detected_commands: set[str] = set()

    @property
    def profile(self) -> str:
        """The active persona profile, read by the prompt and screen policy."""
        return self.cfg.profile

    # ------------------------------------------------------------------ workspace

    def set_workspace(
        self, root: str, *, active: bool, trusted: bool = True
    ) -> None:
        self.workspace = resolve(root)
        self.workspace_active = bool(active)
        self.workspace_trusted = bool(trusted)
        self.clear_detected_commands()

    def set_detected_commands(self, root: str, commands: list[str]) -> None:
        self._detected_root = resolve(root)
        self._detected_commands = {
            str(command).strip() for command in commands if str(command).strip()
        }

    def clear_detected_commands(self) -> None:
        self._detected_root = ""
        self._detected_commands = set()

    # ------------------------------------------------------------------ paths

    def classify_path(self, path: str) -> tuple[Verdict, str]:
        canonical = resolve(path)
        if denied(canonical, self.cfg.deny_paths):
            return BLOCK, "path is on the deny list"
        if any(_within(canonical, root) for root in self.cfg.allow_paths):
            return ALLOW, "path is inside an allowlisted root"
        return CONFIRM, "path is outside the allowlisted roots"

    def in_workspace(self, path: str) -> bool:
        return self.workspace_active and _within(path, self.workspace)

    # ------------------------------------------------------------------ tools

    def check(self, spec: ToolSpec, args: dict[str, Any]) -> Decision:
        # Silent is a posture, not just a tone: observing still works, but she
        # does not touch the desktop until the profile changes back.
        if self.cfg.profile == "silent" and spec.name.startswith("screen.") and spec.tier >= 1:
            return Decision(BLOCK, 1, "policy.profile is silent; screen control is paused")
        handler = getattr(self, f"_check_{spec.name.replace('.', '_')}", None)
        if handler is not None:
            return handler(args)
        # Unknown tools inherit their declared tier, which is the safe default.
        if spec.tier >= 3:
            return Decision(BLOCK, spec.tier, "tool is blocked by its own manifest")
        if spec.tier == 2:
            return Decision(CONFIRM, spec.tier, "tool declares destructive effects")
        return Decision(ALLOW, spec.tier, "tool declares no side effects")

    def _check_fs_read(self, args: dict[str, Any]) -> Decision:
        path = resolve_path(str(args.get("path", "")), self.workspace)
        verdict, reason = self.classify_path(path)
        if verdict == BLOCK:
            return Decision(BLOCK, 3, reason)
        return Decision(ALLOW, 0, "reading is free")

    def _check_fs_list(self, args: dict[str, Any]) -> Decision:
        path = resolve_path(str(args.get("path", "")), self.workspace)
        verdict, reason = self.classify_path(path)
        if verdict == BLOCK:
            return Decision(BLOCK, 3, reason)
        return Decision(ALLOW, 0, "listing is free")

    def _check_fs_grep(self, args: dict[str, Any]) -> Decision:
        raw = str(args.get("path") or "")
        path = resolve_path(raw or self.workspace, self.workspace)
        verdict, reason = self.classify_path(path)
        if verdict == BLOCK:
            return Decision(BLOCK, 3, reason)
        return Decision(ALLOW, 0, "searching is free")

    def _check_fs_write(self, args: dict[str, Any]) -> Decision:
        path = resolve_path(str(args.get("path", "")), self.workspace)
        verdict, reason = self.classify_path(path)
        if verdict == BLOCK:
            return Decision(BLOCK, 3, reason)

        # The workspace check comes before "file exists": inside a trusted
        # project an overwrite is the normal act, not a destructive one.
        if self.in_workspace(path):
            if not self.workspace_trusted:
                return Decision(
                    CONFIRM,
                    2,
                    "this project reopened read-only; open it again to trust edits",
                )
            if self.cfg.workspace_edits == "allow":
                return Decision(ALLOW, 1, "inside the active workspace")
            return Decision(CONFIRM, 2, "workspace_edits is confirm")

        exists = os.path.exists(path)
        mode = str(args.get("mode") or "overwrite")
        if mode == "append":
            return Decision(CONFIRM, 2, "appending modifies an existing file")
        if not exists and verdict == ALLOW:
            return Decision(ALLOW, 1, "new file inside an allowlisted root")
        if not exists:
            return Decision(CONFIRM, 2, reason)
        return Decision(CONFIRM, 2, "overwriting an existing file")

    def _check_fs_patch(self, args: dict[str, Any]) -> Decision:
        path = resolve_path(str(args.get("path", "")), self.workspace)
        verdict, reason = self.classify_path(path)
        if verdict == BLOCK:
            return Decision(BLOCK, 3, reason)
        if self.in_workspace(path):
            if not self.workspace_trusted:
                return Decision(
                    CONFIRM,
                    2,
                    "this project reopened read-only; open it again to trust edits",
                )
            if self.cfg.workspace_edits == "allow":
                return Decision(ALLOW, 1, "inside the active workspace")
            return Decision(CONFIRM, 2, "workspace_edits is confirm")
        return Decision(CONFIRM, 2, "patching a file outside the active workspace")

    def _check_fs_undo(self, args: dict[str, Any]) -> Decision:
        # Undo writes a whole file. Even inside a trusted project it is worth a
        # card: the user is choosing which earlier state wins.
        return Decision(CONFIRM, 2, "undo rewrites a file; confirm the restore")

    def _check_screen_act(self, args: dict[str, Any]) -> Decision:
        action = str(args.get("action") or "").strip().lower()
        target = str(args.get("target_name") or "").strip().lower()
        element_id = str(args.get("element_id") or "").strip()
        if element_id and self.screen is not None:
            observation = getattr(self.screen, "last", None)
            element = observation.by_id(element_id) if observation is not None else None
            if element is not None:
                # The real control wins over a model-supplied label.
                target = element.name.lower()
        destructive = (
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
        if any(word in target for word in destructive):
            return Decision(CONFIRM, 2, f"{action} on a target that looks destructive")
        return Decision(ALLOW, 1, f"benign screen action in an allowlisted app")

    def _check_screen_clipboard(self, args: dict[str, Any]) -> Decision:
        mode = str(args.get("mode") or "").strip().lower()
        if mode == "read":
            return Decision(
                CONFIRM,
                2,
                "reading the clipboard can expose a password or token; confirm first",
            )
        return Decision(ALLOW, 1, "replacing the clipboard is reversible")

    def _check_screen_focus(self, args: dict[str, Any]) -> Decision:
        return Decision(ALLOW, 1, "focusing an allowlisted window is benign")

    def _check_screen_launch(self, args: dict[str, Any]) -> Decision:
        raw = str(args.get("app") or "").strip()
        name = raw.lower()
        if (
            not name
            or os.path.basename(raw).lower() != name
            or "/" in raw
            or "\\" in raw
        ):
            return Decision(BLOCK, 3, "launch takes an allowlisted executable name, not a path")
        patterns = [pattern.lower() for pattern in self.cfg.screen_allow_apps]
        if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            return Decision(BLOCK, 3, f"{name} is not in policy.screen_allow_apps")
        return Decision(ALLOW, 1, f"launch of allowlisted {name}")

    def is_detected_command(self, command: str, cwd: str = "") -> bool:
        """True for an exact command the detector read out of the project."""
        if not self._detected_commands or not self._detected_root:
            return False
        if cwd and _normcase(cwd) != _normcase(self._detected_root):
            return False
        return str(command).strip() in self._detected_commands

    def _check_shell_run(self, args: dict[str, Any]) -> Decision:
        command = str(args.get("command") or "").strip()
        if not command:
            return Decision(CONFIRM, 2, "empty shell command")
        # The blocklist is absolute: a manifest cannot declare `format C:` a
        # project command and have the allowlist override this.
        if _command_blocked(command):
            return Decision(BLOCK, 3, "command matches the destructive blocklist")

        raw_cwd = str(args.get("cwd") or "").strip()
        cwd = resolve_path(raw_cwd, self.workspace) if raw_cwd else self.workspace
        if self.is_detected_command(command, cwd):
            return Decision(ALLOW, 1, "detected project command")

        if _command_benign(command):
            return Decision(ALLOW, 1, "read-only inspection command")
        return Decision(CONFIRM, 2, "arbitrary shell command")

    # ------------------------------------------------------------------ apply

    def apply_confirm_prefs(self, decision: Decision) -> Decision:
        if decision.verdict != CONFIRM:
            return decision
        if decision.tier >= 3:
            return decision
        if self.cfg.confirm.destructive == "never":
            return Decision(ALLOW, decision.tier, decision.reason + " (auto-approved)")
        return decision
