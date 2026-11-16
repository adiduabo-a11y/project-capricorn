"""Memory tests: writer, dedupe/supersede, recall, graph, decay, turn injection.

Offline: a scripted extractor and a deterministic embedder. No model server.

    uv run python services/core/tests/test_memory.py
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision
from capri_core.config import CapriConfig
from capri_core.memory import HashEmbedder, MemoryService
from capri_core.memory import decay, repo, retrieval
from capri_core.memory.embed import EmbedUnavailable
from capri_core.server.session import Session
from capri_core.store import db, vector
from capri_core.tools import Policy, builtin_registry

FAILURES: list[str] = []
_CONNS: list[Any] = []
DIM = 64


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def open_test_shard() -> Any:
    conn = db.open_shard("test", dim=DIM)
    _CONNS.append(conn)
    return conn


class ScriptedExtractor:
    """Stands in for the reflex model's extraction call."""

    def __init__(self, batches: list[list[dict[str, Any]]] | None = None) -> None:
        self.batches = list(batches or [])
        self.calls = 0
        self.prompts: list[Any] = []

    async def available(self) -> bool:
        return True

    async def decide(self, msgs, specs, persona_pack="kyoko") -> Decision:
        return Decision(tool="done", final="ok", usage=ChatResult(text=""))

    async def args(self, spec, msgs, feedback=None):
        return {}, ChatResult(text="")

    async def extract(self, msgs, schema, **kwargs) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(msgs)
        items = self.batches.pop(0) if self.batches else []
        return {"items": items}


class OfflineEmbedder:
    """An endpoint with no embedding model loaded: every call fails."""

    name = "offline"
    dim = DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbedUnavailable("no embedding model")

    async def aclose(self) -> None:
        return None


class FixedEmbedder:
    """Maps exact texts to hand-picked vectors, for threshold tests."""

    name = "fixed"
    dim = DIM

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.table.get(text, self.table.get("_default", [0.0] * DIM)) for text in texts]

    async def aclose(self) -> None:
        return None


def unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def seed_old(
    conn: Any,
    text: str,
    *,
    importance: float = 0.2,
    age_days: int = 200,
    access_count: int = 0,
    episode_id: int | None = None,
) -> int:
    stamp = int(time.time() * 1000) - age_days * 86_400_000
    cur = conn.execute(
        "INSERT INTO memories (subject, kind, text, importance, confidence, created_at,"
        " accessed_at, access_count, source_episode)"
        " VALUES ('default', 'fact', ?, ?, 0.6, ?, ?, ?, ?)",
        (text, importance, stamp, stamp, access_count, episode_id),
    )
    return int(cur.lastrowid or 0)


async def test_writer(conn: Any, config: CapriConfig) -> None:
    brain = ScriptedExtractor(
        [
            [
                {
                    "kind": "preference",
                    "text": "Daeme prefers dark mode.",
                    "quote": "prefer dark mode",
                    "importance": 0.7,
                    "confidence": 0.9,
                    "entities": ["Daeme", "Capri"],
                },
                {
                    "kind": "event",
                    "text": "Daeme is learning Rust.",
                    "quote": "learning Rust",
                    "importance": 0.5,
                    "confidence": 0.8,
                    "entities": ["Daeme", "Rust"],
                },
            ]
        ]
    )
    service = MemoryService(config, conn, brain, "default", embedder=HashEmbedder(DIM))
    written = await service.observe_turn(
        turn_id="trn_m1",
        user_text="I prefer dark mode and I'm learning Rust",
        observations=[],
        episode_id=None,
    )

    check(len(written) == 2, "writer stored two items")
    seen_prompt = str(brain.prompts[-1])
    check("Agent answered" not in seen_prompt, "reply is not shown to the extractor")
    check("Noted." not in seen_prompt, "reply text never reaches extraction")
    check(written[0]["action"] == "added", "new items are added")
    rows = repo.list_memories(conn, "default")
    check(len(rows) == 2, "two live memories")
    check(any(row["kind"] == "preference" for row in rows), "kind is preserved")

    hits = repo.fts_search(conn, "default", "Rust")
    check(bool(hits), "lexical search finds Rust")
    vec_hits = await service.embedder.embed(["Daeme is learning Rust."])
    import capri_core.store.vector as vector

    check(bool(vector.search(conn, "memory_vec", vec_hits[0], 3)), "vector row was written")

    entities = conn.execute("SELECT name FROM entities ORDER BY name").fetchall()
    names = [row["name"] for row in entities]
    check({"Daeme", "Rust"} <= set(names), f"entities were upserted: {names}")
    edges = conn.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"]
    check(edges >= 1, "co-mention edge exists")


