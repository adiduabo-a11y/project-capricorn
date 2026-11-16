"""Vector storage with a graceful fallback.

Preferred backend is the ``sqlite-vec`` extension. Building that extension on
Windows is occasionally painful, so the same interface is served by a BLOB
column + brute-force cosine scan. Retrieval code never knows which is live.

The dimension is stored in ``meta`` when the tables are created. Changing it
while vectors exist is refused rather than silently mixing incompatible
vectors; an empty shard simply adopts the new dimension.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

import numpy as np

log = logging.getLogger("capri.store.vector")

DEFAULT_DIM = 1024
VEC_TABLES = ("chunk_vec", "memory_vec", "memory_entity_vec")



def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except (sqlite3.Error, AttributeError) as exc:
        log.warning("sqlite-vec present but unusable (%s); using blob backend", exc)
        return False


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def ensure_vector_tables(conn: sqlite3.Connection, dim: int = DEFAULT_DIM) -> str:
    """Create vector tables if needed. Returns the active backend name."""
    if dim < 8 or dim > 8192:
        raise ValueError(f"unreasonable embedding dimension: {dim}")

    existing = conn.execute("SELECT value FROM meta WHERE key = 'vector_dim'").fetchone()
    if existing is not None and int(existing["value"]) != dim:
        stored = int(existing["value"])
        held = {table: _rows(conn, table) for table in VEC_TABLES}
        if any(held.values()):
            raise RuntimeError(
                f"shard holds {stored}-dim vectors ({sum(held.values())} rows) but the "
                f"configured embedder produces {dim}. Point at the same embedding model "
                f"or reindex the shard."
            )
        # Empty tables carry no vectors, so a new embedder can adopt the dim.
        for table in VEC_TABLES:
            if _table_exists(conn, table):
                conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM meta WHERE key = 'vector_dim'")
        log.info("vector dimension %d -> %d (empty tables recreated)", stored, dim)

    backend = "sqlite-vec" if _load_sqlite_vec(conn) else "blob"
    for table in VEC_TABLES:
        if _table_exists(conn, table):
            continue
        if backend == "sqlite-vec":
            conn.execute(
                f"CREATE VIRTUAL TABLE {table} USING vec0("
                f" row_ref INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
            )
        else:
            conn.execute(
                f"CREATE TABLE {table} ("
                " row_ref INTEGER PRIMARY KEY, dim INTEGER NOT NULL, vec BLOB NOT NULL)"
            )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('vector_dim', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(dim),),
    )
    if backend == "blob":
        log.info("vector backend: blob (brute force, %d dims)", dim)
    return backend


def dimension_of(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'vector_dim'").fetchone()
    return int(row["value"]) if row else DEFAULT_DIM


def backend_of(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'vector_backend'").fetchone()
    return row["value"] if row else "blob"


# --------------------------------------------------------------------------- write


def upsert(conn: sqlite3.Connection, table: str, row_ref: int, vector: Sequence[float]) -> None:
    if table not in VEC_TABLES:
        raise ValueError(f"unknown vector table: {table}")
    dim = dimension_of(conn)
    vec = np.asarray(vector, dtype=np.float32)
    if vec.shape != (dim,):
        raise ValueError(f"expected {dim}-dim vector, got {vec.shape}")
    if backend_of(conn) == "sqlite-vec":
        conn.execute(f"DELETE FROM {table} WHERE row_ref = ?", (row_ref,))
        conn.execute(
            f"INSERT INTO {table} (row_ref, embedding) VALUES (?, ?)",
            (row_ref, vec.tobytes()),
        )
    else:
        conn.execute(
            f"INSERT INTO {table} (row_ref, dim, vec) VALUES (?, ?, ?)"
            " ON CONFLICT(row_ref) DO UPDATE SET dim = excluded.dim, vec = excluded.vec",
            (row_ref, dim, vec.tobytes()),
        )


def delete(conn: sqlite3.Connection, table: str, row_ref: int) -> None:
    conn.execute(f"DELETE FROM {table} WHERE row_ref = ?", (row_ref,))


# --------------------------------------------------------------------------- read


def search(
    conn: sqlite3.Connection, table: str, query: Sequence[float], k: int = 32
) -> list[tuple[int, float]]:
    """Return [(row_ref, score)] ordered by descending cosine similarity."""
    if table not in VEC_TABLES:
        raise ValueError(f"unknown vector table: {table}")
    dim = dimension_of(conn)
    q = np.asarray(query, dtype=np.float32)
    if q.shape != (dim,):
        raise ValueError(f"expected {dim}-dim vector, got {q.shape}")
    k = max(1, k)

    if backend_of(conn) == "sqlite-vec":
        rows = conn.execute(
            f"SELECT row_ref, distance FROM {table}"
            " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (q.tobytes(), k),
        ).fetchall()
        return [(int(r["row_ref"]), 1.0 - float(r["distance"])) for r in rows]

    rows = conn.execute(f"SELECT row_ref, vec FROM {table}").fetchall()
    if not rows:
        return []
    ids = np.fromiter((int(r["row_ref"]) for r in rows), dtype=np.int64, count=len(rows))
    mat = np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    scores = mat @ (q / (np.linalg.norm(q) + 1e-9))
    order = np.argsort(-scores)[:k]
    return [(int(ids[i]), float(scores[i])) for i in order]
