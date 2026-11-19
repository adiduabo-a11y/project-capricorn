"""Idle maintenance.

Nothing here needs a model. When the conversation has been quiet for a while,
Capri closes the books: completed episodes get a deterministic journal, old
memory decays, and retention limits remove traces/episodes that no longer earn
their space. A closed episode is never pruned before its journal is written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..config import CapriConfig
from ..store import db
from ..work import store as work_store

log = logging.getLogger("capri.scheduler")

MAX_JOURNAL_MESSAGES = 200
MAX_MESSAGE_CHARS = 1200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(stamp: int | None) -> str | None:
    if not stamp:
        return None
    return datetime.fromtimestamp(stamp / 1000, tz=UTC).isoformat()


def _journal_episode(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    messages = conn.execute(
        "SELECT turn_id, role, content, tool_name, created_at FROM messages"
        " WHERE episode_id = ? ORDER BY id LIMIT ?",
        (int(row["id"]), MAX_JOURNAL_MESSAGES),
    ).fetchall()
    turns = len({m["turn_id"] for m in messages if m["turn_id"]})
    tools = sorted({m["tool_name"] for m in messages if m["tool_name"]})
    user_lines = [
        " ".join(str(m["content"]).split())[:300]
        for m in messages
        if m["role"] == "user" and not str(m["content"]).startswith("[observation from ")
    ]
    assistant_lines = [
        " ".join(str(m["content"]).split())[:300]
        for m in messages
        if m["role"] == "assistant"
    ]
    summary_parts = []
    if user_lines:
        summary_parts.append("asked: " + " / ".join(user_lines[:3]))
    if assistant_lines:
        summary_parts.append("answered: " + assistant_lines[-1])
    summary = " · ".join(summary_parts)[:600] or f"Episode {int(row['id'])}: {turns} turns"
    journal = {
        "version": 1,
        "episodeId": int(row["id"]),
        "startedAt": _iso(int(row["started_at"])),
        "endedAt": _iso(int(row["ended_at"] or 0)),
        "turns": turns,
        "tokenUsage": int(row["token_usage"] or 0),
        "tools": tools,
        "messages": [
            {
                "role": str(m["role"]),
                "text": str(m["content"])[:MAX_MESSAGE_CHARS],
                "tool": m["tool_name"],
                "at": _iso(int(m["created_at"])),
            }
            for m in messages
        ],
    }
    conn.execute(
        "UPDATE episodes SET summary = ?, journal = ? WHERE id = ?",
        (summary, json.dumps(journal, ensure_ascii=False), int(row["id"])),
    )
    db.trace(
        conn,
        span="episode",
        name="episode.journal",
        payload={"episode": int(row["id"]), "turns": turns, "tools": tools},
    )
    return journal


class IdleScheduler:
    def __init__(
        self,
        config: CapriConfig,
        conn: sqlite3.Connection,
        subject: str,
        memory: Any,
        active_episodes: Callable[[], set[int]],
        active_turns: Callable[[], int] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        # Told about a pass that actually ran, with its counts. A line about it
        # is the caller's business; maintenance never waits on one.
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.subject = subject
        self.memory = memory
        self.active_episodes = active_episodes
        self.active_turns = active_turns or (lambda: 0)
        self.clock = clock
        self.on_result = on_result
        self.last_activity = clock()
        self.last_run: float | None = None
        self.last_result: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def touch(self) -> None:
        self.last_activity = self.clock()

    def status(self) -> dict[str, Any]:
        active = sorted(self.active_episodes())
        return {
            "enabled": self.config.scheduler.enabled,
            "running": self._task is not None and not self._task.done(),
            "idleForS": max(0.0, self.clock() - self.last_activity),
            "lastRun": _iso(int(self.last_run * 1000)) if self.last_run else None,
            "lastResult": self.last_result,
            "activeEpisodes": active,
            "activeTurns": self.active_turns(),
        }

    def start(self) -> None:
        if not self.config.scheduler.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="capri-scheduler")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("scheduler stopped with an error")

    async def _loop(self) -> None:
        while True:
            interval = max(1.0, min(15.0, self.config.scheduler.interval_s / 4))
            await asyncio.sleep(interval)
            try:
                self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("idle maintenance failed; will retry next tick")

    # ------------------------------------------------------------------ work

    def run_once(self, *, force: bool = False, now: float | None = None) -> dict[str, Any]:
        stamp = self.clock() if now is None else now
        active = set(self.active_episodes())
        cfg = self.config.scheduler
        if not force:
            if self.active_turns() > 0:
                return self._skip("active turn", stamp)
            if stamp - self.last_activity < cfg.idle_s:
                return self._skip("not idle yet", stamp)
            if self.last_run is not None and stamp - self.last_run < cfg.interval_s:
                return self._skip("maintenance interval has not elapsed", stamp)

        result: dict[str, Any] = {
            "skipped": False,
            "reason": "",
            "journaled": 0,
            "prunedMemories": 0,
            "prunedEpisodes": 0,
            "prunedTraces": 0,
            "prunedBackups": 0,
            "remainingMemories": 0,
            "at": _iso(int(stamp * 1000)),
        }
        if cfg.journal_episodes:
            result["journaled"] = self._journal_completed(active)

        protected = sorted(active)[0] if active else None
        maintained = self.memory.maintain(protect_episode=protected)
        result["prunedMemories"] = len(maintained.get("pruned") or [])
        result["remainingMemories"] = int(maintained.get("remaining") or 0)

        result["prunedTraces"] = self._prune_traces(stamp)
        result["prunedEpisodes"] = self._prune_episodes(stamp, active)
        # Backups follow the trace window; a shard keeps an undo history for as
        # long as it keeps its own audit trail, and no longer.
        result["prunedBackups"] = work_store.prune(
            self.conn, days=self.config.scheduler.trace_retention_days
        )
        self.last_run = stamp
        self.last_result = result
        db.trace(
            self.conn,
            span="scheduler",
            name="idle.maintenance",
            payload={k: v for k, v in result.items() if k != "at"},
        )
        log.info(
            "idle maintenance: %d journals, %d memories, %d episodes, %d traces,"
            " %d backups",
            result["journaled"],
            result["prunedMemories"],
            result["prunedEpisodes"],
            result["prunedTraces"],
            result["prunedBackups"],
        )
        if self.on_result is not None:
            try:
                self.on_result(dict(result))
            except Exception:
                # A comment about housekeeping must never break housekeeping.
                log.exception("maintenance notice failed")
        return result

    def _skip(self, reason: str, stamp: float) -> dict[str, Any]:
        self.last_result = {"skipped": True, "reason": reason, "at": _iso(int(stamp * 1000))}
        return self.last_result

    def _journal_completed(self, active: set[int]) -> int:
        rows = self.conn.execute(
            "SELECT id, started_at, ended_at, token_usage FROM episodes"
            " WHERE subject = ? AND ended_at IS NOT NULL AND journal IS NULL"
            " ORDER BY ended_at ASC LIMIT 25",
            (self.subject,),
        ).fetchall()
        count = 0
        for row in rows:
            if int(row["id"]) in active:
                continue
            _journal_episode(self.conn, row)
            count += 1
        return count

    def _prune_traces(self, stamp: float) -> int:
        days = self.config.scheduler.trace_retention_days
        if days <= 0:
            return 0
        cutoff = int((stamp - days * 86_400) * 1000)
        cur = self.conn.execute("DELETE FROM traces WHERE created_at < ?", (cutoff,))
        return int(cur.rowcount or 0)

    def _prune_episodes(self, stamp: float, active: set[int]) -> int:
        days = self.config.scheduler.episode_retention_days
        if days <= 0:
            return 0
        cutoff = int((stamp - days * 86_400) * 1000)
        rows = self.conn.execute(
            "SELECT id FROM episodes WHERE subject = ? AND ended_at IS NOT NULL"
            " AND ended_at < ? AND journal IS NOT NULL",
            (self.subject, cutoff),
        ).fetchall()
        doomed = [int(row["id"]) for row in rows if int(row["id"]) not in active]
        if doomed:
            marks = ",".join("?" for _ in doomed)
            self.conn.execute(f"DELETE FROM episodes WHERE id IN ({marks})", doomed)
        return len(doomed)
