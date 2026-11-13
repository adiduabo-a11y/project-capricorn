"""Filesystem locations for Capri's local state.

Everything lives under ``CAPRI_HOME`` (default ``~/.capri``) so a user can move
or back up the whole harness by copying one directory.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DEFAULT_SUBJECT = "default"


def capri_home() -> Path:
    raw = os.environ.get("CAPRI_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".capri"


def run_dir() -> Path:
    return capri_home() / "run"


def logs_dir() -> Path:
    return capri_home() / "logs"


def shards_dir() -> Path:
    return capri_home() / "shards"


def config_path() -> Path:
    return capri_home() / "config.yaml"


def packs_dir() -> Path:
    """Repo ``packs/`` directory, or ``CAPRI_PACKS`` when set.

    Persona and sprite packs are data: they are edited far more often than code,
    so they live outside the Python package.
    """
    override = os.environ.get("CAPRI_PACKS")
    if override:
        return Path(override).expanduser()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packs"
        if candidate.is_dir():
            return candidate
    return Path.cwd() / "packs"


def db_path(subject: str = DEFAULT_SUBJECT) -> Path:
    return shards_dir() / subject / "capri.db"


def ensure_dirs() -> None:
    for d in (capri_home(), run_dir(), logs_dir(), shards_dir()):
        d.mkdir(parents=True, exist_ok=True)


def token_path() -> Path:
    return run_dir() / "token"


def token(create: bool = True) -> str:
    """Loopback auth token. Generated once, reused across restarts."""
    ensure_dirs()
    path = token_path()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    if not create:
        return ""
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="utf-8")
    try:  # best effort on Windows; POSIX-only otherwise
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value
