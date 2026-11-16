"""Observation shaping and message compaction.

A tool returns everything it found; the planner sees a bounded slice of it. One
flat limit cannot serve both ends of the job: an edit needs several kilobytes
of file text, and a failing test reports at the *end* of its output.

Shapes are declared on ``ToolSpec.observation`` and budgeted in
``agent.observation``. Nothing here knows a tool by name.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import ObservationCfg
from ..tools.base import ToolSpec

# Observations carry this prefix so the loop can recognise (and compact) them
# without guessing. It is also what the model sees, so it has to stay stable.
OBSERVATION_PREFIX = "[observation from "
COMPACT_MARK = "[compacted]"
COMPACT_HINT_CHARS = 100
COMMAND_HEAD_CHARS = 1000

# Line-numbered file output looks like "  100: text".
_LINE_RE = re.compile(r"^\s*(\d+): ", re.MULTILINE)
_TOTAL_RE = re.compile(r"of (\d+); continue with start_line=")


def estimate_tokens(text: str) -> int:
    """Cheap, deliberately pessimistic: four characters per token."""
    return max(1, (len(text) + 3) // 4)


def budget_for(spec: ToolSpec, cfg: ObservationCfg) -> int:
    value = getattr(cfg, spec.observation, None) or cfg.default
    return max(200, int(value))


def shape(text: str, spec: ToolSpec, cfg: ObservationCfg) -> str:
    """Bound one tool result for the planner.

    Command output keeps its head and its tail: the first line says what ran,
    the last lines say why it failed. File output keeps whole numbered lines and
    says where to resume, so a truncated read never leaves the planner guessing.
    """
    body = text.strip()
    limit = budget_for(spec, cfg)
    if len(body) <= limit:
        return body

    if spec.observation == "file":
        return _shape_file(body, limit)

    if spec.observation == "command":
        head = min(COMMAND_HEAD_CHARS, max(200, limit // 4))
        tail = max(200, limit - head)
        omitted = len(body) - head - tail
        if omitted > 0:
            return f"{body[:head]}\n[... {omitted} chars elided ...]\n{body[-tail:]}"

    return f"{body[:limit]}\n[... {len(body) - limit} chars elided ...]"


def _shape_file(body: str, limit: int) -> str:
    head = body[:limit]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]

    numbers = [int(number) for number in _LINE_RE.findall(head)]
    if not numbers:
        return f"{head}\n[... {len(body) - len(head)} chars elided ...]"

    last = numbers[-1]
    total_match = _TOTAL_RE.search(body)
    total = int(total_match.group(1)) if total_match else None
    if total is not None and last >= total:
        return head

    remaining = f"{total - last} more lines; " if total is not None else ""
    return f"{head}\n[{remaining}continue with start_line={last + 1}]"


def compact_message(msg: dict[str, Any]) -> tuple[str, str] | None:
    """Collapse one observation to a single line.

    Returns ``(before, after)`` when it changed the message, ``None`` when the
    message is not an observation or has already been compacted.
    """
    content = str(msg.get("content") or "")
    if not content.startswith(OBSERVATION_PREFIX) or COMPACT_MARK in content:
        return None

    header, _, body = content.partition("\n")
    hint = " ".join(body.strip().split())[:COMPACT_HINT_CHARS]
    short = f"{header} {hint} {COMPACT_MARK}".strip()
    if len(short) >= len(content):
        return None
    msg["content"] = short
    return content, short
