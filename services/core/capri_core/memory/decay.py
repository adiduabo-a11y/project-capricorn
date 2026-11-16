"""Decay: what to forget when nobody is looking.

Score is for pruning only, never for ranking:

    importance * exp(-age / halflife) * (1 + log1p(access_count))

A row is soft-deleted, not removed. The current episode is always protected.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from typing import Any

from ..store import db
from . import repo

log = logging.getLogger("capri.memory.decay")

MIN_AGE_MS = 86_400_000  # never prune something written today


def score(
    *,
    importance: float,
    accessed_at: int,
    access_count: int,
    halflife_days: int,
    now: int | None = None,
) -> float:
    stamp = now if now is not None else int(time.time() * 1000)
    age_days = max(0.0, (stamp - accessed_at) / 86_400_000)
    return (
        max(0.0, float(importance))
        * math.exp(-age_days / max(1.0, float(halflife_days)))
        * (1.0 + math.log1p(max(0, int(access_count))))
    )


def prune(
    conn: sqlite3.Connection,
    subject: str,
    *,
    halflife_days: int = 30,
    threshold: float = 0.05,
    protect_episode: int | None = None,
    max_rows: int = 500,
) -> list[int]:
    now = int(time.time() * 1000)
    rows = conn.execute(
        "SELECT id, importance, accessed_at, created_at, access_count, source_episode"
        " FROM memories WHERE subject = ? AND deleted_at IS NULL AND superseded_by IS NULL"
        " ORDER BY accessed_at ASC LIMIT ?",
        (subject, max_rows),
    ).fetchall()

    doomed: list[int] = []
    for row in rows:
        if protect_episode is not None and row["source_episode"] == protect_episode:
            continue
        if now - int(row["created_at"] or 0) < MIN_AGE_MS:
            continue
        value = score(
            importance=float(row["importance"] or 0.0),
            accessed_at=int(row["accessed_at"] or row["created_at"] or now),
            access_count=int(row["access_count"] or 0),
            halflife_days=halflife_days,
            now=now,
        )
        if value < threshold:
            doomed.append(int(row["id"]))

    if doomed:
        repo.forget(conn, subject, ids=doomed)
        db.trace(
            conn,
            span="memory",
            name="memory.prune",
            payload={"pruned": len(doomed), "threshold": threshold},
        )
        log.info("pruned %d memories", len(doomed))
    return doomed


def maintain(
    conn: sqlite3.Connection,
    subject: str,
    *,
    halflife_days: int = 30,
    threshold: float = 0.05,
    protect_episode: int | None = None,
) -> dict[str, Any]:
    pruned = prune(
        conn,
        subject,
        halflife_days=halflife_days,
        threshold=threshold,
        protect_episode=protect_episode,
    )
    return {"pruned": pruned, "remaining": repo.count(conn, subject)}
