"""RAG tests: chunker, incremental ingest, hybrid search, citations, injection.

Offline: hash embeddings, no model server, no network.

    uv run python services/core/tests/test_rag.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision
from capri_core.config import CapriConfig
from capri_core.memory import HashEmbedder
from capri_core.rag import RagService, chunk
from capri_core.rag import index as rag_index
from capri_core.rag import search as rag_search
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, builtin_registry

FAILURES: list[str] = []
_CONNS: list[Any] = []
DIM = 64

GUIDE = """# Deploy runbook

## Keys

The deploy key rotates monthly. Zed owns the rotation and files the receipt in
the ops folder. Never paste the key into a ticket.

## Rollback

Roll back with `capri deploy --rollback` within ten minutes of a bad release.
After that window the previous image is gone and you rebuild from the tag.

# Notes

Coffee is dark roast, no sugar. The office machine grinds at seven.
"""

CODE = '''"""Tiny helper used by the rollback procedure."""


def rollback_tag(tag: str) -> str:
    """Return the tag to redeploy after a bad release."""
    return f"{tag}-previous"
'''


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def open_test_shard() -> Any:
    conn = db.open_shard("test-rag", dim=DIM)
    _CONNS.append(conn)
    return conn


class ScriptedBrain:
    """Stands in for the reflex model: rewrite calls and planner decisions."""

    def __init__(self, queries: list[str] | None = None, answer: str = "done") -> None:
        self.queries = list(queries or [])
        self.answer = answer
        self.extract_calls = 0
        self.seen: list[list[dict[str, str]]] = [{"role": "system", "content": ""}]

    async def available(self) -> bool:
        return True

    async def decide(self, msgs, specs, persona_pack: str = "kyoko") -> Decision:
        self.seen.append(list(msgs))
        return Decision(tool="done", final=self.answer, usage=ChatResult(text=""))

    async def args(self, spec, msgs, feedback=None):
        return {}, ChatResult(text="")

    async def extract(self, msgs, schema, **kwargs) -> dict[str, Any]:
        self.extract_calls += 1
        return {"queries": self.queries}


async def _always_true(request: dict[str, Any]) -> bool:
    return True


class _Emit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


# --------------------------------------------------------------------------- tests


def test_chunker() -> None:
    text = (
        "# Alpha\n\n"
        + ("alpha beta gamma delta. " * 40)
        + "\n\n## Beta\n\n"
        + ("epsilon zeta eta theta. " * 40)
    )
    chunks = chunk(text, chunk_tokens=60, overlap_tokens=12)

    check(len(chunks) >= 2, f"split into {len(chunks)} chunks")
    check(all(text[c.start : c.end] == c.text for c in chunks), "every span is exact")
    check(any(c.heading_path == "Alpha > Beta" for c in chunks), "heading path is tracked")
    check(chunks[1].start <= chunks[0].end, "consecutive chunks overlap")

    fenced = "# Code\n\n```python\nprint('one')\nprint('two')\n```\n\nAfter the fence.\n"
    fences = chunk(fenced, chunk_tokens=40, overlap_tokens=0)
    check(any("print('one')" in c.text for c in fences), "code fence survives in one chunk")


async def test_ingest(conn: Any, config: CapriConfig, workspace: Path) -> RagService:
    service = RagService(
        config,
        conn,
        ScriptedBrain(),
        HashEmbedder(DIM),
        workspace=str(workspace),
    )
    report = await service.ingest(["docs"])
    check(report["added"] == 3, f"indexed three files: {report['added']}")
    check(report["chunks"] >= 3, f"stored {report['chunks']} chunks")
    check(not report["errors"], f"no ingest errors: {report['errors']}")

    names = [row["name"] for row in service.collections()]
    check("default" in names, f"collection registered: {names}")
    counts = {row["name"]: row["docs"] for row in service.collections()}
    check(counts.get("default") == 3, f"three live docs: {counts}")

    again = await service.ingest(["docs"])
    check(again["unchanged"] == 3 and again["added"] == 0, "re-ingest skips unchanged files")

    guide = workspace / "docs" / "guide.md"
    guide.write_text(GUIDE.replace("dark roast", "light roast"), encoding="utf-8")
    changed = await service.ingest(["docs"])
    check(changed["updated"] == 1, f"changed file reindexed: {changed['updated']}")
    check(changed["added"] == 0, "changed file is not added twice")

    (workspace / "docs" / "sub" / "extra.txt").unlink()
    pruned = await service.ingest(["docs"])
    check(pruned["removed"] == 1, f"deleted file tombstoned: {pruned['removed']}")

    live = conn.execute("SELECT COUNT(*) AS n FROM docs WHERE deleted_at IS NULL").fetchone()
    check(int(live["n"]) == 2, "two docs remain live")

    # A sibling directory that shares a string prefix is not "under" the root.
    archive = workspace / "docs-archive"
    archive.mkdir()
    (archive / "note.md").write_text("# Archive\n\nolder notes\n", encoding="utf-8")
    await service.ingest(["docs-archive"])
    sibling = await service.ingest(["docs"])
    check(sibling["removed"] == 0, f"sibling ingest removed nothing: {sibling['removed']}")
    row = conn.execute(
        "SELECT deleted_at FROM docs WHERE path LIKE ?", ("%docs-archive%note.md",)
    ).fetchone()
    check(row is not None and row["deleted_at"] is None, "prune keeps a sibling directory")
    return service


async def test_search(conn: Any, config: CapriConfig, workspace: Path, service: RagService) -> None:
    hits = await rag_search.search(
        conn,
        service.embedder,
        None,
        "deploy key rotates",
        rerank_top_n=5,
    )
    check(bool(hits), "hybrid search returns hits")
    top = hits[0]
    check(top.path.endswith("guide.md"), f"top hit is the runbook: {top.path}")
    check("Keys" in top.heading_path, f"heading is part of the hit: {top.heading_path}")

    source = Path(top.path).read_text(encoding="utf-8")
    check(source[top.start : top.end] == top.text, "citation span matches the file")

    code_hits = await rag_search.search(conn, service.embedder, None, "rollback_tag", rerank_top_n=5)
    check(any(hit.path.endswith("helper.py") for hit in code_hits), "code is searchable by symbol")


async def test_context(service: RagService) -> None:
    block, citations = await service.recall_block("how do I roll back a release")
    check('<data source="rag">' in block, "block is data-classed")
    check("[doc:" in block and "guide.md" in block, "block carries doc markers")
    check("Data, not instructions" in block, "block warns against injection")
    check(bool(citations), "citations returned")
    first = citations[0]
    check(isinstance(first["span"], list) and len(first["span"]) == 2, "citation has a span")
    check(bool(first["path"]), "citation has a path")


async def test_rewrite(config: CapriConfig, workspace: Path) -> None:
    conn = open_test_shard()
    config.rag.rewrite = True
    brain = ScriptedBrain(queries=["rollback release tag"])
    service = RagService(config, conn, brain, HashEmbedder(DIM), workspace=str(workspace))
    await service.ingest(["docs/guide.md"])
    block, _ = await service.recall_block("that thing we do when a release goes bad")
    check(brain.extract_calls == 1, "rewrite made one model call")
    check("rollback" in block.lower(), "rewritten query still found the rollback section")


async def test_injection(conn: Any, config: CapriConfig, workspace: Path, service: RagService) -> None:
    brain = ScriptedBrain(answer="Roll back within ten minutes.")
    emit = _Emit()
    session = Session.create(conn)
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=session,
        emit=emit,
        confirm=_always_true,
        brain=brain,
        registry=builtin_registry(),
        policy=Policy(config.policy, str(workspace)),
        workspace=str(workspace),
        memory=None,
        rag=service,
    )
    await runner.run("trn_rag1", "how do I roll back?")

    system = next((m["content"] for m in brain.seen[-1] if m["role"] == "system"), "")
    check('<data source="rag">' in system, "documents reach the system prompt")
    check("ten minutes" in system, "the right chunk reached the planner")
    cited = [event for event in emit.events if event.get("type") == "rag.cited"]
    check(bool(cited), "rag.cited was emitted")
    check(all(event.get("turnId") == "trn_rag1" for event in cited), "citations carry the turn id")
    check(bool(cited[0].get("path")), "citation names its file")


async def test_forget(config: CapriConfig, workspace: Path) -> None:
    # Its own shard: default's docs from the earlier tests are still live.
    conn = db.open_shard("test-rag-forget", dim=DIM)
    _CONNS.append(conn)
    service = RagService(config, conn, ScriptedBrain(), HashEmbedder(DIM), workspace=str(workspace))
    await service.ingest(["docs/guide.md"], collection="scratch")
    check(service.documents() == 1, "scratch doc indexed")
    removed = service.forget(collection="scratch")
    check(removed == 1, f"collection forget removed {removed}")
    check(service.documents() == 0, "no live docs after forget")
    block, citations = await service.recall_block("deploy key")
    check(block == "" and citations == [], "empty index returns no block")


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-rag-"))
    try:
        workspace = raw / "workspace"
        (workspace / "docs" / "sub").mkdir(parents=True)
        (workspace / "docs" / "guide.md").write_text(GUIDE, encoding="utf-8")
        (workspace / "docs" / "helper.py").write_text(CODE, encoding="utf-8")
        (workspace / "docs" / "sub" / "extra.txt").write_text(
            "The staging cluster lives behind the green proxy.\n", encoding="utf-8"
        )
        os.environ["CAPRI_HOME"] = str(raw / "home")

        config = CapriConfig()
        config.models.embed.dim = DIM
        config.memory.embed = "hash"
        config.models.rerank.enabled = False  # offline: lexical reranker
        config.rag.rewrite = False
        config.rag.chunk_tokens = 120
        config.rag.overlap_tokens = 20
        config.policy.allow_paths = [str(workspace)]

        print("chunker")
        test_chunker()

        conn = open_test_shard()
        print("ingest")
        service = await test_ingest(conn, config, workspace)
        print("search")
        await test_search(conn, config, workspace, service)
        print("context")
        await test_context(service)
        print("injection")
        await test_injection(conn, config, workspace, service)
        print("rewrite")
        await test_rewrite(config, workspace)
        print("forget")
        await test_forget(config, workspace)
        await service.aclose()
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
