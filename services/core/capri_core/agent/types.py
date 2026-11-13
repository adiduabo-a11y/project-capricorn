"""Shared types for the model layer and the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


class Msg(dict):
    """A chat message. A plain dict, because that is what the wire wants."""

    def __init__(self, role: Role, content: str) -> None:
        super().__init__(role=role, content=content)


@dataclass
class ChatResult:
    text: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0


@dataclass
class Decision:
    """One planner step: either call a tool or finish the turn."""

    thought: str = ""
    tool: str = "done"
    final: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    usage: ChatResult | None = None

    @property
    def is_final(self) -> bool:
        return self.tool == "done"


class ModelUnavailable(RuntimeError):
    """The configured endpoint could not serve the request."""


class ToolArgsError(ValueError):
    """The model produced arguments that do not satisfy the tool schema."""
