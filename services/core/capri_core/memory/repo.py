"""Shard-level CRUD for memories, entities and edges.

Everything above this module works with plain dicts; nothing outside needs to
know the column order. Soft deletion everywhere: rows are recoverable until a
shard export drops them.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

KINDS = ("fact", "preference", "event", "relationship", "rule")
_WORD = re.compile(r"[a-z0-9]+")

_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "it", "this", "that", "i", "you",
    "my", "me", "we", "do", "does", "did", "so", "if", "at", "as", "by", "from",
    "what", "which", "who", "how", "when", "where", "about",
}


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- write


def insert(
    conn: sqlite3.Connection,
    subject: str,
    *,
    kind: str,
    text: str,
    importance: float = 0.5,
    confidence: float = 0.8,
    source_episode: int | None = None,
    source_msg: int | None = None,
    subject_entity: str | None = None,
    workspace_id: str | None = None,
) -> int:
    stamp = now_ms()
    cur = conn.execute(
        "INSERT INTO memories (subject, kind, text, subject_entity, importance,"
        " confidence, source_episode, source_msg, workspace_id, created_at,"
        " accessed_at, access_count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            subject,
            kind if kind in KINDS else "fact",
            text,
            subject_entity,
            float(importance),
            float(confidence),
            source_episode,
            source_msg,
            workspace_id or None,
            stamp,
            stamp,
        ),
    )
    return int(cur.lastrowid or 0)


def merge_into(conn: sqlite3.Connection, memory_id: int, *, text: str, confidence: float) -> None:
    """Near-duplicate: keep the richer text, raise confidence."""
    conn.execute(
        "UPDATE memories SET text = ?, confidence = MAX(confidence, ?),"
        " accessed_at = ?, access_count = access_count + 1"
        " WHERE id = ?",
        (text, float(confidence), now_ms(), memory_id),
    )


def supersede(conn: sqlite3.Connection, old_id: int, new_id: int) -> None:
    conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (new_id, old_id))


def touch(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    stamp = now_ms()
    conn.executemany(
        "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
        [(stamp, int(memory_id)) for memory_id in ids],
    )


def forget(
    conn: sqlite3.Connection,
    subject: str,
    *,
    ids: list[int] | None = None,
    query: str | None = None,
) -> list[int]:
    """Soft-delete by id list or lexical query. Returns the affected ids."""
    targets: list[int] = [int(i) for i in (ids or [])]
    if not targets and query:
        targets = [row["id"] for row in like_search(conn, subject, query, 100)]
    if not targets:
        return []
    stamp = now_ms()
    conn.executemany(
        "UPDATE memories SET deleted_at = ? WHERE id = ? AND subject = ? AND deleted_at IS NULL",
        [(stamp, memory_id, subject) for memory_id in targets],
    )
    return targets


# --------------------------------------------------------------------------- read


def get(conn: sqlite3.Connection, memory_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return dict(row) if row else None


def list_memories(
    conn: sqlite3.Connection,
    subject: str,
    *,
    limit: int = 50,
    kind: str | None = None,
    since: int | None = None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, kind, text, subject_entity, importance, confidence, created_at,"
        " accessed_at, access_count"
        " FROM memories WHERE subject = ? AND deleted_at IS NULL AND superseded_by IS NULL"
    )
    params: list[Any] = [subject]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if since:
        sql += " AND created_at >= ?"
        params.append(int(since))
    sql += " ORDER BY importance DESC, accessed_at DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def by_ids(
    conn: sqlite3.Connection, ids: list[int], *, include_dead: bool = False
) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    sql = f"SELECT * FROM memories WHERE id IN ({marks})"
    if not include_dead:
        sql += " AND deleted_at IS NULL AND superseded_by IS NULL"
    rows = conn.execute(sql, [int(i) for i in ids]).fetchall()
    order = {int(memory_id): index for index, memory_id in enumerate(ids)}
    return sorted((dict(row) for row in rows), key=lambda row: order.get(row["id"], 999))


def count(conn: sqlite3.Connection, subject: str, *, live_only: bool = True) -> int:
    sql = "SELECT COUNT(*) AS n FROM memories WHERE subject = ?"
    if live_only:
        sql += " AND deleted_at IS NULL AND superseded_by IS NULL"
    return int(conn.execute(sql, (subject,)).fetchone()["n"])


# --------------------------------------------------------------------------- lexical


def fts_query(text: str, max_tokens: int = 24) -> str:
    tokens = [token for token in _WORD.findall((text or "").lower()) if len(token) > 1]
    tokens = [token for token in tokens if token not in _STOP]
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return " OR ".join(f'"{token}"' for token in seen[:max_tokens])


def fts_search(
    conn: sqlite3.Connection, subject: str, query: str, limit: int = 40
) -> list[dict[str, Any]]:
    match = fts_query(query)
    if not match:
        return []
    try:
        rows = conn.execute(
            "SELECT m.id, bm25(memory_fts) AS score"
            " FROM memory_fts JOIN memories m ON m.id = memory_fts.rowid"
            " WHERE memory_fts MATCH ? AND m.subject = ?"
            " AND m.deleted_at IS NULL AND m.superseded_by IS NULL"
            " ORDER BY score LIMIT ?",
            (match, subject, max(1, limit)),
        ).fetchall()
    except sqlite3.Error:
        return like_search(conn, subject, query, limit)
    return [dict(row) for row in rows]


def like_search(
    conn: sqlite3.Connection, subject: str, query: str, limit: int = 40
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, 0.0 AS score FROM memories"
        " WHERE subject = ? AND text LIKE ? AND deleted_at IS NULL AND superseded_by IS NULL"
        " ORDER BY importance DESC LIMIT ?",
        (subject, f"%{query.strip()}%", max(1, limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def find_exact(
    conn: sqlite3.Connection, subject: str, text: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM memories WHERE subject = ? AND lower(text) = lower(?)"
        " AND deleted_at IS NULL AND superseded_by IS NULL"
        " ORDER BY id DESC LIMIT 1",
        (subject, text.strip()),
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- graph


def upsert_entity(
    conn: sqlite3.Connection,
    subject: str,
    name: str,
    kind: str = "mention",
    aliases: list[str] | None = None,
) -> int:
    clean = name.strip()[:120]
    if not clean:
        return 0
    row = conn.execute(
        "SELECT id FROM entities WHERE subject = ? AND name = ? AND kind = ?",
        (subject, clean, kind),
    ).fetchone()
    if row:
        if aliases:
            conn.execute(
                "UPDATE entities SET aliases = ? WHERE id = ?",
                (json.dumps(sorted(set(aliases))), row["id"]),
            )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO entities (subject, name, kind, aliases) VALUES (?, ?, ?, ?)",
        (subject, clean, kind, json.dumps(sorted(set(aliases or [])))),
    )
    return int(cur.lastrowid or 0)


def add_edge(
    conn: sqlite3.Connection, src: int, dst: int, rel: str, weight: float = 1.0
) -> None:
    if not src or not dst or src == dst:
        return
    conn.execute(
        "INSERT INTO edges (src, dst, rel, weight) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(src, dst, rel) DO UPDATE SET weight = weight + excluded.weight",
        (src, dst, rel, float(weight)),
    )


def entities_matching(
    conn: sqlite3.Connection, subject: str, text: str, limit: int = 12
) -> list[dict[str, Any]]:
    tokens = [token for token in _WORD.findall((text or "").lower()) if len(token) > 2]
    if not tokens:
        return []
    found: dict[int, dict[str, Any]] = {}
    for token in tokens[:12]:
        rows = conn.execute(
            "SELECT id, name, kind, aliases FROM entities"
            " WHERE subject = ? AND (lower(name) LIKE ? OR lower(aliases) LIKE ?)"
            " LIMIT ?",
            (subject, f"%{token}%", f"%{token}%", limit),
        ).fetchall()
        for row in rows:
            found[int(row["id"])] = dict(row)
    return list(found.values())


def memories_for_entities(
    conn: sqlite3.Connection, subject: str, names: list[str], limit: int = 20
) -> list[int]:
    if not names:
        return []
    found: list[int] = []
    for name in names:
        rows = conn.execute(
            "SELECT id FROM memories WHERE subject = ? AND subject_entity = ?"
            " AND deleted_at IS NULL AND superseded_by IS NULL"
            " ORDER BY importance DESC LIMIT ?",
            (subject, name, limit),
        ).fetchall()
        found.extend(int(row["id"]) for row in rows)
    return list(dict.fromkeys(found))
