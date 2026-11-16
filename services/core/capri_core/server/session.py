"""Sessions and turns.

A session binds one conversation to one episode in the shard. A turn is a unit
of work: user message in, streamed events out, messages and traces persisted.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from .. import paths

log = logging.getLogger("capri.session")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Session:
    subject: str
    conn: sqlite3.Connection
    episode_id: int
    id: str = field(default_factory=lambda: f"ses_{uuid.uuid4().hex[:12]}")
    created_at: int = field(default_factory=_now_ms)
    turns: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    # A chat opened by `session.new` starts detached: it does not inherit the
    # task that was already open. A work request in it creates its own task,
    # and only `task.continue` adopts an existing one.
    adopt_open_task: bool = True

    @classmethod
    def create(cls, conn: sqlite3.Connection, subject: str = paths.DEFAULT_SUBJECT) -> Session:
        cur = conn.execute(
            "INSERT INTO episodes (subject, started_at) VALUES (?, ?)", (subject, _now_ms())
        )
        return cls(subject=subject, conn=conn, episode_id=int(cur.lastrowid or 0))

    def add_message(
        self,
        turn_id: str,
        role: str,
        content: str,
        *,
        tool_name: str | None = None,
        tool_args: str | None = None,
        tool_result: str | None = None,
        task_id: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (episode_id, turn_id, role, content, tool_name,"
            " tool_args, tool_result, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.episode_id,
                turn_id,
                role,
                content,
                tool_name,
                tool_args,
                tool_result,
                task_id or None,
                _now_ms(),
            ),
        )
        return int(cur.lastrowid or 0)

    def add_tokens(self, estimate: int) -> None:
        self.conn.execute(
            "UPDATE episodes SET token_usage = token_usage + ? WHERE id = ?",
            (max(0, estimate), self.episode_id),
        )

    def recent_messages(self, limit: int = 40) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT role, content, created_at FROM messages WHERE episode_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (self.episode_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def cancel_all(self) -> None:
        for turn_id, task in list(self.turns.items()):
            if not task.done():
                task.cancel()
            self.turns.pop(turn_id, None)

    def close(self) -> None:
        self.cancel_all()
        self.conn.execute(
            "UPDATE episodes SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (_now_ms(), self.episode_id),
        )
