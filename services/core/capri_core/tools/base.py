"""Tool primitives.

Every capability is a tool: a manifest describing its effects, and one async
function. Nothing in the agent loop calls a subsystem directly.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict[str, Any]
    tier: int = 0
    timeout_ms: int = 15_000
    max_calls_per_turn: int = 8


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    turn_id: str
    subject: str
    workspace: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def cancelled(self) -> bool:
        return self.cancel.is_set()


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] | None = None
    ms: int = 0

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(ok=False, output=message)

    def preview(self, limit: int = 400) -> str:
        text = self.output.strip()
        return text if len(text) <= limit else text[:limit] + "…"


ToolHandler = Callable[..., Awaitable[ToolResult]]


class Registry(Protocol):
    def get(self, name: str) -> ToolSpec | None: ...
    def specs(self) -> list[ToolSpec]: ...


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
