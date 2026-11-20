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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..config import CapriConfig
from ..models.vision import asks_to_look, vision_enabled
from ..store import db
from ..tools import review
from ..tools.builtin.notes import NAMES as NOTE_TOOL_NAMES
from ..tools.base import ScreenState, ToolContext, ToolResult
from ..tools.policy import TIER_NAMES, Policy
from ..tools.registry import Registry
from ..work.tasks import TaskService
from . import observe, prompt
from .types import Decision, ModelUnavailable, Msg, ToolArgsError

log = logging.getLogger("capri.agent")

_RESUME_NOTE = (
    "Continue this task from the record above. Pick up at the next unfinished "
    "step; do not repeat a call whose result is already recorded."
)

# Said when a reply promises the work instead of doing it. Twice is the limit:
# after that, asking a third time only spends the budget on a model that will
# not act.
_MAX_NUDGES = 2
_NUDGE = (
    "[system] You said you would do that, but no tool ran. Do it now, in this "
    "turn: choose a tool and act. If something is genuinely blocking you, say "
    "exactly what it is and stop."
)


@dataclass
class _Seen:
    """What this turn has already done.

    ``epoch`` moves after every successful state-changing call, so observing
    again to check that an edit landed is not mistaken for a loop. ``mutated``
    keeps the signatures that *did* change something: replaying one of those is
    never useful, so it stays refused for the rest of the turn.
    """

    memo: dict[str, int] = field(default_factory=dict)
    mutated: set[str] = field(default_factory=set)
    epoch: int = 0


Emit = Callable[[dict[str, Any]], Awaitable[None]]
Confirm = Callable[[dict[str, Any]], Awaitable[bool]]
# The answer to a card that asks for more than yes/no; the tool decides what
# it proposes, the person decides what comes back.
Choose = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    return {"type": event_type, **fields}


def _estimate_tokens(text: str) -> int:
    return observe.estimate_tokens(text)


# Assistant rows written by older harness versions. They are not conversation;
# replaying them teaches the next planner call to imitate the failure.
_INTERNAL_FAILURES = {
    "My planner produced something that wasn't a decision. Try again.",
    "Bad decision shape.",
    "I couldn't get a valid planner decision from the model. "
    "Check the Brain model and structured-output setting, then try again.",
    # Current wording; mirrors models.router.PLANNER_FAILED, kept as a literal
    # so the loop does not import the router back through the agent package.
    "That one slipped through — I couldn't turn it into a usable step. "
    "Say it again and I'll take another run at it.",
}


def _is_internal_failure(content: str) -> bool:
    text = content.strip()
    return text in _INTERNAL_FAILURES or text.startswith(
        "My planner produced something that wasn't a decision."
    )


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


