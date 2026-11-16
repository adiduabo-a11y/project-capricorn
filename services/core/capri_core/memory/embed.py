"""Embeddings over the same OpenAI-compatible endpoint as the brain.

Two implementations:

- ``EndpointEmbedder`` posts to ``/v1/embeddings`` (llama-server, LM Studio,
  Ollama's OpenAI shim). This is the real one.
- ``HashEmbedder`` is a deterministic feature-hash stand-in. It captures
  lexical overlap, not meaning, so it exists for tests and offline work —
  never mix the two inside one shard, the vectors live in different spaces.

Failure to embed is not fatal: the memory writer still stores text and lexical
recall keeps working. Callers catch ``EmbedUnavailable``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol

import httpx
import numpy as np

from ..config import CapriConfig

log = logging.getLogger("capri.memory.embed")

_WORD = re.compile(r"[a-z0-9]+")
BATCH = 32


class EmbedUnavailable(RuntimeError):
    """The embedding model could not be reached or returned the wrong shape."""


class Embedder(Protocol):
    name: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EndpointEmbedder:
    def __init__(self, config: CapriConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = config.models
        self.name = self.cfg.embed.name or "server default"
        self.dim = self.cfg.embed.dim
        self._client = client
        self._own_client = client is None

    def reconfigure(self, config: CapriConfig) -> None:
        """Follow a live settings change without rebuilding the service."""
        self.cfg = config.models
        self.name = self.cfg.embed.name or "server default"
        self.dim = self.cfg.embed.dim

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=4.0)
            )
        return self._client

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        base = self.cfg.endpoint.rstrip("/")
        for start in range(0, len(texts), BATCH):
            batch = [text or " " for text in texts[start : start + BATCH]]
            payload: dict[str, Any] = {"input": batch}
            if self.cfg.embed.name.strip():
                payload["model"] = self.cfg.embed.name.strip()
            try:
                response = await self._http().post(
                    f"{base}/embeddings",
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise EmbedUnavailable(f"cannot reach {base}: {exc}") from exc
            if response.status_code >= 400:
                raise EmbedUnavailable(
                    f"embeddings returned HTTP {response.status_code}: {response.text[:200]}"
                )
            try:
                data = response.json()["data"]
                ordered = sorted(data, key=lambda item: item.get("index", 0))
                vectors = [item["embedding"] for item in ordered]
            except (ValueError, KeyError, TypeError) as exc:
                raise EmbedUnavailable("embeddings response was unreadable") from exc
            if len(vectors) != len(batch):
                raise EmbedUnavailable(
                    f"asked for {len(batch)} embeddings, got {len(vectors)}"
                )
            for vector in vectors:
                if len(vector) != self.dim:
                    raise EmbedUnavailable(
                        f"{self.name} returned {len(vector)} dims, config says {self.dim}. "
                        "Set models.embed.dim to match, then reindex."
                    )
                out.append([float(x) for x in vector])
        return out


class HashEmbedder:
    """Feature hashing: deterministic, offline, lexical."""

    def __init__(self, dim: int = 1024) -> None:
        self.name = "hash"
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text or " ") for text in texts]

    async def aclose(self) -> None:
        return None

    def _one(self, text: str) -> list[float]:
        vector = np.zeros(self.dim, dtype=np.float32)
        words = _WORD.findall(text.lower())
        features: list[tuple[str, float]] = [(word, 1.0) for word in words]
        features += [(f"{a}_{b}", 0.6) for a, b in zip(words, words[1:])]
        if not features:
            features = [("<empty>", 1.0)]
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign * weight
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector.tolist()


def build_embedder(config: CapriConfig) -> Embedder:
    if config.memory.embed == "hash":
        log.info("memory embedder: hash (offline, lexical)")
        return HashEmbedder(config.models.embed.dim)
    return EndpointEmbedder(config)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    left = np.asarray(a, dtype=np.float32)
    right = np.asarray(b, dtype=np.float32)
    if left.shape != right.shape:
        return 0.0
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator == 0.0:
        return 0.0
    return float(left @ right / denominator)
