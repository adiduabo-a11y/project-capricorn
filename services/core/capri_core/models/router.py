"""OpenAI-compatible model router.

One endpoint and one user-selected model. Works unchanged against
``llama-server`` and LM Studio. The interesting part is not the HTTP — it is
that both brain calls are *constrained*: the decision is a three-field object
with an enum for the tool name, and the arguments are generated against a
grammar derived from the tool's own JSON Schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ..agent import prompt
from ..agent.types import (
    ChatResult,
    Decision,
    ModelUnavailable,
    Msg,
    ToolArgsError,
    wire_messages,
)
from ..config import ModelsCfg
from ..tools.base import ToolSpec
from ..tools.schema import validate
from . import gbnf

log = logging.getLogger("capri.models")

# A tool call that fails validation gets exactly one cheaper second attempt.
_REPAIR_TEMPERATURE = 0.1

# What she says when no attempt produced a usable step. The diagnostic belongs in
# the log, not in a chat bubble: "check the structured-output setting" is not an
# answer, and the failure is usually one bad turn rather than a broken config.
PLANNER_FAILED = (
    "That one slipped through — I couldn't turn it into a usable step. "
    "Say it again and I'll take another run at it."
)

_THINK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+.-]+)?\s*(.*?)```", re.DOTALL)


def _json_candidates(text: str) -> Iterator[Any]:
    """Yield JSON values from raw, fenced, or prose-wrapped model output.

    Local models are inconsistent about obeying structured-output mode. A valid
    object can arrive after a sentence of reasoning, inside a Markdown fence, or
    after a closed reasoning block. Strict ``json.loads`` turns all of those into
    a failed turn; scanning for the first decodable value lets the loop continue.
    """
    raw = (text or "").strip()
    if not raw:
        return

    cleaned = _THINK.sub("", raw).strip()
    seen: set[str] = set()
    for candidate in (raw, cleaned):
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate

    for match in _FENCE.finditer(raw):
        block = match.group(1).strip()
        if block and block not in seen:
            seen.add(block)
            yield block

    decoder = json.JSONDecoder()
    for source in (cleaned, raw):
        for index, char in enumerate(source):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(source[index:])
            except (ValueError, TypeError):
                continue
            yield value


def _decode_json(text: str) -> Any:
    for candidate in _json_candidates(text):
        if not isinstance(candidate, str):
            return candidate
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    raise ValueError("model response did not contain JSON")


def _parse_decision(text: str, tools: list[str]) -> tuple[Decision | None, str]:
    """Find and validate the first usable planner decision in a response."""
    reason = "no JSON object found"
    allowed = {"done", *tools}
    for candidate in _json_candidates(text):
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except (ValueError, TypeError):
                continue
        if not isinstance(candidate, dict):
            reason = "the decision was not a JSON object"
            continue

        raw_tool = candidate.get("tool")
        if not isinstance(raw_tool, str) or not raw_tool.strip():
            reason = "the decision is missing a string 'tool'"
            continue
        tool = raw_tool.strip()
        if tool not in allowed:
            reason = f"{tool!r} is not an available tool"
            continue

        final = candidate.get("final", "")
        if final is None:
            final = ""
        if not isinstance(final, str):
            reason = "'final' must be a string"
            continue
        if tool == "done" and not final.strip():
            reason = "a 'done' decision needs a non-empty 'final'"
            continue

        thought = candidate.get("thought", "")
        if thought is None:
            thought = ""
        return (
            Decision(
                thought=str(thought),
                tool=tool,
                final=final,
                raw=candidate,
            ),
            "",
        )
    return None, reason


def _repair_prompt(reason: str, tools: list[str]) -> str:
    allowed = ", ".join(["done", *tools])
    return (
        "Your previous reply could not be used as a Capri decision: "
        f"{reason}. Reply again with exactly one JSON object and no markdown.\n"
        'Shape: {"thought": "one short sentence", "tool": "done or a tool name", '
        '"final": "reply text"}.\n'
        f"Allowed tools: {allowed}. For an answer use 'done' with a non-empty "
        "'final'; for an action use a tool name and leave 'final' empty.\n"
        "The previous reply is above as your own message, not as something the "
        "user said. Do not repeat it and do not answer it."
    )


def _repair_turn(
    msgs: list[Msg], reason: str, tools: list[str], previous: str
) -> list[Msg]:
    """The retry conversation: her own bad reply, then the instruction.

    The previous reply used to be pasted into a *user* message. Small models
    read that as the person speaking — her own half-formed reasoning came back
    to her as a prompt, and she answered it as though it were one. Her output
    belongs on the assistant role, where it is labelled as hers.
    """
    said = (previous or "").strip()[:1200]
    turn = [*msgs]
    if said:
        turn.append(Msg("assistant", said))
    turn.append(Msg("user", _repair_prompt(reason, tools)))
    return turn


def _usage(data: dict[str, Any]) -> tuple[int, int]:
    usage = data.get("usage") or {}
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


class Router:
    """Implements the brain protocol the agent loop expects."""

    def __init__(self, cfg: ModelsCfg) -> None:
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._healthy: bool | None = None
        self._checked_at = 0.0
        self._probe_lock = asyncio.Lock()

    # ------------------------------------------------------------------ http

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.request_timeout_s, connect=4.0)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _base(self) -> str:
        return self.cfg.endpoint.strip().rstrip("/")

    def reconfigure(self, cfg: ModelsCfg) -> None:
        """Apply a settings change and force the next health check to be fresh."""
        self.cfg = cfg
        self._healthy = None
        self._checked_at = 0.0

    async def available(self) -> bool:
        """Is the endpoint serving models? Cached briefly; a dead endpoint is not retried per step."""
        if not self._base:
            self._healthy = False
            self._checked_at = time.monotonic()
            return False
        now = time.monotonic()
        if self._healthy is not None and now - self._checked_at < self.cfg.health_ttl_s:
            return self._healthy
        async with self._probe_lock:
            if self._healthy is not None and time.monotonic() - self._checked_at < self.cfg.health_ttl_s:
                return self._healthy
            try:
                response = await self._http().get(f"{self._base}/models", timeout=3.0)
                self._healthy = response.status_code < 400
            except (httpx.HTTPError, OSError):
                self._healthy = False
            self._checked_at = time.monotonic()
            return self._healthy

    async def complete(
        self,
        msgs: list[Msg],
        *,
        role: str = "planner",
        grammar: str | None = None,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        structured: str | None = None,
    ) -> ChatResult:
        base = self._base
        if not base:
            raise ModelUnavailable(
                "No brain endpoint configured. Open Settings → Brain and enter "
                "the LM Studio or llama.cpp server URL."
            )
        model = self.cfg.model.strip()
        body: dict[str, Any] = {
            "messages": wire_messages(msgs),
            "max_tokens": max_tokens,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "stream": False,
        }
        if model:
            body["model"] = model
        mode = structured if structured is not None else self.cfg.structured
        if mode == "grammar" and grammar:
            body["grammar"] = grammar
        elif mode == "json_schema" and schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "capri_step", "strict": False, "schema": schema},
            }
        if self.cfg.disable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}

        started = time.monotonic()
        try:
            response = await self._http().post(f"{base}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            self._healthy = False
            raise ModelUnavailable(f"cannot reach {base}: {exc}") from exc

        # Older llama.cpp servers reject response_format with 400/422 instead
        # of ignoring it. GBNF is the native fallback on those servers; keep it
        # to one request so a bad model or message still fails normally.
        if (
            response.status_code in (400, 422)
            and "response_format" in body
            and grammar
        ):
            detail = response.text[:300].replace("\n", " ")
            log.warning(
                "server rejected response_format with HTTP %s; retrying with "
                "GBNF grammar: %s",
                response.status_code,
                detail,
            )
            body.pop("response_format", None)
            body["grammar"] = grammar
            try:
                response = await self._http().post(
                    f"{base}/chat/completions", json=body
                )
            except httpx.HTTPError as exc:
                self._healthy = False
                raise ModelUnavailable(f"cannot reach {base}: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise ModelUnavailable(
                f"{model or 'the configured model'} returned HTTP "
                f"{response.status_code}: {detail}"
            )

        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable(
                f"{model or 'the configured model'} returned an unreadable response"
            ) from exc

        if not text.strip():
            text = message.get("reasoning_content") or ""

        tokens_in, tokens_out = _usage(data)
        return ChatResult(
            text=text,
            model=data.get("model") or model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------------ brain

    async def decide(
        self, msgs: list[Msg], specs: list[ToolSpec], persona_pack: str = prompt.DEFAULT_PACK
    ) -> Decision:
        """One planner step. Returns either a tool call or the final answer."""
        tools = [spec.name for spec in specs]
        schema = gbnf.decision_schema(tools)
        # The generic schema grammar accepts property reordering, which local
        # models do constantly. The parser below supplies the stricter checks.
        grammar = gbnf.from_schema(schema)
        result = await self.complete(
            msgs, role="planner", grammar=grammar,
            schema=schema, max_tokens=self.cfg.max_output_tokens,
        )

        parsed, reason = _parse_decision(result.text, tools)
        if parsed is not None:
            parsed.usage = result
            return parsed

        log.warning("decision was not usable: %s: %r", reason, result.text[:500])
        repair = _repair_turn(msgs, reason, tools, result.text)

        # If grammar was ignored by the server, response_format is the useful
        # fallback. If that is also unsupported, retry with the configured mode.
        modes: list[str] = []
        if self.cfg.structured != "json_schema":
            modes.append("json_schema")
        modes.append(self.cfg.structured)

        last = result
        for mode in modes:
            try:
                repaired = await self.complete(
                    repair,
                    role="planner",
                    grammar=grammar,
                    schema=schema,
                    max_tokens=self.cfg.max_output_tokens,
                    temperature=_REPAIR_TEMPERATURE,
                    structured=mode,
                )
            except ModelUnavailable as exc:
                log.warning("planner repair with %s failed: %s", mode, exc)
                continue

            last = repaired
            parsed, reason = _parse_decision(repaired.text, tools)
            if parsed is not None:
                parsed.usage = repaired
                return parsed

            log.warning(
                "decision repair with %s was still unusable: %s: %r",
                mode,
                reason,
                repaired.text[:500],
            )
            repair = _repair_turn(msgs, reason, tools, repaired.text)

        return Decision(
            thought="",
            tool="done",
            final=PLANNER_FAILED,
            usage=last,
            failed=True,
        )

    async def extract(
        self,
        msgs: list[Msg],
        schema: dict[str, Any],
        *,
        name: str = "extract",
        role: str = "reflex",
        max_tokens: int = 900,
    ) -> Any:
        """Constrained JSON extraction (memory writer and friends)."""
        result = await self.complete(
            msgs,
            role=role,
            grammar=gbnf.from_schema(schema),
            schema=schema,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        try:
            parsed = _decode_json(result.text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ModelUnavailable(f"{name}: model did not return JSON") from exc
        if not isinstance(parsed, dict):
            raise ModelUnavailable(f"{name}: expected a JSON object")
        return parsed

    async def args(
        self, spec: ToolSpec, msgs: list[Msg], feedback: str | None = None
    ) -> tuple[dict[str, Any], ChatResult]:
        """Generate and validate arguments for one tool call, with one repair pass."""
        grammar = gbnf.from_schema(spec.params)
        ask: list[Msg] = [
            *msgs,
            Msg("user", prompt.args_prompt(spec, feedback)),
        ]
        result = await self.complete(
            ask, role="planner", grammar=grammar, schema=spec.params,
            max_tokens=max(900, self.cfg.max_output_tokens), temperature=_REPAIR_TEMPERATURE,
        )

        try:
            parsed = _decode_json(result.text)
        except (ValueError, json.JSONDecodeError):
            parsed = None

        problems = ["arguments were not valid JSON"] if parsed is None else validate(
            spec.params, parsed
        )
        if not problems and not isinstance(parsed, dict):
            problems = ["arguments must be a JSON object"]

        if problems and feedback is None:
            return await self.args(spec, msgs, feedback="; ".join(problems))

        if problems:
            raise ToolArgsError(f"{spec.name}: {'; '.join(problems)}")
        if not isinstance(parsed, dict):
            raise ToolArgsError(f"{spec.name}: arguments must be a JSON object")

        return parsed, result
