"""Tool primitives.

Every capability is a tool: a manifest describing its effects, and one async
function. Nothing in the agent loop calls a subsystem directly.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol


ObservationShape = Literal["default", "file", "command", "grep", "tree", "summary", "screen"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict[str, Any]
    tier: int = 0
    timeout_ms: int = 15_000
    max_calls_per_turn: int = 8
    version: str = "builtin"
    source: str = "builtin"
    # How much of the result the planner gets to see. The loop reads the budget
    # from config by shape; a tool never hardcodes its own character limit.
    observation: ObservationShape = "default"
    # True when a successful call changes something a later call could see
    # (a file, the screen, a shell). The loop uses it to tell a verification
    # re-read apart from a repeat that cannot help.
    mutates: bool = False
    # Consent that depends on arguments rather than the tier: "reads ask
    # first". The catalogue shows it so she knows a tool can ask even when its
    # tier says it does not.
    consent: str = ""


@dataclass
class ScreenState:
    """Screen context shared across one turn's tool calls.

    A ToolContext is built per call, so anything a later call has to see lives
    here, exactly like the read hashes. ``verify`` is armed by a screen action
    that carried an expectation; ``frames`` holds the PNG hashes already sent,
    so the same unchanged pixels do not ride along twice (the tree can stay
    still while a canvas redraws, which is why this is not the UIA digest).
    """

    frames: set[str] = field(default_factory=set)
    verify: bool = False


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    turn_id: str
    subject: str
    workspace: str
    screen: Any = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    # The loop installs this for tools that need a second, purpose-built
    # approval after staging (Blackwall proposals).
    confirm: Callable[[dict[str, Any]], Awaitable[bool]] | None = None
    # A confirmation that carries a decision rather than a yes/no (mcp.register:
    # which servers, trusted how much). The tool proposes the defaults; the
    # answer comes from the person, and a missing channel means no approval.
    choose: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # One event to the shell. A tool that acts outside the daemon — moving her
    # own body — asks the shell rather than pretending the daemon can do it.
    emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    # The live policy, so a tool that detects project commands can feed the
    # shell allowlist. Tools never widen their own permissions.
    policy: Any = None
    # The workspace service. Present so project.* can read the open project;
    # it cannot open or switch one.
    project: Any = None
    # Absolute path -> sha256 at read time, for this turn. fs.read writes here
    # and fs.patch refuses a file whose hash has moved since.
    reads: dict[str, str] = field(default_factory=dict)
    # Attribution for traces, artifacts and backup rows.
    workspace_id: str = ""
    task_id: str = ""
    # Resolved from screen.vision and the configured model. A tool that can
    # produce an image (screen.observe) only attaches one when this is true;
    # the image is transient and never reaches the shard.
    vision: bool = False
    # The request is about looking at the screen. Vision plus this (or a
    # pending verification) is what allows an attachment.
    look: bool = False
    screen_state: ScreenState = field(default_factory=ScreenState)

    def cancelled(self) -> bool:
        return self.cancel.is_set()


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] | None = None
    ms: int = 0
    # Transient. The loop attaches these to the observation message for one
    # call; they are never persisted, traced or replayed.
    attachments: list[Any] = field(default_factory=list)

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


def signature(spec: ToolSpec) -> str:
    """``name(arg, optional?)`` from the schema, so the index cannot drift."""
    required = set(spec.params.get("required") or [])
    props = spec.params.get("properties") or {}
    args = ", ".join(
        f"{name}{'' if name in required else '?'}" for name in props
    )
    return f"{spec.name}({args})"


def consent_note(spec: ToolSpec) -> str:
    """How a call gets approved, in the words the catalogue uses."""
    if spec.consent.strip():
        return spec.consent.strip()
    if spec.tier >= 3:
        return "blocked by policy"
    if spec.tier == 2:
        return "asks the person first"
    return ""


def describe_tool(spec: ToolSpec) -> str:
    """The full contract: what it does, every argument, how it is approved.

    The prompt only carries a one-line index; this is what ``tool.detail``
    returns when she needs the whole thing.
    """
    lines = [signature(spec), spec.description.strip()]
    note = consent_note(spec)
    lines.append(f"Tier {spec.tier}" + (f" — {note}." if note else "."))
    params = spec.params.get("properties") or {}
    required = set(spec.params.get("required") or [])
    if params:
        lines.append("Arguments:")
        for key, value in params.items():
            value = value if isinstance(value, dict) else {}
            kind = value.get("type") or "string"
            if kind == "array" and isinstance(value.get("items"), dict):
                kind = f"array of {value['items'].get('type') or 'string'}"
            flags = "required" if key in required else "optional"
            detail = f"  {key} ({kind}, {flags})"
            enum = value.get("enum")
            if isinstance(enum, list) and enum:
                detail += ": one of " + ", ".join(str(item) for item in enum)
            description = value.get("description")
            if isinstance(description, str) and description.strip():
                detail += ("" if enum else ":") + " " + description.strip()
            lines.append(detail)
    else:
        lines.append("Takes no arguments.")
    if spec.source and spec.source != "builtin":
        lines.append(f"From {spec.source} (version {spec.version}).")
    return "\n".join(lines)
