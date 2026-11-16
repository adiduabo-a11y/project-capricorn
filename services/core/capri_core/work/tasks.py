"""Tasks: one thread of work inside a project.

A task is created by the first turn that runs with a project open, updated as
the turn goes, and *paused* rather than finished when a ceiling stops it. The
summary is composed from rows that already exist — backups, traces, messages —
so there is no second model pass and nothing that can drift from the transcript.

Nothing here decides when a turn starts. That is the server's job; this module
only records what happened.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

TASK_STATUSES = ("active", "paused", "done", "failed", "abandoned")
# A task in either of these states is what the next turn belongs to.
OPEN_STATUSES = ("active", "paused")
TITLE_LIMIT = 60
SUMMARY_LIMIT = 900
OBSERVATION_LIMIT = 3
OBSERVATION_CHARS = 400


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return f"tsk_{uuid.uuid4().hex[:12]}"


def title_for(request: str) -> str:
    """A task title is the request, trimmed. No model, no cleverness."""
    text = " ".join(str(request or "").split())
    if len(text) <= TITLE_LIMIT:
        return text
    cut = text[:TITLE_LIMIT]
    space = cut.rfind(" ")
    if space >= TITLE_LIMIT // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:") + "…"


@dataclass
class Task:
    id: str
    workspace_id: str
    title: str
    request: str
    status: str
    summary: str
    steps: int
    result: str
    created_at: int
    updated_at: int
    last_turn_id: str | None

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspaceId": self.workspace_id,
            "title": self.title,
            "request": self.request,
            "status": self.status,
            "summary": self.summary,
            "steps": self.steps,
            "result": self.result,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastTurnId": self.last_turn_id,
        }


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        title=str(row["title"]),
        request=str(row["request"]),
        status=str(row["status"]),
        summary=str(row["summary"]),
        steps=int(row["steps"] or 0),
        result=str(row["result"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        last_turn_id=row["last_turn_id"],
    )


class TaskService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ create

    def create(
        self,
        workspace_id: str,
        request: str,
        *,
        title: str = "",
        status: str = "active",
    ) -> Task:
        now = _now_ms()
        task_id = _new_id()
        text = str(request or "")
        self.conn.execute(
            "INSERT INTO tasks (id, workspace_id, title, request, status, summary,"
            " steps, result, created_at, updated_at, last_turn_id)"
            " VALUES (?, ?, ?, ?, ?, '', 0, 'none', ?, ?, NULL)",
            (
                task_id,
                workspace_id,
                (title or title_for(text)) or "Untitled task",
                text,
                status if status in TASK_STATUSES else "active",
                now,
                now,
            ),
        )
        created = self.get(task_id)
        assert created is not None
        return created

    def get(self, task_id: str) -> Task | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _task(row) if row else None

    def open_for(self, workspace_id: str) -> Task | None:
        """The task the next turn should attach to, if there is one."""
        if not workspace_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE workspace_id = ? AND status IN ('active', 'paused')"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        return _task(row) if row else None

    def recent(self, workspace_id: str, limit: int = 10) -> list[Task]:
        if not workspace_id:
            return []
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE workspace_id = ?"
            " ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (workspace_id, max(1, min(int(limit or 10), 100))),
        ).fetchall()
        return [_task(row) for row in rows]

    # ------------------------------------------------------------------ update

    def _update(self, task_id: str, fields: dict[str, Any]) -> Task | None:
        if not fields:
            return self.get(task_id)
        fields["updated_at"] = _now_ms()
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE tasks SET {columns} WHERE id = ?",
            (*fields.values(), task_id),
        )
        return self.get(task_id)

    def touch(self, task_id: str, *, last_turn_id: str = "") -> Task | None:
        fields: dict[str, Any] = {}
        if last_turn_id:
            fields["last_turn_id"] = last_turn_id
        return self._update(task_id, fields)

    def add_step(self, task_id: str, count: int = 1) -> None:
        self.conn.execute(
            "UPDATE tasks SET steps = steps + ?, updated_at = ? WHERE id = ?",
            (max(0, int(count)), _now_ms(), task_id),
        )

    def resume(self, task_id: str) -> Task | None:
        """Put a paused task back to work. Idempotent for an active one."""
        return self._update(task_id, {"status": "active"})

    def pause(self, task_id: str, summary: str = "") -> Task | None:
        fields: dict[str, Any] = {"status": "paused"}
        if summary:
            fields["summary"] = summary[:SUMMARY_LIMIT]
        return self._update(task_id, fields)

    def finish(self, task_id: str, *, result: str = "none", summary: str = "") -> Task | None:
        fields: dict[str, Any] = {
            "status": "done",
            "result": result if result in ("none", "passed", "failed") else "none",
        }
        if summary:
            fields["summary"] = summary[:SUMMARY_LIMIT]
        return self._update(task_id, fields)

    def abandon(self, task_id: str) -> Task | None:
        """End a task without touching its files, its messages or its record."""
        return self._update(task_id, {"status": "abandoned"})

    # ------------------------------------------------------------------ reading

    def observations(self, task_id: str, limit: int = OBSERVATION_LIMIT) -> list[str]:
        """The last few tool observations, for a continuation prompt.

        Read from ``messages`` rather than rebuilt from traces: this is exactly
        what the model saw.
        """
        rows = self.conn.execute(
            "SELECT content FROM messages WHERE task_id = ? AND role = 'tool'"
            " ORDER BY id DESC LIMIT ?",
            (task_id, max(1, min(int(limit or OBSERVATION_LIMIT), 10))),
        ).fetchall()
        out: list[str] = []
        for row in reversed(rows):
            text = " ".join(str(row["content"] or "").split())
            if text:
                out.append(text[:OBSERVATION_CHARS])
        return out

    def summarize(self, task_id: str) -> str:
        """Compose the task's state from rows, not from a model pass."""
        task = self.get(task_id)
        if task is None:
            return ""
        lines: list[str] = []

        files = self.conn.execute(
            "SELECT path, SUM(lines_added) AS added, SUM(lines_removed) AS removed,"
            " MAX(created_at) AS latest FROM file_backups WHERE task_id = ?"
            " GROUP BY path ORDER BY latest DESC LIMIT 6",
            (task_id,),
        ).fetchall()
        if files:
            added = sum(int(row["added"] or 0) for row in files)
            removed = sum(int(row["removed"] or 0) for row in files)
            names = ", ".join(os.path.basename(str(row["path"])) for row in files)
            lines.append(
                f"Changed {len(files)} file(s) (+{added} -{removed}): {names}"
            )
        else:
            lines.append("Changed nothing yet")

        runs = self.conn.execute(
            "SELECT payload FROM traces WHERE task_id = ? AND span = 'tool'"
            " AND name = 'shell.run' ORDER BY id DESC LIMIT 4",
            (task_id,),
        ).fetchall()
        commands: list[str] = []
        for row in reversed(runs):
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            command = str((payload.get("args") or {}).get("command") or "command")
            command = " ".join(command.split())
            if len(command) > 100:
                # The interpreter is at the front and the script at the back;
                # cutting only the tail would hide what was actually run.
                command = f"{command[:47]} ... {command[-47:]}"
            commands.append(f"{command} ({'ok' if payload.get('ok') else 'failed'})")
        if commands:
            lines.append("Ran: " + "; ".join(commands))

        if task.steps:
            lines.append(f"Steps so far: {task.steps} tool call(s)")

        return "\n".join(lines)[:SUMMARY_LIMIT]
