"""Bounded project trees.

One walker for ``fs.list``, ``fs.grep`` and the workspace panel. It excludes
build output, honours a root ``.gitignore``, and never follows a symlink or a
Windows junction: ``os.walk(followlinks=False)`` still follows junctions, so
the walk has to check them itself.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Iterable

from ..pathsafe import denied, resolve, within

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        ".next",
        ".turbo",
    }
)

MAX_TREE_DEPTH = 6
MAX_TREE_ENTRIES = 500


def is_link(path: Path) -> bool:
    """Symlink or directory junction."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    checker = getattr(os.path, "isjunction", None)
    if checker is None:
        return False
    try:
        return bool(checker(path))
    except OSError:
        return True


def gitignore_rules(root: Path) -> list[str]:
    """Root-level .gitignore, approximated.

    Negations are ignored and only the root file is read. That is enough to
    keep a walk out of build output; it is not a full gitignore implementation
    and does not pretend to be.
    """
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rules: list[str] = []
    for line in text.splitlines():
        line = line.strip().replace("\\", "/")
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        rules.append(line.lstrip("./").rstrip("/"))
    return rules


def is_ignored(rel: str, name: str, rules: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    for pattern in rules:
        if not pattern:
            continue
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        if rel.startswith(pattern + "/"):
            return True
    return False


def walk_tree(
    root: str | Path,
    start: str | Path | None = None,
    *,
    depth: int = 3,
    deny_paths: Iterable[str] = (),
    max_depth: int = MAX_TREE_DEPTH,
    max_entries: int = MAX_TREE_ENTRIES,
) -> dict[str, Any]:
    """Collect workspace-relative entries, directories first.

    ``depth`` counts levels below ``start``; 1 lists direct children only.
    Raising it above ``max_depth`` is clamped rather than refused.
    """
    root_path = Path(resolve(root))
    start_path = Path(resolve(start, str(root_path))) if start else root_path
    if not within(str(start_path), str(root_path)):
        raise ValueError("path is outside the workspace root")
    if not start_path.exists():
        raise ValueError("path does not exist")
    if not start_path.is_dir():
        raise ValueError("path is not a directory")

    levels = max(1, min(int(depth or 3), max_depth))
    rules = gitignore_rules(root_path)
    deny = list(deny_paths)
    entries: list[dict[str, Any]] = []
    truncated = False

    def visit(directory: Path, level: int) -> None:
        nonlocal truncated
        if len(entries) >= max_entries:
            truncated = True
            return
        try:
            children = sorted(
                os.scandir(directory),
                key=lambda item: (
                    not item.is_dir(follow_symlinks=False),
                    item.name.lower(),
                ),
            )
        except OSError:
            return

        for item in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            if item.name in EXCLUDED_DIRS:
                continue
            path = Path(item.path)
            if is_link(path):
                continue
            rel = os.path.relpath(item.path, root_path).replace(os.sep, "/")
            if is_ignored(rel, item.name, rules):
                continue
            if deny and denied(item.path, deny):
                continue
            is_dir = item.is_dir(follow_symlinks=False)
            entries.append(
                {
                    "path": rel,
                    "name": item.name,
                    "dir": is_dir,
                    "depth": level,
                }
            )
            if is_dir and level + 1 < levels:
                visit(path, level + 1)

    visit(start_path, 0)
    return {
        "root": str(root_path),
        "path": os.path.relpath(start_path, root_path).replace(os.sep, "/")
        if start_path != root_path
        else "",
        "entries": entries,
        "truncated": truncated,
        "maxEntries": max_entries,
    }


def render_tree(result: dict[str, Any], *, limit: int = 220) -> str:
    """A compact indented view for the planner."""
    entries = result.get("entries") or []
    lines: list[str] = []
    for entry in entries[:limit]:
        indent = "  " * int(entry.get("depth") or 0)
        suffix = "/" if entry.get("dir") else ""
        lines.append(f"{indent}{entry.get('name')}{suffix}")
    if result.get("truncated") or len(entries) > limit:
        lines.append(
            f"[showing {min(len(entries), limit)} of {len(entries)} entries; "
            "narrow the path or depth]"
        )
    return "\n".join(lines) or "(empty directory)"
