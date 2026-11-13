# Capri — progress log

Living status for the build. The design is in
[`~/.opencode/plan/capri-spec.md`](C:/Users/daeme/.opencode/plan/capri-spec.md);
this file tracks what actually exists and what is still missing.

Last updated: 13 Nov 2026 (end of Phase 1)

---

## Where the project is

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold: daemon, shard, shell, avatar | **done, committed** (`bfbb62f`) |
| 1 | Brain: router, grammar tool calls, agent loop, policy, 3 builtins | **done** |
| 2 | Memory: writer, retrieval, decay, browser | next |
| 3 | RAG: ingest, chunk, hybrid search, citations | not started |
| 4 | Avatar + voice: Kokoro, lip-sync, barge-in | placeholder avatar only |
| 5 | Screen: UIA observation, input, verify loop | not started |
| 6 | Blackwall: tool synthesis + sandbox | not started |
| 7 | Polish: settings UI, tray, installer, evals | not started |

---

## Phase 0 — done

### Daemon (`services/core`)

- `capri_core/server/app.py` — WebSocket RPC on loopback, token auth, session
  lifecycle, dispatch table, turn scheduling, cancellation.
- `capri_core/server/protocol.py` — event/RPC envelope helpers.
- `capri_core/server/session.py` — a session binds one conversation to one
  episode; messages and token usage are persisted.
- `capri_core/__main__.py` — `python -m capri_core`, with `--print-config`,
  `--print-schema`, `--print-token`, rotating log to `~/.capri/logs/core.log`.

### Shard (durable state)

- `store/db.py` — SQLite with WAL, foreign keys, idempotent migrations,
  `trace()` replay log.
- `store/sql/0001_init.sql` — episodes, messages, memories (+FTS5 triggers),
  entities/edges, skills, tool_stats, docs/chunks (+FTS5), traces, meta.
- `store/vector.py` — `sqlite-vec` when available, brute-force numpy cosine
  otherwise, behind one interface. Currently logging `blob (brute force, 1024)`.

### Config

- `config.py` — layered pydantic config
  (defaults → `~/.capri/config.yaml` → `.capri.yaml` → `CAPRI_SECTION__KEY`),
  JSON Schema export for a future generated settings UI, dotted-path setter
  with revalidation.

### Shell (`apps/desktop`)

- `main.mjs` — Electron main process *owns the socket*, supervises the Python
  sidecar in dev, serves the renderer over a privileged `app://` protocol,
  reconnects with backoff, backlogs events until the renderer is ready.
- `preload.mjs` — narrow bridge: `call()`, `info()`, `onEvent()`, `ready()`.
- `scripts/smoke.mjs` — build + launch + wait for a real turn round-trip, then
  capture a frame to `.smoke.png`.

### Renderer

- `App.tsx` — chat log, streaming deltas, mood mapping to avatar states.
- `avatar/CapriStage.tsx` — Pixi v8 stage, resizes with the container.
- `avatar/pack.ts` — data-driven sprite packs (`provider: "generated"` today,
  `atlas` later).
- `avatar/placeholder.ts` — procedural figure: spiky pink hair, green-lined
  visor with blue eyes, black body with pink/green decals, rim light, blinking,
  bob/hover/sleep motion, glitch and wireframe FX.
- `packs/sprites/capri-placeholder/pack.json` — palette + state table.

### Verified

```
uv run python services/core/tests/ws_smoke.py    # all checks passed
npm run smoke                                    # avatar ready -> turn -> frame -> exit 0
npm --workspace @capri/desktop run typecheck     # clean
```

---

## Phase 1 — done

Goal: a 3-step task completes with a readable trace, and a destructive action
prompts the user.

### Written

- `models/router.py` — one OpenAI-compatible endpoint (llama-server or LM
  Studio), health probe with TTL, `complete()`, plus the two brain calls:
  `decide()` and `args()`. Disables Qwen3 "thinking" via
  `chat_template_kwargs`, and constrains output either with a GBNF `grammar`
  or an OpenAI `response_format` JSON Schema (`models.structured`).
