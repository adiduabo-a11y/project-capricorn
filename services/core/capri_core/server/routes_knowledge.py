"""Memory, vault notes, and RAG document route handlers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from ..config import save_config, set_value
from ..notes import VaultError
from ..pathsafe import resolve as resolve_path_safe
from ..tools.policy import resolve_path
from . import protocol
from .rpc import Client, RpcError


class KnowledgeRoutesMixin:
    """Memory, notes vault, and RAG knowledge handlers."""

    async def _memory_list(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        limit = max(1, min(int(params.get("limit") or 50), 500))
        kind = str(params.get("kind") or "").strip() or None
        since = params.get("since")
        items = self.memory.list(
            kind=kind,
            since=int(since) if since is not None else None,
            limit=limit,
        )
        return {"items": items, "count": self.memory.count()}

    async def _memory_search(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        query = str(params.get("q") or "").strip()
        if not query:
            raise RpcError("invalid_args", "q is required")
        limit = max(1, min(int(params.get("k") or 10), 50))
        active = self.workspaces.active
        items = await self.memory.search(
            query, limit, workspace_id=active.id if active is not None else ""
        )
        return {"items": items, "mode": "hybrid"}

    async def _memory_write(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        raw = params.get("items")
        if not isinstance(raw, list) or not raw:
            raise RpcError("invalid_args", "items must be a non-empty list")
        items = await self.memory.write(raw)
        for item in items:
            await client.send(protocol.event("memory.written", **item))
        return {"items": items}

    async def _memory_forget(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        ids = params.get("ids") or []
        query = str(params.get("q") or "").strip() or None
        if not ids and not query:
            raise RpcError("invalid_args", "ids or q is required")
        removed = self.memory.forget(
            ids=[int(i) for i in ids] if isinstance(ids, list) else [],
            query=query,
        )
        return {"forgotten": removed}

    async def _memory_merge(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        raw = params.get("ids")
        if not isinstance(raw, list):
            raise RpcError("invalid_args", "ids must be a list of memory ids")
        ids = list(dict.fromkeys(int(i) for i in raw))
        if len(ids) < 2:
            raise RpcError("invalid_args", "merge needs at least two memory ids")
        return await self.memory.merge(ids)

    async def _memory_maintain(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(client)
        result = self.memory.maintain(protect_episode=session.episode_id)
        return result

    async def _notes_status(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return self.notes.status()

    def _notes_state(self, state: dict[str, Any]) -> None:
        """Every open panel hears how the vault pass is going, without polling.

        The Settings panel asks `notes.status` while a pass runs, but the rest
        of the app had no way to know a vault was being read. This is a push, so
        the line appears in whatever panel is open and clears itself at the end.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # nothing is listening yet; there is no loop to send on
            return
        task = loop.create_task(self._broadcast_event(protocol.event("notes.index", **state)))
        self._notes_events.add(task)
        task.add_done_callback(self._notes_events.discard)

    async def _notes_set(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        """Point her at a vault, or clear it. Saving starts an index pass."""
        self._require_session(client)
        raw = params.get("path")
        if not isinstance(raw, str):
            raise RpcError("invalid_args", "path is required; send an empty string to clear it")
        text = raw.strip()
        if text:
            folder = Path(resolve_path_safe(os.path.expanduser(text)))
            verdict, reason = self.policy.classify_path(str(folder))
            if verdict == "block":
                raise RpcError("invalid_args", f"{text}: {reason}")
            if not folder.is_dir():
                raise RpcError("invalid_args", f"no folder at {folder}")
            text = str(folder)
        try:
            updated = set_value(self.config, "notes.vault", text)
        except (KeyError, ValueError) as exc:
            raise RpcError("invalid_args", str(exc)) from exc
        try:
            save_config(updated)
        except OSError as exc:
            raise RpcError("internal", f"could not write config: {exc}") from exc
        self.config = updated
        self.notes.reconfigure(updated)
        self.rag.reconfigure(updated)
        if not text:
            # Stop a pass that is still running, or it would write the folder it
            # started on back into a collection this call is emptying.
            await self.notes.cancel_index()
            removed = self.notes.clear()
            return {**self.notes.status(), "removed": removed}
        try:
            # The first pass runs in the background: a large vault would
            # otherwise hold the reply - and the panel - for minutes. The reply
            # carries `indexing`, and the panel polls until it is done.
            return await self.notes.start_index()
        except VaultError as exc:
            raise RpcError("invalid_args", str(exc)) from exc

    async def _notes_index(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        try:
            return await self.notes.index()
        except VaultError as exc:
            raise RpcError("invalid_args", str(exc)) from exc

    async def _rag_ingest(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        raw = params.get("paths", params.get("path"))
        if isinstance(raw, str):
            paths = [raw.strip()] if raw.strip() else []
        elif isinstance(raw, list):
            paths = [str(entry).strip() for entry in raw if str(entry).strip()]
        else:
            paths = []
        if not paths:
            raise RpcError("invalid_args", "paths is required")

        # Refuse denied roots before reading a single byte. Ingest is a read,
        # but the deny list exists precisely so reads cannot wander.
        for entry in paths:
            target = resolve_path(entry, self.workspace)
            verdict, reason = self.policy.classify_path(target)
            if verdict == "block" or (
                verdict != "allow" and not self.policy.in_workspace(target)
            ):
                raise RpcError("invalid_args", f"{entry}: {reason}")

        collection = str(params.get("collection") or "").strip() or None
        return await self.rag.ingest(paths, collection=collection)

    async def _rag_search(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        query = str(params.get("q") or "").strip()
        if not query:
            raise RpcError("invalid_args", "q is required")
        limit = max(1, min(int(params.get("k") or self.config.rag.rerank_top_n), 50))
        collections = params.get("collections")
        if not isinstance(collections, list):
            collections = None
        items = await self.rag.search(
            query,
            k=limit,
            collections=[str(name) for name in collections] if collections else None,
        )
        return {"items": items, "mode": "hybrid"}

    async def _rag_collections(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return {
            "collections": self.rag.collections(),
            "documents": self.rag.documents(),
        }

    async def _rag_forget(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        collection = str(params.get("collection") or "").strip() or None
        path = str(params.get("path") or "").strip() or None
        if not collection and not path:
            raise RpcError("invalid_args", "collection or path is required")
        return {"removed": self.rag.forget(collection=collection, path=path)}
