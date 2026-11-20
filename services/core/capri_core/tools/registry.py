"""The tool registry: manifests plus their implementations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..store import db
from .base import ToolContext, ToolHandler, ToolResult, ToolSpec, elapsed_ms

log = logging.getLogger("capri.tools")


class Registry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}
        # Set by the Blackwall manager. Built-ins are left alone by the callback.
        self.on_quarantine: Any = None

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def unregister(self, name: str) -> ToolSpec | None:
        """Remove a dynamic tool. Returns the spec so callers can restore it."""
        self._handlers.pop(name, None)
        return self._specs.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    async def call(
        self, name: str, ctx: ToolContext, args: dict[str, Any], *, timeout_ms: int | None = None
    ) -> ToolResult:
        spec = self._specs.get(name)
        handler = self._handlers.get(name)
        if spec is None or handler is None:
            return ToolResult.error(f"no such tool: {name}")

        limit = (timeout_ms or spec.timeout_ms) / 1000
        started = asyncio.get_running_loop().time()
        try:
            result = await asyncio.wait_for(handler(ctx, **args), timeout=limit)
        except asyncio.TimeoutError:
            result = ToolResult(
                ok=False,
                output=f"{name} timed out after {limit:.1f}s",
                ms=int((asyncio.get_running_loop().time() - started) * 1000),
            )
        except asyncio.CancelledError:
            raise
        except TypeError as exc:
            # the model supplied an argument the tool does not accept
            result = ToolResult.error(f"{name}: bad arguments ({exc})")
        except Exception as exc:  # a tool must never take the turn down
            log.exception("tool %s failed", name)
            result = ToolResult.error(f"{name} failed: {exc}")

        if result.ms == 0:
            result.ms = elapsed_ms(started)
        cancelled = bool((result.data or {}).get("cancelled"))
        if ctx.conn is not None and not cancelled:
            # A cancelled call says nothing about tool health; counting it as a
            # failure would let three cancel buttons quarantine the tool.
            try:
                streak = db.record_tool_stat(
                    ctx.conn,
                    name,
                    ok=result.ok,
                    ms=result.ms,
                    version=spec.version,
                )
                if not result.ok and streak >= 3 and self.on_quarantine is not None:
                    self.on_quarantine(name)
            except Exception:
                log.exception("could not record tool stats for %s", name)
        return result


def builtin_registry() -> Registry:
    """The built-ins, wired by name."""
    from .builtin import avatar, detail, fs, notes, patch, process, project, screen, shell, web

    registry = Registry()
    registry.register(avatar.PLACE_SPEC, avatar.avatar_place)
    registry.register(web.FETCH_SPEC, web.web_fetch)
    registry.register(process.LIST_SPEC, process.process_list)
    registry.register(process.CLOSE_SPEC, process.process_close)
    registry.register(project.INSPECT_SPEC, project.project_inspect)
    registry.register(project.TREE_SPEC, project.project_tree)
    registry.register(fs.READ_SPEC, fs.fs_read)
    registry.register(patch.PATCH_SPEC, patch.fs_patch)
    registry.register(patch.UNDO_SPEC, patch.fs_undo)
    registry.register(fs.GREP_SPEC, fs.fs_grep)
    registry.register(fs.WRITE_SPEC, fs.fs_write)
    registry.register(fs.LIST_SPEC, fs.fs_list)
    registry.register(notes.SEARCH_SPEC, notes.note_search)
    registry.register(notes.READ_SPEC, notes.note_read)
    registry.register(notes.WRITE_SPEC, notes.note_write)
    registry.register(shell.RUN_SPEC, shell.shell_run)
    registry.register(screen.OBSERVE_SPEC, screen.screen_observe)
    registry.register(screen.WINDOWS_SPEC, screen.screen_windows)
    registry.register(screen.FOCUS_SPEC, screen.screen_focus)
    registry.register(screen.LAUNCH_SPEC, screen.screen_launch)
    registry.register(screen.CLIPBOARD_SPEC, screen.screen_clipboard)
    registry.register(screen.ACT_SPEC, screen.screen_act)
    # Last, and bound to this registry: it reports on whatever is registered,
    # including MCP tools added after startup.
    registry.register(detail.DETAIL_SPEC, detail.make_detail(registry))
    return registry
