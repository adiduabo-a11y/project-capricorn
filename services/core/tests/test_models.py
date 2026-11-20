"""Model configuration checks: no presets, legacy migration, request shape.

Offline: the HTTP request is inspected with httpx.MockTransport.

    uv run python services/core/tests/test_models.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

from capri_core.agent.types import ModelUnavailable, Msg
from capri_core.config import CapriConfig, dump_yaml, load_config, schema, set_value
from capri_core.memory.embed import EndpointEmbedder
from capri_core.models.router import Router, _decode_json, _parse_decision
from capri_core.rag.rerank import EndpointReranker

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def test_defaults() -> None:
    config = CapriConfig()
    check(config.models.endpoint == "", "endpoint starts empty")
    check(config.models.model == "", "model starts empty")
    check(not hasattr(config.models, "planner"), "no planner preset")
    check(not hasattr(config.models, "reflex"), "no reflex preset")
    check(not hasattr(config.models, "vision"), "no vision preset")
    check(not hasattr(config.models, "governor"), "no governor preset")
    check(config.models.embed.name == "", "no embedding preset")
    check(config.models.rerank.name == "", "no rerank preset")
    check(config.models.structured == "json_schema", "JSON schema is the default structured mode")
    loaded = load_config_with("")
    check(loaded.models.structured == "json_schema", "a fresh config file gets JSON schema")
    check("qwen" not in dump_yaml(config).lower(), "dumped config contains no Qwen names")
    check("bge-" not in dump_yaml(config).lower(), "dumped config contains no BGE names")

    proj = Path(tempfile.mkdtemp(prefix="capri-proj-"))
    try:
        (proj / ".capri.yaml").write_text(
            "server:\n  require_token: false\n"
            "policy:\n  deny_paths: []\n"
            "agent:\n  max_steps: 7\n",
            encoding="utf-8",
        )
        proj_cfg = load_config(proj)
        check(proj_cfg.server.require_token is True, "project .capri.yaml cannot disable require_token")
        check(bool(proj_cfg.policy.deny_paths), "project .capri.yaml cannot clear deny_paths")
        check(proj_cfg.agent.max_steps == 7, "project .capri.yaml can configure allowed agent fields")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_wake_config() -> None:
    """The wake gate's settings: defaults, schema labels, validation."""
    config = CapriConfig()
    check(config.voice.wake.enabled, "the wake gate is on by default")
    check(config.voice.wake.min_confidence == 0.35, "confidence default is 0.35")
    check(config.voice.wake.max_no_speech_prob == 0.6, "no-speech default is 0.6")
    check(config.voice.wake.silence_ms == 550, "silence gap default is 550 ms")
    check(config.voice.wake.name_grace_ms == 700, "name grace default is 700 ms")
    check(config.voice.wake.follow_up_ms == 6000, "follow-up window default is 6 s")
    check(config.voice.wake.threshold_rms == 0.012, "speech threshold default is 0.012")
    check(config.voice.wake.min_utterance_ms == 300, "shortest utterance default is 300 ms")

    wake = schema().get("$defs", {}).get("WakeCfg", {}).get("properties", {})
    for key in (
        "min_confidence",
        "max_no_speech_prob",
        "silence_ms",
        "name_grace_ms",
        "follow_up_ms",
        "threshold_rms",
        "min_utterance_ms",
    ):
        check(key in wake, f"the settings schema offers {key}")
    check(
        wake.get("min_confidence", {}).get("title") == "Wake confidence",
        "the confidence control is labelled",
    )
    check(
        wake.get("follow_up_ms", {}).get("title") == "Follow-up window",
        "the follow-up window is labelled in settings",
    )

    updated = set_value(config, "voice.wake.min_confidence", 0.5)
    check(updated.voice.wake.min_confidence == 0.5, "the confidence gate is settable")
    check("min_confidence: 0.5" in dump_yaml(updated), "the value is written to the config file")

    measured = set_value(config, "voice.wake.threshold_rms", 0.02)
    check(measured.voice.wake.threshold_rms == 0.02, "the calibrated threshold is settable")
    check("threshold_rms: 0.02" in dump_yaml(measured), "the threshold reaches the file")
    ms = set_value(config, "voice.wake.min_utterance_ms", 420)
    check(ms.voice.wake.min_utterance_ms == 420, "the calibrated minimum is settable")
    check("min_utterance_ms: 420" in dump_yaml(ms), "the minimum reaches the file")

    raised = False
    try:
        set_value(config, "voice.wake.min_confidence", 1.5)
    except (KeyError, ValueError):
        raised = True
    check(raised, "a confidence above 1 is refused")

    raised = False
    try:
        set_value(config, "voice.wake.silence_ms", 10)
    except (KeyError, ValueError):
        raised = True
    check(raised, "a silence gap below 200 ms is refused")

    raised = False
    try:
        set_value(config, "voice.wake.min_utterance_ms", 90)
    except (KeyError, ValueError):
        raised = True
    check(raised, "a minimum under 100 ms is refused")

    raised = False
    try:
        set_value(config, "voice.wake.threshold_rms", 0.5)
    except (KeyError, ValueError):
        raised = True
    check(raised, "a threshold above 0.2 is refused")


