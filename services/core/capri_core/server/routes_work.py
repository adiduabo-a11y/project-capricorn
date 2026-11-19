"""Project workspace, work diff/undo, task, and Blackwall tool handlers."""

from __future__ import annotations

import os
import uuid
from typing import Any

from ..agent import prompt
from ..agent.prompt import WorkContext
from ..store import db
from ..tools.policy import resolve_path
from ..work import store as work_store
from ..work.tasks import Task
from ..workspace import WorkspaceError
from .rpc import Client, RpcError
from .session import Session

_is_conversation = prompt.looks_like_conversation


class WorkRoutesMixin:
    """Workspace lifecycle, work tracking, task, and Blackwall handlers."""

    async def _project_open(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        raw = str(params.get("path") or "").strip()
        if not raw:
            raise RpcError("invalid_args", "path is required")
        try:
            workspace = self.workspaces.open(raw)
        except WorkspaceError as exc:
            raise RpcError(exc.code, str(exc)) from exc
        self._adopt_workspace(workspace)
        await self._workspace_changed("open")
        return {
            "workspace": self.workspaces.current(),
            "inspection": self.workspaces.inspection,
        }

    async def _project_current(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return {
            "workspace": self.workspaces.current(),
            "inspection": self.workspaces.inspection,
        }

    async def _project_recent(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        limit = int(params.get("limit") or 8)
        return {"items": self.workspaces.recent(limit)}

    async def _project_close(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        try:
            closed = self.workspaces.close()
        except WorkspaceError as exc:
            raise RpcError(exc.code, str(exc)) from exc
        if closed:
            self._release_workspace()
            await self._workspace_changed("close")
        return {"closed": closed}

    async def _project_tree(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        path = str(params.get("path") or "")
        try:
            depth = int(params.get("depth") or 3)
        except (TypeError, ValueError) as exc:
            raise RpcError("invalid_args", "depth must be an integer") from exc
        try:
            return self.workspaces.tree(path, depth)
        except WorkspaceError as exc:
            raise RpcError(exc.code, str(exc)) from exc

    async def _project_inspect(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        try:
            return self.workspaces.inspect()
        except WorkspaceError as exc:
            raise RpcError(exc.code, str(exc)) from exc

    async def _work_changes(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        active = self.workspaces.active
        task_id = str(params.get("taskId") or params.get("task_id") or "")
        limit = int(params.get("limit") or 40)
        items = work_store.recent_changes(
            self.conn,
            workspace_root=active.root if active is not None else "",
            task_id=task_id,
            limit=limit,
        )
        return {"items": items, "taskId": task_id}

    async def _work_artifact(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        artifact_id = str(
            params.get("artifactId") or params.get("artifact_id") or ""
        ).strip()
        if not artifact_id:
            raise RpcError("invalid_args", "artifactId is required")
        row = work_store.get_artifact(self.conn, artifact_id)
        if row is None:
            raise RpcError("not_found", f"no artifact {artifact_id!r}")
        return {"artifact": row}

    async def _work_undo(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        backup_id = str(params.get("backupId") or params.get("backup_id") or "")
        raw_path = str(params.get("path") or "").strip()
        target = resolve_path(raw_path, self.workspace) if raw_path else ""
        if not backup_id and not target:
            raise RpcError("invalid_args", "backupId or path is required")
        active = self.workspaces.active
        # No task id: a workspace is not a task, and M3 has no task rows yet.
        result = work_store.restore(
            self.conn,
            backup_id=backup_id,
            path=target,
            turn_id=f"undo_{uuid.uuid4().hex[:10]}",
        )
        if result.get("ok"):
            db.trace(
                self.conn,
                span="work",
                name="work.undo",
                workspace_id=active.id if active is not None else None,
                payload={"backup": backup_id, "path": target},
            )
        return result

    async def _tools_list(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "tier": spec.tier,
                    "params": spec.params,
                    "version": spec.version,
                    "source": spec.source,
                }
                for spec in self.registry.specs()
            ],
            "workspace": self.workspace,
        }

    async def _tools_stats(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return {"tools": db.tool_stats(self.conn, int(params.get("limit") or 100))}

    async def _tools_proposals(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return {"proposals": self.blackwall.list_proposals()}

    async def _tools_installed(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return {"tools": self.blackwall.list_installed()}

    async def _tools_recipes(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        return {"recipes": self.blackwall.list_recipes()}

    async def _tools_recipe(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        raw = params.get("recipe")
        if not isinstance(raw, dict):
            raise RpcError("invalid_args", "recipe must be an object")
        result = self.blackwall.install_recipe(raw)
        if not result.get("ok"):
            raise RpcError("invalid_args", str(result.get("output") or "recipe rejected"))
        return result

    async def _tools_rollback(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        name = str(params.get("name") or "").strip()
        if not name:
            raise RpcError("invalid_args", "name is required")
        result = self.blackwall.quarantine(name)
        if not result.get("ok"):
            raise RpcError("not_found", str(result.get("output") or "tool not found"))
        return result

    def _adopt_workspace(self, workspace: Any) -> None:
        """Point the whole process at the project the user just opened.

        Policy, screen and RAG all read a root path; keeping them in lockstep
        is what makes "inside the active workspace" mean one thing. A switch
        while a turn runs is refused in ``WorkspaceService``, so no call can
        see half of one project and half of another.
        """
        self.workspace = workspace.root
        self.policy.set_workspace(workspace.root, active=True, trusted=workspace.trusted)
        self.screen.workspace = workspace.root
        if getattr(self, "rag", None) is not None:
            self.rag.workspace = workspace.root
        self.policy.set_detected_commands(
            workspace.root, self.workspaces.confident_commands()
        )

    def _release_workspace(self) -> None:
        root = os.path.abspath(self.config.policy.workspace or os.getcwd())
        self.workspace = root
        self.policy.set_workspace(root, active=False)
        self.screen.workspace = root
        if getattr(self, "rag", None) is not None:
            self.rag.workspace = root

    def _task_for_turn(self, text: str, session: Session) -> Task | None:
        """The task this turn belongs to, creating one when needed.

        Only a turn with a project open gets a task: outside a project there is
        nothing for the task to be about. Greetings and corrections are not
        jobs either, even while a task is open: they are answered as chat, so
        they neither continue the work nor resurrect a paused one.

        A chat opened by `session.new` is detached until it has a task of its
        own: it does not adopt the task that was already open, so the first
        turn cannot answer from that task's summary and observations. Asking
        for work there starts a new task; only `task.continue` reaches back.
        """
        workspace = self.workspaces.active
        if workspace is None:
            return None
        if _is_conversation(text):
            return None
        current = self.tasks.open_for(workspace.id) if session.adopt_open_task else None
        if current is not None:
            # Work in progress absorbs more work, and speaking to a paused task
            # with a new instruction is how it goes back to work.
            return self.tasks.resume(current.id) if current.status == "paused" else current
        task = self.tasks.create(workspace.id, text)
        # This chat owns a task now; later turns attach to it normally.
        session.adopt_open_task = True
        return task

    async def _broadcast_task(self, task: Task | None, reason: str = "changed") -> None:
        await self._broadcast_event(
            {
                "type": "task.changed",
                "reason": reason,
                "task": task.as_dict() if task is not None else None,
            }
        )

    def _task_target(self, params: dict[str, Any]) -> Task:
        task_id = str(params.get("taskId") or params.get("task_id") or "").strip()
        if task_id:
            task = self.tasks.get(task_id)
        else:
            active = self.workspaces.active
            task = self.tasks.open_for(active.id) if active is not None else None
        if task is None:
            raise RpcError("not_found", "no task to work on")
        return task

    async def _task_current(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        active = self.workspaces.active
        task: Task | None = None
        if active is not None:
            task = self.tasks.open_for(active.id)
            if task is None:
                recent = self.tasks.recent(active.id, limit=1)
                task = recent[0] if recent else None
        if task is None:
            return {"task": None, "open": False}
        return {"task": task.as_dict(), "open": task.open}

    async def _task_recent(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        active = self.workspaces.active
        limit = max(1, min(int(params.get("limit") or 10), 50))
        items = self.tasks.recent(active.id, limit=limit) if active is not None else []
        return {"items": [task.as_dict() for task in items]}

    async def _task_continue(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(client)
        task = self._task_target(params)
        if task.status == "abandoned":
            raise RpcError("invalid_args", "that task was abandoned")
        if self._turns_busy():
            raise RpcError("busy", "a turn is already running")
        resumed = self.tasks.resume(task.id)
        if resumed is None:
            raise RpcError("not_found", "no task to work on")
        # This was explicit: the chat works on that task from here on.
        session.adopt_open_task = True
        await self._broadcast_task(resumed, reason="resume")
        turn_id = await self._start_turn(
            client, session, "Continue the task.", task=resumed, resume=True
        )
        return {"turnId": turn_id, "taskId": resumed.id}

    async def _task_abandon(self, client: Client, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(client)
        if self._turns_busy():
            raise RpcError("busy", "let the turn finish before abandoning it")
        task = self._task_target(params)
        abandoned = self.tasks.abandon(task.id)
        await self._broadcast_task(abandoned, reason="abandon")
        return {"task": abandoned.as_dict() if abandoned is not None else None}

    def _work_context(self, task: Task | None = None, *, resume: bool = False) -> WorkContext | None:
        workspace = self.workspaces.active
        if workspace is None:
            return None
        info = self.workspaces.inspection or {}
        commands = tuple(
            (str(item.get("kind") or ""), str(item.get("command") or ""))
            for item in info.get("commands") or []
            if item.get("confident")
        )
        return WorkContext(
            workspace_id=workspace.id,
            name=workspace.name,
            root=workspace.root,
            task_id=task.id if task is not None else "",
            task_title=task.title if task is not None else "",
            task_status=task.status if task is not None else "",
            task_summary=str(task.summary or "") if task is not None else "",
            # Only a continuation needs the observations: an ordinary turn has
            # the transcript.
            task_observations=(
                tuple(self.tasks.observations(task.id))
                if resume and task is not None
                else ()
            ),
            commands=commands,
            conventions=str(info.get("conventions") or ""),
        )

    async def _workspace_changed(self, reason: str) -> None:
        await self._broadcast_event(
            {
                "type": "workspace.changed",
                "reason": reason,
                "workspace": self.workspaces.current(),
            }
        )
