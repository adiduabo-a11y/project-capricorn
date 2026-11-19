"""SQLite shard: connection, migrations, tracing.

One file holds everything Capri knows. WAL mode, foreign keys on, migrations
applied idempotently and recorded in ``schema_migrations``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import paths

log = logging.getLogger("capri.store")

SQL_DIR = Path(__file__).parent / "sql"
_MIGRATION_RE = re.compile(r"^(\d+)_(.+)\.sql$")
_FALLBACK_LOCKS: dict[int, threading.RLock] = {}
_FALLBACK_GUARD = threading.Lock()


class LockedConnection(sqlite3.Connection):
    """SQLite connection carrying its own re-entrant mutex for cross-thread safety."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._capri_lock = threading.RLock()


def lock_for(conn: sqlite3.Connection) -> Any:
    """Return the re-entrant lock protecting ``conn`` across threads."""
    lock = getattr(conn, "_capri_lock", None)
    if lock is not None:
        return lock
    key = id(conn)
    with _FALLBACK_GUARD:
        existing = _FALLBACK_LOCKS.get(key)
        if existing is None:
            existing = threading.RLock()
            _FALLBACK_LOCKS[key] = existing
        return existing


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialize and wrap multi-statement writes on ``conn`` in an atomic transaction."""
    with lock_for(conn):
        started = not conn.in_transaction
        if started:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if started and conn.in_transaction:
                conn.rollback()
            raise
        else:
            if started and conn.in_transaction:
                conn.commit()


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or paths.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        target,
        isolation_level=None,
        check_same_thread=False,
        factory=LockedConnection,
    )
    conn.row_factory = sqlite3.Row
    # Another process holding the write lock is a wait, not an immediate
    # "database is locked" in the middle of a turn.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrations() -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for file in sorted(SQL_DIR.glob("*.sql")):
        match = _MIGRATION_RE.match(file.name)
        if not match:
            continue
        found.append((int(match.group(1)), match.group(2), file.read_text(encoding="utf-8")))
    found.sort(key=lambda item: item[0])
    return found


def latest_schema_version() -> int:
    """The newest migration shipped with this build."""
    found = _migrations()
    return found[-1][0] if found else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    current = 0
    for version, name, sql in _migrations():
        if version in applied:
            current = max(current, version)
            continue
        log.info("applying migration %03d_%s", version, name)
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, int(time.time() * 1000)),
        )
        current = max(current, version)
    return current


def trace(
    conn: sqlite3.Connection,
    *,
    name: str,
    span: str = "tool",
    turn_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    payload: Any = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    ms: int | None = None,
) -> int:
    """Append one trace row. Traces are the replay log for the whole harness.

    ``workspace_id`` and ``task_id`` are recorded from Milestone 0 onward, even
    while they are empty: retrofitting them later would mean a migration and a
    trace-viewer change landing together.
    """
    blob = json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None
    with lock_for(conn):
        cur = conn.execute(
            "INSERT INTO traces (turn_id, workspace_id, task_id, span, name, payload,"
            " tokens_in, tokens_out, ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                workspace_id or None,
                task_id or None,
                span,
                name,
                blob,
                tokens_in,
                tokens_out,
                ms,
                int(time.time() * 1000),
            ),
        )
        return int(cur.lastrowid or 0)


def recent_traces(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, turn_id, workspace_id, task_id, span, name, payload, tokens_in,"
        " tokens_out, ms, created_at FROM traces ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(row) for row in rows]


def record_tool_stat(
    conn: sqlite3.Connection,
    name: str,
    *,
    ok: bool,
    ms: int,
    version: str | None = None,
) -> int:
    """Update the live tool-health row and return the current failure streak."""
    row = conn.execute(
        "SELECT calls, ok, avg_ms, fail_streak FROM tool_stats WHERE name = ?", (name,)
    ).fetchone()
    calls = int(row["calls"]) if row else 0
    successes = int(row["ok"]) if row else 0
    average = float(row["avg_ms"]) if row and row["avg_ms"] is not None else 0.0
    streak = int(row["fail_streak"]) if row else 0
    new_calls = calls + 1
    new_ok = successes + (1 if ok else 0)
    new_average = (average * calls + max(0, int(ms))) / new_calls
    new_streak = 0 if ok else streak + 1
    conn.execute(
        "INSERT INTO tool_stats (name, calls, ok, avg_ms, fail_streak, version, quarantined)"
        " VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT quarantined FROM tool_stats WHERE name = ?), 0))"
        " ON CONFLICT(name) DO UPDATE SET"
        " calls = excluded.calls, ok = excluded.ok, avg_ms = excluded.avg_ms,"
        " fail_streak = excluded.fail_streak,"
        " version = COALESCE(excluded.version, tool_stats.version)",
        (
            name,
            new_calls,
            new_ok,
            new_average,
            new_streak,
            version,
            name,
        ),
    )
    return new_streak


def set_tool_quarantined(conn: sqlite3.Connection, name: str, value: bool) -> None:
    conn.execute(
        "INSERT INTO tool_stats (name, calls, ok, avg_ms, fail_streak, quarantined)"
        " VALUES (?, 0, 0, 0, 0, ?)"
        " ON CONFLICT(name) DO UPDATE SET quarantined = excluded.quarantined",
        (name, 1 if value else 0),
    )


def tool_stats(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, calls, ok, avg_ms, fail_streak, version, quarantined"
        " FROM tool_stats ORDER BY calls DESC, name LIMIT ?",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(row) for row in rows]


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def open_shard(subject: str = paths.DEFAULT_SUBJECT, dim: int = 1024) -> sqlite3.Connection:
    """Open (and migrate) the shard for a subject."""
    from .vector import ensure_vector_tables

    conn = connect(paths.db_path(subject))
    version = migrate(conn)
    backend = ensure_vector_tables(conn, dim)
    set_meta(conn, "schema_version", str(version))
    set_meta(conn, "vector_backend", backend)
    return conn