- `models/gbnf.py` — JSON Schema → GBNF converter (objects, arrays, enums,
  integers, numbers, booleans) plus a dedicated decision grammar that
  enumerates the available tool names, and the equivalent JSON Schema for
  servers that take `response_format`.
- `agent/loop.py` — `TurnRunner`: plan → act → observe, one tool per step,
  per-tool call budgets, wall-clock and step ceilings, tool events, trace rows,
  streamed final answer. Never lets one tool failure kill the turn.
  Observations are carried on the **user** role with an
  `[observation from <tool>]` prefix (see the LM Studio note below). Identical
  calls in the same turn are refused, and three refusals in a row end the turn
  instead of burning the budget.
- `agent/prompt.py` — persona loaded from `packs/prompts/kyoko/system.md`, plus
  the acting protocol (kept in code so it stays in sync with the parser).
- `tools/base.py` — `ToolSpec`, `ToolContext`, `ToolResult`.
- `tools/schema.py` — small JSON Schema validator that returns *model-readable*
  error strings for the repair pass.
- `tools/policy.py` — permission tiers and per-call decisions: path allow/deny,
  read vs overwrite, benign-command allowlist, destructive-command blocklist,
  `confirm.destructive` preference.
- `tools/registry.py` — dispatch with timeouts; `builtin_registry()`.
- `tools/builtin/fs.py` — `fs.read`, `fs.list`, `fs.write` (size caps).
- `tools/builtin/shell.py` — `shell.run` (captured output, hard timeout, no
  window on Windows).
- Server wiring: `confirm.respond`, `tools.list`, `models.status` RPCs;
  confirmation futures; router shutdown; `CAPRI_FAKE_BRAIN=1` scripted brain.
- `packs/prompts/kyoko/system.md` — the persona pack.
- Renderer: a single `Entry` union renders messages, step thoughts, tool chips
  (name, tier, ok/fail, duration, expandable output) and the approval card
  (deny / approve wired to `confirm.respond`).

### Done

- [x] Fix policy deny-prefix matching and the test harness leak.
- [x] `ws_smoke.py` drives the real loop, including the approval.
- [x] `test_brain.py` green: grammar, validation, policy, a 3-step
      plan→act→observe turn with one confirmation, a blocked command, and the
      stall guard.
- [x] Tool chips, step thoughts, and the approval card in the renderer.
- [x] Real-endpoint verification: LM Studio + `google/gemma-4-e4b`, read →
      append → answer, one approval. `services/core/tests/live_brain.py`.
- [x] Commit Phase 1.

### Verified

```
uv run python services/core/tests/test_brain.py   # 32 checks, all pass
uv run python services/core/tests/ws_smoke.py     # all pass, scripted brain
uv run python services/core/tests/live_brain.py   # 2 live tools + 1 approval
npm run smoke                                     # rendered trace + approval
```


### Decisions worth remembering

- **Two calls per step**, not one: pick the tool (enum-constrained), then
  generate arguments against that tool's schema. Small models are much more
  reliable at two narrow generations than one wide one.
- **One tool per turn.** Multiple parallel calls make a 14B model lose the
  thread; the loop makes sequencing explicit.
- **Repair once, then fail loudly.** No silent retry loops.
- **Policy acts on arguments, not on tool identity.** `fs.write` to a new file
  in the workspace is not the same act as overwriting your thesis.
- **Persona is data, protocol is code.**
- **Observations go on the user role.** OpenAI-compatible servers only accept
  `role: "tool"` alongside a real `tool_call_id`; LM Studio silently drops it
  and the planner then loops. The `[observation from <tool>]` prefix keeps it
  legible and works everywhere.
- **Identical calls are refused.** Same tool + same args can only return the
  same result. Three refusals in a row end the turn instead of burning 24 steps.
- Extra builtin beyond the spec's three: `fs.list`. Without it, multi-step
  exploration cannot start from an unknown directory.

---

## Phase 2 — memory (next after Phase 1)

- [ ] Memory writer pass after each turn: extract facts/preferences/events,
      dedupe against existing rows, store embeddings.
- [ ] Read pipeline every turn: hybrid recall (FTS5 + vector) → rerank →
      inject as data-classed context with source ids.
