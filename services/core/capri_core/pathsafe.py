"""Canonical paths and root containment.

Every trust decision in the harness comes down to "is this path really inside
that root", and on Windows a path is not its string: junctions and symlinks can
move it somewhere else after the comparison. This module is the one place that
resolves a path before anyone compares it.

``resolve()`` also handles a path that does not exist yet. The nearest existing
parent is canonicalized and the remaining names are appended, so a new file in
a junction's target cannot smuggle itself into the workspace.
"""

from __future__ import annotations

import fnmatch
import os

_GLOB_CHARS = frozenset("*?[")


def normcase(path: str) -> str:
    """Absolute, case-normalized path for comparisons (no disk access)."""
    return os.path.normcase(os.path.abspath(path))


def resolve(raw: str | os.PathLike[str], base: str = "") -> str:
    """Canonical absolute path.

    ``base`` anchors a relative path. Existing components are resolved with
    ``realpath`` (junctions and symlinks included); a missing tail is kept and
    appended to the canonical parent.
    """
    text = os.path.expanduser(os.fspath(raw))
    if base and not os.path.isabs(text):
        text = os.path.join(base, text)
    absolute = os.path.abspath(text)

    anchor = absolute
    tail: list[str] = []
    while not os.path.exists(anchor):
        parent = os.path.dirname(anchor)
        if parent == anchor:
            break
        tail.append(os.path.basename(anchor))
        anchor = parent

    real = os.path.realpath(anchor)
    if tail:
        return os.path.join(real, *reversed(tail))
    return real


def within(child: str, root: str) -> bool:
    """True when ``child`` is ``root`` or below it, after canonicalization."""
    child_n = normcase(resolve(child))
    root_n = normcase(resolve(root)).rstrip(os.sep)
    return child_n == root_n or child_n.startswith(root_n + os.sep)


def matches_deny(path: str, entry: str) -> bool:
    """One deny entry: a directory root, an absolute path, or a glob."""
    text = str(entry or "").strip()
    if not text:
        return False
    pattern = text.replace("\\", "/")
    if _GLOB_CHARS.intersection(pattern):
        lowered = resolve(path).replace("\\", "/").lower()
        pat = pattern.lower()
        return fnmatch.fnmatch(lowered, pat) or fnmatch.fnmatch(
            os.path.basename(lowered), pat
        )
    return within(path, text)


def denied(path: str, deny_paths: list[str] | tuple[str, ...]) -> bool:
    return any(matches_deny(path, entry) for entry in deny_paths)
