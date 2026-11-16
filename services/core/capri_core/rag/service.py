"""RAG facade: ingest, query expansion, retrieval, citations.

The turn loop asks one question — "what do you know about this?" — and gets
back a data block plus the citations behind it. Everything else here is in
service of that, and every step is allowed to fail without failing the turn.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..agent.types import Msg
from ..config import CapriConfig
from ..memory.embed import Embedder
from . import index
from . import rerank as reranking
from . import search as retrieval
from .chunker import estimate_tokens

log = logging.getLogger("capri.rag")

_REWRITE_SYSTEM = """You rewrite search queries for a local document index.

Given a question, return 1-3 short queries that would find the answer in a
technical corpus. Keep identifiers, filenames and exact nouns. Do not answer the
question. Return JSON: {"queries": ["...", "..."]}."""

_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}


class RagService:
    def __init__(
        self,
        config: CapriConfig,
        conn: sqlite3.Connection,
        brain: Any = None,
        embedder: Embedder | None = None,
        *,
        workspace: str | None = None,
        reranker: Any = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.brain = brain
        self.embedder = embedder
        self.workspace = workspace
        self.reranker = reranker if reranker is not None else reranking.build_reranker(config)

    async def aclose(self) -> None:
        await reranking.aclose(self.reranker)

    def reconfigure(self, config: CapriConfig) -> None:
        """Follow a live model/endpoint change in the shared reranker."""
        self.config = config
        reconfigure = getattr(self.reranker, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(config)

    # ------------------------------------------------------------------ ingest

    async def ingest(
        self,
        paths: list[str],
        *,
        collection: str | None = None,
        prune: bool = True,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        name = collection or (self.config.rag.collections[0] if self.config.rag.collections else "default")
        report = await index.ingest(
            self.conn,
            self.embedder,
            paths,
            collection=name,
            workspace=self.workspace,
            chunk_tokens=self.config.rag.chunk_tokens,
            overlap_tokens=self.config.rag.overlap_tokens,
            max_bytes=self.config.rag.max_doc_bytes,
            prune=prune,
            turn_id=turn_id,
        )
        result = report.as_dict()
        result["collections"] = index.collections(self.conn)
        return result

    def forget(self, *, collection: str | None = None, path: str | None = None) -> int:
        return index.forget(self.conn, collection=collection, path=path)

    def collections(self) -> list[dict[str, Any]]:
        return index.collections(self.conn)

    def documents(self) -> int:
        return index.count_docs(self.conn)

    # ------------------------------------------------------------------ read

    async def search(
        self,
        query: str,
        *,
        k: int | None = None,
        collections: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        hits = await retrieval.search(
            self.conn,
            self.embedder,
            self.reranker,
            query,
            collections=collections,
            top_k=self.config.rag.top_k,
            rerank_top_n=self.config.rag.rerank_top_n,
            budget_tokens=self.config.rag.context_budget_tokens,
        )
        limit = max(1, min(k or self.config.rag.rerank_top_n, 50))
        return [hit.as_dict() for hit in hits[:limit]]

    async def recall_block(self, query: str) -> tuple[str, list[dict[str, Any]]]:
        """Return (data block, citations). Empty when nothing is indexed."""
        if index.count_docs(self.conn) == 0:
            return "", []
        if not (query or "").strip():
            return "", []

        fused: dict[int, dict[str, Any]] = {}
        for position, variant in enumerate(await self._expand(query)):
            hits = await retrieval.search(
                self.conn,
                self.embedder,
                self.reranker,
                variant,
                top_k=self.config.rag.top_k,
                rerank_top_n=self.config.rag.rerank_top_n,
                budget_tokens=self.config.rag.context_budget_tokens,
            )
            weight = 1.0 if position == 0 else 0.7
            for rank, hit in enumerate(hits):
                entry = fused.setdefault(hit.chunk_id, {"hit": hit, "score": 0.0})
                entry["score"] += weight / (retrieval.RRF_K + rank + 1)

        if not fused:
            return "", []

        ranked = sorted(fused.values(), key=lambda entry: entry["score"], reverse=True)
        packed: list[retrieval.Hit] = []
        used = 0
        for entry in ranked:
            hit: retrieval.Hit = entry["hit"]
            if len(packed) >= self.config.rag.context_items:
                break
            cost = estimate_tokens(hit.text) + 24
            if packed and used + cost > self.config.rag.context_budget_tokens:
                break
            packed.append(hit)
            used += cost

        block = retrieval.format_context(packed)
        citations = [hit.as_citation() for hit in packed]
        log.info("rag recalled %d chunks for %r", len(packed), query[:60])
        return block, citations

    async def _expand(self, query: str) -> list[str]:
        if self.brain is None or not self.config.rag.rewrite:
            return [query]
        try:
            data = await self.brain.extract(
                [Msg("system", _REWRITE_SYSTEM), Msg("user", query[:2000])],
                _QUERY_SCHEMA,
                name="rag_queries",
                max_tokens=220,
            )
        except Exception as exc:  # rewrite is an optimisation, not a dependency
            log.info("query rewrite skipped: %s", exc)
            return [query]

        raw = data.get("queries") if isinstance(data, dict) else None
        wanted = 3 if self.config.rag.multi_query else 1
        out = [query]
        for entry in raw or []:
            if not isinstance(entry, str):
                continue
            cleaned = " ".join(entry.split())[:300]
            if cleaned and cleaned.lower() not in {item.lower() for item in out}:
                out.append(cleaned)
            if len(out) >= 1 + wanted:
                break
        return out
