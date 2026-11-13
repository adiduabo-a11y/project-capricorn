"""OpenAI-compatible model router.

One endpoint, several model names. Works unchanged against ``llama-server`` and
LM Studio. The interesting part is not the HTTP — it is that both brain calls
are *constrained*: the decision is a three-field object with an enum for the
tool name, and the arguments are generated against a grammar derived from the
tool's own JSON Schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from ..agent import prompt
from ..agent.types import ChatResult, Decision, ModelUnavailable, Msg, ToolArgsError
from ..config import ModelCfg, ModelsCfg
from ..tools.base import ToolSpec
from ..tools.schema import validate
from . import gbnf

log = logging.getLogger("capri.models")

# A tool call that fails validation gets exactly one cheaper second attempt.
_REPAIR_TEMPERATURE = 0.1


def _decode_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned)


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
        return self.cfg.endpoint.rstrip("/")

    async def available(self) -> bool:
        """Is the endpoint serving models? Cached briefly; a dead endpoint is not retried per step."""
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
    ) -> ChatResult:
        cfg: ModelCfg = getattr(self.cfg, role, self.cfg.planner)
        body: dict[str, Any] = {
            "model": cfg.name,
            "messages": list(msgs),
            "max_tokens": max_tokens,
            "temperature": cfg.temperature if temperature is None else temperature,
            "stream": False,
        }
        mode = self.cfg.structured
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
            response = await self._http().post(f"{self._base}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            self._healthy = False
            raise ModelUnavailable(f"cannot reach {self._base}: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise ModelUnavailable(f"{cfg.name} returned HTTP {response.status_code}: {detail}")

        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable(f"{cfg.name} returned an unreadable response") from exc

        if not text.strip():
            text = message.get("reasoning_content") or ""

        tokens_in, tokens_out = _usage(data)
        return ChatResult(
            text=text,
            model=data.get("model") or cfg.name,
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
        grammar = gbnf.decision(tools)
        result = await self.complete(
            msgs, role="planner", grammar=grammar,
            schema=gbnf.decision_schema(tools), max_tokens=700,
        )

        try:
            parsed = _decode_json(result.text)
        except (ValueError, json.JSONDecodeError):
            log.warning("decision was not JSON: %r", result.text[:200])
            return Decision(
                thought="",
                tool="done",
                final="My planner produced something that wasn't a decision. Try again.",
                usage=result,
            )

        if not isinstance(parsed, dict):
            return Decision(tool="done", final="Bad decision shape.", usage=result)

        tool = str(parsed.get("tool") or "done")
        thought = str(parsed.get("thought") or "")
        final = str(parsed.get("final") or "")

        if tool != "done" and tool not in {spec.name for spec in specs}:
            return Decision(tool="done", thought=thought,
                            final=f"I reached for {tool}, which I don't have.", usage=result)

        return Decision(thought=thought, tool=tool, final=final, raw=parsed, usage=result)

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
            max_tokens=900, temperature=_REPAIR_TEMPERATURE,
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
