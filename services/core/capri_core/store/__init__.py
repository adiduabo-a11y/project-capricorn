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
from .vector import DEFAULT_DIM, backend_of, dimension_of, ensure_vector_tables, search, upsert

__all__ = [
    "DEFAULT_DIM",
    "backend_of",
    "connect",
    "dimension_of",
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