@dataclass
class ActResult:
    """One step's outcome, shaped for the planner."""

    observation: str
    ran: bool
    attachments: list[Any] = field(default_factory=list)
    # What the step was, for a transcript rebuilt from the shard. The
    # observation alone cannot be turned back into a chip: it is prose, already
    # prefixed and shaped for the planner.
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    tier: int = 0
    output: str = ""
    ms: int = 0


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
        work: prompt.WorkContext | None = None,
        project: Any = None,
        notes: Any = None,
        memory: Any = None,
        rag: Any = None,
        voice: Any = None,
        screen: Any = None,
        tasks: TaskService | None = None,
        resume: bool = False,
        # The shell asked for work in a window (the app panel), so the screen
        # protocol belongs in the prompt even if the wording is not "look".
        screen_task: bool = False,
        # Last because Python wants the defaulted parameters together; every
        # caller names it, so the position is not part of the contract.
        choose: Choose | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.session = session
        self.emit = emit
        self.confirm = confirm
        self.choose = choose
        self.brain = brain
        self.registry = registry
        self.policy = policy
        self.workspace = workspace
        self.work = work
        self.project = project
        self.notes = notes
        self.memory = memory
        self.rag = rag
        self.voice = voice
        self.screen = screen
        self.tasks = tasks
        # A continuation gets the task summary and the last observations, not
        # the transcript of the turns that came before it.
        self.resume = resume
        self.screen_task = screen_task
        self.planner_failed = False
        # Set by the loop when a ceiling or an interruption cut the turn short.
        # The task is paused, never marked done, when this is non-empty.
        self.paused_reason = ""
        # The last shell result this turn, for the task's outcome.
        self._last_command_ok: bool | None = None
        # Resolved once per turn: the switch plus the model behind it.
        self.vision = vision_enabled(config.screen.vision, config.models.model)
        # Set by run() from the request. A planner calling screen.observe is
        # not the user asking to look.
        self.look = False
        # The turn's request, in the person's own words. The destructive-call
        # reviewer needs it; before it existed, every review was refused for
        # "touching something outside the workspace" and close/quit always asked.
        self.request = ""
        # Shared by this turn's calls: verification arming and sent frames.
        self._screen = ScreenState()
        # One per runner, and a runner is one turn. Tools that poll see it;
        # tools that await see CancelledError. Both paths must clean up.
        self._cancel = asyncio.Event()
        # path -> sha256 at read time, for this turn. Passed to every call, so
        # fs.patch can refuse a file that moved under the model.
        self._reads: dict[str, str] = {}

    @property
    def workspace_id(self) -> str:
        return self.work.workspace_id if self.work is not None else ""

    @property
    def task_id(self) -> str:
        return self.work.task_id if self.work is not None else ""

    # ------------------------------------------------------------------ public

    async def run(self, turn_id: str, text: str) -> None:
        self.look = asks_to_look(text)
        # Kept for the destructive-call reviewer: what was asked is what makes
        # "close the app" checkable against "close Roblox Studio".
        self.request = text
        started = time.monotonic()
        cost = StepCost()
        self.planner_failed = False
        self.session.add_message(turn_id, "user", text, task_id=self.task_id)
        self._touch_task(turn_id)
        await self.emit(_state("thinking"))

        try:
            final, observations = await self._drive(turn_id, text, cost, started)
        except asyncio.CancelledError:
            log.info("turn %s cancelled", turn_id)
            self._cancel.set()
            await self.emit(_state("idle", "cancelled"))
            raise

        final = final.strip() or "…"
        await self.emit(_state("speaking"))
        await self._stream(turn_id, final)

        if not self.planner_failed:
            self.session.add_message(turn_id, "assistant", final, task_id=self.task_id)
        self.session.add_tokens(cost.tokens_in + cost.tokens_out)

        # Before agent.done: the shell refreshes the task list on the done
        # event, so the final status has to be on the wire first.
        task_payload = self._finish_task()
        if task_payload is not None:
            await self.emit(_event("task.changed", turnId=turn_id, task=task_payload))

        ms = int((time.monotonic() - started) * 1000)
        db.trace(
            self.conn,
            span="turn",
            turn_id=turn_id,
            workspace_id=self.workspace_id,
            task_id=self.task_id,
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
                workspaceId=self.workspace_id,
                taskId=self.task_id,
                text=final,
                usage={"in": cost.tokens_in, "out": cost.tokens_out, "ms": ms},
            )
        )

        if self.voice is not None:
            try:
                await self.voice.speak(turn_id, final, self.emit)
            except Exception:
                log.exception("speech failed; the turn is unaffected")

        if self.memory is not None and not self.planner_failed:
            try:
                written = await self.memory.observe_turn(
                    turn_id=turn_id,
                    user_text=text,
                    observations=observations,
                    episode_id=self.session.episode_id,
                    workspace_id=self.workspace_id,
                )
                for item in written:
                    await self.emit(_event("memory.written", **item))
            except Exception:
                log.exception("memory write failed; the turn is unaffected")

        await self.emit(_state("idle"))

    # ------------------------------------------------------------------ inner

    async def _drive(
        self, turn_id: str, text: str, cost: StepCost, started: float
    ) -> tuple[str, list[str]]:
        if not await self.brain.available():
            self.paused_reason = "offline"
            return self._offline_notice(), []

        memory_block = ""
        if self.memory is not None:
            try:
                memory_block = await self.memory.recall_block(
                    text,
                    episode_id=self.session.episode_id,
                    workspace_id=self.workspace_id,
                )
            except Exception:
                log.exception("memory recall failed; continuing without it")

        # The vault read as her wiki on this turn: its notes get their own
        # section, and are kept out of the general document block, so a note
        # cannot arrive twice under two headings.
        wiki = self.notes is not None and self.notes.wants_recall()
        documents_block = ""
        if self.rag is not None:
            try:
                documents_block, citations = await self.rag.recall_block(
                    text, exclude=[self.notes.collection] if wiki else None
                )
                for citation in citations:
                    await self.emit(_event("rag.cited", turnId=turn_id, **citation))
            except Exception:
                log.exception("rag recall failed; continuing without it")

        note_ctx: prompt.NotesContext | None = None
        if wiki:
            try:
                vault = self.notes.status()
                note_ctx = prompt.NotesContext(
                    path=str(vault.get("vault") or ""),
                    count=int(vault.get("indexed") or 0),
                )
                vault_block, vault_citations = await self.notes.recall_block(text)
                if vault_block:
                    documents_block = (
                        f"{vault_block}\n\n{documents_block}" if documents_block else vault_block
                    )
                for citation in vault_citations:
                    await self.emit(_event("rag.cited", turnId=turn_id, **citation))
            except Exception:
                log.exception("vault recall failed; continuing without it")

        all_specs = self.registry.specs()
        recent_msgs = self.session.recent_messages(self.config.agent.history_messages)
        hidden_mcp = prompt.mcp_hidden_tools(all_specs, request=text, history=recent_msgs)
        messages = self._history(
            memory_block,
            documents_block,
            request=text,
            notes=note_ctx,
            hidden=hidden_mcp,
        )
        calls: dict[str, int] = {}
        seen = _Seen()
        stalled = 0
        nudges = 0
        notes: list[str] = []
        wall_limit = self.config.agent.max_wall_clock_s
        compacted = False
        budget = self._context_budget()

        for step in range(1, self.config.agent.max_steps + 1):
            if time.monotonic() - started > wall_limit:
                self.paused_reason = "time limit"
                return (
                    f"I burned the whole {wall_limit}s budget on this. "
                    "You'll want to narrow the ask or raise the limit."
                ), notes

            # Budget before every planner call, not just at turn start. The
            # in-turn list grows with each observation and used to be checked
            # exactly once, which let a long turn overflow the window and get
            # truncated from the front — where the rules live.
            dropped, saved = self._guard_context(messages, budget)
            if dropped and not compacted:
                # Once per turn: the event explains the first compaction, and a
                # second one would only be noise.
                compacted = True
                await self.emit(
                    _event(
                        "context.compacted",
                        turnId=turn_id,
                        droppedObservations=dropped,
                        tokensSaved=saved,
                    )
                )

            vault = str(getattr(self.config.notes, "vault", "") or "").strip()
            active_hidden = set(NOTE_TOOL_NAMES if not vault else ()) | hidden_mcp
            active_mcp_servers = {
                c.split(".")[1]
                for c in calls
                if c.startswith("mcp.") and len(c.split(".")) > 1
            }
            if active_mcp_servers:
                active_hidden = {
                    h
                    for h in active_hidden
                    if not (
                        h.startswith("mcp.")
                        and len(h.split(".")) > 1
                        and h.split(".")[1] in active_mcp_servers
                    )
                }
            active_specs = [s for s in all_specs if s.name not in active_hidden]

            try:
                decision = await self.brain.decide(
                    messages, active_specs, self.config.agent.persona_pack
                )
            except ModelUnavailable as exc:
                log.warning("model unavailable mid-turn: %s", exc)
                self.paused_reason = "model unavailable"
                return "The model fell over mid-run. Signal's gone.", notes

            cost.add(decision.usage)

            if decision.is_final:
                self.planner_failed = decision.failed
                if decision.failed:
                    self.paused_reason = "planner failed"
                    return (decision.final or "Done."), notes
                reply = (decision.final or "").strip()
                # A model that narrates the work and stops ends the turn with
                # the user waiting on something no tool was ever asked to do.
                # It is asked again, twice at most; after that a third nudge
                # only burns the budget.
                if (
                    nudges < _MAX_NUDGES
                    and not prompt.looks_like_conversation(text)
                    and prompt.announces_action(reply)
                ):
                    nudges += 1
                    db.trace(
                        self.conn,
                        span="turn",
                        turn_id=turn_id,
                        workspace_id=self.workspace_id,
                        task_id=self.task_id,
                        name="turn.nudge",
                        payload={"attempt": nudges, "said": reply[:300]},
                    )
                    if step == self.config.agent.max_steps:
                        # No step left to act in: asking again would only run
                        # out the loop, and the ceiling line would throw away
                        # the reply the person is about to read. Keep her words
                        # and say the true part after them.
                        self.paused_reason = "step ceiling"
                        return (
                            f"{reply} (That was my last step, so nothing actually "
                            "ran: tell me to keep going and I'll do it.)"
                        ), notes
                    messages.append(Msg("assistant", reply))
                    messages.append(Msg("user", _NUDGE))
                    continue
                return (reply or "Done."), notes

            await self.emit(_event("agent.step", turnId=turn_id, step=step,
                                   thought=decision.thought, tool=decision.tool))

            acted = await self._act(turn_id, decision, messages, cost, calls, seen)
            notes.append(acted.observation)
            # Observations ride on the user role. OpenAI-compatible servers
            # (LM Studio in particular) silently drop `role: "tool"` messages
            # that carry no tool_call_id, which leaves the planner blind and
            # looping. The prefix keeps the meaning unambiguous.
            messages.append(
                Msg("user", acted.observation, attachments=acted.attachments)
            )
            self.session.add_message(
                turn_id,
                "tool",
                acted.observation,
                task_id=self.task_id,
                tool_name=acted.name,
                tool_args=json.dumps(acted.args, ensure_ascii=False, default=str),
                tool_result=acted.output,
                tool_ok=acted.ok,
                tool_tier=acted.tier,
                tool_ms=acted.ms,
            )

            # Three refused steps in a row means the planner is stuck, not
            # thinking. Cut the turn rather than burn the remaining budget.
            stalled = 0 if acted.ran else stalled + 1
            if stalled >= 3:
                self.paused_reason = "stalled"
                return (
                    "I'm going in circles: I repeated an action that cannot change "
                    "anything. Point me at the next step."
                ), notes

        self.paused_reason = "step ceiling"
        return (
            f"I hit the {self.config.agent.max_steps}-step ceiling without landing it. "
            "Tell me to keep going and I'll pick it up from here."
        ), notes

    async def _auto_review(
        self, turn_id: str, name: str, args: dict[str, Any], verdict: Any
    ) -> bool:
        """Is this destructive call safe enough to skip the question?

        A model is asked, because only a reader can tell `rm -rf build` from
        `rm -rf src`. It has to say yes on both counts, it has to be there, and
        the command must not be on the list no model may waive. Anything else —
        a timeout, a malformed answer, a failed call — leaves the question where
        it was.
        """
        confirm = self.config.policy.confirm
        if confirm.review == "off" or verdict.tier >= 3:
            return False
        why = review.veto(name, args)
        if why:
            await self._trace_review(turn_id, name, False, why)
            return False
        try:
            if not await self.brain.available():
                return False
            payload, _usage = await asyncio.wait_for(
                self.brain.args(
                    review.REVIEW_SPEC,
                    review.messages(
                        name, args, verdict.reason, self.workspace, self.request
                    ),
                ),
                timeout=confirm.review_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "ask"
            await self._trace_review(turn_id, name, False, f"the reviewer did not answer: {exc}")
            return False
        found = review.judge(payload)
        await self._trace_review(turn_id, name, found.approved, found.reason)
        return found.approved

    async def _trace_review(
        self, turn_id: str, name: str, approved: bool, reason: str
    ) -> None:
        db.trace(
            self.conn,
            # Its own span: a review is not a tool that ran, and counting it as
            # one would make the tool spans lie.
            span="review",
            turn_id=turn_id,
            workspace_id=self.workspace_id,
            task_id=self.task_id,
            name="tool.review",
            payload={"tool": name, "approved": approved, "reason": reason[:400]},
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
    ) -> ActResult:
        """Report a call that will not run, in the UI and in the trace."""
        await self.emit(
            _event(
                "agent.tool",
                turnId=turn_id,
                workspaceId=self.workspace_id,
                taskId=self.task_id,
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
        return ActResult(
            observation=f"{observe.OBSERVATION_PREFIX}{name}] {output}",
            ran=False,
            name=name,
            args=dict(args or {}),
            tier=tier,
            output=output,
        )

    async def _act(
        self,
        turn_id: str,
        decision: Decision,
        messages: list[Msg],
        cost: StepCost,
        calls: dict[str, int],
        seen: _Seen,
    ) -> ActResult:
        name = decision.tool
        spec = self.registry.get(name)
        if spec is None:
            return await self._refuse(
                turn_id, name, None, tier=3, output="no such tool"
            )

        # The turn-level ceiling is the real limit; per-tool caps only stop one
        # tool from monopolising the budget.
        used_total = sum(calls.values())
        ceiling = self.config.agent.max_tool_calls_per_turn
        if used_total >= ceiling:
            # A ceiling is an interruption, not an answer: even if the model
            # then says done, the task stays paused.
            self.paused_reason = "tool ceiling"
            return await self._refuse(
                turn_id,
                name,
                None,
                tier=spec.tier,
                output=(
                    f"this turn has already run {used_total} tool calls, the limit is "
                    f"{ceiling}. Answer with what you have, or ask to continue."
                ),
            )

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
            )

        try:
            args, usage = await self.brain.args(spec, messages)
        except ToolArgsError as exc:
            cost.add(getattr(exc, "usage", None))
            return await self._refuse(
                turn_id, name, None, tier=spec.tier, output=f"rejected arguments: {exc}"
            )
        except ModelUnavailable:
            return await self._refuse(
                turn_id,
                name,
                None,
                tier=spec.tier,
                output="the model went away while composing arguments",
            )
        cost.add(usage)

        signature = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if signature in seen.mutated:
            return await self._refuse(
                turn_id,
                name,
                args,
                tier=spec.tier,
                output=(
                    "skipped: this call already ran and changed something this turn. "
                    "Take a different step or answer with done."
                ),
            )
        # A repeat only counts while nothing has changed since the first one:
        # read a file, patch it, read it again is verification, not a loop.
        if seen.memo.get(signature) == seen.epoch:
            return await self._refuse(
                turn_id,
                name,
                args,
                tier=spec.tier,
                output=(
                    "skipped: this exact call already ran this turn with nothing in "
                    "between. Take a different step or answer with done."
                ),
            )
        seen.memo[signature] = seen.epoch

        verdict = self.policy.apply_confirm_prefs(self.policy.check(spec, args))
        if verdict.verdict == "block":
            return await self._refuse(
                turn_id,
                name,
                args,
                tier=verdict.tier,
                output=f"blocked by policy: {verdict.reason}",
                reason=verdict.reason,
            )

        if verdict.verdict == "confirm" and not await self._auto_review(
            turn_id, name, args, verdict
        ):
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
                )

        calls[name] = used + 1
        await self.emit(_state("acting", name))
        context = ToolContext(
            conn=self.conn,
            turn_id=turn_id,
            subject=self.session.subject,
            workspace=self.workspace,
            screen=self.screen,
            cancel=self._cancel,
            confirm=self.confirm,
            choose=self.choose,
            emit=self.emit,
            policy=self.policy,
            project=self.project,
            notes=self.notes,
            reads=self._reads,
            workspace_id=self.workspace_id,
            task_id=self.task_id,
            vision=self.vision,
            look=self.look,
            screen_state=self._screen,
        )
        result = await self.registry.call(name, context, args)
        if result.ok and spec.mutates:
            seen.mutated.add(signature)
            seen.epoch += 1
        if self.tasks is not None and self.task_id:
            self.tasks.add_step(self.task_id)
            if name == "shell.run":
                # The task's outcome is the last command's, not a guess.
                self._last_command_ok = bool(result.ok)

        # A diff or command transcript is a reference on the wire; the text
        # lives in the shard so a megabyte of test output never becomes an
        # event on the renderer's socket.
        artifact = (result.data or {}).get("artifact")
        if isinstance(artifact, dict) and artifact.get("id"):
            await self.emit(
                _event(
                    "agent.artifact",
                    turnId=turn_id,
                    workspaceId=self.workspace_id,
                    taskId=self.task_id,
                    **artifact,
                )
            )

        shown = observe.shape(result.output, spec, self.config.agent.observation)
        await self.emit(
            _event(
                "agent.tool",
                turnId=turn_id,
                workspaceId=self.workspace_id,
                taskId=self.task_id,
                name=name,
                args=args,
                ok=result.ok,
                tier=verdict.tier,
                tierName=TIER_NAMES.get(verdict.tier, "?"),
                # The event carries what the planner saw, not the raw buffer.
                output=shown,
                ms=result.ms,
            )
        )
        self._trace_tool(turn_id, name, args, ok=result.ok,
                         ms=result.ms, tokens=usage, preview=result.preview(200))
        await self.emit(_state("thinking"))

        observation = (
            f"{observe.OBSERVATION_PREFIX}{name}] "
            f"{'ok' if result.ok else 'error'}\n{shown}"
        )
        return ActResult(
            observation=observation,
            ran=True,
            attachments=list(result.attachments or ()),
            name=name,
            args=args,
            ok=bool(result.ok),
            tier=verdict.tier,
            output=shown,
            ms=int(result.ms or 0),
        )

    # ------------------------------------------------------------------ utils

    def _context_budget(self) -> int:
        """Tokens the message list may use before compaction.

        The work profile gets the larger budget; when no workspace is open and
        the profile is not work, the old number stands. A model's real context
        window is not known yet, so the configured value is the whole budget.
        """
        base = self.config.agent.context_budget_tokens
        if self.config.policy.profile == "work":
            base = max(base, self.config.agent.work_context_budget_tokens)
        return max(512, base)

    def _guard_context(self, messages: list[Msg], budget: int) -> tuple[int, int]:
        """Compact old observations until the in-turn list fits.

        The system prompt, the live request (the oldest and newest anchors) and
        the last two messages are never touched. Returns
        ``(observations_compacted, tokens_saved)``.
        """
        total = sum(_estimate_tokens(str(m.get("content") or "")) for m in messages)
        if total <= budget:
            return 0, 0

        dropped = 0
        saved = 0
        for msg in messages[1:-2]:
            changed = observe.compact_message(msg)
            if changed is None:
                continue
            before, after = changed
            delta = _estimate_tokens(before) - _estimate_tokens(after)
            if delta <= 0:
                continue
            dropped += 1
            saved += delta
            total -= delta
            if total <= budget:
                break
        return dropped, saved

    def _history(
        self,
        memories: str = "",
        documents: str = "",
        request: str = "",
        notes: prompt.NotesContext | None = None,
        hidden: Collection[str] = (),
    ) -> list[Msg]:
        # A note tool with no vault behind it can only fail, so it is not
        # advertised: the registry keeps it callable and the error says what to
        # do, but the index is budget and this would be spent for nothing.
        vault = str(getattr(self.config.notes, "vault", "") or "").strip()
        combined_hidden = set(hidden)
        if not vault:
            combined_hidden.update(NOTE_TOOL_NAMES)
        system = prompt.system_prompt(
            self.registry.specs(),
            self.config.agent.persona_pack,
            memories,
            documents,
            profile=self.config.policy.profile,
            work=self.work,
            notes=notes,
            screen=self.look or self.screen_task,
            request=request,
            # What she is allowed to reach on this machine, so "open it" has an
            # answer either way instead of a guess and a refusal.
            apps=tuple(getattr(self.policy.cfg, "screen_allow_apps", ()) or ()),
            hidden=combined_hidden,
        )
        messages = [Msg("system", system)]
        budget = max(
            512,
            self._context_budget() - _estimate_tokens(system),
        )
        selected: list[Msg] = []
        used = 0
        for row in reversed(self.session.recent_messages(self.config.agent.history_messages)):
            role = str(row.get("role") or "")
            content = str(row.get("content") or "")
            if role == "assistant" and _is_internal_failure(content):
                continue
            # Stored tool rows replay on the user role, matching the live turn.
            if role not in ("user", "assistant", "tool"):
                continue
            cost = _estimate_tokens(content) + 8
            if selected and used + cost > budget:
                break
            selected.append(Msg("user" if role == "tool" else role, content))
            used += cost
        selected.reverse()
        messages.extend(selected)
        if self.resume:
            # A continuation still needs the conversation: the task record can
            # be stale, and the user's last line is usually the reason the work
            # is being picked up again.
            messages.append(Msg("user", _RESUME_NOTE))
        return messages

    # ------------------------------------------------------------------ tasks

    def _touch_task(self, turn_id: str) -> None:
        if self.tasks is None or not self.task_id:
            return
        try:
            self.tasks.touch(self.task_id, last_turn_id=turn_id)
        except Exception:
            log.exception("task touch failed; the turn is unaffected")

    def _task_result(self) -> str:
        if self._last_command_ok is None:
            return "none"
        return "passed" if self._last_command_ok else "failed"

    def _finish_task(self) -> dict[str, Any] | None:
        """Pause or finish the task and return it for the event."""
        if self.tasks is None or not self.task_id:
            return None
        try:
            summary = self.tasks.summarize(self.task_id)
            if self.paused_reason:
                task = self.tasks.pause(self.task_id, summary)
            else:
                task = self.tasks.finish(
                    self.task_id, result=self._task_result(), summary=summary
                )
        except Exception:
            log.exception("task update failed; the turn is unaffected")
            return None
        return task.as_dict() if task is not None else None

    def _offline_notice(self) -> str:
        endpoint = self.config.models.endpoint.strip()
        if not endpoint:
            return (
                "I don't have a brain server configured yet. Open Settings → Brain "
                "and point me at LM Studio or llama.cpp."
            )
        return (
            "No model on the other end. I'm pointed at "
            f"{endpoint} and nothing answered. "
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
            workspace_id=self.workspace_id,
            task_id=self.task_id,
            name=name,
            payload=payload,
            tokens_in=getattr(tokens, "tokens_in", None) if tokens else None,
            tokens_out=getattr(tokens, "tokens_out", None) if tokens else None,
            ms=ms,
        )


def dump_step(decision: Decision) -> str:
    """Convenience for logs and tests."""
    return json.dumps({"thought": decision.thought, "tool": decision.tool}, ensure_ascii=False)
