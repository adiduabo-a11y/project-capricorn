"""Backups, artifacts and undo.

Every write path records what the file looked like first. This is an undo log,
not version control: small files are kept inline, large ones spill to
``<capri home>/backups``, and the log is pruned by age and by per-file count so
the shard cannot grow without bound.

The restore path is shared by ``fs.undo`` and the ``work.undo`` RPC so the UI
button cannot diverge from the tool the model calls.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .. import paths
from . import patch

BACKUP_INLINE_LIMIT = 512 * 1024
MAX_BACKUPS_PER_FILE = 20
ARTIFACT_LIMIT = 200_000
ARTIFACT_KINDS = ("diff", "command", "backup", "test")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def backups_dir(task_id: str | None = "") -> Path:
    return paths.capri_home() / "backups" / (task_id or "global")


def _drop_blob(blob: str | None) -> None:
    if not blob:
        return
    try:
        Path(blob).unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- backups


def record_backup(
    conn: sqlite3.Connection,
    *,
    path: str,
    before_text: str,
    before_hash: str,
    after_hash: str = "",
    task_id: str | None = None,
    turn_id: str | None = None,
    added: int = 0,
    removed: int = 0,
) -> str:
    """Store the pre-edit text and return the backup id."""
    backup_id = _new_id("bk")
    stored: str | None = before_text
    before_blob: str | None = None
    if len(before_text.encode("utf-8")) > BACKUP_INLINE_LIMIT:
        blob = backups_dir(task_id) / backup_id
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(before_text.encode("utf-8"))
        before_blob = str(blob)
        stored = None

    conn.execute(
        "INSERT INTO file_backups (id, task_id, turn_id, path, before_hash,"
        " after_hash, before_text, before_blob, lines_added, lines_removed,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            backup_id,
            task_id or None,
            turn_id,
            path,
            before_hash,
            after_hash,
            stored,
            before_blob,
            max(0, int(added)),
            max(0, int(removed)),
            _now_ms(),
        ),
    )
    prune_for_path(conn, path, task_id)
    return backup_id


def backup_text(conn: sqlite3.Connection, backup_id: str) -> str | None:
    """The exact pre-edit text. Bytes on disk are read without newline
    translation, or a CRLF file would come back as LF."""
    row = conn.execute(
        "SELECT before_text, before_blob FROM file_backups WHERE id = ?", (backup_id,)
    ).fetchone()
    if row is None:
        return None
    if row["before_text"] is not None:
        return str(row["before_text"])
    blob = str(row["before_blob"] or "")
    if not blob:
        return None
    try:
        return Path(blob).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def find_backup(
    conn: sqlite3.Connection, *, backup_id: str = "", path: str = ""
) -> dict[str, Any] | None:
    if backup_id:
        row = conn.execute(
            "SELECT * FROM file_backups WHERE id = ?", (backup_id,)
        ).fetchone()
    elif path:
        row = conn.execute(
            "SELECT * FROM file_backups WHERE path = ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (path,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM file_backups ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def prune_for_path(
    conn: sqlite3.Connection,
    path: str,
    task_id: str | None,
    keep: int = MAX_BACKUPS_PER_FILE,
) -> int:
    rows = conn.execute(
        "SELECT id FROM file_backups WHERE path = ?"
        " AND COALESCE(task_id, '') = COALESCE(?, '')"
        " ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
        (path, task_id, max(1, keep)),
    ).fetchall()
    removed = 0
    for row in rows:
        backup_id = str(row["id"])
        blob = conn.execute(
            "SELECT before_blob FROM file_backups WHERE id = ?", (backup_id,)
        ).fetchone()
        _drop_blob(str(blob["before_blob"]) if blob else None)
        conn.execute("DELETE FROM file_backups WHERE id = ?", (backup_id,))
        removed += 1
    return removed


def prune(
    conn: sqlite3.Connection, *, days: int, keep: int = MAX_BACKUPS_PER_FILE
) -> int:
    """Age first, then the per-file cap. Returns rows removed."""
    removed = 0
    if days > 0:
        cutoff = int((time.time() - days * 86_400) * 1000)
        rows = conn.execute(
            "SELECT id, before_blob FROM file_backups WHERE created_at < ?", (cutoff,)
        ).fetchall()
        for row in rows:
            _drop_blob(row["before_blob"])
            conn.execute("DELETE FROM file_backups WHERE id = ?", (str(row["id"]),))
            removed += 1

    groups = conn.execute(
        "SELECT path, task_id, COUNT(*) AS n FROM file_backups"
        " GROUP BY path, task_id HAVING n > ?",
        (max(1, keep),),
    ).fetchall()
    for group in groups:
        removed += prune_for_path(
            conn, str(group["path"]), group["task_id"], max(1, keep)
        )
    return removed


def recent_changes(
    conn: sqlite3.Connection,
    *,
    workspace_root: str = "",
    task_id: str = "",
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Latest change per path, newest first.

    Derived from ``file_backups`` on purpose: no watcher is running, so this is
    the only list that cannot disagree with what was actually written.
    """
    sql = (
        "SELECT id, task_id, turn_id, path, lines_added, lines_removed, created_at"
        " FROM file_backups"
    )
    params: list[Any] = []
    if task_id:
        sql += " WHERE task_id = ?"
        params.append(task_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(500)

    root = os.path.normcase(os.path.abspath(workspace_root)) if workspace_root else ""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, params).fetchall():
        path = str(row["path"])
        key = os.path.normcase(path)
        if key in seen:
            continue
        if root and not (key == root or key.startswith(root + os.sep)):
            continue
        seen.add(key)
        # The diff that came with this edit, so a change row can show it
        # without guessing an id. The backup is written before the write and
        # the artifact after it, so tie them together by turn rather than by
        # timestamp.
        artifact = conn.execute(
            "SELECT id FROM artifacts WHERE kind = 'diff' AND path = ?"
            " AND (? = '' OR turn_id = ?) ORDER BY created_at DESC LIMIT 1",
            (path, str(row["turn_id"] or ""), str(row["turn_id"] or "")),
        ).fetchone()
        out.append(
            {
                "path": path,
                "backupId": str(row["id"]),
                "artifactId": str(artifact["id"]) if artifact else "",
                "taskId": row["task_id"],
                "turnId": row["turn_id"],
                "added": int(row["lines_added"] or 0),
                "removed": int(row["lines_removed"] or 0),
                "at": int(row["created_at"]),
            }
        )
        if len(out) >= max(1, min(int(limit or 40), 200)):
            break
    return out


