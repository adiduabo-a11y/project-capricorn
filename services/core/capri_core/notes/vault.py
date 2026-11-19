"""An Obsidian vault, as the one folder Capri keeps notes in.

A vault is a pile of small markdown files, so the whole integration is a folder
setting plus four verbs: search, read, write, index. Everything she does stays
inside that folder - the path guard here is the only thing standing between "add
a note" and "rewrite something in Documents", and it is checked on every call
rather than at setup time, because the vault can be moved or renamed later.

Indexing rides on the document index she already has: the vault becomes the
`vault` collection, so a note is recalled in the same block as everything else
she has read, and a note deleted from the vault is pruned from the index on the
next pass. Obsidian's own bookkeeping (`.obsidian`, `.trash`) is never walked, so
workspace layouts and plugin state do not end up in her memory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import pathsafe

log = logging.getLogger("capri.notes")

COLLECTION = "vault"
SUFFIXES = frozenset({".md", ".markdown"})
# Obsidian keeps its own state in dot-directories; the rest is noise a vault can
# accumulate from being a git repo or a place code was dumped.
SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".venv", "venv"})
MAX_NOTE_BYTES = 400_000
MAX_NOTE_FILES = 5_000
MODES = frozenset({"create", "append", "replace"})
# The wiki block the turn starts with: a handful of notes, not the whole vault.
WIKI_ITEMS = 4
WIKI_BUDGET_TOKENS = 900
# Seconds between progress tellings while a pass runs. The end of a pass, and
# its start, are always told at once; a thousand notes should not be.
STATE_INTERVAL = 1.0


class VaultError(Exception):
    """A path or a request the vault refuses, with the reason for the person."""


class VaultService:
    # The RAG collection the notes live in. Read by the loop so a note can be
    # kept out of the general document block without importing this package.
    collection = COLLECTION

    def __init__(
        self,
        config: Any,
        conn: Any,
        rag: Any = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.rag = rag
        # Told about every pass, so a panel that is not the one asking can show
        # that the vault is being read. Called from the event loop; must return.
        self.on_state = on_state
        self._state_at = 0.0
        # One pass at a time: a refresh asked for while the first one is still
        # going waits for it instead of racing it over the same collection.
        self._index_lock = asyncio.Lock()
        self._index_task: asyncio.Task[Any] | None = None
        self._progress: dict[str, Any] = {
            "running": False,
            "done": 0,
            "total": 0,
            "startedAt": 0,
            "error": "",
        }

    def reconfigure(self, config: Any) -> None:
        self.config = config

    # ------------------------------------------------------------------ the folder

    @property
    def raw(self) -> str:
        return str(getattr(self.config.notes, "vault", "") or "").strip()

    def root(self) -> Path:
        """The vault folder, canonical, or a refusal that says what to do."""
        raw = self.raw
        if not raw:
            raise VaultError(
                "no Obsidian vault is set up yet: choose the folder in Settings -> Notes"
            )
        path = Path(pathsafe.resolve(os.path.expanduser(raw)))
        if not path.is_dir():
            raise VaultError(f"the vault folder is not there any more: {path}")
        return path

    def note_path(self, note: str) -> Path:
        """Resolve a note name inside the vault, refusing anything that leaves it."""
        text = str(note or "").strip().replace("\\", "/")
        if not text:
            raise VaultError("a note path is required, for example 'Inbox/idea.md'")
        root = self.root()
        target = Path(text) if os.path.isabs(text) else root / text
        if not pathsafe.within(str(target), str(root)):
            raise VaultError(f"{note!r} is outside the vault")
        parts = Path(text.replace(root.as_posix() + "/", "")).parts
        if Path(parts[-1] if parts else text).suffix.lower() not in SUFFIXES:
            raise VaultError("notes are markdown: give the path a .md suffix")
        if any(part.lower() in SKIP_DIRS for part in parts[:-1]) or any(
            part.startswith(".") for part in parts[:-1]
        ):
            raise VaultError(f"{note!r} is inside a folder Obsidian keeps for itself")
        return target

    def relative(self, path: Path | str) -> str:
        target = Path(path)
        try:
            return target.relative_to(self.root()).as_posix()
        except ValueError:
            return target.as_posix()

    def _walk(self) -> list[Path]:
        root = self.root()
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".") and name.lower() not in SKIP_DIRS
            )
            for name in sorted(filenames):
                candidate = Path(dirpath) / name
                if candidate.suffix.lower() in SUFFIXES:
                    found.append(candidate)
                    if len(found) >= MAX_NOTE_FILES:
                        return found
        return found

    def notes(self) -> list[str]:
        """Every note in the vault, as vault-relative paths. Empty when unset."""
        try:
            return [self.relative(path) for path in self._walk()]
        except (VaultError, OSError):
            return []

    def status(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "vault": self.raw,
            "exists": False,
            "notes": 0,
            "indexed": 0,
            "chunks": 0,
            "lastIndexed": 0,
            "collection": COLLECTION,
            "recall": bool(getattr(self.config.notes, "recall", True)),
            # Present even when idle, so the panel can tell "not running" from
            # "this core never says".
            "indexing": dict(self._progress),
            "error": "",
        }
        try:
            root = self.root()
        except VaultError as exc:
            # An unset vault is not an error; a missing folder is.
            data["error"] = str(exc) if self.raw else ""
            return data
        data["vault"] = str(root)
        data["exists"] = True
        try:
            data["notes"] = len(self._walk())
        except OSError as exc:
            data["error"] = str(exc)
        if self.rag is not None:
            for row in self.rag.collections():
                if row.get("name") != COLLECTION:
                    continue
                data["indexed"] = int(row.get("docs") or 0)
                data["chunks"] = int(row.get("chunks") or 0)
                data["lastIndexed"] = int(row.get("indexed_at") or 0)
        return data

    # ------------------------------------------------------------------ indexing

    async def index(self, *, turn_id: str | None = None) -> dict[str, Any]:
        """Bring the vault collection up to date with the folder."""
        if self.rag is None:
            raise VaultError("the document index is not available in this mode")
        root = self.root()
        started = time.monotonic()
        async with self._index_lock:
            self._progress.update(
                running=True, done=0, total=0, startedAt=int(time.time()), error=""
            )
            try:
                # Markdown only: a vault can hold PDFs and spreadsheets, and
                # they are not what "her notes" means. Passing the folder (not
                # the files) is what lets the ingest prune a note deleted in
                # Obsidian.
                report = await self.rag.ingest(
                    [str(root)],
                    collection=COLLECTION,
                    prune=True,
                    turn_id=turn_id,
                    suffixes=SUFFIXES,
                    progress=self._note_progress,
                )
            except Exception as exc:
                # Kept in the status, and in the telling, before it is raised:
                # the panel should say what went wrong, not "finished".
                self._progress["error"] = str(exc)
                raise
            finally:
                self._progress["running"] = False
                self._emit_state(force=True)
        log.info(
            "indexed vault %s in %.1fs: +%s ~%s =%s -%s",
            root,
            time.monotonic() - started,
            report.get("added"),
            report.get("updated"),
            report.get("unchanged"),
            report.get("removed"),
        )
        return {**self.status(), "report": report}

    def _note_progress(self, done: int, total: int) -> None:
        self._progress.update(done=done, total=total)
        self._emit_state()

    def _emit_state(self, *, force: bool = False) -> None:
        """Tell the listeners how the pass is going, at most once a second."""
        callback = self.on_state
        if callback is None:
            return
        now = time.monotonic()
        if not force and self._progress["running"] and now - self._state_at < STATE_INTERVAL:
            return
        self._state_at = now
        try:
            callback(dict(self._progress))
        except Exception as exc:  # a listener that throws must not stop the pass
            log.info("notes listener failed: %s", exc)

    async def start_index(self, *, turn_id: str | None = None) -> dict[str, Any]:
        """Start a pass in the background; the reply is the status, not the result.

        Choosing a vault used to index inside the RPC, so the panel sat frozen
        on a large one with nothing to show. The folder is validated here, so a
        bad path still fails the call; the pass itself is a task and reports
        through `status()`.
        """
        if self.rag is None:
            raise VaultError("the document index is not available in this mode")
        self.root()
        task = self._index_task
        if task is None or task.done():
            self._progress.update(
                running=True, done=0, total=0, startedAt=int(time.time()), error=""
            )
            self._index_task = asyncio.create_task(
                self._index_quietly(turn_id=turn_id)
            )
        self._emit_state(force=True)
        return self.status()

    async def _index_quietly(self, *, turn_id: str | None = None) -> None:
        """The background pass: a failure lands in the status, not in a traceback."""
        try:
            await self.index(turn_id=turn_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._progress["error"] = str(exc)
            log.warning("vault index stopped: %s", exc)
        finally:
            self._progress["running"] = False
            self._emit_state(force=True)

    async def cancel_index(self) -> None:
        """Stop a running pass. The collection keeps whatever it already has."""
        task = self._index_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._progress["running"] = False
        self._emit_state(force=True)

    def clear(self) -> int:
        """Forget every indexed note, leaving the files alone."""
        if self.rag is None:
            return 0
        return self.rag.forget(collection=COLLECTION)

    # ------------------------------------------------------------------ the wiki

    def wants_recall(self) -> bool:
        """Is the vault her wiki on this turn: set up, switched on, readable?"""
        if self.rag is None or not self.raw:
            return False
        if not bool(getattr(self.config.notes, "recall", True)):
            return False
        try:
            self.root()
        except VaultError:
            return False
        return True

    async def recall_block(self, query: str) -> tuple[str, list[dict[str, Any]]]:
        """The notes that answer this request, as a prompt section.

        No query rewriting: the vault is read on every turn, and a second model
        call per turn to paraphrase the question is not worth a marginal hit.
        """
        if not self.wants_recall():
            return "", []
        text = " ".join(str(query or "").split())
        if not text:
            return "", []
        # Imported here, not at module level: the rag package reaches the agent
        # package on import, and the agent reaches the tool registry, which
        # reaches these note tools. A module-level import closes that loop.
        from ..rag import search as retrieval
        from ..rag.chunker import estimate_tokens

        config = self.rag.config
        hits = await retrieval.search(
            self.conn,
            self.rag.embedder,
            self.rag.reranker,
            text,
            collections=[COLLECTION],
            top_k=config.rag.top_k,
            rerank_top_n=config.rag.rerank_top_n,
            budget_tokens=WIKI_BUDGET_TOKENS,
        )
        packed: list[Any] = []
        used = 0
        for hit in hits:
            # Cite and open notes by their vault path: that is what note.read
            # accepts, and it is the form worth putting in the prompt.
            hit.path = self.relative(hit.path)
            if len(packed) >= WIKI_ITEMS:
                break
            cost = estimate_tokens(hit.text) + 24
            if packed and used + cost > WIKI_BUDGET_TOKENS:
                break
            packed.append(hit)
            used += cost
        if not packed:
            return "", []
        block = retrieval.format_context(
            packed, title="From your notes", source="notes", kind="note"
        )
        log.info("vault recalled %d notes for %r", len(packed), text[:60])
        return block, [hit.as_citation() for hit in packed]

    async def index_note(self, path: Path) -> None:
        """Index one note after writing it, so she can find it in the next turn.

        A full pass would be correct and slow; the note she just wrote is the
        one she is about to look for, and a failure here only leaves the note
        for the next index run.
        """
        if self.rag is None:
            return
        try:
            await self.rag.ingest([str(path)], collection=COLLECTION, prune=False)
        except Exception as exc:  # indexing is an optimisation, not the write
            log.info("could not index %s after writing it: %s", path, exc)

    # ------------------------------------------------------------------ the notes

    async def search(self, query: str, *, limit: int = 6) -> list[dict[str, Any]]:
        text = " ".join(str(query or "").split())
        if not text:
            raise VaultError("a search query is required")
        self.root()
        if self.rag is None:
            raise VaultError("the document index is not available in this mode")
        hits = await self.rag.search(text, k=limit, collections=[COLLECTION])
        notes: list[dict[str, Any]] = []
        for hit in hits:
            path = str(hit.get("path") or "")
            notes.append(
                {
                    "note": self.relative(path) if path else "",
                    "title": str(hit.get("title") or ""),
                    "heading": str(hit.get("heading") or ""),
                    "snippet": " ".join(str(hit.get("text") or "").split())[:400],
                    "score": hit.get("score"),
                }
            )
        return notes

    def read(self, note: str) -> dict[str, Any]:
        path = self.note_path(note)
        if not path.is_file():
            raise VaultError(f"there is no note at {self.relative(path)}")
        size = path.stat().st_size
        if size > MAX_NOTE_BYTES:
            raise VaultError(
                f"{self.relative(path)} is {size} bytes; that is not a note, and too much to read"
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        return {
            "note": self.relative(path),
            "text": text,
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
            "bytes": size,
            "modified": int(path.stat().st_mtime * 1000),
        }

    def write(self, note: str, content: str, *, mode: str = "create") -> dict[str, Any]:
        """Create, append to, or rewrite one note.

        `create` refuses an existing note on purpose: clobbering a page of
        someone's vault because a filename was reused is the one mistake this
        tool must not make by accident. `replace` is the deliberate form.
        """
        path = self.note_path(note)
        kind = str(mode or "create").strip().lower()
        if kind not in MODES:
            raise VaultError("mode must be one of create, append, replace")
        body = "" if content is None else str(content)
        encoded = len(body.encode("utf-8"))
        if encoded > MAX_NOTE_BYTES:
            raise VaultError(f"{encoded} bytes is too much for one note")
        exists = path.exists()
        if kind == "create" and exists:
            raise VaultError(
                f"{self.relative(path)} already exists: use append to add to it, "
                "or replace to rewrite it"
            )
        if kind == "replace" and not exists:
            kind = "create"
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "append" and exists:
            current = path.read_text(encoding="utf-8", errors="replace")
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(("" if not current or current.endswith("\n") else "\n") + body)
        else:
            if body and not body.endswith("\n"):
                body += "\n"
            path.write_text(body, encoding="utf-8", newline="")
        written = path.read_text(encoding="utf-8", errors="replace")
        return {
            "note": self.relative(path),
            "path": str(path),
            "mode": kind,
            "bytes": path.stat().st_size,
            "lines": written.count("\n") + (0 if written.endswith("\n") or not written else 1),
        }