def test_legacy_migration() -> None:
    old = """\
models:
  endpoint: http://127.0.0.1:1234/v1
  structured: grammar
  planner:
    name: qwen3-14b-instruct-q4_k_m
    ctx: 16384
    temperature: 0.6
  reflex:
    name: qwen3-8b-q4_k_m
    ctx: 8192
    temperature: 0.8
  vision:
    name: qwen3-vl-4b-q4_k_m
    ctx: 8192
    temperature: 0.2
  embed:
    name: bge-m3
    device: cpu
    dim: 1024
  rerank:
    name: bge-reranker-v2-m3
    device: cpu
    enabled: true
  governor:
    max_vram_gb: 14
    keep_planner_resident: true
"""
    config = load_config_with(old)
    check(config.models.endpoint == "http://127.0.0.1:1234/v1", "endpoint is preserved")
    check(config.models.model == "", "legacy preset names are not carried forward")
    check(not hasattr(config.models, "planner"), "legacy planner is gone")
    check(config.models.embed.name == "", "legacy embedding preset is cleared")
    check(config.models.rerank.name == "", "legacy rerank preset is cleared")
    check(config.models.embed.dim == 1024, "embedding dimension is preserved")
    check(
        "temperature" not in config.models.model_dump(),
        "a legacy sampler setting is dropped",
    )
    check(config.models.structured == "grammar", "explicit legacy structured mode is preserved")

    custom = """\
models:
  planner:
    name: my-local-model
    ctx: 8192
    temperature: 0.4
  reflex:
    name: qwen3-8b-q4_k_m
"""
    chosen = load_config_with(custom)
    check(chosen.models.model == "my-local-model", "a custom model choice is preserved")
    check(
        "temperature" not in chosen.models.model_dump(),
        "a custom planner temperature is dropped too",
    )

    # A config written by the current version has the field at the top level of
    # `models`, not inside the old planner block. It has to load, not raise.
    modern = """\
models:
  endpoint: http://127.0.0.1:8080/v1
  temperature: 0.9
"""
    loaded = load_config_with(modern)
    check(
        "temperature" not in loaded.models.model_dump(),
        "a retired top-level temperature is dropped, not rejected",
    )
    check(
        loaded.models.endpoint == "http://127.0.0.1:8080/v1",
        "the rest of that file still loads",
    )


def load_config_with(text: str) -> CapriConfig:
    root = Path(tempfile.mkdtemp(prefix="capri-models-"))
    try:
        home = root / "home"
        home.mkdir()
        (home / "config.yaml").write_text(text, encoding="utf-8")
        previous = os.environ.get("CAPRI_HOME")
        os.environ["CAPRI_HOME"] = str(home)
        try:
            return load_config()
        finally:
            if previous is None:
                os.environ.pop("CAPRI_HOME", None)
            else:
                os.environ["CAPRI_HOME"] = previous
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_retired_fields() -> None:
    old = """\
version: 1
agent:
  one_tool_per_turn: false
memory:
  max_semantic_items: 100
voice:
  wake_word: hey capri
  voice_blend:
    b: af_nicole
    mix: 0.4
  dsp:
    formant: 1.2
avatar:
  pack: old-pack
  scale: 1.5
  mood_default: sleepy
policy:
  allow_hosts: ["example.com"]
  confirm:
    purchase: always
    passwords: always
"""
    config = load_config_with(old)
    check(not hasattr(config, "version"), "retired config version is dropped")
    check(not hasattr(config.agent, "one_tool_per_turn"), "retired agent switch is dropped")
    check(not hasattr(config.memory, "max_semantic_items"), "retired memory cap is dropped")
    check(not hasattr(config.voice, "wake_word"), "retired wake word is dropped")
    check(not hasattr(config.voice.voice_blend, "b"), "unused blend voice is dropped")
    check(not hasattr(config.voice.voice_blend, "mix"), "unused blend mix is dropped")
    check(not hasattr(config.voice.dsp, "formant"), "unused formant control is dropped")
    check(not hasattr(config.avatar, "pack"), "unused avatar pack is dropped")
    check(not hasattr(config.avatar, "scale"), "unused avatar scale is dropped")
    check(not hasattr(config.avatar, "mood_default"), "unused default mood is dropped")
    check(not hasattr(config.policy, "allow_hosts"), "unused network allowlist is dropped")
    check(not hasattr(config.policy.confirm, "purchase"), "unused purchase policy is dropped")
    check(not hasattr(config.policy.confirm, "passwords"), "unused password policy is dropped")


