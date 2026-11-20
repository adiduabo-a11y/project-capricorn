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

# What she says when no attempt produced a usable step. The diagnostic belongs in
# the log, not in a chat bubble: "check the structured-output setting" is not an
# answer, and the failure is usually one bad turn rather than a broken config.
PLANNER_FAILED = (
    "That one slipped through — I couldn't turn it into a usable step. "
    "Say it again and I'll take another run at it."
)

_THINK = re.compile(r"<think\b[^>]*>(?:.*?</think\s*>|.*$)", re.IGNORECASE | re.DOTALL)
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

        final = candidate.get("final", "")
        if final is None:
            final = ""
        if not isinstance(final, str):
            reason = "'final' must be a string"
            continue

        raw_tool = candidate.get("tool")
        if raw_tool is None or (isinstance(raw_tool, str) and raw_tool.strip().lower() in ("none", "null", "done", "")):
            if final.strip():
                tool = "done"
            else:
                reason = "the decision is missing a string 'tool'"
                continue
        elif isinstance(raw_tool, str) and raw_tool.strip():
            tool = raw_tool.strip()
            if tool not in allowed:
                reason = f"{tool!r} is not an available tool"
                continue
        else:
            reason = "the decision is missing a string 'tool'"
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
        self._request_lock = asyncio.Lock()

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

    def _base_for(self, role: str) -> str:
        if role in ("decision", "reflex", "route", "extract") and self.cfg.decision_endpoint.strip():
            raw = self.cfg.decision_endpoint.strip().rstrip("/")
            if raw.endswith("/chat/completions"):
                raw = raw[:-len("/chat/completions")].rstrip("/")
            elif raw.endswith("/systemone"):
                raw = raw[:-len("/systemone")].rstrip("/")
            return raw
        return self._base

    def _model_for(self, role: str) -> str:
        if role in ("decision", "reflex", "route", "extract") and self.cfg.decision_model.strip():
            return self.cfg.decision_model.strip()
        return self.cfg.model.strip()

    @property
    def _decision_base(self) -> str:
        return self._base_for("decision")

    @property
    def _decision_model(self) -> str:
        return self._model_for("decision")

    def reconfigure(self, cfg: ModelsCfg) -> None:
        """Apply a settings change and force the next health check to be fresh."""
        timeout_changed = cfg.request_timeout_s != self.cfg.request_timeout_s
        self.cfg = cfg
        self._healthy = None
        self._checked_at = 0.0
        if timeout_changed and self._client is not None:
            old = self._client
            self._client = None
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(old.aclose())
            except RuntimeError:
                pass

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

    async def decision_available(self) -> dict[str, Any]:
        """Probe the decision endpoint if configured."""
        if not self.cfg.decision_endpoint.strip():
            return {"configured": False, "online": False}
        endpoint = self._base_for("decision")
        if not endpoint:
            return {"configured": False, "online": False}
        t0 = time.monotonic()
        try:
            resp = await self._http().get(f"{endpoint}/models", timeout=3.0)
            ms = round((time.monotonic() - t0) * 1000)
            return {"configured": True, "online": resp.status_code < 400, "ms": ms}
        except Exception:
            return {"configured": True, "online": False}

    async def _post_chat(
        self, base: str, body: dict[str, Any], grammar: str | None = None
    ) -> httpx.Response:
        for attempt in range(2):
            try:
                response = await self._http().post(f"{base}/chat/completions", json=body)
                break
            except (httpx.TransportError, httpx.RemoteProtocolError, ConnectionResetError, OSError) as exc:
                if attempt == 0:
                    log.info("connection to %s dropped (%s); refreshing client and retrying", base, exc)
                    if self._client is not None:
                        old = self._client
                        self._client = None
                        try:
                            await old.aclose()
                        except Exception:
                            pass
                    continue
                raise

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
            response = await self._http().post(
                f"{base}/chat/completions", json=body
            )
        return response

    async def complete(
        self,
        msgs: list[Msg],
        *,
        role: str = "planner",
        grammar: str | None = None,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        structured: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ChatResult:
        base = endpoint.strip().rstrip("/") if endpoint and endpoint.strip() else self._base_for(role)
        if not base:
            raise ModelUnavailable(
                "No brain endpoint configured. Open Settings → Brain and enter "
                "the LM Studio or llama.cpp server URL."
            )
        chosen_model = (
            model.strip() if model is not None else self._model_for(role)
        )
        # No sampling parameters: the endpoint that runs the model owns them.
        # Determinism on the structured calls comes from the grammar and schema
        # below, not from a low temperature.
        body: dict[str, Any] = {
            "messages": wire_messages(msgs),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if chosen_model:
            body["model"] = chosen_model
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
        async with self._request_lock:
            fallback = False
            response: httpx.Response | None = None
            try:
                response = await self._post_chat(base, body, grammar)
                if response.status_code >= 400 and (base != self._base or chosen_model != self.cfg.model):
                    detail = response.text[:300].replace("\n", " ")
                    log.warning(
                        "decision endpoint %s (model=%r) returned HTTP %s: %s; falling back to main endpoint",
                        base, chosen_model, response.status_code, detail,
                    )
                    fallback = True
            except (httpx.HTTPError, OSError) as exc:
                if base != self._base or chosen_model != self.cfg.model:
                    log.warning(
                        "cannot reach decision endpoint %s (%s); falling back to main endpoint",
                        base, exc,
                    )
                    fallback = True
                else:
                    self._healthy = False
                    raise ModelUnavailable(f"cannot reach {base}: {exc}") from exc

            if fallback:
                base = self._base
                chosen_model = self.cfg.model.strip()
                if chosen_model:
                    body["model"] = chosen_model
                else:
                    body.pop("model", None)
                try:
                    response = await self._post_chat(base, body, grammar)
                except (httpx.HTTPError, OSError) as exc:
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
                    f"{base}/chat/completions", json=body, headers=headers
                )
            except httpx.HTTPError as exc:
                self._healthy = False
                raise ModelUnavailable(f"cannot reach {base}: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise ModelUnavailable(
                f"{chosen_model or 'the configured model'} returned HTTP "
                f"{response.status_code}: {detail}"
            )

        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable(
                f"{chosen_model or 'the configured model'} returned an unreadable response"
            ) from exc

        if not text.strip():
            text = message.get("reasoning_content") or ""

        tokens_in, tokens_out = _usage(data)
        return ChatResult(
            text=text,
            model=data.get("model") or chosen_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------------ brain

    async def _try_systemone(
        self, base: str, model: str, msgs: list[Msg], specs: list[ToolSpec]
    ) -> Decision | None:
        """Attempt non-autoregressive decision via the ggml-org /v1/systemone protocol."""
        state_parts: list[str] = []
        for m in msgs[-6:]:
            content = str(getattr(m, "text", getattr(m, "content", "")) or "").strip()
            if content:
                state_parts.append(f"{m.role}: {content}")
        state = "\n".join(state_parts)

        tools = [spec.name for spec in specs]
        criteria: dict[str, str] = {}
        for s in specs:
            desc = s.description.strip().split(".")[0] if s.description else s.name
            criteria[s.name] = desc or f"Execute {s.name}"
        criteria["done"] = "All actions are complete or no tools are required; answer the user."

        body = {
            "model": model,
            "state": state,
            "questions": {
                "tool": {
                    "type": "choice",
                    "instructions": "Which tool should be called next to make progress on the user request?",
                    "criteria": criteria,
                }
            },
        }

        try:
            started = time.monotonic()
            resp = await self._http().post(f"{base}/systemone", json=body, timeout=20.0)
            if resp.status_code == 200:
                data = resp.json()
                choice = data.get("answers", {}).get("tool", {}).get("choice")
                if choice:
                    ms = int((time.monotonic() - started) * 1000)
                    tokens_in = data.get("usage", {}).get("input_tokens", 0)
                    usage = ChatResult(text="", model=model, tokens_in=tokens_in, tokens_out=0, ms=ms)
                    if choice == "done":
                        primary = await self.complete(msgs, role="planner", max_tokens=self.cfg.max_output_tokens)
                        primary.ms += ms
                        return Decision(thought="Task concluded.", tool="done", final=primary.text, usage=primary)
                    if choice in tools:
                        return Decision(thought=f"Selected tool: {choice}", tool=choice, final="", usage=usage)
        except Exception as exc:
            log.debug("systemone check skipped: %s", exc)
            return None
        return None

    async def decide(
        self, msgs: list[Msg], specs: list[ToolSpec], persona_pack: str = prompt.DEFAULT_PACK
    ) -> Decision:
        """One planner step. Returns either a tool call or the final answer."""
        tools = [spec.name for spec in specs]

        # Support ggml-org non-autoregressive decision models (e.g. Laya) via /v1/systemone
        base = self._base_for("decision")
        model = self._model_for("decision")
        if self.cfg.decision_model.strip() or self.cfg.decision_endpoint.strip():
            sys1 = await self._try_systemone(base, model, msgs, specs)
            if sys1 is not None:
                return sys1

        schema = gbnf.decision_schema(tools)
        # The generic schema grammar accepts property reordering, which local
        # models do constantly. The parser below supplies the stricter checks.
        grammar = gbnf.from_schema(schema)
        endpoint = self._decision_base
        model = self._decision_model
        result = await self.complete(
            msgs,
            role="decision",
            grammar=grammar,
            schema=schema,
            max_tokens=self.cfg.max_output_tokens,
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
        total_in = result.tokens_in
        total_out = result.tokens_out
        total_ms = result.ms
        for mode in modes:
            try:
                repaired = await self.complete(
                    repair,
                    role="decision",
                    grammar=grammar,
                    schema=schema,
                    max_tokens=self.cfg.max_output_tokens,
                    structured=mode,
                    endpoint=endpoint,
                    model=model,
                )
            except ModelUnavailable as exc:
                log.warning("planner repair with %s failed: %s", mode, exc)
                continue

            total_in += repaired.tokens_in
            total_out += repaired.tokens_out
            total_ms += repaired.ms
            last = ChatResult(
                text=repaired.text,
                tokens_in=total_in,
                tokens_out=total_out,
                ms=total_ms,
                model=repaired.model,
            )
            parsed, reason = _parse_decision(repaired.text, tools)
            if parsed is not None:
                parsed.usage = last
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
            usage=ChatResult(
                text=last.text,
                tokens_in=total_in,
                tokens_out=total_out,
                ms=total_ms,
                model=last.model,
            ),
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
        endpoint: str | None = None,
        model: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Constrained JSON extraction (memory writer and friends)."""
        result = await self.complete(
            msgs,
            role=role,
            grammar=gbnf.from_schema(schema),
            schema=schema,
            max_tokens=max_tokens,
            endpoint=endpoint,
            model=model,
            headers=headers,
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
            max_tokens=max(900, self.cfg.max_output_tokens),
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
            repaired_args, repaired_usage = await self.args(
                spec, msgs, feedback="; ".join(problems)
            )
            combined = ChatResult(
                text=repaired_usage.text,
                tokens_in=result.tokens_in + repaired_usage.tokens_in,
                tokens_out=result.tokens_out + repaired_usage.tokens_out,
                ms=result.ms + repaired_usage.ms,
                model=repaired_usage.model or result.model,
            )
            return repaired_args, combined

        if problems:
            raise ToolArgsError(f"{spec.name}: {'; '.join(problems)}")
        if not isinstance(parsed, dict):
            raise ToolArgsError(f"{spec.name}: arguments must be a JSON object")

        return parsed, result
