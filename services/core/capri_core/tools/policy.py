"""Permission tiers.

The rule that keeps this honest: a tool declares what it *can* do, but the
policy decides what this call is allowed to do *right now*, based on the actual
arguments. ``fs.write`` to a new file in the workspace is not the same act as
overwriting your thesis.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..config import PolicyCfg
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
    "type ",
    "where ",
    "echo ",
    "git status",
    "git log",
    "git diff",
    "git branch",
    "python --version",
    "node --version",
    "uv --version",
    "tasklist",
    "whoami",
    "hostname",
)

# Never, regardless of approvals. The user can override the policy file, but the
# daemon will not volunteer these.
_BLOCKED_FRAGMENTS = (
    "format ",
    "diskpart",
    "bcdedit",
    "del /f",
    "del /s",
    "rd /s",
    "rmdir /s",
    "shutdown",
    "reg delete",
    "net user",
    "cipher /w",
    "vssadmin delete",
    "wevtutil cl",
)


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
    """Absolute, normalised path. Relative paths hang off the workspace."""
    expanded = os.path.expanduser(str(raw))
    if not os.path.isabs(expanded):
        expanded = os.path.join(workspace, expanded)
    return os.path.abspath(expanded)


def _within(child: str, root: str) -> bool:
    child_n = _normcase(child)
    root_n = _normcase(root).rstrip(os.sep)
    return child_n == root_n or child_n.startswith(root_n + os.sep)


def _matches_glob(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    pattern_l = pattern.replace("\\", "/").lower()
    return fnmatch.fnmatch(lowered, pattern_l) or fnmatch.fnmatch(
        os.path.basename(lowered), pattern_l
    )


class Policy:
    def __init__(self, cfg: PolicyCfg, workspace: str) -> None:
        self.cfg = cfg
        self.workspace = workspace

    # ------------------------------------------------------------------ paths

    def classify_path(self, path: str) -> tuple[Verdict, str]:
        for entry in self.cfg.deny_paths:
            # Deny entries are either a directory prefix or a glob.
            if _within(path, entry) or _matches_glob(path, entry):
                return BLOCK, "path is on the deny list"
        if any(_within(path, root) for root in self.cfg.allow_paths):
            return ALLOW, "path is inside an allowlisted root"
        return CONFIRM, "path is outside the allowlisted roots"

    # ------------------------------------------------------------------ tools

    def check(self, spec: ToolSpec, args: dict[str, Any]) -> Decision:
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

    def _check_fs_write(self, args: dict[str, Any]) -> Decision:
        path = resolve_path(str(args.get("path", "")), self.workspace)
        verdict, reason = self.classify_path(path)
        if verdict == BLOCK:
            return Decision(BLOCK, 3, reason)
        exists = os.path.exists(path)
        mode = str(args.get("mode") or "overwrite")
        if mode == "append":
            return Decision(CONFIRM, 2, "appending modifies an existing file")
        if not exists and verdict == ALLOW:
            return Decision(ALLOW, 1, "new file inside an allowlisted root")
        if not exists:
            return Decision(CONFIRM, 2, reason)
        return Decision(CONFIRM, 2, "overwriting an existing file")

    def _check_shell_run(self, args: dict[str, Any]) -> Decision:
        command = str(args.get("command") or "").strip()
        lowered = command.lower()
        if any(fragment in lowered for fragment in _BLOCKED_FRAGMENTS):
            return Decision(BLOCK, 3, "command matches the destructive blocklist")
        if lowered.startswith(_BENIGN_PREFIXES):
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