def test_live_reconfigure() -> None:
    old = CapriConfig()
    old.models.endpoint = "http://old.test/v1"
    old.models.embed.name = "old-embed"
    embedder = EndpointEmbedder(old)
    reranker = EndpointReranker(old)

    new = old.model_copy(deep=True)
    new.models.endpoint = "http://new.test/v1"
    new.models.embed.name = "new-embed"
    embedder.reconfigure(new)
    reranker.reconfigure(new)

    check(embedder.cfg.endpoint == "http://new.test/v1", "embedder follows a live endpoint change")
    check(embedder.name == "new-embed", "embedder follows a live model change")
    check(reranker.base == "http://new.test/v1", "reranker follows a live endpoint change")


def test_parser() -> None:
    decoded = _decode_json(
        'Thinking about it...\n```json\n{"tool": "done", "final": "hi"}\n```\n'
    )
    check(decoded == {"tool": "done", "final": "hi"}, "JSON is found inside prose and a fence")
    decision, reason = _parse_decision(
        'Sure.\n{"thought": "answer", "tool": "done", "final": "hello"}',
        [],
    )
    check(decision is not None and decision.final == "hello", "a prose-wrapped decision is usable")
    check(not reason, "a valid decision has no failure reason")
    _, reason = _parse_decision("I don't have a decision.", [])
    check("JSON" in reason, f"plain prose gives a useful reason: {reason}")


async def test_decision_repair() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        text = (
            "I can help with that."
            if len(calls) == 1
            else '{"thought":"answer","tool":"done","final":"Hello, Jayden."}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": text}}], "usage": {}},
        )

    config = CapriConfig()
    config.models.endpoint = "http://model.test/v1"
    config.models.structured = "grammar"
    router = Router(config.models)
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        decision = await router.decide([Msg("user", "say hi")], [])
        check(decision.final == "Hello, Jayden.", "a prose decision is repaired")
        check(not decision.failed, "a repaired decision is not marked failed")
        check(len(calls) == 2, "exactly one repair call is made")
        check("grammar" in calls[0], "the first call uses the configured grammar")
        check("response_format" in calls[1], "the repair falls back to JSON schema")
        # Her unusable reply is her own turn, not the person's: pasted into a
        # user message, small models answered their own reasoning as a request.
        sent = calls[1]["messages"]
        roles = [m["role"] for m in sent]
        check(
            roles[-2:] == ["assistant", "user"],
            f"the retry is her reply, then the instruction: {roles}",
        )
        check(
            "I can help with that." in sent[-2]["content"],
            "her unusable reply rides on the assistant role",
        )
        check(
            "I can help with that." not in sent[-1]["content"],
            "and is not pasted into what the user is made to say",
        )
    finally:
        await router.aclose()


async def test_schema_request_fallback(status: int) -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(
                status,
                json={"error": {"message": "response_format is not supported"}},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"thought":"answer","tool":"done","final":"Hello, Jayden."}'
                        }
                    }
                ],
                "usage": {},
            },
        )

    config = CapriConfig()
    config.models.endpoint = "http://model.test/v1"
    config.models.structured = "json_schema"
    router = Router(config.models)
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        decision = await router.decide([Msg("user", "say hi")], [])
        check(decision.final == "Hello, Jayden.", f"HTTP {status} rejection recovers with valid JSON")
        check("response_format" in calls[0], f"HTTP {status} first request uses response_format")
        check("grammar" in calls[1], f"HTTP {status} retry sends GBNF grammar")
        check("response_format" not in calls[1], f"HTTP {status} retry removes response_format")
        check(len(calls) == 2, f"HTTP {status} uses exactly one grammar retry")
    finally:
        await router.aclose()


async def test_server_error_does_not_retry() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(500, json={"error": {"message": "boom"}})

    config = CapriConfig()
    config.models.endpoint = "http://model.test/v1"
    config.models.structured = "json_schema"
    router = Router(config.models)
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        try:
            await router.decide([Msg("user", "say hi")], [])
        except ModelUnavailable:
            pass
        else:
            check(False, "HTTP 500 refuses to fall back")
        check(len(calls) == 1, "HTTP 500 is not treated as a response_format rejection")
    finally:
        await router.aclose()