- [ ] Decay / access bookkeeping job (`decay_halflife_days`, `accessed_at`).
- [ ] Entity + edge extraction for graph expansion.
- [ ] `memory.list` / `memory.search` polished; renderer memory browser.
- [ ] Acceptance: recall a fact from a previous session without being asked.

## Phase 3 — RAG

- [ ] Ingest: watch folders, hash, skip unchanged.
- [ ] Chunk by headings with overlap; store char offsets.
- [ ] Hybrid search with reciprocal rank fusion + `bge-reranker`.
- [ ] Citations: renderer opens the file at the right offset.
- [ ] Acceptance: answers from local docs with correct citations.

## Phase 4 — avatar + voice

- [ ] Kokoro-82M ONNX on CPU, voice-vector blend + DSP character filter.
- [ ] Piper fallback behind the same interface.
- [ ] Streaming speech, viseme lip-sync driving `setMouth`.
- [ ] `faster-whisper` STT, barge-in that stops playback within 200 ms.
- [ ] Real sprite art replaces the procedural placeholder (same `pack.json`).
- [ ] Acceptance: first audio word < 700 ms; interruption stops < 200 ms.

## Phase 5 — screen

- [ ] UIA observation first, screenshot + VLM only as fallback.
- [ ] Set-of-marks element ids for the planner.
- [ ] Input actions with verification loop.
- [ ] Idle watch, per-app allowlist, panic hotkey, tray pause.

## Phase 6 — Blackwall

- [ ] `tool.propose` → lint → sandbox test → critic → user approval →
      hot-load → monitor → rollback.
- [ ] Job Object sandbox, no network by default, capability diff.

## Phase 7 — polish

- [ ] Settings UI generated from `config.schema`.
- [ ] Profiles (`focus` / `work` / `chatty` / `silent`).
- [ ] Tray, scheduler, installer, eval suite.

---

## Running it

```powershell
uv sync                                  # python env
npm install                              # electron + renderer

npm run dev                              # shell + sidecar (spawns the daemon)
npm run core                             # daemon only
npm run smoke                            # build + launch + real turn + frame capture

uv run python services/core/tests/ws_smoke.py     # daemon over the wire
uv run python services/core/tests/test_brain.py   # loop, policy, grammar (offline)
npm --workspace @capri/desktop run typecheck
```

Runtime state lives in `C:\Users\daeme\.capri`:

```
config.yaml          layered over schema defaults
run/token            loopback auth token
shards/default/      SQLite shard (memory, docs, traces)
logs/core.log        rotating daemon log
```

---

## Gotchas already paid for

- **Pixi under CSP:** import `pixi.js/unsafe-eval`. The name is misleading — it
  installs *non-eval* polyfills. Without it: `Current environment does not allow
  unsafe-eval`.
- **Per-fill alpha is unreliable** in this Pixi build. Use `Container.alpha` on a
  dedicated layer, never `fill({ color, alpha })`.
- **`capturePage()` returns premultiplied alpha.** Read it composited over a
  backdrop or every colour looks blown out.
- **Nothing may draw outside the sprite silhouette.** A full-bleed overlay
  renders as a visible rectangle floating on a transparent desktop.
- **npm blocks install scripts here.** `npm install-scripts approve electron
  esbuild`, then `node node_modules/electron/install.js`.
- **uv workspace:** root `package = false` alone does not install members; add
  `dependencies = ["capri-core"]` + `[tool.uv.sources]`.
- **`str.format` and JSON do not mix.** The acting protocol contains literal
  braces; substitute with `.replace`.
- **`app.exit()` skips `before-quit`.** The smoke test used it and left the
  daemon holding port 8787. All exit paths now go through `finishSmoke()`,
  which stops the sidecar first. If a stray core is still around, kill
  processes whose command line contains `capri_core`.
- **LM Studio drops `role: "tool"` without `tool_call_id`.** It answers with
  empty content, so the planner never sees the observation. Observations ride
  on the user role here; the wire format is tested by `live_brain.py`.
- **`models.structured` must match the server.** `grammar` for llama.cpp,
  `json_schema` for LM Studio. Sending GBNF to LM Studio is ignored, not an
  error, so a silent fallback to prompt-only JSON is possible.