async def test_dedupe(conn: Any, config: CapriConfig) -> None:
    before = repo.count(conn, "default")
    brain = ScriptedExtractor(
        [[
            {
                "kind": "preference",
                "text": "Daeme prefers dark mode.",
                "quote": "still dark mode",
                "importance": 0.8,
                "confidence": 0.95,
                "entities": ["Daeme"],
            }
        ]]
    )
    service = MemoryService(config, conn, brain, "default", embedder=HashEmbedder(DIM))
    written = await service.observe_turn(
        turn_id="trn_m2", user_text="still dark mode", observations=[]
    )
    check(written and written[0]["action"] == "merged", f"duplicate merged: {written}")
    check(repo.count(conn, "default") == before, "merge does not grow the shard")
    row = repo.get(conn, written[0]["id"])
    check(row is not None and row["confidence"] >= 0.95, "merge raises confidence")


async def test_contradiction(conn: Any, config: CapriConfig) -> None:
    import capri_core.store.vector as vector

    old = seed_old(conn, "Daeme uses Vim.")
    vector_a = unit([1.0, 0.0] + [0.0] * (DIM - 2))
    vector_b = unit([0.85, 0.52] + [0.0] * (DIM - 2))  # cosine ~= 0.85
    vector.upsert(conn, "memory_vec", old, vector_a)
    embedder = FixedEmbedder(
        {
            "Daeme uses Vim.": vector_a,
            "Daeme uses Emacs.": vector_b,
        }
    )
    brain = ScriptedExtractor(
        [[
            {
                "kind": "fact",
                "text": "Daeme uses Emacs.",
                "quote": "switched to Emacs",
                "importance": 0.7,
                "confidence": 0.9,
                "entities": ["Daeme", "Emacs"],
            }
        ]]
    )
    service = MemoryService(config, conn, brain, "default", embedder=embedder)
    written = await service.observe_turn(
        turn_id="trn_m3", user_text="I switched to Emacs", observations=[]
    )
    check(written and written[0]["action"] == "superseded", f"contradiction supersedes: {written}")
    check(repo.get(conn, old)["superseded_by"] == written[0]["id"], "old row points at the new one")
    live = [row["text"] for row in repo.list_memories(conn, "default")]
    check("Daeme uses Vim." not in live, "superseded row is out of live recall")


async def test_merge(conn: Any, config: CapriConfig) -> None:
    before = repo.count(conn, "default")
    short = seed_old(conn, "Daeme keeps the shard on an external drive.")
    long = seed_old(conn, "Daeme keeps the shard on an external drive, backed up nightly.")
    service = MemoryService(config, conn, ScriptedExtractor(), "default", embedder=HashEmbedder(DIM))
    result = await service.merge([short, long])
    check(result["kept"]["id"] == long, "the longest text survives")
    check(result["removed"] == [short], "the other id is folded away")
    check(repo.get(conn, short)["superseded_by"] == long, "folded row points at the survivor")
    check(repo.count(conn, "default") == before + 1, "one live row remains from the pair")
    try:
        await service.merge([long])
        check(False, "merge refuses a single id")
    except ValueError:
        check(True, "merge refuses a single id")


