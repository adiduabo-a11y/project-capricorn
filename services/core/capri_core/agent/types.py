"""Shared types for the model layer and the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class Attachment:
    """One transient image for one model call.

    Never persisted: not in messages, not in traces, not in artifacts. The text
    content of the message must describe it well enough to replay without it.
    """

    kind: Literal["image"] = "image"
    data_url: str = ""
    label: str = ""


class Msg(dict):
    """A chat message. A plain dict, because that is what the wire wants.

    Attachments live on the object, not in the dict, so a plain ``json.dumps``
    of the message cannot leak base64 into the shard by accident.
    """

    def __init__(
        self, role: Role, content: str, attachments: list[Attachment] | None = None
    ) -> None:
        super().__init__(role=role, content=content)
        self.attachments: tuple[Attachment, ...] = tuple(attachments or ())


def wire_messages(msgs: list[Msg]) -> list[dict[str, Any]]:
    """OpenAI-style payloads, expanding attachments into content blocks.

    A message with no attachments serializes exactly as before, which keeps
    text-only servers and every existing prompt untouched.
    """
    wire: list[dict[str, Any]] = []
    for msg in msgs:
        attachments = getattr(msg, "attachments", ())
        if not attachments:
            wire.append(dict(msg))
            continue
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": str(msg.get("content") or "")}
        ]
        for item in attachments:
            blocks.append({"type": "image_url", "image_url": {"url": item.data_url}})
        wire.append({"role": msg.get("role"), "content": blocks})
    return wire


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
    # True when the router could not obtain a valid planner object. The loop
    # shows the message but does not persist it as an assistant turn.
    failed: bool = False

    @property
    def is_final(self) -> bool:
        return self.tool == "done"


class ModelUnavailable(RuntimeError):
    """The configured endpoint could not serve the request."""


class ToolArgsError(ValueError):
    """The model produced arguments that do not satisfy the tool schema."""