async def test_request_shape() -> None:
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "server-model",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    config = CapriConfig()
    config.models.endpoint = "http://model.test/v1"
    router = Router(config.models)
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await router.complete([Msg("user", "hello")])
        check(result.text == "ok", "request completes")
        check("model" not in bodies[0], "a blank model ID is omitted")
        check("temperature" not in bodies[0], "sampling is left to the server")

        configured = config.models.model_copy(update={"model": "local-id"})
        router.reconfigure(configured)
        await router.complete([Msg("user", "hello")])
        check(bodies[1]["model"] == "local-id", "the configured model ID is sent")

        router.reconfigure(config.models.model_copy(update={"endpoint": ""}))
        try:
            await router.complete([Msg("user", "hello")])
        except ModelUnavailable as exc:
            check("settings" in str(exc).lower(), "an empty endpoint gives a settings hint")
        else:
            check(False, "an empty endpoint refuses to call out")
    finally:
        await router.aclose()


async def test_separate_endpoints() -> None:
    calls: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"thought":"done","tool":"done","final":"yes"}'
                        }
                    }
                ],
                "data": [{"index": 0, "embedding": [0.1] * 16}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    config = CapriConfig()
    config.models.endpoint = "http://main.test/v1"
    config.models.model = "main-model"
    config.models.decision_endpoint = "http://decision.test/v1"
    config.models.decision_model = "small-fast"
    config.models.embed.endpoint = "http://embed.test/v1"
    config.models.embed.dim = 16

    embedder = EndpointEmbedder(config)
    check(embedder._base == "http://embed.test/v1", "embedder uses custom embed endpoint")

    router = Router(config.models)
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await router.complete([Msg("user", "think")], role="planner")
        check(calls[-1][0] == "http://main.test/v1/chat/completions", "planner complete routes to main endpoint")
        check(calls[-1][1].get("model") == "main-model", "planner complete uses main model")

        await router.complete([Msg("user", "decide")], role="decision")
        check(calls[-1][0] == "http://decision.test/v1/chat/completions", "decision complete routes to decision endpoint")
        check(calls[-1][1].get("model") == "small-fast", "decision complete uses decision model")

        await router.decide([Msg("user", "what to do")], [])
        check(calls[-1][0] == "http://decision.test/v1/chat/completions", "decide() routes to decision endpoint")
        check(calls[-1][1].get("model") == "small-fast", "decide() uses decision model")
    finally:
        await router.aclose()


async def test_endpoint_fallbacks() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.read())
        calls.append((url, body))
        if "decision.test" in url:
            # Simulate decision endpoint being down / returning error
            return httpx.Response(500, json={"error": "decision model crashed"})
        if "embed.test" in url:
            # Simulate embed endpoint being down
            return httpx.Response(502, json={"error": "embed service unavailable"})
        if "/embeddings" in url:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 16}]})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"thought":"ok","tool":null,"final":"Recovered on main"}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    config = CapriConfig()
    config.models.endpoint = "http://main.test/v1"
    config.models.model = "main-model"
    config.models.decision_endpoint = "http://decision.test/v1"
    config.models.decision_model = "small-fast"
    config.models.embed.endpoint = "http://embed.test/v1"
    config.models.embed.dim = 16

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = EndpointEmbedder(config, client=mock_client)
    router = Router(config.models)
    router._client = mock_client
    try:
        # Decision call should fail on decision.test and cleanly fall back to main.test
        res = await router.complete([Msg("user", "do fast work")], role="decision")
        check(res.text == '{"thought":"ok","tool":null,"final":"Recovered on main"}', "recovers on main endpoint")
        check(any("decision.test" in c[0] for c in calls), "attempted decision endpoint first")
        check(calls[-1][0] == "http://main.test/v1/chat/completions", "fell back to main endpoint")
        check(calls[-1][1].get("model") == "main-model", "fallback used main model")
        check(router._healthy is not False, "router health was not poisoned by decision failure")

        # Embed call should fail on embed.test and fall back to main.test
        vecs = await embedder.embed(["hello world"])
        check(len(vecs) == 1 and len(vecs[0]) == 16, "embedder recovered via main endpoint")
        check(any("embed.test" in c[0] for c in calls), "attempted custom embed endpoint first")
        check(calls[-1][0] == "http://main.test/v1/embeddings", "embedder fell back to main endpoint")
    finally:
        await router.aclose()
        await embedder.aclose()


async def main() -> int:
    print("defaults")
    test_defaults()
    print("wake config")
    test_wake_config()
    print("legacy migration")
    test_legacy_migration()
    print("retired fields")
    test_retired_fields()
    print("live reconfigure")
    test_live_reconfigure()
    print("parser")
    test_parser()
    print("decision repair")
    await test_decision_repair()
    print("schema request fallback")
    await test_schema_request_fallback(400)
    await test_schema_request_fallback(422)
    print("schema request errors")
    await test_server_error_does_not_retry()
    print("request shape")
    await test_request_shape()
    print("separate endpoints")
    await test_separate_endpoints()
    print("endpoint fallbacks")
    await test_endpoint_fallbacks()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
