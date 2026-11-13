"""The shard: durable state for one subject."""

from .db import (
    connect,
    get_meta,
    migrate,
    open_shard,
    recent_traces,
    set_meta,
    trace,
)
from .vector import DIM, backend_of, ensure_vector_tables, search, upsert

__all__ = [
    "DIM",
    "backend_of",
    "connect",
    "ensure_vector_tables",
    "get_meta",
    "migrate",
    "open_shard",
    "recent_traces",
    "search",
    "set_meta",
    "trace",
    "upsert",
]
