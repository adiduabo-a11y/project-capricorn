"""Incremental ingest.

The contract is simple: the same file indexed twice is a no-op, a changed file
replaces its own chunks, and a file that disappeared is tombstoned rather than
deleted. Every doc carries a content hash, so "unchanged" is decided by what is
inside the file, not by its timestamp.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..memory.embed import EmbedUnavailable, Embedder
from ..store import db, vector
from .chunker import chunk as chunk_document
from .loaders import LoadError, is_supported, load

log = logging.getLogger("capri.rag.index")

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", "out", ".next", ".nuxt", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".capri", "site-packages", ".idea", ".vscode",
}


@dataclass
class IngestReport:
    collection: str = "default"
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "collection": self.collection,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "chunks": self.chunks,
            "files": self.files[:50],
            "errors": self.errors[:50],
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def resolve(raw: str, workspace: str | None) -> Path:
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute() and workspace:
        path = Path(workspace) / path
    return path.resolve()


def walk(
    paths: list[str],
    workspace: str | None,
    suffixes: frozenset[str] | None = None,
) -> tuple[list[Path], list[Path], list[str]]:
    """Return (files, roots, errors). Roots are directories that were ingested.

    ``suffixes`` narrows what counts as a document. A folder ingest otherwise
    takes every supported type, which is right for a project and wrong for an
    Obsidian vault, where the notes are the markdown and the rest is not hers.
    """
    files: list[Path] = []
    roots: list[Path] = []
    errors: list[str] = []

    def wanted(candidate: Path) -> bool:
        if not is_supported(candidate):
            return False
        return suffixes is None or candidate.suffix.lower() in suffixes

    for raw in paths:
        path = resolve(raw, workspace)
        if not path.exists():
            errors.append(f"{raw}: no such path")
            continue
        if path.is_file():
            if wanted(path):
                files.append(path)
            else:
                errors.append(f"{raw}: unsupported file type {path.suffix or '(none)'}")
            continue

        roots.append(path)
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name.lower() not in SKIP_DIRS and not name.startswith(".")
            )
            for name in sorted(filenames):
                candidate = Path(dirpath) / name
                if wanted(candidate):
                    files.append(candidate)

    unique = list(dict.fromkeys(files))
    return unique, roots, errors


def _drop_chunks(conn: sqlite3.Connection, doc_id: int) -> None:
    rows = conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall()
    for row in rows:
        vector.delete(conn, "chunk_vec", int(row["id"]))
    # The FTS delete trigger fires from the row deletion itself.
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))


def _store_chunks(
    conn: sqlite3.Connection,
    doc_id: int,
    chunks: list,
    vectors: list[list[float]] | None,
) -> int:
    stamp = int(time.time() * 1000)
    for index, piece in enumerate(chunks):
        cur = conn.execute(
            "INSERT INTO chunks (doc_id, ord, text, heading_path, start_char, end_char, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                index,
                piece.text,
                piece.heading_path,
                piece.start,
                piece.end,
                _digest(piece.text)[:24],
            ),
        )
        if vectors is not None:
            vector.upsert(conn, "chunk_vec", int(cur.lastrowid or 0), vectors[index])
    return len(chunks)


async def _embed(embedder: Embedder | None, texts: list[str]) -> list[list[float]] | None:
    if embedder is None or not texts:
        return None
    try:
        return list(await embedder.embed(texts))
    except (EmbedUnavailable, ValueError) as exc:
        log.info("indexing without vectors: %s", exc)
        return None


@dataclass
class _Prepared:
    """One walked file, read and hashed outside the event loop."""

    key: str
    title: str = ""
    mime: str = ""
    digest: str = ""
    mtime: int = 0
    doc_id: int | None = None
    pieces: list = field(default_factory=list)
    unchanged: bool = False
    error: str = ""


def _prepare(
    conn: sqlite3.Connection,
    collection: str,
    path: Path,
    *,
    max_bytes: int,
    chunk_tokens: int,
    overlap_tokens: int,
    stamp: int,
) -> _Prepared:
    """Read, hash and chunk one file. Runs in a worker thread.

    ``load`` reads from disk and chunking is CPU work, so neither belongs on
    the loop. The doc lookup is a cheap read next to the daemon's own writes.
    """
    prepared = _Prepared(key=str(path))
    try:
        loaded = load(path, max_bytes=max_bytes)
    except LoadError as exc:
        prepared.error = f"{path.name}: {exc}"
        return prepared
    if not loaded.text.strip():
        prepared.error = f"{path.name}: no extractable text"
        return prepared

    prepared.title = loaded.title
    prepared.mime = loaded.mime
    prepared.digest = _digest(loaded.text)
    row = conn.execute(
        "SELECT id, hash, deleted_at FROM docs WHERE collection = ? AND path = ?",
        (collection, prepared.key),
    ).fetchone()
    if row is not None and row["hash"] == prepared.digest and row["deleted_at"] is None:
        prepared.unchanged = True
        return prepared

    try:
        prepared.mtime = int(path.stat().st_mtime * 1000)
    except OSError:
        prepared.mtime = stamp
    prepared.doc_id = int(row["id"]) if row is not None else None
    prepared.pieces = chunk_document(
        loaded.text, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens
    )
    return prepared


def _apply(
    conn: sqlite3.Connection,
    collection: str,
    prepared: _Prepared,
    vectors: list[list[float]] | None,
    stamp: int,
) -> tuple[bool, int]:
    """Write the doc row and its chunks. Returns (added, chunks stored)."""
    added = prepared.doc_id is None
    # Upsert on the (collection, path) key: embedding is awaited between the
    # lookup and this write, so a second ingest of the same file must not race
    # into a duplicate row.
    conn.execute(
        "INSERT INTO docs (collection, path, title, mime, hash, mtime, indexed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(collection, path) DO UPDATE SET title = excluded.title,"
        " mime = excluded.mime, hash = excluded.hash, mtime = excluded.mtime,"
        " indexed_at = excluded.indexed_at, deleted_at = NULL",
        (
            collection,
            prepared.key,
            prepared.title,
            prepared.mime,
            prepared.digest,
            prepared.mtime,
            stamp,
        ),
    )
    row = conn.execute(
        "SELECT id FROM docs WHERE collection = ? AND path = ?",
        (collection, prepared.key),
    ).fetchone()
    doc_id = int(row["id"]) if row is not None else 0
    _drop_chunks(conn, doc_id)
    return added, _store_chunks(conn, doc_id, prepared.pieces, vectors)


async def ingest(
    conn: sqlite3.Connection,
    embedder: Embedder | None,
    paths: list[str],
    *,
    collection: str = "default",
    workspace: str | None = None,
    chunk_tokens: int = 700,
    overlap_tokens: int = 80,
    max_bytes: int = 2_000_000,
    prune: bool = False,
    turn_id: str | None = None,
    suffixes: frozenset[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> IngestReport:
    # Walking the tree and reading files is disk-bound; a large ingest must not
    # freeze audio, events or pending confirmations while it runs.
    files, roots, errors = await asyncio.to_thread(walk, paths, workspace, suffixes)
    report = IngestReport(collection=collection, errors=errors)
    seen: set[str] = set()
    stamp = int(time.time() * 1000)
    total = len(files)

    def step(done: int) -> None:
        """How far a pass has got, for a caller that reports it to a person."""
        if progress is not None:
            progress(done, total)

    step(0)

    for position, path in enumerate(files, start=1):
        prepared = await asyncio.to_thread(
            _prepare,
            conn,
            collection,
            path,
            max_bytes=max_bytes,
            chunk_tokens=chunk_tokens,
            overlap_tokens=overlap_tokens,
            stamp=stamp,
        )
        # Seen even when it failed to load: a broken file is not a missing one.
        seen.add(prepared.key)
        if prepared.error:
            report.errors.append(prepared.error)
            step(position)
            continue
        if prepared.unchanged:
            report.unchanged += 1
            step(position)
            continue

        vectors = (
            await _embed(embedder, [piece.text for piece in prepared.pieces])
            if prepared.pieces
            else None
        )
        added, chunks = await asyncio.to_thread(
            _apply, conn, collection, prepared, vectors, stamp
        )
        report.added += 1 if added else 0
        report.updated += 0 if added else 1
        report.chunks += chunks
        report.files.append(prepared.key)
        step(position)

    if prune and roots:
        report.removed = await asyncio.to_thread(
            prune_under, conn, collection, roots, seen
        )

    await asyncio.to_thread(
        db.trace,
        conn,
        span="rag",
        name="rag.ingest",
        turn_id=turn_id,
        payload={
            "collection": collection,
            "added": report.added,
            "updated": report.updated,
            "unchanged": report.unchanged,
            "removed": report.removed,
            "chunks": report.chunks,
            "errors": report.errors[:10],
        },
    )
    log.info(
        "ingest %s: +%d ~%d =%d -%d (%d chunks)",
        collection,
        report.added,
        report.updated,
        report.unchanged,
        report.removed,
        report.chunks,
    )
    return report


def _under(path: str, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it, by real path boundary.

    ``C:/notes-archive/x.md`` is not under ``C:/notes``, even though the string
    starts with it.
    """
    try:
        return Path(path).is_relative_to(root)
    except (OSError, ValueError):
        return False


