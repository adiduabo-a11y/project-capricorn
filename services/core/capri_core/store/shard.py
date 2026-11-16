"""Portable shard bundles.

A bundle is a zip with two entries: ``manifest.json`` (format version, schema
version, embedding dimension, checksum) and ``shard.db``. Import validates all
of that before touching the live connection, then uses SQLite's backup API so
the swap is atomic at the database level.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import CapriConfig
from . import db

FORMAT = "capri-shard"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
DATABASE = "shard.db"
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024


class ShardError(ValueError):
    """A bundle failed validation before the live shard was touched."""


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: _count(conn, table)
        for table in ("episodes", "messages", "memories", "docs", "chunks", "traces")
    }


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0) if row else 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_shard(
    conn: sqlite3.Connection,
    target: Path,
    *,
    config: CapriConfig,
    subject: str,
) -> dict[str, Any]:
    """Write a consistent snapshot. The live database keeps serving."""
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capri-export-") as raw:
        copy = Path(raw) / DATABASE
        snapshot = sqlite3.connect(copy)
        try:
            conn.backup(snapshot)
            schema_version = _schema_version(snapshot)
            counts = _counts(snapshot)
        finally:
            snapshot.close()

        manifest = {
            "format": FORMAT,
            "formatVersion": FORMAT_VERSION,
            "appVersion": __version__,
            "schemaVersion": schema_version,
            "embedDim": int(config.models.embed.dim),
            "subject": subject,
            "createdAt": int(time.time() * 1000),
            "databaseBytes": copy.stat().st_size,
            "sha256": _sha256(copy),
            "counts": counts,
        }

        temp = target.with_suffix(target.suffix + ".tmp")
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                bundle.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
                bundle.write(copy, DATABASE)
            temp.replace(target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
    return {"path": str(target), "manifest": manifest}


def _read_manifest(bundle: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = bundle.read(MANIFEST)
    except KeyError as exc:
        raise ShardError("bundle is missing manifest.json") from exc
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShardError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ShardError("manifest must be an object")
    if manifest.get("format") != FORMAT:
        raise ShardError(f"not a {FORMAT} bundle")
    if manifest.get("formatVersion") != FORMAT_VERSION:
        raise ShardError(
            f"unsupported shard format {manifest.get('formatVersion')!r}; "
            f"this build reads format {FORMAT_VERSION}"
        )
    return manifest


def import_shard(
    conn: sqlite3.Connection,
    source: Path,
    *,
    config: CapriConfig,
    subject: str,
) -> dict[str, Any]:
    """Validate a bundle, then replace ``conn``'s contents with it.

    Validation is complete before the first write, so a rejected import leaves
    the live shard untouched.
    """
    source = source.expanduser()
    if not source.exists():
        raise ShardError(f"bundle not found: {source}")
    if source.stat().st_size > MAX_BUNDLE_BYTES:
        raise ShardError("bundle is too large")

    with zipfile.ZipFile(source) as bundle:
        manifest = _read_manifest(bundle)
        try:
            info = bundle.getinfo(DATABASE)
        except KeyError as exc:
            raise ShardError("bundle is missing shard.db") from exc
        if info.file_size > MAX_BUNDLE_BYTES:
            raise ShardError("shard.db is too large")
        if manifest.get("subject") != subject:
            raise ShardError(
                f"bundle belongs to subject {manifest.get('subject')!r}, not {subject!r}"
            )
        expected_dim = int(config.models.embed.dim)
        try:
            recorded_dim = int(manifest.get("embedDim"))
        except (TypeError, ValueError) as exc:
            raise ShardError("manifest has no valid embedDim") from exc
        if recorded_dim != expected_dim:
            raise ShardError(
                f"bundle holds {recorded_dim}-dim vectors but this shard uses {expected_dim}. "
                "Point at the same embedding model before importing."
            )
        latest_schema = db.latest_schema_version()
        try:
            schema_version = int(manifest.get("schemaVersion"))
        except (TypeError, ValueError) as exc:
            raise ShardError("manifest has no valid schemaVersion") from exc
        if schema_version < 1 or schema_version > latest_schema:
            raise ShardError(
                f"bundle schema {schema_version} is not supported by this build "
                f"(latest is {latest_schema})"
            )

        with tempfile.TemporaryDirectory(prefix="capri-import-") as raw:
            copy = Path(raw) / DATABASE
            digest = hashlib.sha256()
            with bundle.open(DATABASE) as src, copy.open("wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
                    dst.write(chunk)
            if manifest.get("sha256") and digest.hexdigest() != manifest["sha256"]:
                raise ShardError("shard.db checksum does not match the manifest")

            imported = sqlite3.connect(copy)
            imported.row_factory = sqlite3.Row
            try:
                integrity = imported.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise ShardError("shard.db failed SQLite integrity_check")
                dim_row = imported.execute(
                    "SELECT value FROM meta WHERE key = 'vector_dim'"
                ).fetchone()
                if dim_row is not None and int(dim_row["value"]) != expected_dim:
                    raise ShardError(
                        f"shard.db holds {dim_row['value']}-dim vectors but this shard uses "
                        f"{expected_dim}"
                    )
                if imported.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'episodes'"
                ).fetchone() is None:
                    raise ShardError("shard.db is missing the episodes table")
                imported.backup(conn)
            finally:
                imported.close()

    # Older bundles are upgraded in place; an imported vector table is already
    # known to have the configured dimension.
    version = db.migrate(conn)
    from .vector import ensure_vector_tables

    ensure_vector_tables(conn, int(config.models.embed.dim))
    db.set_meta(conn, "schema_version", str(version))
    result = dict(manifest)
    result["counts"] = _counts(conn)
    result["path"] = str(source)
    return result
