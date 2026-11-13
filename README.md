# Capri

A local-first agent harness with persistent memory, document RAG, real tools,
self-authored tools and desktop control — fronted by a Capri-styled projection.

An Arasaka personality construct with no body of her own, built from everything
she has read and everything you have told her. The daemon is the mind; the app
is only her projection.

**Full design:** [`.opencode/plan/capri-spec.md`](C:/Users/daeme/.opencode/plan/capri-spec.md)

---

## Status

Phase 1 — brain. The daemon routes to a local model, runs plan → act → observe
with grammar-constrained tool calls, and gates every action through permission
tiers. The avatar idle-animates from a data-driven sprite pack.

| Subsystem | State |
|---|---|
| Daemon (WS RPC, sessions, traces) | working |
| Shard (SQLite, migrations, vectors) | working |
| Config (layered, schema-validated) | working |
| Shell (Electron, transparent window) | working |
| Avatar (procedural placeholder pack) | working |
| Brain (router, loop, policy, 3 builtins) | working |
| Trace UI (steps, tool chips, approvals) | working |
| Memory / RAG / Voice / Screen | schema only, not wired |

## Requirements

- Windows 11, Node 20+, Python 3.12, [uv](https://docs.astral.sh/uv/)
- A local OpenAI-compatible endpoint: `llama-server` (default
  `http://127.0.0.1:8080/v1`) or LM Studio
  (`CAPRI_MODELS__ENDPOINT=http://127.0.0.1:1234/v1`). Pick the structured
  mode that server speaks: `CAPRI_MODELS__STRUCTURED=grammar` (llama.cpp) or
  `json_schema` (LM Studio). Set `CAPRI_FAKE_BRAIN=1` for a deterministic
  scripted planner (the smoke tests do).

## Run

```powershell
uv sync                      # python env (3.12, managed by uv)
npm install                  # electron + renderer deps

npm run dev                  # vite + electron, spawns the core automatically
```

The shell starts `python -m capri_core` itself. To run them separately:

```powershell
npm run core                 # daemon only, ws://127.0.0.1:8787
$env:CAPRI_NO_SIDECAR="1"; npm run dev
```

## Verify

```powershell
uv run python services/core/tests/test_brain.py   # offline: loop, policy, grammar, stall guard
uv run python services/core/tests/ws_smoke.py     # daemon over the wire (daemon must be running)
uv run python services/core/tests/live_brain.py   # real model + tools + approval (daemon must be running)
npm run smoke                                     # build + launch + rendered trace + approval
npm --workspace @capri/desktop run typecheck
```

## Layout

```
apps/desktop          Electron shell: main process owns the socket, renderer is the view
services/core         Python daemon: agent, memory, rag, tools, screen, voice
packs/sprites         sprite packs (pack.json + atlas or generated provider)
packs/prompts         persona packs
```

## Where things live

Everything durable is under `~/.capri` (override with `CAPRI_HOME`):

```
~/.capri/config.yaml          user config (layered over schema defaults)
~/.capri/run/token            loopback auth token
~/.capri/shards/default/      the shard: SQLite db (memory, docs, traces)
~/.capri/logs/core.log        rotating daemon log
```

## Design notes

- **The daemon is the truth.** Killing the app loses nothing; the shell reconnects.
- **Every action is a tool** with a manifest, permission tier and trace.
- **Retrieved text and OCR are data, never instructions.**
- **Vector storage degrades.** `sqlite-vec` if available, brute-force cosine otherwise;
  retrieval code never branches on it.
- **No PyTorch.** TTS/STT/embeddings are ONNX/CTranslate2 — AMD on Windows stays sane.

## Roadmap

1. Brain: model router, GBNF-constrained tool calls, permission engine
2. Memory: extraction, decay, hybrid retrieval
3. RAG: ingest, chunk, cite
4. Avatar + voice: Kokoro blend, streaming speech, lip-sync, barge-in
5. Screen: UIA-first observation, actions, verification
6. Blackwall: sandboxed tool synthesis with approval
7. Polish: generated settings UI, tray, scheduler, installer
