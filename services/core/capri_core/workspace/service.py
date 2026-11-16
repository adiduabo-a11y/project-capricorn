"""The open project: persistence, trust and inspection.

A workspace is a row in the shard plus one meta key pointing at it. Opening one
is a user act, so it grants write trust for this process; a restart can either
keep that trust (``policy.trust_persisted_workspace``) or reopen read-only until
the user opens it again. Nothing in the agent can call ``open``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import pathsafe
from ..config import CapriConfig
from ..store import db
from . import detect, tree

log = logging.getLogger("capri.workspace")

ACTIVE_META_KEY = "workspace.active"


class WorkspaceError(Exception):
    """A refused workspace operation, with an RPC-friendly code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Workspace:
    id: str
    name: str
    root: str
    created_at: int
    last_opened_at: int
    trusted: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root": self.root,
            "createdAt": self.created_at,
            "lastOpenedAt": self.last_opened_at,
            "trusted": self.trusted,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _name_for(root: str) -> str:
    return os.path.basename(root.rstrip("\\/")) or root


class WorkspaceService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: CapriConfig,
        *,
        busy: Callable[[], bool] | None = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.busy = busy or (lambda: False)
        self.active: Workspace | None = None
        self.inspection: dict[str, Any] | None = None

    def reconfigure(self, config: CapriConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ lifecycle

    def restore(self) -> Workspace | None:
        """Reopen the workspace from the shard, if it is still usable."""
        raw_id = db.get_meta(self.conn, ACTIVE_META_KEY, "") or ""
        if not raw_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE id = ?", (raw_id,)
        ).fetchone()
        if row is None:
            db.set_meta(self.conn, ACTIVE_META_KEY, "")
            return None
        root = pathsafe.resolve(row["root"])
        if not os.path.isdir(root):
            log.info("persisted workspace %s no longer exists", root)
            db.set_meta(self.conn, ACTIVE_META_KEY, "")
            return None
        if pathsafe.denied(root, self.config.policy.deny_paths):
            log.warning("persisted workspace %s is on the deny list", root)
            db.set_meta(self.conn, ACTIVE_META_KEY, "")
            return None

        trusted = bool(self.config.policy.trust_persisted_workspace)
        self.active = Workspace(
            id=str(row["id"]),
            name=str(row["name"]),
            root=root,
            created_at=int(row["created_at"]),
            last_opened_at=int(row["last_opened_at"]),
            trusted=trusted,
        )
        self.inspection = detect.inspect_project(root)
        log.info(
            "restored workspace %s (%s)",
            self.active.name,
            "trusted" if trusted else "read-only until opened again",
        )
        return self.active

    def open(self, path: str, *, base: str = "") -> Workspace:
        if self.busy():
            raise WorkspaceError(
                "busy", "cannot switch projects while a turn is running"
            )
        root = pathsafe.resolve(path, base)
        if not os.path.exists(root):
            raise WorkspaceError("invalid_args", f"no such folder: {root}")
        if not os.path.isdir(root):
            raise WorkspaceError("invalid_args", f"not a folder: {root}")
        if pathsafe.denied(root, self.config.policy.deny_paths):
            raise WorkspaceError("blocked", "that folder is on the deny list")

        stamp = _now_ms()
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE lower(root) = lower(?)", (root,)
        ).fetchone()
        if row is None:
            workspace_id = f"ws_{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                "INSERT INTO workspaces (id, name, root, created_at, last_opened_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (workspace_id, _name_for(root), root, stamp, stamp),
            )
            created = stamp
        else:
            workspace_id = str(row["id"])
            created = int(row["created_at"])
            self.conn.execute(
                "UPDATE workspaces SET root = ?, name = ?, last_opened_at = ?"
                " WHERE id = ?",
                (root, _name_for(root), stamp, workspace_id),
            )

        db.set_meta(self.conn, ACTIVE_META_KEY, workspace_id)
        self.active = Workspace(
            id=workspace_id,
            name=_name_for(root),
            root=root,
            created_at=created,
            last_opened_at=stamp,
            trusted=True,
        )
        self.inspection = detect.inspect_project(root)
        return self.active

    def close(self) -> bool:
        if self.busy():
            raise WorkspaceError(
                "busy", "cannot close the project while a turn is running"
            )
        if self.active is None:
            return False
        db.set_meta(self.conn, ACTIVE_META_KEY, "")
        self.active = None
        self.inspection = None
        return True

    def current(self) -> dict[str, Any] | None:
        return self.active.as_dict() if self.active is not None else None

    def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM workspaces ORDER BY last_opened_at DESC LIMIT ?",
            (max(1, min(int(limit or 8), 32)),),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "root": row["root"],
                    "createdAt": int(row["created_at"]),
                    "lastOpenedAt": int(row["last_opened_at"]),
                    "exists": os.path.isdir(str(row["root"])),
                    "active": bool(self.active and self.active.id == row["id"]),
                }
            )
        return out

    # ------------------------------------------------------------------ inspection

    def inspect(self) -> dict[str, Any]:
        if self.active is None:
            raise WorkspaceError("not_found", "no project is open")
        self.inspection = detect.inspect_project(self.active.root)
        return self.inspection

    def confident_commands(self) -> list[str]:
        return detect.confident_commands(self.inspection or {})

    @property
    def conventions(self) -> str:
        return str((self.inspection or {}).get("conventions") or "")

    def tree(self, path: str = "", depth: int = 3) -> dict[str, Any]:
        if self.active is None:
            raise WorkspaceError("not_found", "no project is open")
        try:
            result = tree.walk_tree(
                self.active.root,
                path or None,
                depth=depth,
                deny_paths=self.config.policy.deny_paths,
            )
        except ValueError as exc:
            raise WorkspaceError("invalid_args", str(exc)) from exc
        result["name"] = self.active.name
        return result

    def path_for(self, raw: str) -> str:
        """Resolve a project-relative path or refuse if it escapes the root."""
        if self.active is None:
            raise WorkspaceError("not_found", "no project is open")
        target = pathsafe.resolve(raw, self.active.root)
        if not pathsafe.within(target, self.active.root):
            raise WorkspaceError("blocked", "path is outside the project")
        if pathsafe.denied(target, self.config.policy.deny_paths):
            raise WorkspaceError("blocked", "path is on the deny list")
        return target

    # ------------------------------------------------------------------ helpers

    def default_root(self) -> Path:
        return Path(self.config.policy.workspace or os.getcwd())