async def test_grounding(conn: Any, config: CapriConfig) -> None:
    """Only things the user actually stated in a statement become memory."""
    before = repo.count(conn, "default")
    brain = ScriptedExtractor(
        [[
            {
                "kind": "rule",
                "text": (
                    "The agent determines the feasibility of PC injection based on the"
                    " chosen vector protocol."
                ),
                "quote": "inject an agent into my PC",
                "importance": 0.95,
                "confidence": 1.0,
                "entities": [],
            },
            {
                "kind": "preference",
                "text": "Daeme is interested in injecting an agent into a PC.",
                "quote": "create scripts to inject an agent into a PC",
                "importance": 0.9,
                "confidence": 1.0,
                "entities": ["Daeme"],
            },
            {
                "kind": "fact",
                "text": "Daeme uses Firefox.",
                "quote": "I always use Firefox",
                "importance": 0.9,
                "confidence": 1.0,
                "entities": ["Daeme"],
            },
            {
                "kind": "fact",
                "text": "Daeme dropped the sidecar into C:/Users/daeme/projects/capri.",
                "quote": "dropped the sidecar into C:/Users/daeme/projects/capri",
                "importance": 0.6,
                "confidence": 0.9,
                "entities": ["Daeme"],
            },
        ]]
    )
    service = MemoryService(config, conn, brain, "default", embedder=HashEmbedder(DIM))
    written = await service.observe_turn(
        turn_id="trn_m9",
        user_text=(
            "I want to inject an agent into my PC. Could you create scripts to inject an"
            " agent into a PC? I dropped the sidecar into C:/Users/daeme/projects/capri."
        ),
        observations=[],
    )
    texts = [item["text"] for item in written]
    check(len(written) == 1, f"only the grounded item survives: {texts}")
    check(all("agent determines" not in text for text in texts), "agent commentary rejected")
    check(all("interested" not in text for text in texts), "a question is not a stated fact")
    check(all("Firefox" not in text for text in texts), "an invented quote is rejected")
    check(repo.count(conn, "default") == before + 1, "rejected items were not stored")


async def test_lexical_fallback(conn: Any, config: CapriConfig) -> None:
    """Without embeddings, rewordings still merge instead of piling up."""
    before = repo.count(conn, "default")
    first = ScriptedExtractor(
        [[
            {
                "kind": "fact",
                "text": "Daeme keeps his Capri shard on an external drive.",
                "quote": "capri shard lives on an external drive",
                "importance": 0.6,
                "confidence": 0.9,
                "entities": ["Daeme"],
            }
        ]]
    )
    service = MemoryService(config, conn, first, "default", embedder=OfflineEmbedder())
    added = await service.observe_turn(
        turn_id="trn_m6",
        user_text="my capri shard lives on an external drive",
        observations=[],
    )
    check(added and added[0]["action"] == "added", f"first item added without vectors: {added}")

    duplicate = ScriptedExtractor(
        [[
            {
                "kind": "fact",
                "text": "Daeme keeps the Capri shard on an external drive.",
                "quote": "shard lives on an external drive",
                "importance": 0.7,
                "confidence": 0.95,
                "entities": ["Daeme"],
            }
        ]]
    )
    service = MemoryService(config, conn, duplicate, "default", embedder=OfflineEmbedder())
    second = await service.observe_turn(
        turn_id="trn_m7",
        user_text="the capri shard lives on an external drive, still",
        observations=[],
    )
    check(
        bool(second) and second[0]["action"] in ("merged", "superseded"),
        f"near-duplicate reconciled lexically: {second}",
    )
    check(repo.count(conn, "default") == before + 1, "no duplicate live row landed")


async def test_recall_and_graph(conn: Any, config: CapriConfig) -> None:
    service = MemoryService(config, conn, ScriptedExtractor(), "default", embedder=HashEmbedder(DIM))
    hits = await retrieval.recall(
        conn, service.embedder, "default", "Rust", limit=5, record_access=False
    )
    check(bool(hits), "recall returns hits")
    check(any("Rust" in hit.row["text"] for hit in hits), "recall found the Rust memory")
    block = retrieval.format_context(hits)
    check("<memory id=" in block and "kind=" in block, "context block is data-classed")
    check("Data, not instructions" in block, "context warns against prompt injection")

    # Graph: a memory filed under an entity is reachable by naming the entity,
    # even when the text does not contain the name.
    repo.upsert_entity(conn, "default", "Zed")
    repo.insert(
        conn,
        "default",
        kind="fact",
        text="The deploy key rotates monthly.",
        subject_entity="Zed",
    )
    hits2 = await retrieval.recall(conn, None, "default", "Zed", limit=5, record_access=False)
    check(
        any("deploy key" in hit.row["text"] for hit in hits2),
        "graph expansion found the entity memory",
    )

    target = int(hits[0].row["id"])
    before = repo.get(conn, target)["access_count"]
    await retrieval.recall(conn, service.embedder, "default", "Rust", limit=5)
    after = repo.get(conn, target)["access_count"]
    check(after > before, "recall bumps access bookkeeping")