def prune_under(
    conn: sqlite3.Connection,
    collection: str,
    roots: list[Path],
    seen: set[str],
) -> int:
    """Tombstone docs under ``roots`` that were not present in this walk."""
    rows = conn.execute(
        "SELECT id, path FROM docs WHERE collection = ? AND deleted_at IS NULL",
        (collection,),
    ).fetchall()

    stamp = int(time.time() * 1000)
    removed = 0
    for row in rows:
        path = str(row["path"])
        if path in seen:
            continue
        if not any(_under(path, root) for root in roots):
            continue
        _drop_chunks(conn, int(row["id"]))
        conn.execute("UPDATE docs SET deleted_at = ? WHERE id = ?", (stamp, int(row["id"])))
        removed += 1
    return removed


def forget(
    conn: sqlite3.Connection, *, collection: str | None = None, path: str | None = None
) -> int:
    """Tombstone docs by path or collection. Returns how many were removed."""
    sql = "SELECT id FROM docs WHERE deleted_at IS NULL"
    params: list[object] = []
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
    if path:
        sql += " AND path = ?"
        params.append(path)
    rows = conn.execute(sql, params).fetchall()
    stamp = int(time.time() * 1000)
    for row in rows:
        _drop_chunks(conn, int(row["id"]))
        conn.execute("UPDATE docs SET deleted_at = ? WHERE id = ?", (stamp, int(row["id"])))
    return len(rows)


def collections(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT d.collection AS name, COUNT(DISTINCT d.id) AS docs,"
        " COUNT(c.id) AS chunks, MAX(d.indexed_at) AS indexed_at"
        " FROM docs d LEFT JOIN chunks c ON c.doc_id = d.id"
        " WHERE d.deleted_at IS NULL GROUP BY d.collection ORDER BY d.collection"
    ).fetchall()
    return [dict(row) for row in rows]


def count_docs(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM docs WHERE deleted_at IS NULL"
    ).fetchone()
    return int(row["n"] if row else 0)
