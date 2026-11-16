"""Local RAG: loaders, chunking, incremental index, hybrid search, citations."""

from __future__ import annotations

from .service import RagService
from .loaders import LoadError, Loaded, load
from .chunker import Chunk, chunk

__all__ = ["Chunk", "LoadError", "Loaded", "RagService", "chunk", "load"]