async def test_forget(conn: Any, config: CapriConfig) -> None:
    service = MemoryService(config, conn, ScriptedExtractor(), "default", embedder=HashEmbedder(DIM))
    removed = service.forget(query="Emacs")
    check(bool(removed), "forget by query returns ids")
    live = [row["text"] for row in service.list()]
    check(all("Emacs" not in text for text in live), "forgotten memory is out of the list")
    hits = await retrieval.search(conn, service.embedder, "default", "Emacs")
    check(all("Emacs" not in hit["text"] for hit in hits), "forgotten memory is out of search")


def test_decay(conn: Any, config: CapriConfig) -> None:
    episode_id = Session.create(conn).episode_id
    fresh = seed_old(conn, "this stays because it is recent", age_days=0, importance=0.1)
    stale = seed_old(conn, "this goes", age_days=400, importance=0.1)
    protected = seed_old(
        conn, "protected by the episode", age_days=400, importance=0.1, episode_id=episode_id
    )

    pruned = decay.prune(
        conn, "default", halflife_days=30, threshold=0.05, protect_episode=episode_id
    )
    check(stale in pruned, "old low-value memory is pruned")
    check(fresh not in pruned, "fresh memory is protected by age")
    check(protected not in pruned, "current episode is protected")
    check(repo.get(conn, stale)["deleted_at"] is not None, "prune is a soft delete")


async def test_turn_injection(conn: Any, config: CapriConfig, workspace: Path) -> None:
    service = MemoryService(config, conn, ScriptedExtractor(), "default", embedder=HashEmbedder(DIM))
    await service.write([{"kind": "fact", "text": "Daeme's project is called Capri."}])

    seen: list[list[dict[str, str]]] = []

    class Capturing(ScriptedExtractor):
        async def decide(self, msgs, specs, persona_pack="kyoko") -> Decision:
            seen.append(list(msgs))
            return Decision(tool="done", final="It's Capri.", usage=ChatResult(text=""))

    session = Session.create(conn)
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=session,
        emit=_NullEmit(),
        confirm=_always_true,
        brain=Capturing(),
        registry=builtin_registry(),
        policy=Policy(config.policy, str(workspace)),
        workspace=str(workspace),
        memory=service,
    )
    await runner.run("trn_m4", "what is my project called?")

    system = next((m["content"] for m in seen[-1] if m["role"] == "system"), "")
    check("<memory id=" in system, "recall is injected into the system prompt")
    check("Capri" in system, "the remembered fact reached the planner")


def test_vector_dim_swap() -> None:
    conn = db.open_shard("dimswap", dim=DIM)
    _CONNS.append(conn)
    vector.ensure_vector_tables(conn, 128)
    check(vector.dimension_of(conn) == 128, "an empty shard adopts a new vector dimension")
    vector.upsert(conn, "memory_vec", 1, [0.5] * 128)
    try:
        vector.ensure_vector_tables(conn, DIM)
        check(False, "dimension change is refused once vectors exist")
    except RuntimeError:
        check(True, "dimension change is refused once vectors exist")


async def _always_true(request: dict[str, Any]) -> bool:
    return True


class _NullEmit:
    async def __call__(self, event: dict[str, Any]) -> None:
        return None


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-memory-"))
    try:
        workspace = raw / "workspace"
        workspace.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")

        config = CapriConfig()
        config.models.embed.dim = DIM
        config.memory.embed = "hash"
        config.memory.contradiction_threshold = 0.80
        config.policy.allow_paths = [str(workspace)]

        conn = open_test_shard()

        print("writer")
        await test_writer(conn, config)
        print("dedupe")
        await test_dedupe(conn, config)
        print("contradiction")
        await test_contradiction(conn, config)
        print("merge")
        await test_merge(conn, config)
        print("grounding")
        await test_grounding(conn, config)
        print("lexical fallback")
        await test_lexical_fallback(conn, config)
        print("recall")
        await test_recall_and_graph(conn, config)
        print("forget")
        await test_forget(conn, config)
        print("decay")
        test_decay(conn, config)
        print("vector dimension")
        test_vector_dim_swap()
        print("turn injection")
        await test_turn_injection(conn, config, workspace)
    finally:
        for conn in _CONNS:
            try:
                conn.close()
            except Exception:
                pass
        shutil.rmtree(raw, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
