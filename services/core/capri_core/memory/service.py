"""Memory facade used by the server and the turn loop.

One object owns the writer, the retriever, the embedder and decay. The turn
loop only sees three methods: ``recall_block``, ``observe_turn`` and
``maintain``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..config import CapriConfig
from ..store import vector
from . import decay, repo, retrieval
from .embed import Embedder, build_embedder
from .writer import MemoryWriter

log = logging.getLogger("capri.memory")


class MemoryService:
    def __init__(
        self,
        config: CapriConfig,
        conn: sqlite3.Connection,
        brain: Any,
        subject: str,
        embedder: Embedder | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.subject = subject
        self.embedder = embedder if embedder is not None else build_embedder(config)
        self.writer = MemoryWriter(conn, brain, self.embedder, config, subject)

    async def aclose(self) -> None:
        closer = getattr(self.embedder, "aclose", None)
        if closer is not None:
            await closer()

    def reconfigure(self, config: CapriConfig) -> None:
        """Keep the writer and embedder pointed at the same live server."""
        self.config = config
        self.writer.config = config
        reconfigure = getattr(self.embedder, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(config)

    # ------------------------------------------------------------------ read

    async def recall_block(
        self,
        query: str,
        *,
        episode_id: int | None = None,
        workspace_id: str = "",
    ) -> str:
        hits = await retrieval.recall(
            self.conn,
            self.embedder,
            self.subject,
            query,
            limit=self.config.memory.recall_items,
            budget_tokens=self.config.memory.recall_budget_tokens,
            halflife_days=self.config.memory.decay_halflife_days,
            workspace_id=workspace_id,
        )
        if not hits:
            return ""
        log.info("recalled %d memories", len(hits))
        return retrieval.format_context(hits)

    async def search(
        self, query: str, limit: int = 10, *, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await retrieval.search(
            self.conn,
            self.embedder,
            self.subject,
            query,
            limit=limit,
            halflife_days=self.config.memory.decay_halflife_days,
            workspace_id=workspace_id,
        )

    def list(self, *, kind: str | None = None, since: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return repo.list_memories(
            self.conn, self.subject, kind=kind, since=since, limit=limit
        )

    # ------------------------------------------------------------------ write

    async def observe_turn(
        self,
        *,
        turn_id: str,
        user_text: str,
        observations: list[str],
        episode_id: int | None = None,
        source_msg: int | None = None,
        workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        if not self.config.memory.extract_after_turn:
            return []
        try:
            return await self.writer.observe(
                turn_id=turn_id,
                user_text=user_text,
                observations=observations,
                episode_id=episode_id,
                source_msg=source_msg,
                workspace_id=workspace_id,
            )
        except Exception:
            log.exception("memory writer failed; turn is unaffected")
            return []

    async def write(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Explicit user-authored memories (memory.write)."""
        prepared: list[dict[str, Any]] = []
        for entry in items[:16]:
            if not isinstance(entry, dict):
                continue
            text = " ".join(str(entry.get("text") or "").split())[:400]
            if len(text) < 3:
                continue
            kind = str(entry.get("kind") or "fact").lower()
            entities = [
                " ".join(str(name).split())[:80]
                for name in (entry.get("entities") or [])
                if str(name).strip()
            ][:6]
            prepared.append(
                {
                    "kind": kind if kind in repo.KINDS else "fact",
                    "text": text,
                    "importance": max(0.0, min(1.0, float(entry.get("importance", 0.6)))),
                    "confidence": max(0.0, min(1.0, float(entry.get("confidence", 0.95)))),
                    "entities": entities,
                }
            )
        if not prepared:
            return []

        vectors: list[list[float] | None]
        try:
            vectors = list(await self.embedder.embed([item["text"] for item in prepared]))
        except Exception as exc:
            log.info("explicit memory write without vectors: %s", exc)
            vectors = [None] * len(prepared)

        out: list[dict[str, Any]] = []
        for item, item_vector in zip(prepared, vectors):
            # Reuse the writer's dedupe/supersede rules.
            out.append(
                self.writer._store(  # noqa: SLF001 - same subsystem, one rulebook
                    item,
                    item_vector,
                    episode_id=None,
                    source_msg=None,
                )
            )
        return out

    def forget(
        self, *, ids: list[int] | None = None, query: str | None = None
    ) -> list[int]:
        return repo.forget(self.conn, self.subject, ids=ids, query=query)

    async def merge(self, ids: list[int]) -> dict[str, Any]:
        """Fold duplicate memories into one row (memory.merge).

        The longest text survives, confidence is the highest of the set, and
        the folded rows point at the survivor so recall and decay see one
        memory. Their vectors go with them.
        """
        wanted = list(dict.fromkeys(int(i) for i in ids))
        live = [
            row
            for row in (repo.get(self.conn, memory_id) for memory_id in wanted)
            if row is not None and not row["deleted_at"] and not row["superseded_by"]
        ]
        if len(live) < 2:
            raise ValueError("merge needs two live memories")

        kept = max(live, key=lambda row: len(str(row["text"])))
        kept_id = int(kept["id"])
        repo.merge_into(
            self.conn,
            kept_id,
            text=str(kept["text"]),
            confidence=max(float(row["confidence"]) for row in live),
        )
        removed = [int(row["id"]) for row in live if int(row["id"]) != kept_id]
        for memory_id in removed:
            repo.supersede(self.conn, memory_id, kept_id)
            self.conn.execute("DELETE FROM memory_vec WHERE row_ref = ?", (memory_id,))

        # A survivor that predates working embeddings still deserves a vector.
        if self.conn.execute(
            "SELECT 1 FROM memory_vec WHERE row_ref = ?", (kept_id,)
        ).fetchone() is None:
            try:
                embedded = await self.embedder.embed([str(kept["text"])])
                vector.upsert(self.conn, "memory_vec", kept_id, embedded[0])
            except Exception as exc:
                log.info("merged memory left without a vector: %s", exc)

        row = repo.get(self.conn, kept_id) or kept
        return {"kept": dict(row), "removed": removed}

    def maintain(self, *, protect_episode: int | None = None) -> dict[str, Any]:
        return decay.maintain(
            self.conn,
            self.subject,
            halflife_days=self.config.memory.decay_halflife_days,
            threshold=self.config.memory.prune_below,
            protect_episode=protect_episode,
        )

    def count(self) -> int:
        return repo.count(self.conn, self.subject)
