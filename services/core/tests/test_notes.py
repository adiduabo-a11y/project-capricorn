"""Notes tests: an Obsidian vault as the one folder she reads and writes.

No model, no network. The vault is a temp directory with the shapes a real one
has - nested folders, an `.obsidian` directory, a file that is not markdown, and
a path that tries to leave - and the shard lives under a temp ``CAPRI_HOME``.

    uv run python services/core/tests/test_notes.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from capri_core.agent import prompt
from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision
from capri_core.config import CapriConfig, set_value
from capri_core.memory.embed import HashEmbedder
from capri_core.notes import VaultError, VaultService
from capri_core.rag import RagService
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy
from capri_core.tools.base import ToolContext
from capri_core.tools.registry import builtin_registry

DIM = 64
FAILURES: list[str] = []
_CONNS: list[Any] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def open_test_shard() -> Any:
    conn = db.open_shard("test-notes", dim=DIM)
    _CONNS.append(conn)
    return conn


class RecordingBrain:
    """Plays one "done" and keeps every message list it was shown."""

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    async def available(self) -> bool:
        return True

    async def decide(
        self, msgs: list[Any], specs: list[Any], persona_pack: str = "kyoko"
    ) -> Decision:
        self.requests.append(list(msgs))
        return Decision(tool="done", final="ok", usage=ChatResult(text="", tokens_in=1, tokens_out=1))

    async def args(self, spec: Any, msgs: list[Any], feedback: str | None = None):
        return {}, ChatResult(text="", tokens_in=1, tokens_out=1)


async def _one_turn(config: CapriConfig, conn: Any, workspace: Path, request: str) -> tuple[
    str, list[dict[str, Any]]
]:
    """Run a turn with no tools taken, and return what the model was shown."""
    service = make_service(config, conn)
    brain = RecordingBrain()
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def confirm(payload: dict[str, Any]) -> bool:
        return True

    runner = TurnRunner(
        config=config,
        conn=conn,
        session=Session.create(conn),
        emit=emit,
        confirm=confirm,
        brain=brain,
        registry=builtin_registry(),
        policy=Policy(config.policy, str(workspace)),
        workspace=str(workspace),
        notes=service,
        rag=service.rag,
    )
    await runner.run("trn_wiki", request)
    system = "\n".join(
        str(row.get("content", "")) for row in brain.requests[0] if row.get("role") == "system"
    )
    return system, events


async def test_turn(config: CapriConfig, conn: Any, vault: Path) -> None:
    print("the wiki reaches the turn")
    workspace = vault.parent
    system, events = await _one_turn(config, conn, workspace, "when is the launch")
    check("## Your notes" in system, "the wiki rules ride with the turn")
    check("note.search" in system, "the note tools are in the index once a vault is set")
    check("## From your notes" in system, "the matching notes ride with the turn")
    check("[note:Projects/launch.md" in system, "the note is marked with its vault path")
    check(system.count("twenty-first of March") == 1, "the note arrives once, not under two headings")
    cited = [
        event
        for event in events
        if event.get("type") == "rag.cited" and str(event.get("path") or "").endswith("launch.md")
    ]
    check(len(cited) >= 1, f"the note is cited as it arrives: {len(cited)}")
    check(cited[0]["path"] == "Projects/launch.md", f"cited by vault path: {cited[0]['path']!r}")

    off = set_value(config, "notes.recall", False)
    plain, _ = await _one_turn(off, conn, workspace, "when is the launch")
    check("## From your notes" not in plain, "switched off, the vault is not read")
    check("## Your notes" not in plain, "and the wiki rules are not there either")
    check("twenty-first of March" in plain, "the note is still an ordinary index it can search")

    # No vault: three lines of index are budget spent on tools that can only
    # refuse. They stay registered, so the refusal still explains itself.
    bare = set_value(config, "notes.vault", "")
    quiet, _ = await _one_turn(bare, conn, workspace, "when is the launch")
    check("note.search" not in quiet, "no vault means the note tools are not advertised")
    check("note.write" not in quiet, "not the writing one either")
    check("## Your notes" not in quiet, "and no wiki rules for a vault that is not there")


def make_vault(root: Path) -> Path:
    """A vault the way Obsidian leaves one: folders, notes, and its own state."""
    (root / "Projects").mkdir(parents=True)
    (root / "Inbox" / "deep").mkdir(parents=True)
    (root / ".obsidian").mkdir(parents=True)
    (root / ".trash").mkdir()
    (root / "Projects" / "launch.md").write_text(
        "---\ntags: [launch]\n---\n\n# Launch\n\nThe launch is on the twenty-first of March.\n",
        encoding="utf-8",
    )
    (root / "Inbox" / "deep" / "idea.md").write_text(
        "# Idea\n\nIndex the vault before drawing the panel.\n",
        encoding="utf-8",
    )
    # Obsidian's own bookkeeping and a non-note file: neither may be touched.
    (root / ".obsidian" / "workspace.md").write_text("# layout\n\nplugin state\n", encoding="utf-8")
    (root / ".trash" / "old.md").write_text("# deleted\n\nshould stay deleted\n", encoding="utf-8")
    (root / "notes.txt").write_text("not a note\n", encoding="utf-8")
    return root


def make_service(config: CapriConfig, conn: Any) -> VaultService:
    rag = RagService(config, conn, None, HashEmbedder(DIM), workspace=None)
    return VaultService(config, conn, rag)


def test_unset(config: CapriConfig, conn: Any) -> None:
    print("unset vault")
    service = make_service(config, conn)
    status = service.status()
    check(status["exists"] is False, "no vault is not an error state")
    check(status["notes"] == 0, "an unset vault has no notes")
    check(status["error"] == "", f"an unset vault says nothing: {status['error']!r}")
    try:
        service.read("Inbox/idea.md")
    except VaultError as exc:
        check("Settings" in str(exc), f"reading without a vault points at the setting: {exc}")
    else:
        check(False, "reading without a vault must refuse")


async def test_index(config: CapriConfig, conn: Any) -> None:
    print("index")
    service = make_service(config, conn)
    status = service.status()
    check(status["exists"] is True, "the folder is found")
    check(status["notes"] == 2, f"only markdown outside Obsidian's folders counts: {status['notes']}")

    report = await service.index()
    check(report["report"]["added"] == 2, f"two notes were added: {report['report']['added']}")
    check("notes.txt" not in " ".join(report["report"].get("files", [])), "the text file stayed out")
    check(
        ".obsidian" not in " ".join(report["report"].get("files", [])),
        "Obsidian's own workspace was not indexed",
    )
    check(report["indexed"] == 2, f"status counts the indexed notes: {report['indexed']}")
    check(report["chunks"] >= 2, f"the notes were chunked: {report['chunks']}")

    again = await service.index()
    check(
        again["report"]["added"] == 0 and again["report"]["unchanged"] == 2,
        f"a second pass changes nothing: {again['report']}",
    )


async def test_search(config: CapriConfig, conn: Any) -> None:
    print("search")
    service = make_service(config, conn)
    hits = await service.search("when is the launch")
    check(len(hits) >= 1, f"the note was recalled: {len(hits)}")
    check(
        any(hit["note"] == "Projects/launch.md" for hit in hits),
        f"the right note came back: {[hit['note'] for hit in hits]}",
    )
    check(any("twenty-first" in hit["snippet"] for hit in hits), "the passage travels with it")
    for blank in ("", "   "):
        try:
            await service.search(blank)
        except VaultError as exc:
            check("query" in str(exc), f"a blank query is refused: {exc}")
        else:
            check(False, "a blank query must be refused")


async def test_background_index(config: CapriConfig, conn: Any) -> None:
    print("background index")
    heard: list[dict[str, Any]] = []
    service = VaultService(
        config,
        conn,
        RagService(config, conn, None, HashEmbedder(DIM), workspace=None),
        on_state=heard.append,
    )
    service.clear()
    status = await service.start_index()
    check(status["indexing"]["running"] is True, "the reply is the status, not the finished pass")
    check(status["indexed"] == 0, f"nothing is indexed when it returns: {status['indexed']}")

    for _ in range(500):
        status = service.status()
        if not status["indexing"]["running"]:
            break
        await asyncio.sleep(0.02)
    check(not status["indexing"]["running"], "the pass finishes without being awaited")
    check(status["indexed"] == 2, f"both notes end up indexed: {status['indexed']}")
    check(
        status["indexing"]["done"] == status["indexing"]["total"] == 2,
        f"progress reached the total: {status['indexing']}",
    )
    check(status["indexing"]["error"] == "", "no error is reported")

    # The panel that is not the Notes one is told, without asking: start, some
    # progress, and the end - not one telling per note.
    check(bool(heard), "a pass is announced to the listeners")
    check(heard[0]["running"] is True, f"the start is announced: {heard[0]}")
    check(
        heard[-1]["running"] is False and heard[-1]["error"] == "",
        f"the end is announced, with no error: {heard[-1]}",
    )
    check(
        heard[-1]["done"] == heard[-1]["total"] == 2,
        f"the end carries the count: {heard[-1]}",
    )
    check(len(heard) <= 6, f"the tellings are throttled: {len(heard)}")

    # Asking twice while one runs does not start a second pass.
    again = await service.start_index()
    stop = await service.start_index()
    check(again["indexed"] == 2 and stop["indexed"] == 2, "a rerun is still a status reply")
    await service.cancel_index()

    missing = set_value(config, "notes.vault", "")
    try:
        await make_service(missing, conn).start_index()
        check(False, "an unset vault is refused before anything starts")
    except VaultError as exc:
        check("Settings" in str(exc), f"an unset vault is refused before anything starts: {exc}")


async def test_wiki(config: CapriConfig, conn: Any, vault: Path) -> None:
    print("wiki")
    service = make_service(config, conn)
    check(service.wants_recall(), "a vault that is set up is read every turn")

    off = set_value(config, "notes.recall", False)
    quiet = make_service(off, conn)
    check(not quiet.wants_recall(), "the wiki can be switched off")
    check(await quiet.recall_block("launch") == ("", []), "switched off means no block")

    block, citations = await service.recall_block("when is the launch")
    check("## From your notes" in block, f"the notes arrive in their own section: {block[:60]!r}")
    check("[note:Projects/launch.md" in block, "the passage is marked as a note, not a document")
    check("[doc:" not in block, "notes are not labelled as documents")
    check(len(citations) >= 1 and citations[0]["path"].endswith("launch.md"), "it cites the note")
    check(len(block) <= 4000, f"the wiki block stays small: {len(block)} chars")

    # The general block leaves the vault out, so a note cannot appear twice.
    unfiltered = await service.rag.search("launch")
    excluded = await service.rag.search("launch", exclude=["vault"])
    check(len(unfiltered) >= 1, f"the vault is searchable: {len(unfiltered)}")
    check(excluded == [], f"excluding the vault leaves nothing else: {len(excluded)}")

    specs = builtin_registry().specs()
    wiki = prompt.system_prompt(
        specs, "kyoko", notes=prompt.NotesContext(path=str(vault), count=2)
    )
    check("## Your notes" in wiki, "the prompt explains the wiki when there is one")
    check("[[its name]]" in wiki and "note.write" in wiki, "it says how to cite and where to write")
    plain = prompt.system_prompt(specs, "kyoko")
    check("## Your notes" not in plain, "a turn with no vault carries no wiki block")
    section = prompt.notes_block(prompt.NotesContext(path=str(vault), count=2))
    check(len(section) <= 800, f"the wiki rules stay short: {len(section)} chars")


def test_read(config: CapriConfig, conn: Any) -> None:
    print("read")
    service = make_service(config, conn)
    note = service.read("Projects/launch.md")
    check("twenty-first" in note["text"], "the whole note comes back")
    check(note["lines"] >= 6, f"frontmatter included: {note['lines']} lines")

    for bad, why in (
        ("../outside.md", "a path out of the vault"),
        (".obsidian/workspace.md", "Obsidian's own state"),
        ("notes.txt", "a file that is not markdown"),
        ("Projects/missing.md", "a note that is not there"),
    ):
        try:
            service.read(bad)
        except VaultError as exc:
            check(True, f"{why} is refused: {exc}")
        else:
            check(False, f"{why} must be refused")


def test_write(config: CapriConfig, conn: Any, vault: Path) -> None:
    print("write")
    service = make_service(config, conn)
    created = service.write("Inbox/new.md", "# New\n\nfirst line\n")
    check(created["mode"] == "create", "a new note is created")
    check((vault / "Inbox" / "new.md").read_text(encoding="utf-8").endswith("\n"), "it ends in a newline")

    try:
        service.write("Inbox/new.md", "clobber\n")
    except VaultError as exc:
        check("already exists" in str(exc), f"create will not overwrite: {exc}")
    else:
        check(False, "create must not overwrite an existing note")

    service.write("Inbox/new.md", "second line", mode="append")
    text = (vault / "Inbox" / "new.md").read_text(encoding="utf-8")
    check("first line" in text and "second line" in text, "append keeps what was there")
    check(not text.endswith("first linesecond line"), "append starts on its own line")

    service.write("Inbox/new.md", "# Replaced\n", mode="replace")
    text = (vault / "Inbox" / "new.md").read_text(encoding="utf-8")
    check(text.startswith("# Replaced"), "replace rewrites the note")
    check("first line" not in text, "replace leaves nothing of the old text")

    for bad, why in (
        ("../escape.md", "a path out of the vault"),
        (".obsidian/plugin.md", "Obsidian's own folder"),
        ("Inbox/new.txt", "a non-markdown suffix"),
        ("Inbox/new.md", "an unknown mode"),
    ):
        try:
            if why == "an unknown mode":
                service.write("Inbox/new.md", "x", mode="upsert")
            else:
                service.write(bad, "x")
        except VaultError as exc:
            check(True, f"{why} is refused: {exc}")
        else:
            check(False, f"{why} must be refused")


async def test_prune(config: CapriConfig, conn: Any, vault: Path) -> None:
    """A note deleted in Obsidian stops being recalled."""
    print("prune")
    service = make_service(config, conn)
    (vault / "Projects" / "launch.md").unlink()
    report = await service.index()
    check(report["report"]["removed"] == 1, f"the deleted note was pruned: {report['report']['removed']}")
    check(report["notes"] == 1, f"the folder count followed: {report['notes']}")
    check(report["indexed"] == 1, f"the index followed: {report['indexed']}")
    hits = await service.search("launch twenty-first")
    check(
        all(hit["note"] != "Projects/launch.md" for hit in hits),
        f"a pruned note is not recalled: {[hit['note'] for hit in hits]}",
    )


async def test_tools(config: CapriConfig, conn: Any, vault: Path) -> None:
    print("tools")
    service = make_service(config, conn)
    await service.index()
    registry = builtin_registry()
    check(
        {"note.search", "note.read", "note.write"} <= set(registry.names()),
        "the note tools are registered",
    )
    spec = registry.get("note.write")
    check(spec is not None and spec.tier == 2, "writing asks first")
    check(spec is not None and spec.mutates, "writing is marked as a mutation")

    ctx = ToolContext(conn=conn, turn_id="trn_notes", subject="test", workspace=str(vault))
    ctx.notes = service

    found = await registry.call("note.search", ctx, {"query": "vault panel"})
    check(found.ok and "idea.md" in found.output, f"note.search finds a note: {found.output[:80]}")

    read = await registry.call("note.read", ctx, {"note": "Inbox/deep/idea.md"})
    check(read.ok and "Index the vault" in read.output, "note.read returns the note")

    wrote = await registry.call(
        "note.write", ctx, {"note": "Inbox/tool.md", "content": "# From the tool\n"}
    )
    check(wrote.ok and (vault / "Inbox" / "tool.md").exists(), f"note.write creates it: {wrote.output}")

    missing = await registry.call("note.read", ctx, {"note": "../escape.md"})
    check(not missing.ok, f"the tool refuses a path outside the vault: {missing.output[:80]}")

    # A tool with no vault answers with the setting, not a stack trace.
    bare = ToolContext(conn=conn, turn_id="trn_bare", subject="test", workspace=str(vault))
    unset = CapriConfig()
    bare.notes = VaultService(unset, conn, None)
    refused = await registry.call("note.search", bare, {"query": "launch"})
    check(not refused.ok and "Settings" in refused.output, f"no vault is explained: {refused.output}")


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-notes-"))
    try:
        os.environ["CAPRI_HOME"] = str(raw / "home")
        vault = make_vault(raw / "vault")
        conn = open_test_shard()
        config = CapriConfig()
        # Lexical reranking only: the test must not look for a model endpoint.
        config = set_value(config, "models.rerank.enabled", False)

        test_unset(config, conn)
        config = set_value(config, "notes.vault", str(vault))
        await test_index(config, conn)
        await test_search(config, conn)
        await test_background_index(config, conn)
        await test_wiki(config, conn, vault)
        await test_turn(config, conn, vault)
        test_read(config, conn)
        await test_prune(config, conn, vault)
        test_write(config, conn, vault)
        await test_tools(config, conn, vault)
    finally:
        for open_conn in _CONNS:
            try:
                open_conn.close()
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