# --------------------------------------------------------------------------- artifacts


def record_artifact(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    kind: str,
    content: str,
    task_id: str | None = None,
    path: str = "",
    limit: int = ARTIFACT_LIMIT,
) -> dict[str, Any]:
    """Store a diff or command output and return its reference.

    Large output belongs here, not in ``agent.tool``: the observation carries a
    reference, the panel fetches the text.
    """
    text = str(content or "")
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + f"\n[truncated at {limit} of {len(content)} chars]"
    artifact_id = _new_id("art")
    conn.execute(
        "INSERT INTO artifacts (id, turn_id, task_id, kind, path, content,"
        " truncated, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id,
            turn_id,
            task_id or None,
            kind if kind in ARTIFACT_KINDS else "command",
            path or None,
            text,
            1 if truncated else 0,
            _now_ms(),
        ),
    )
    return {
        "id": artifact_id,
        "kind": kind if kind in ARTIFACT_KINDS else "command",
        "path": path or "",
        "truncated": truncated,
        "bytes": len(text),
    }


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- undo


def restore(
    conn: sqlite3.Connection,
    *,
    backup_id: str = "",
    path: str = "",
    turn_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Put a file back from its backup, refusing to stomp a later edit.

    The check is the file's current hash against the hash recorded *after* the
    edit being undone. If they differ, a different write happened since; a
    restore would silently destroy it.
    """
    record = find_backup(conn, backup_id=backup_id, path=path)
    if record is None:
        return {"ok": False, "output": "no matching backup"}

    target = Path(str(record["path"]))
    before_text = backup_text(conn, str(record["id"]))
    if before_text is None:
        return {"ok": False, "output": "the backup text could not be read"}

    current_exists = target.exists()
    current_text = ""
    if current_exists:
        try:
            current_text = patch.decode_file(target.read_bytes())
        except patch.PatchError as exc:
            return {"ok": False, "output": f"the current file cannot be read: {exc.message}"}
    current_hash = patch.sha256_text(current_text) if current_exists else ""

    expected = str(record.get("after_hash") or "")
    if expected and current_hash != expected:
        diff = patch.unified_diff(
            os.path.basename(str(record["path"])), current_text, before_text
        )
        return {
            "ok": False,
            "output": (
                "the file changed after that edit, so undo would overwrite a "
                "newer change. Current content against the backup:\n\n" + diff
            ),
            "data": {"path": str(record["path"]), "backupId": str(record["id"])},
        }

    try:
        patch.write_atomic(target, patch.encode_file(before_text))
    except OSError as exc:
        return {"ok": False, "output": f"could not restore the file: {exc}"}

    new_hash = patch.sha256_text(before_text)
    artifact = record_artifact(
        conn,
        turn_id=turn_id or str(record.get("turn_id") or "undo"),
        task_id=task_id or record.get("task_id"),
        kind="backup",
        path=str(record["path"]),
        content=(
            f"restored {record['path']} from {record['id']}\n"
            f"before {record.get('after_hash') or '(unknown)'}\n"
            f"after  {new_hash}\n"
        ),
    )
    return {
        "ok": True,
        "output": f"restored {record['path']} from {record['id']}",
        "data": {
            "path": str(record["path"]),
            "sha256": new_hash,
            "backupId": str(record["id"]),
            "artifact": artifact,
        },
    }
