"""The tool registry: manifests plus their implementations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import ToolContext, ToolHandler, ToolResult, ToolSpec, elapsed_ms

log = logging.getLogger("capri.tools")


class ToolFailure(Exception):
    pass


class Registry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

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
            return ToolResult(ok=False, output=f"{name} timed out after {limit:.1f}s",
                              ms=int((asyncio.get_running_loop().time() - started) * 1000))
        except asyncio.CancelledError:
            raise
        except TypeError as exc:
            # the model supplied an argument the tool does not accept
            return ToolResult.error(f"{name}: bad arguments ({exc})")
        except Exception as exc:  # a tool must never take the turn down
            log.exception("tool %s failed", name)
            return ToolResult.error(f"{name} failed: {exc}")

        if result.ms == 0:
            result.ms = elapsed_ms(started)
        return result


def builtin_registry() -> Registry:
    """The three Phase 1 builtins, wired by name."""
    from .builtin import fs, shell

    registry = Registry()
    registry.register(fs.READ_SPEC, fs.fs_read)
    registry.register(fs.WRITE_SPEC, fs.fs_write)
    registry.register(fs.LIST_SPEC, fs.fs_list)
    registry.register(shell.RUN_SPEC, shell.shell_run)
    return registry
