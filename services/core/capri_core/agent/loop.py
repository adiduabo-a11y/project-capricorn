"""The agent loop: plan -> act -> observe, until it can answer.

Deliberately boring. One decision per step, one tool per step, an observation
fed straight back. Small local models get lost in anything cleverer, and the
trace reads like a story when each step is a single line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from ..config import CapriConfig
from ..store import db
from ..tools.base import ToolContext, ToolResult
from ..tools.policy import TIER_NAMES, Policy
from ..tools.registry import Registry
from . import prompt
from .types import Decision, ModelUnavailable, Msg, ToolArgsError

log = logging.getLogger("capri.agent")

Emit = Callable[[dict[str, Any]], Awaitable[None]]
Confirm = Callable[[dict[str, Any]], Awaitable[bool]]


class SessionLike(Protocol):
    subject: str
    episode_id: int

    def add_message(self, turn_id: str, role: str, content: str, **kwargs: Any) -> int: ...
    def add_tokens(self, estimate: int) -> None: ...
    def recent_messages(self, limit: int = 40) -> list[dict[str, Any]]: ...


class Brain(Protocol):
    async def available(self) -> bool: ...
    async def decide(
        self, msgs: list[Msg], specs: list[Any], persona_pack: str = ...
    ) -> Decision: ...
    async def args(
        self, spec: Any, msgs: list[Msg], feedback: str | None = None
    ) -> tuple[dict[str, Any], Any]: ...


def chunk_text(text: str, size: int = 3) -> list[str]:
    words = text.split(" ")
    out: list[str] = []
    for index in range(0, len(words), size):
        piece = " ".join(words[index : index + size])
        if index + size < len(words):
            piece += " "
        out.append(piece)
    return out


def _event(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


def _state(value: str, detail: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": value}
    if detail:
        payload["detail"] = detail
    return _event("agent.state", **payload)


@dataclass
class StepCost:
    tokens_in: int = 0
    tokens_out: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        self.tokens_in += int(getattr(usage, "tokens_in", 0) or 0)
        self.tokens_out += int(getattr(usage, "tokens_out", 0) or 0)


class TurnRunner:
    def __init__(
        self,
        config: CapriConfig,
        conn: Any,
        session: SessionLike,
        emit: Emit,
        confirm: Confirm,
        brain: Brain,
        registry: Registry,
        policy: Policy,
        workspace: str,
    ) -> None:
        self.config = config
        self.conn = conn
        self.session = session
        self.emit = emit
        self.confirm = confirm
        self.brain = brain
        self.registry = registry
        self.policy = policy
        self.workspace = workspace

    # ------------------------------------------------------------------ public

    async def run(self, turn_id: str, text: str) -> None:
        started = time.monotonic()
        cost = StepCost()
        self.session.add_message(turn_id, "user", text)
        await self.emit(_state("thinking"))

        try:
            final = await self._drive(turn_id, text, cost, started)
        except asyncio.CancelledError:
            log.info("turn %s cancelled", turn_id)
            await self.emit(_state("idle", "cancelled"))
            raise

        final = final.strip() or "…"
        await self.emit(_state("speaking"))
        await self._stream(turn_id, final)

        self.session.add_message(turn_id, "assistant", final)
        self.session.add_tokens(cost.tokens_in + cost.tokens_out)

        ms = int((time.monotonic() - started) * 1000)
        db.trace(
            self.conn,
            span="turn",
            turn_id=turn_id,
            name="turn.complete",
            payload={"in": text, "out": final},
            tokens_in=cost.tokens_in,
            tokens_out=cost.tokens_out,
            ms=ms,
        )
        await self.emit(
            _event(
                "agent.done",
                turnId=turn_id,
                text=final,
                usage={"in": cost.tokens_in, "out": cost.tokens_out, "ms": ms},
            )
        )
        await self.emit(_state("idle"))

    # ------------------------------------------------------------------ inner

    async def _drive(
        self, turn_id: str, text: str, cost: StepCost, started: float
    ) -> str:
        if not await self.brain.available():
            return self._offline_notice()

        messages = self._history()
        specs = self.registry.specs()
        calls: dict[str, int] = {}
        seen: dict[str, int] = {}
        stalled = 0
        wall_limit = self.config.agent.max_wall_clock_s

        for step in range(1, self.config.agent.max_steps + 1):
            if time.monotonic() - started > wall_limit:
                return (
                    f"I burned the whole {wall_limit}s budget on this. "
                    "You'll want to narrow the ask or raise the limit."
                )

            try:
                decision = await self.brain.decide(
                    messages, specs, self.config.agent.persona_pack
                )
            except ModelUnavailable as exc:
                log.warning("model unavailable mid-turn: %s", exc)
                return "The model fell over mid-run. Signal's gone."

            cost.add(decision.usage)

            if decision.is_final:
                return decision.final or "Done."

            await self.emit(_event("agent.step", turnId=turn_id, step=step,
                                   thought=decision.thought, tool=decision.tool))

            observation, ran = await self._act(
                turn_id, decision, messages, cost, calls, seen
            )
            # Observations ride on the user role. OpenAI-compatible servers
            # (LM Studio in particular) silently drop `role: "tool"` messages
            # that carry no tool_call_id, which leaves the planner blind and
            # looping. The prefix keeps the meaning unambiguous.
            messages.append(Msg("user", observation))
            self.session.add_message(turn_id, "tool", observation)

            # Three refused steps in a row means the planner is stuck, not
            # thinking. Cut the turn rather than burn the remaining budget.
            stalled = 0 if ran else stalled + 1
            if stalled >= 3:
                return (
                    "I'm going in circles: I repeated an action that cannot change "
                    "anything. Point me at the next step."
                )

        return (
            f"I hit the {self.config.agent.max_steps}-step ceiling without landing it. "
            "Tell me to keep going and I'll pick it up from here."
        )

    async def _refuse(
        self,
        turn_id: str,
        name: str,
        args: dict[str, Any] | None,
        *,
        tier: int,
        output: str,
        reason: str | None = None,
    ) -> str:
        """Report a call that will not run, in the UI and in the trace."""
        await self.emit(
            _event(
                "agent.tool",
                turnId=turn_id,
                name=name,
                args=args or {},
                ok=False,
                tier=tier,
                tierName=TIER_NAMES.get(tier, "?"),
                output=output,
                ms=0,
            )
        )
        self._trace_tool(turn_id, name, args, ok=False, ms=0, reason=reason or output)
        return f"[observation from {name}] {output}"

    async def _act(
        self,
        turn_id: str,
        decision: Decision,
        messages: list[Msg],
        cost: StepCost,
        calls: dict[str, int],
        seen: dict[str, int],
    ) -> tuple[str, bool]:
        name = decision.tool
        spec = self.registry.get(name)
        if spec is None:
            return await self._refuse(
                turn_id, name, None, tier=3, output="no such tool"
            ), False

        used = calls.get(name, 0)
        if used >= spec.max_calls_per_turn:
            return await self._refuse(
                turn_id,
                name,
                None,
                tier=spec.tier,
                output=(
                    f"call budget for this turn is spent ({spec.max_calls_per_turn}). "
                    "Use a different tool or answer with done."
                ),
            ), False

        try:
            args, usage = await self.brain.args(spec, messages)
        except ToolArgsError as exc:
            cost.add(getattr(exc, "usage", None))
            return await self._refuse(
                turn_id, name, None, tier=spec.tier, output=f"rejected arguments: {exc}"
            ), False
        except ModelUnavailable:
            return await self._refuse(
                turn_id,
                name,
                None,
                tier=spec.tier,
                output="the model went away while composing arguments",
            ), False
        cost.add(usage)

        signature = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if signature in seen:
            seen[signature] += 1
            return await self._refuse(
                turn_id,
                name,
                args,
                tier=spec.tier,
                output=(
                    "skipped: this exact call already ran this turn and returned the "
                    "same result. Take a different step or answer with done."
                ),
            ), False
        seen[signature] = 1

        verdict = self.policy.apply_confirm_prefs(self.policy.check(spec, args))
        if verdict.verdict == "block":
            return await self._refuse(
                turn_id,
                name,
                args,
                tier=verdict.tier,
                output=f"blocked by policy: {verdict.reason}",
                reason=verdict.reason,
            ), False

        if verdict.verdict == "confirm":
            approved = await self.confirm(
                {
                    "turnId": turn_id,
                    "confirmId": f"cfm_{uuid.uuid4().hex[:10]}",
                    "tool": name,
                    "args": args,
                    "tier": verdict.tier,
                    "tierName": TIER_NAMES.get(verdict.tier, "?"),
                    "reason": verdict.reason,
                    "timeout": self.config.agent.confirm_timeout_s,
                }
            )
            if not approved:
                return await self._refuse(
                    turn_id,
                    name,
                    args,
                    tier=verdict.tier,
                    output="denied by the user. Do not repeat this call.",
                    reason="denied",
                ), False

        calls[name] = used + 1
        await self.emit(_state("acting", name))
        context = ToolContext(
            conn=self.conn,
            turn_id=turn_id,
            subject=self.session.subject,
            workspace=self.workspace,
        )
        result = await self.registry.call(name, context, args)

        await self.emit(
            _event(
                "agent.tool",
                turnId=turn_id,
                name=name,
                args=args,
                ok=result.ok,
                tier=verdict.tier,
                tierName=TIER_NAMES.get(verdict.tier, "?"),
                output=result.preview(),
                ms=result.ms,
            )
        )
        self._trace_tool(turn_id, name, args, ok=result.ok,
                         ms=result.ms, tokens=usage, preview=result.preview(200))
        await self.emit(_state("thinking"))

        return (
            f"[observation from {name}] {'ok' if result.ok else 'error'}\n"
            f"{result.preview(2000)}"
        ), True

    # ------------------------------------------------------------------ utils

    def _history(self) -> list[Msg]:
        system = prompt.system_prompt(
            self.registry.specs(), self.config.agent.persona_pack
        )
        messages = [Msg("system", system)]
        for row in self.session.recent_messages(self.config.agent.history_messages):
            role = str(row.get("role") or "")
            content = str(row.get("content") or "")
            # Stored tool rows replay on the user role, matching the live turn.
            if role in ("user", "assistant", "tool"):
                messages.append(Msg("user" if role == "tool" else role, content))
        return messages

    def _offline_notice(self) -> str:
        return (
            "No model on the other end. I'm pointed at "
            f"{self.config.models.endpoint} and nothing answered. "
            "Start llama-server or LM Studio, then say the word."
        )

    async def _stream(self, turn_id: str, text: str) -> None:
        for chunk in chunk_text(text):
            await self.emit(_event("agent.delta", turnId=turn_id, text=chunk))
            await asyncio.sleep(0.015)

    def _trace_tool(
        self,
        turn_id: str,
        name: str,
        args: dict[str, Any] | None,
        *,
        ok: bool,
        ms: int,
        reason: str | None = None,
        tokens: Any = None,
        preview: str = "",
    ) -> None:
        payload: dict[str, Any] = {"ok": ok, "args": args, "preview": preview}
        if reason:
            payload["reason"] = reason
        db.trace(
            self.conn,
            span="tool",
            turn_id=turn_id,
            name=name,
            payload=payload,
            tokens_in=getattr(tokens, "tokens_in", None) if tokens else None,
            tokens_out=getattr(tokens, "tokens_out", None) if tokens else None,
            ms=ms,
        )


def dump_step(decision: Decision) -> str:
    """Convenience for logs and tests."""
    return json.dumps({"thought": decision.thought, "tool": decision.tool}, ensure_ascii=False)
