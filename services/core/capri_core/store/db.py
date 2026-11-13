"""SQLite shard: connection, migrations, tracing.

One file holds everything Capri knows. WAL mode, foreign keys on, migrations
applied idempotently and recorded in ``schema_migrations``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import paths

log = logging.getLogger("capri.store")

SQL_DIR = Path(__file__).parent / "sql"
_MIGRATION_RE = re.compile(r"^(\d+)_(.+)\.sql$")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or paths.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
    payload: Any = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    ms: int | None = None,
) -> int:
    """Append one trace row. Traces are the replay log for the whole harness."""
    blob = json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None
    cur = conn.execute(
        "INSERT INTO traces (turn_id, span, name, payload, tokens_in, tokens_out, ms, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (turn_id, span, name, blob, tokens_in, tokens_out, ms, int(time.time() * 1000)),
    )
    return int(cur.lastrowid or 0)


def recent_traces(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, turn_id, span, name, payload, tokens_in, tokens_out, ms, created_at"
        " FROM traces ORDER BY id DESC LIMIT ?",
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


def open_shard(subject: str = paths.DEFAULT_SUBJECT) -> sqlite3.Connection:
    """Open (and migrate) the shard for a subject."""
    from .vector import ensure_vector_tables

    conn = connect(paths.db_path(subject))
    version = migrate(conn)
    backend = ensure_vector_tables(conn)
    set_meta(conn, "schema_version", str(version))
    set_meta(conn, "vector_backend", backend)
    return conn
