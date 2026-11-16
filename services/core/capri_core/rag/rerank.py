"""Reranking, with an offline fallback.

``llama-server --reranking`` exposes ``POST /rerank``. LM Studio does not, and
neither does a daemon with no model loaded, so a lexical scorer stands in. The
caller gets scores either way and never learns which one answered — that is
deliberate: retrieval quality may degrade, retrieval itself may not fail.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

import httpx

from ..config import CapriConfig

log = logging.getLogger("capri.rag.rerank")

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "it", "this", "that", "i", "you", "we", "do",
    "does", "did", "so", "if", "at", "as", "by", "from", "what", "which",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _WORD.findall((text or "").lower())
        if len(token) > 2 and token not in _STOP
    }


def lexical_scores(query: str, documents: list[str]) -> list[float]:
    """IDF-free overlap. Cheap, deterministic, and hard to surprise."""
    wanted = _tokens(query)
    if not wanted:
        return [0.0 for _ in documents]
    scores: list[float] = []
    for document in documents:
        found = _tokens(document)
        if not found:
            scores.append(0.0)
            continue
        overlap = len(wanted & found)
        # Coverage of the query matters more than chunk length.
        scores.append(overlap / len(wanted))
    return scores


class Reranker(Protocol):
    name: str

    async def rerank(self, query: str, documents: list[str]) -> list[float] | None: ...


class HeuristicReranker:
    name = "lexical"

    async def rerank(self, query: str, documents: list[str]) -> list[float] | None:
        return lexical_scores(query, documents)


class EndpointReranker:
    """llama-server's /rerank, or anything that speaks the same shape."""

    name = "endpoint"

    def __init__(self, config: CapriConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = config.models.rerank
        self.base = config.models.endpoint.rstrip("/")
        self._client = client
        self._own_client = client is None

    def reconfigure(self, config: CapriConfig) -> None:
        self.cfg = config.models.rerank
        self.base = config.models.endpoint.strip().rstrip("/")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=4.0))
        return self._client

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def rerank(self, query: str, documents: list[str]) -> list[float] | None:
        if not documents:
            return []
        payload: dict[str, object] = {
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        }
        if self.cfg.name.strip():
            payload["model"] = self.cfg.name.strip()
        try:
            response = await self._http().post(
                f"{self.base}/rerank",
                json=payload,
            )
            if response.status_code >= 400:
                log.info("rerank returned HTTP %s; using lexical", response.status_code)
                return lexical_scores(query, documents)
            data = response.json()
            results = data.get("results")
            if not isinstance(results, list) or len(results) != len(documents):
                return lexical_scores(query, documents)
            scores = [0.0] * len(documents)
            for entry in results:
                index = int(entry.get("index", -1))
                if 0 <= index < len(scores):
                    scores[index] = float(
                        entry.get("relevance_score", entry.get("score", 0.0)) or 0.0
                    )
            return scores
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            log.info("rerank unavailable (%s); using lexical", exc)
            return lexical_scores(query, documents)


def build_reranker(config: CapriConfig) -> Reranker:
    if not config.models.rerank.enabled:
        return HeuristicReranker()
    return EndpointReranker(config)


async def aclose(reranker: Reranker | None) -> None:
    closer = getattr(reranker, "aclose", None)
    if closer is not None:
        await closer()
