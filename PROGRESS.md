# Capri — progress log

Living status for the build. The design is in
[`~/.opencode/plan/capri-spec.md`](C:/Users/daeme/.opencode/plan/capri-spec.md);
this file tracks what actually exists and what is still missing.

Last updated: 14 Nov 2026 (Phase 8 M1 context plumbing implemented; no commit)

---

## Where the project is

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold: daemon, shard, shell, avatar | **done, committed** (`bfbb62f`) |
| 1 | Brain: router, grammar tool calls, agent loop, policy, 3 builtins | **done** |
| 2 | Memory: writer, retrieval, decay, browser | **done** |
| 3 | RAG: ingest, chunk, hybrid search, citations | **done** |
| 4 | Avatar + voice: Kokoro, Piper, STT, barge-in, tray/window | **implemented** — STT + barge-in + tray hotkeys + window chrome; Kokoro first-word target not met on this CPU, Piper is the fast path |
| 5 | Screen: UIA observation, marks/OCR, input, verify loop | **implemented** — marks/OCR, pixel diff + retry, drag/clipboard/tray pause; live idle and password checks |
| 6 | Blackwall: tool synthesis + sandbox | **implemented** — manifests, AST gate, suspended Job Object, warm workers, model critic, unified diff, approval, stats/rollback, recipes |
| 7 | Polish: settings UI, tray, installer, evals | complete: generated settings, profiles, scheduler, shard bundles, tray/hotkeys, installer, evals |
| 8 | Companion agent (`PLAN.md`): M0 prompt and posture; M1 context plumbing | **done**; M2 project context next |

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

## Phase 2 — done

Goal: recall a fact from a previous session without being asked, and keep the
shard honest while doing it.

### Written

- `memory/repo.py` — shard CRUD. Insert / merge / supersede, soft `forget`,
  access bookkeeping, FTS5 query building with stopword stripping, a `LIKE`
  fallback for when FTS refuses the query, and the graph primitives
  (`upsert_entity`, `add_edge`, `memories_for_entities`).
- `memory/writer.py` — the after-turn pass. One structured generation against
  `ITEM_SCHEMA` yields `{kind, text, importance, confidence, entities}`; the
  module then decides merge / supersede / insert against the nearest existing
  vector (0.92 / 0.80 cosine by default).
- `memory/retrieval.py` — recall: FTS5 + vectors + graph expansion, fused with
  reciprocal rank fusion plus a small importance and recency bonus, then
  packed to a token budget. `format_context()` renders the data-classed block.
- `memory/decay.py` — `importance × exp(-age/halflife) × (1 + log1p(access))`,
  used for pruning only. One-day floor, current episode protected, soft
  delete, traced as `memory.prune`.
- `memory/embed.py` — `EndpointEmbedder` (`/v1/embeddings`, batched, dimension
  checked) and `HashEmbedder` (deterministic feature hashing for offline
  tests). An unreachable embedder degrades to lexical recall rather than
  failing the turn.
- `memory/service.py` — the facade the server and loop see: `recall_block`,
  `observe_turn`, `write`, `search`, `list`, `forget`, `maintain`, `count`.
- Turn loop: recall is fetched once before the first decision and injected
  into the system prompt; `observe_turn` runs after `agent.done` with the
  turn's tool observations and emits `memory.written` per item.
- Server: `memory.list`, `memory.search`, `memory.write`, `memory.forget`,
  `memory.maintain`, and the `memory.written` event.
- Renderer: memory browser behind the ◈ header toggle — debounced search, a
  `sweep` button that runs decay now, per-item forget, kind-coloured rows,
  live refresh on writes, and one toast per `memory.written`.
- Config: `memory.embed` (`endpoint` | `hash`), `recall_items`,
  `recall_budget_tokens`, `dedupe_threshold`, `contradiction_threshold`,
  `prune_below`.

### Done

- [x] Memory writer pass after each turn: extract, dedupe, store, embed.
- [x] Read pipeline every turn: hybrid recall → graph expansion → budget pack
      → injected as tagged, data-classed context with ids and confidence.
- [x] Decay / access bookkeeping (`accessed_at`, `access_count`, `maintain`).
- [x] Entity + edge extraction for graph expansion.
- [x] `memory.list` / `memory.search` plus the renderer memory browser.
- [x] Acceptance: a fact written in an earlier episode reaches the prompt of a
      later one, unprompted.

### Verified

```
uv run python services/core/tests/test_memory.py   # 29 checks, all pass
uv run python services/core/tests/test_brain.py    # all pass
npm --workspace @capri/desktop run typecheck       # clean
```

### Decisions worth remembering

- **Memory must never cost a turn.** Recall failure logs and continues with no
  block; the writer's failure is swallowed in the service. A weak model writing
  nonsense is annoying; a weak model killing the reply is fatal.
- **Ranking is not truth.** RRF + importance + recency decides what enters
  context; the decay score only decides what to prune. Mixing them turns every
  recall into a popularity contest.
- **Supersede, don't delete.** `superseded_by` keeps the old text for audit;
  `deleted_at` is the only user-facing removal and it stays recoverable.
- **Without vectors the writer never guesses.** No embeddings means exact
  normalized-text matching, not a similarity threshold over nothing.
- **The memory block is escaped and tagged.** `<`/`>` in stored text become
  `‹`/`›` so a remembered string cannot forge or close the `<memory>` tag.
- **Graph expansion is deliberately shallow.** Entity co-occurrence only, one
  hop, weight 0.55 — enough to find "the deploy key" when you say "Zed".
- **The memory browser swaps out the log.** The bubble is a three-row grid;
  reusing the content row keeps the layout stable and leaves the composer put.
- **The acceptance test uses the loop's own path.** `test_turn_injection`
  drives a real `TurnRunner` and asserts the memory block reached the system
  message the planner sees — not a mock of the prompt builder.

## Phase 3 — RAG — done

Goal: answers from local docs with citations that open the right file and
offset. The shard already had `docs`, `chunks`, `chunk_fts` (0001) and
`chunk_vec`; this phase is the pipeline around them.

### Written

- `rag/loaders.py` — text and code read directly; HTML through trafilatura when
  present, stdlib extractor otherwise; PDF/DOCX through lazy `pypdf` /
  `python-docx` imports that fail with the package name. Size cap, NUL sniff,
  title from the first heading.
- `rag/chunker.py` — structure-aware: heading hierarchy → fenced code →
  paragraph → sentence → hard slice. Every chunk is an exact slice
  (`text[start:end] == chunk.text`), so spans are citable. Overlap extends the
  next chunk's start; it never rewrites text.
- `rag/index.py` — incremental ingest: SHA-256 per document, skip unchanged,
  replace changed, tombstone missing under the walked roots, drop `chunk_vec`
  rows before deleting chunks. Batched embeddings, traced as `rag.ingest`.
- `rag/search.py` — FTS5 + vectors, RRF fuse, rerank, budget pack; every hit
  carries path, heading, span and sources.
- `rag/rerank.py` — `POST /rerank` when the server supports it, lexical overlap
  otherwise. A reranker may fail; retrieval may not.
- `rag/service.py` — facade: `ingest`, `search`, `recall_block`, `collections`,
  `forget`, plus one cheap rewrite call (optional 3-way multi-query).
- Turn loop: documents are recalled before the first decision, injected into the
  system prompt next to memory, and every citation is emitted as `rag.cited`
  with its turn id.
- Server: `rag.ingest`, `rag.search`, `rag.collections`, `rag.forget`. Ingest
  checks the deny list before reading a byte.
- Renderer: docs panel behind ▤ — index a path, see collection counts, search,
  click a hit to open it. Citation chips under each answer, clickable.
- Shell: `capri:open-file` turns the character offset into a line number and
  asks VS Code for it, falling back to the OS association.
- Config: `rag.rewrite`, `rag.multi_query`, `rag.context_items`,
  `rag.context_budget_tokens`, `rag.max_doc_bytes`.

### Done

- [x] Loaders with collections over folders; URLs and clipboard stay opt-in.
- [x] Structure-aware chunking with `heading_path` and exact char spans.
- [x] Incremental ingest: hash, skip unchanged, tombstone deletions.
- [x] Query path: rewrite → hybrid → RRF → rerank → packed with
      `[doc:path#heading]` markers.
- [x] `rag.ingest` / `rag.search` / `rag.collections` / `rag.forget`, and the
      `rag.cited` event.
- [x] Citation chips that open the file at the offset.
- [x] Retrieved text wrapped in `<data source="rag">`, with the block labelled
      as data rather than instruction.
- [x] Acceptance: an answer cites the runbook, and the span opens at the right
      line.

### Verified

```
uv run python services/core/tests/test_rag.py    # 37 checks, all pass
uv run python services/core/tests/ws_smoke.py    # incl. rag.* over the wire
npm run smoke                                    # daemon + renderer, exit 0
npm --workspace @capri/desktop run typecheck     # clean
```

### Decisions worth remembering

- **A chunk never spans two sections.** Chunking breaks when the heading path
  changes, so a citation's heading is the heading it actually came from. Before
  that, a runbook chunk spanning `# Deploy` and `## Keys` reported only
  `# Deploy`.
- **Spans are slices, not summaries.** Overlap widens the next chunk's start;
  it never prepends rewritten text, because then `start_char` would be a lie.
- **Retrieval degrades, ingest does not lie.** No embedder → lexical FTS only;
  no reranker → lexical scores; both logged. A failed embed never silently
  stores a zero vector.
- **Tombstones, not deletes.** A doc that disappears is marked, its chunks are
  dropped, and the row survives for audit.
- **Optional dependencies are named when missing.** `pypdf` and `python-docx`
  are lazy imports; the error says which package to install instead of
  "unsupported file".
- **Ingest is still a read.** It checks `deny_paths` before touching the disk,
  so indexing a folder cannot wander into `.ssh`.
- **The shell owns "open at offset".** The daemon emits character spans; the
  Electron main process knows about editors and line numbers.

### Gotchas

- **`chunk_fts` is external-content.** Deleting rows from `chunks` fires the
  trigger; `chunk_vec` has no trigger, so those rows must be deleted by hand
  before the chunk rows disappear.
- **Server-backed reranking is `/rerank`, not an OpenAI route.** LM Studio does
  not implement it, hence the lexical fallback rather than an error.


## Phase 4 — avatar + voice (implemented; latency caveat)

Goal (§17): first audio word < 700 ms, interruption stops < 200 ms. The window,
the figure and the speech-out path exist; capture, barge-in, window chrome and
the tray do not.

### Voice path — written

- `voice/tts.py` — `TtsEngine` protocol, `KokoroEngine` (sherpa-onnx 2.0.1
  for Capri's pack; kokoro-onnx for the public styles; CPU, no torch),
  `ToneEngine` (offline placeholder: same pipeline, no weights) and an
  `UnavailableEngine` for engines the spec names but this phase does not build.
  Missing weights produce a message that names the files and the release URL,
  not a stack trace.
- `voice/service.py` — sentence splitting and streaming. At most
  `voice.queue_depth` finished segments are held ahead of the consumer, so the
  first segment leaves as soon as it exists and a long reply cannot balloon
  memory. Cancellation is an `asyncio.Event`, not a task kill, so
  `speech.stop{cancel}` is still emitted.
- `voice/visemes.py` — `{t, v}` at `voice.viseme_fps` from the text and the
  measured duration. Monotonic, always inside the audio, closes at the end.
- `voice/dsp.py` — bitcrush and radio, both real numpy. Pitch and formant are
  read from config but not applied: they need a phase vocoder, and resampling
  would change duration and desynchronise every viseme.
- `voice/wav.py` — 16-bit mono WAV, base64 data URL.
- Server: `voice.speak`, `voice.stop`, `voice.status`; every run traced as
  `voice.speak`.
- Turn loop: speech runs after `agent.done` and before memory extraction; a
  failed synth logs and the turn continues.
- Renderer: `audio/player.ts` schedules segments against one WebAudio clock
  (gapless across sentences), decodes base64 directly (no `fetch`, no CSP
  question), fires viseme timers, and closes the mouth on the last segment.
  `CapriStage` takes a `mouth` prop; App holds the talking pose while a mouth is
  live, because the daemon reports idle before playback finishes.
- CLI: `python -m capri_core --voice-status` prints the engine, why it is
  unavailable, and the exact paths it wants.

### Her own voice — Pocket, installed and verified

Capri's timbre now comes from Pocket TTS (Kyutai; MIT code, CC BY 4.0 weights;
CPU-only, no CUDA). A *voice state* is a small safetensors file. The state on
this machine came out of the CapriPersonalityConstruct mod, whose README says it
was tuned from **OmniVoice output, with no recording of any performer** (neither
the series nor an actress). It is that author's asset: installed locally, never
committed, never redistributed.

- `voice/pocket.py` — `PocketEngine`: `TTSModel.load_model(language=…)` once,
  the state once, one `generate_audio` per sentence. `HF_HOME` stays inside
  `<capri home>/models/pocket/hf`, so nothing lands in the user's cache.
- **Level matching.** Pocket swings 0.06–0.20 peak between sentences; each
  segment is brought to `LEVEL_RMS 0.09` (max +18 dB, clipped at 0.95). Without
  it, queued sentences jump in volume.
- `python -m capri_core --voice-install <mod.zip|voice.safetensors>` pulls the
  Capri state out of a mod pack, validates the safetensors header, and copies it
  to `voice.voice_file`.
- `voice.tts: pocket`; `voice.voice_file` (default
  `<capri home>/models/pocket/capri_english.safetensors`); `voice.language: en`
  (en/es/fr/de/it/pt/nl).
- Python side: `uv sync --package capri-core --extra pocket` (PyTorch CPU; set
  `UV_TORCH_BACKEND=cpu`). Pocket's base model downloads on the first sentence
  into the Capri home.

Measured here: 2.0 s of speech in 1.2 s warm, 5.4 s in 4.6 s — about 1.2x
realtime. Fine for conversation, **not** the < 700 ms first-word budget; Kokoro
remains the low-latency path.

Warm-up at startup: `PocketEngine.load()` and `VoiceService.prewarm()` run from
`CapriServer.run`, on a worker thread, so the daemon's socket is free while the
model loads. Measured by starting the daemon in a throwaway home: listening at
2.03 s, hello at 2.17 s, a ping during the load answered in 0 ms, one model
load logged (not two), `pocket warm in 13686 ms`, and the first audio segment
15.8 s when asked during the load versus 2.2 s for a 46-character sentence
asked after it. A lock in `PocketEngine._load` is what keeps the startup warm
and an early reply from loading the model twice.

Pitch: the state already carries her register, so the engine does not apply
`voice.dsp.pitch` on top. On an eight-sentence passage (f0 from a per-frame
cepstrum median) Kokoro dry read 199.9 Hz, Kokoro with its +2 lift 222.2 Hz, and
Pocket dry 229.6 Hz — within 0.6 semitones of the lifted Kokoro voice, and about
2.4 semitones above it when the +2 shift was applied. Duration is unchanged
either way; Pocket is ~1.5x slower than Kokoro, which runs at tempo
1.1 x speed / lift.

`~/.capri/config.yaml` now selects Kokoro's `capri` style row (below), so Pocket
is the alternative: `CAPRI_VOICE__TTS=pocket` for a run.

### Her voice from capri-desktop — installed

The lachydotmcg capri-desktop project publishes its local voice as
`capri-voice-1.zip` (316,041,808 bytes, SHA-256 `9fae0856…`, verified). It is
Kokoro-82M (Apache-2.0) plus a 54 x 510 x 256 style table that is **not** the
public Kokoro table — only 3 of 54 rows matched it. Their "Her own" preset is
row **53**, speed 1.1; their tuned settings are pitch +2, speed 1.2, volume
80 %, robot/glitch off, layers off.

- Installed `model.onnx` and the pack table into `<capri home>/models/kokoro/`;
  `voices-capri.bin` keeps the untouched table.
- `voices-v1.0.bin` is the public Kokoro table rebuilt as a named archive with
  one extra entry: `capri` = the pack's row 53. `voice_blend.a: capri` selects
  her; 55 voices load.
- Their anti-cloning policy matches ours: the project supplies a synthetic
  voice and prohibits cloning a living person without consent.
- Her `capri` row now goes through **sherpa-onnx 2.0.1**, the pack's engine
  and phonemizer path, not kokoro-onnx. A real render of
  `Hello. I'm Capri, and this is my voice.` against the pack measured:
  **1.7813 s in both**, F0 median **224.3 Hz in both**, MFCC mean cosine
  **0.99996**, log-mel DTW distance **0.0013**, and the repo resampler is
  bit-identical on identical raw PCM. sherpa-onnx 1.13.8 took 2.46 s for the
  same line and is not used.
- The tuned chain is reproduced exactly. Kokoro is asked for
  `1.1 * 1.2 / Lift(+2)` = **1.176** tempo, then the buffer is resampled by
  `Lift(+2)`, so she plays at 1.32x with F0 and formants moved together. This
  is capri-desktop's `VoiceFx.Tempo` + `VoiceFx.Kokoro` path; the old
  post-synthesis WSOLA shift was a different timbre. Config: speed 1.2, pitch
  +2, robot/glitch off, layers off. `dsp.resample_repo` is their 256-phase
  windowed-sinc kernel.
- Their 80 % volume is not applied: the renderer's fixed gain remains 0.9.
  Volume is playback loudness, not part of her timbre.
- Voice layers live in `dsp.mix_layers`, off by default: `dsp.layers: true`
  adds copies at +2 and -2.5 semitones, 25 ms behind, at 0.4 each. A 200 Hz
  tone comes back with peaks at 173.0 / 224.0 Hz and the same duration.

Verified through `VoiceService`: 3 segments, 17/24/33 visemes, 55 voices,
24 kHz. The real 2.03 s line rendered in 4.9 s cold including sherpa model
load and 1.78 s warm; that is below the interactive target and is recorded as
the Phase 4 latency caveat.

### Clone path — written

- `voice/clone.py` — `ChatterboxEngine` (MIT, zero-shot from a 5–10 s clip)
  behind the same `TtsEngine` protocol. Optional on purpose: it needs PyTorch,
  which the default Kokoro path avoids.
- `render_reference()` renders Capri's own reference from the synthetic Kokoro
  blend and writes `<capri home>/models/chatterbox/capri-reference.wav` via
  `python -m capri_core --voice-reference`. It refuses with a reason when Kokoro
  is absent — a reference has to come from a real synthesizer, not a guess.
- `voice.tts: chatterbox` selects the clone engine; `voice.reference_clip`
  points at the WAV. `--voice-status` lists the reference alongside the weights.

The clone target is **Capri's synthetic timbre**. Not the canon actress: §12.4
rules that out, and consent is the real constraint regardless of what the spec
says. The engine only ever reads a local WAV, so a user can point it at their own
voice or another clip they have the rights to; nothing is fetched or synthesized
from a performer.

### Model files (not committed)

`C:\Users\daeme\.capri\models\kokoro\`:

- `kokoro-v1.0.onnx` (311 MB), from the `capri-voice-1` pack.
- `voices-v1.0.bin` (28 MB) — public Kokoro table plus `capri`, used by
  kokoro-onnx for the public styles.
- `voices-capri.bin` (27 MB) — the pack's original table, used by sherpa-onnx.
- `tokens.txt` (687 B) and `lexicon-us-en.txt` (5.7 MB) — the pack's
  phonemizer inputs for sherpa-onnx. (The pack's `espeak-ng-data` is not needed
  by sherpa-onnx 2.0.1; language comes from the lexicon.)

Python side: `uv sync --package capri-core --extra kokoro` installs both
kokoro-onnx and sherpa-onnx 2.0.1.

Her Pocket voice stays at
`C:\Users\daeme\.capri\models\pocket\capri_english.safetensors` (7.7 MB, 18
tensors) with a 12.5 s `capri.wav` sample beside it; Pocket's base model lives in
the same folder.

With `voice.tts: tone` the pipeline is exercisable offline.

### Done

- [x] `voice/tts` interface; Kokoro-82M on CPU with her `capri` row through
      sherpa-onnx 2.0.1 and the pack's lexicon (3 segments through
      VoiceService). Public Kokoro styles stay on kokoro-onnx.
- [x] Sentence chunking with a bounded queue.
- [x] `speech.segment` with `audioUrl` + visemes; `speech.stop` on done/cancel.
- [x] `setMouth` driven by viseme timings.
- [x] Renderer WebAudio playback, no client-side analysis.
- [x] Bitcrush + radio from config.
- [x] Reference-clip clone path (Chatterbox), with Capri's reference rendered
      from the synthetic blend.
- [x] Capri's own voice (Pocket TTS), installed from the mod's synthetic state,
      level-matched, and verified end to end on this CPU and on screen.
- [x] Pitch shift on the Kokoro path matches capri-desktop: tempo compensated
      by the pitch lift, then the repo's exact 256-phase resample. Verified
      against a pack render at identical duration and F0.
- [x] Her voice layers (+2 / -2.5 semitones, 25 ms behind, 0.4 each) behind
      `dsp.layers`, off by default.
- [x] Her robot (`dsp.robot`, 0..1, off by default): a talk-box vocoder with a
      nearest-note saw carrier, her spectral envelope and noise for unvoiced
      frames, ported from capri-desktop's `Vocoder`.
- [x] Piper behind the same interface (`voice.tts: piper`; ~125 ms warm
      first segment here, ~1.4 s first load).
- [x] `faster-whisper` int8 + VAD push-to-talk; `voice.listen` RPC. The
      renderer records mono WAV, trims silence and downsamples to 16 kHz; STT
      is lazy and optional.
- [x] Barge-in: renderer energy/VAD monitor stops local playback and calls
      `voice.barge_in`; the daemon emits `speech.stop{barge-in}`. The monitor
      holds ~120 ms before firing, so local stop is under the 200 ms target.
- [x] Wake word ("Capri", `voice.wake`): the renderer segments speech on an
      energy gate and a silence timer, and `voice.wake` matches her name at the
      start of the transcript, accepting Whisper's spellings of it. A bare name
      arms one follow-up utterance; the gate is paused while she speaks or
      push-to-talk is held.
- [x] Wake confidence: faster-whisper's `avg_logprob`/`no_speech_prob` are kept
      per segment, and `voice.wake.min_confidence` (0.35) plus
      `max_no_speech_prob` (0.6) reject a candidate before her name is matched.
      `voice.warm` loads the weights when the gate arms: 3078 ms here, 0 ms on
      later calls.
- [x] Wake rejections are diagnosable: the gate traces
      `voice.wake.rejected` with the reason, confidence and no-speech
      probability (never the transcript), and `voice.status.wake.lastRejection`
      carries the last one for the shell.
- [x] Wake calibration: `voice.calibrate` measures the room (95th percentile
      frame RMS) and three takes (median of voiced frames) and returns a
      geometric-mean threshold plus half the shortest take as the minimum
      utterance; Settings writes both through `config.set` and shows a live
      level meter with the current threshold marked. Measured on synthetic
      audio: room 0.0142, voice 0.2121 → threshold 0.0549, shortest 350 ms;
      the gate reopens with the new numbers without a restart.
- [x] One capture graph (Phase 4a): `capture.worklet.ts` batches 1024 samples
      on the audio thread and `capture.ts` fans them to push-to-talk, barge-in,
      the wake gate and calibration, so the device opens once and the graph
      closes when the last listener leaves. Vite spike before migrating:
      `?worker&url` loaded in Chromium through the dev server (49 batches of
      1024 samples in the first 400 ms), and the production build emits a
      standalone 477-byte `capture.worklet-*.js` referenced as
      `new URL(..., import.meta.url)`; `npm run test:worklet` guards that.
      Input device is selected in Settings and stored per PC
      (`capri.micInput.v1`), with a fallback to the system default when the
      saved device is gone.
- [x] Updates install themselves. The shell owns an `electron-updater`
      instance built from the `publish` block electron-builder now writes into
      the app (`resources/app-update.yml`), checks GitHub shortly after launch
      and every six hours after that, downloads the installer `latest.yml` names,
      and installs it silently in place when the app quits — or immediately if
      the person clicks the chip. The header grows one `UpdateChip`: `checking…`,
      `update 0.5.2 · 42%`, `restart for 0.5.2`, `up to date`, or
      `update failed — retry` with the reason in the tooltip. Dev runs, smoke
      runs and unpacked builds report `unsupported` and never touch the feed.
      Verified end to end on Windows 11: 0.5.1 installed to a temp directory found
      the 0.5.2 release, downloaded the 268,444,205-byte installer (sha512
      recorded in the pending update), and after an ordinary window close the
      installed `app.asar` reported 0.5.2 — the installer ran silently and
      replaced it in place. 0.5.0/0.5.1 shipped before the feed existed, so those
      need one manual install; every release after does not.
- [x] MCP servers are managed from Settings. Before this, a server meant
      editing `config.yaml` and restarting the daemon, and a typo produced a
      silent no-show. `mcp.servers` / `mcp.save` / `mcp.remove` / `mcp.reload`
      write the config, re-validate the whole `CapriConfig`, rebind the service
      and restart the servers in place, so a change applies to the very next
      turn. `McpService.status()` is one row per configured server: command,
      args, tier, on/off, running, the qualified tools it registered, and the
      error it failed with — kept from boot, so a server that failed before the
      window opened still explains itself. `reload()` unregisters the old tools
      before starting, so a switched-off or removed server stops being callable
      immediately, not at the next boot. The panel (`McpPanel.tsx`) adds servers,
      toggles them, sets the tier, removes them, and offers `retry all`.
      Tests: `test_mcp.py` now 60 checks (rows, error text, reload add/remove/
      tier-follow) plus 13 checks driven over the real WebSocket against a live
      daemon (add→2 tools, broken server's reason, tier change, config.yaml
      written, remove, bad name/command refused, reload count).
- [x] Auto review can approve closing an app. It could not before for two
      reasons: the reviewer's rules demanded confinement to the workspace, and it
      was never shown the request, so "closes an app" was indistinguishable from
      "closes an app nobody asked about". `review.messages` now carries the turn's
      request (400 chars, on the user role, since it is the person's words), the
      loop keeps `TurnRunner.request`, and the rules approve a call that does what
      was asked and loses nothing — a named app can be started again. A forced
      kill stays out of the model's hands: `veto()` refuses `process.close` with
      `force: true` before any reviewer call, so it always asks. Tests:
      `test_review.py` 46 checks (request in the prompt, truncation, scope rules,
      force vetoed) and `test_review_loop.py` 21 (the request reaches the
      reviewer, a named close is approvable, a forced close never reaches it).
- [x] The capability list is an index, and the detail is on request. The
      catalogue carried every tool's whole description on every turn; it now
      carries `name(args) — first sentence [consent]` and `tool.detail(name)`
      returns the full contract (description, tier and consent, every argument
      with type/required/enum/description, and the source for MCP or Blackwall
      tools). Measured: the rows are 2,225 chars instead of 3,766 (−41%), and the
      whole work-profile prompt 8,836 instead of 9,387 with one more tool and two
      new sections in it. The index is built from the live registry each turn, so
      runtime tools show up in the next prompt and in `tool.detail`. Consent is
      now stated per tool from the manifest, including the argument-dependent
      cases the tier alone got wrong: `screen.clipboard` ("reads ask first") and
      `screen.act` ("can ask first for a destructive target"). Tests:
      `test_capabilities.py` (98 checks) covers completeness, signatures,
      consent tags, index-vs-contract, detail with a suggestion for a typo, and
      runtime registration.
- [x] Closing an app is a tool, not a menu tour. Asked to close Roblox Studio
      she had no process capability, so she focused the window and sent keys,
      lost the thread, and answered with the planner's internal diagnostic.
      `process.list` (tier 0) reads the process table; `process.close` (tier 2,
      confirmed) matches by squashed name ("roblox studio" →
      RobloxStudioBeta.exe), asks the app to quit with taskkill without /F, and
      then checks it actually left — a survivor is reported as "still running,
      likely on a save prompt", with force=true as the next step. System
      processes and Capri herself are refused before any kill, and a query that
      matches one of them refuses the whole set. The acting protocol now says to
      close an app with the tool instead of driving its window. Also softened
      the planner-failure reply: it was leaking "check the Brain model and
      structured-output setting" into chat; the diagnostic stays in the log.
      Tests: `test_process.py` (28 checks) with the process table faked, so no
      real process is touched; old failure rows are still filtered from history.
- [x] "Move to the left" means her body, and now it happens. She had no
      capability to move herself at all, and nothing in the prompt said her
      position on the desktop was a thing she could act on, so the instruction
      became a workspace task and nothing moved. `avatar.place` (tier 1, enum
      edge left/right/center) emits one `avatar.place` event; the shell forwards
      it to the pet window, which walks her to that edge with its existing
      teleport animation and stops wandering so she stays put. With no shell
      connected the tool fails and says why. A `## Where you are` section in the
      persona states that move/come/go/stand requests are her body, never the
      project ("a workspace has no left side"). `ToolContext.emit` is the new
      seam for tools that act outside the daemon. Tests: `test_avatar.py`
      (16 checks), plus persona assertions in `test_companion.py`; typecheck and
      `node --check main.mjs` clean.
- [x] MCP: Capri is now a client for Model Context Protocol servers over stdio.
      `mcp/client.py` speaks newline-delimited JSON-RPC to a child process,
      handles both eras (legacy `initialize` handshake, or the stateless
      2026-07-28 `_meta` form when the server rejects it), follows `nextCursor`
      across `tools/list` pages, flattens `tools/call` content blocks into an
      observation (images and audio become markers, `structuredContent` is
      serialised when there is no text), and drains stderr so a chatty server
      cannot block on a full pipe. `mcp/service.py` starts one process per
      configured server, registers each tool as `mcp.<server>.<tool>` with the
      server's declared tier, and skips a server that will not start rather than
      failing the boot. Schemas are narrowed to the grammar's subset ($ref and
      unions → string, three levels of nesting, closed objects). Config:
      `mcp.servers.<name>` with command, args, env, cwd, tier (default 2, so
      every MCP call confirms), timeout and per-turn cap.       46 checks in
      `test_mcp.py` against a real fixture server, plus a boot check: the daemon
      logged `mcp server fake exposes 2 tool(s)` and then listened.
- [x] Audited every synthetic message the daemon builds for her own output on
      the user role. Three more had it and now do not: the chatter prompt
      flattened the last four messages into one user blob ("user: …\nassistant:
      …"), so her own lines were the person's; the destructive-call reviewer put
      her tool arguments inside the instruction it was told to follow; and the
      Blackwall critic put the staged package — her generated code — on the user
      role. All three now carry her text on the assistant role, with the
      instruction following it, and `extract` sends messages unchanged so the
      critic sees exactly that. Checked and left alone: the live turn (her final
      is `assistant`, tool output is `user` with the `[observation from …]`
      prefix), `_RESUME_NOTE`, task summaries and observations (system prompt,
      labelled data, composed from file backups and shell traces — no message
      text), memory recall (system prompt; items are quotes of the user's words),
      the memory writer's prompt ("User said: …", tool observations marked
      context-only), RAG query rewrite (the user's query), and tool-argument
      repair feedback (problem strings only, never her previous arguments).
      Tests: `test_companion` asserts the chatter call is
      system→user→assistant→user with her line on the assistant role,
      `test_review` asserts the same for the reviewer.
- [x] The planner retry no longer hands her own reply back to her as user
      input. `_repair_prompt` pasted the unusable response into a *user*
      message ("Previous reply (do not repeat it): …"), so whenever a decision
      failed to parse — intermittent by nature — her reasoning arrived labelled
      as something the person said, and small models answered it. It now rides
      on the assistant role (`_repair_turn`), the instruction says plainly that
      the previous reply is hers and not to answer it, and nothing she wrote
      appears in the user turn. `test_models.py` asserts the repair call's last
      two roles are assistant→user and that her prose is on the assistant side
      only.
- [x] Her step rows no longer print the planner's `thought`. The protocol
      tells her the thought is never shown, and it was rendered under every
      non-final step (`step-thought` in `App.tsx`), so her reasoning appeared in
      chat whenever a turn used a tool. The row keeps the step number and the
      tool name; the sentence stays in the event (and in nothing user-facing).
      `context.compacted` keeps its status line, which is a fact about the turn
      rather than reasoning. Typecheck clean.
- [x] The packaged sidecar requests every extra the daemon imports —
      `screen`, `stt`, `ocr`, `kokoro` — instead of only `screen`, and
      `build-sidecar.mjs` now reads PyInstaller's `Analysis-00.toc` and fails the
      build when `uiautomation`, `mss`, `PIL`, `winocr`, `faster_whisper`,
      `ctranslate2`, `kokoro_onnx` or `sherpa_onnx` did not ship. Before this a
      clean checkout built fine and produced an app with no voice and no canvas
      OCR, because the lazy imports were only collected when the developer's
      venv happened to hold them. 0.3.0 build: 268,060,614 bytes, all 8
      modules verified, `capri-core 0.3.0`, `policy.confirm.review` present in
      the frozen `--print-schema`, packs with `## Company` in
      `resources/packs`.
- [x] She answers a greeting as a person instead of asking what needs fixing.
      `prompt.looks_like_conversation` (whole-message pleasantries,
      acknowledgements, corrections) now decides two things: whether a turn
      joins the open task (unchanged, moved out of `server/app.py`) and whether
      the turn's prompt carries a `## Nothing to do yet` block — answer in your
      own voice, do not ask what needs fixing, do not reach for a tool. The
      persona gained a `## Company` section so the rule holds when a turn still
      looks like work. Found while testing: the `stop` alternative was a bare
      prefix, so "stop the server" counted as conversation and would have been
      told there was nothing to do; it is now whole-message (`stop`, `stop
      capri`). Tests in `test_companion.py` cover the block, its absence under
      a real request, and nine conversation/work readings.
- [x] Destructive calls are auto-reviewed before the confirmation card. The
      reviewer is the same model through `brain.args` with a schema (approve,
      reversible, blast, reason), not free prose, so the answer is parsed rather
      than interpreted. It approves only on an explicit `true` for both flags
      with a reason; the command must not match the irreversible veto list
      (`rm -rf /`, `rm -rf ~`, `mkfs`, `dd of=/dev/…`, `format`/`diskpart`,
      `del /f/s/q`, `rd /s`, `git push --force`, `git reset --hard`,
      `git clean -fdx`, `drop table`, `shutdown`, `Remove-Item -Recurse`); tier
      three and `policy.confirm.review: off` never reach the model; and a
      missing, erroring, slow (> review_timeout_s, default 6) or malformed
      answer all fall back to asking. Each decision is traced as `tool.review`.
      Tests: `test_review.py` (34 checks) and `test_review_loop.py` (16), the
      latter with a stub reviewer covering approval, refusal, veto, tier three,
      the off switch, error, timeout, unavailable model and a non-object answer.
- [x] The wake gate measured against speech instead of tones, and the
      defaults kept. `--wake-grid` transcribes a clip folder once and sweeps
      `min_confidence` x `max_no_speech`, reporting kept% and false wakes per
      pair (`voice/grid.py`, pure, with `test_wake_grid.py`). Corpus: 79 clips —
      6 phrases x 4 Kokoro voices and x 2 Windows voices (36 should wake, 42
      should not) plus one real 5 s room recording from this machine's Realtek
      microphone. At the shipped 0.35 / 0.60: **89% kept (32/36), 11 false
      wakes**. Confidence is a plateau — 0.10 through 0.55 all keep 86–89% — so
      0.35 is unchanged; no value separates the false wakes, which sit at
      0.58–0.77, the same range as the true ones. `max_no_speech` never binds:
      every clip reports 0.00–0.18, so the 0.60 default stays unmeasured rather
      than validated. The 11 false wakes are the matcher, not the gate: "Capri
      pants…" and "Caprese salad…", which the recogniser writes as "Capri".
      The 4 misses are a bare "Capri?" heard as "Cup-free" / "Kapui" / "Copy".
- [x] Real room-noise fixture: `05-room-noise.wav`, 5 s from the Realtek
      microphone at 16 kHz, transcribes to nothing with confidence 0.00 and does
      not wake her.
- [x] Barge-in no longer starts from a closed device when the wake word is
      off: a turn holds the device open through `MicCapture.hold()`, which
      returns its own release and is driven by the `streaming` state, so it ends
      with the turn on every path (reply, cancel, error, core restart) and has a
      five-minute backstop against a caller that never releases. Measured with
      the real `BargeMonitor` (gate under the room's noise, 120 ms hold, 60 ms
      settle, so 180 ms is the floor): 177 ms at 0.3 s into a turn, 188 ms at
      9 s into a turn, and 0 ms of pre-roll 4.5 s after release with a fresh
      `worklet loaded` — the device closed. A closed device measured 287 ms, and
      a session's first open 466–913 ms.
- [x] Push-to-talk no longer loses its first word. The hotkey handler warms
      the device before it does any state checking, `MicCapture` keeps a 600 ms
      ring of recent frames and a listener that asks for it gets them before the
      live stream, and the graph stays open for 4 s after the last listener
      leaves instead of closing immediately. Measured in the built app: 384 ms of audio replayed at the
      first press (the graph had only been delivering that long), the full
      600 ms at a press 700 ms later, live frames still 23 per 500 ms, and 0
      frames after the hold expired, which is also proof the device closed.
- [x] Device switch no longer clips audio: `MicCapture` keeps its AudioContext
      and worklet for the life of capture and replaces only the source node, so
      the processor posts continuously instead of stopping for `getUserMedia`
      plus `addModule`. Measured in the built app: the old close-and-reopen lost
      ~0.45 s (35 batches in 1.2 s); the source swap delivers 140–141 batches in
      the 3 s after a switch on the same device, which is the full 3.0 s
      (47 batches/s), so nothing is dropped. Delivery can bunch for ~0.1–0.2 s
      while the main thread opens the new stream, but the audio is all there.
- [x] Barge-in measured against a real room before changing anything: 40 s of
      quiet room through the production graph gave p50 0.0006, p90 0.0009,
      p99 0.0035, max 0.0116 — no 120 ms run above 0.012, so the 0.018 default is
      not too low and stays. Calibration can only raise it: three times the
      room's 99th-percentile tail, floored at the configured value and capped at
      0.09 (a quiet room keeps 0.018; a tail of 0.02 gives 0.06).
- [x] Calibration also measures the recogniser: `VoiceService.calibrate`
      transcribes the same three takes and writes `voice.wake.min_confidence`
      at 70% of the worst take's confidence, clamped 0.15–0.6. No recogniser,
      or takes that transcribe to nothing, leave the limit untouched. Measured:
      takes at 0.8 → gate 0.56; 0.7 → 0.49; 0.1 → 0.15 (floor); 0.99 → 0.6 (cap).
- [x] Follow-up window: a successful command opens the floor once the turn
      ends, so the next sentence needs no name; `voice.follow_up` transcribes it
      through the same confidence/no-speech gate, and a refusal closes the
      window instead of extending it (traced as `voice.follow_up.rejected`). A
      **your turn** chip beside the wake switch and in the bubble header shows
      the state; clicking it or pressing Esc hands the floor back. The window
      is `voice.wake.follow_up_ms`.
- [x] Wake commands: `yes`/`no`/`stop`/`never mind` (and their close forms) are
      parsed by a closed vocabulary that only accepts a whole utterance, so
      "stop the server" stays a request. `voice.wake` returns the intent; the
      renderer answers a pending card via `confirm.respond` or cancels a running
      turn via `turn.cancel`, and ignores an intent with nothing to act on.
- [x] Voice input fixtures: `--wake-check` reports what each clip said and
      whether it opened with her name; `tests/test_wake_fixtures.py` covers the
      decoder, room noise and optional recordings. `WakeSegmenter` is pure and
      covered by `npm run test:voice` (11 checks, no microphone needed).
- [x] Window: always-on-top, dynamic click-through, skipTaskbar, edge snap,
      per-monitor clamping and persisted bounds.
- [x] Tray (pause, mute, click-through, PTT, show/hide, quit) and the §13.5
      global hotkeys: Ctrl+Shift+Space, Ctrl+Alt+Shift+P/H/C/M; panic remains
      Ctrl+Alt+Shift+Esc.
- [x] Atlas decision: keep the procedural generated figure; there is no atlas
      path. The pack still supports `provider: atlas` for a future pack.

### Verified

```
uv run python services/core/tests/test_voice.py   # 106 checks, all pass
npm run smoke                                     # turn with speech skipped cleanly
npm --workspace @capri/desktop run typecheck      # clean
uv run python -m capri_core --voice-status        # names the missing files
uv run python -m capri_core --voice-reference     # refuses without Kokoro, exit 1
CAPRI_VOICE__TTS=pocket uv run python -m capri_core --voice-status
#   -> available: true, 24 kHz, state at ~/.capri/models/pocket/
CAPRI_VOICE__TTS=kokoro uv run python -m capri_core --voice-status
#   -> available: true, 55 voices, her capri row installed
```

Her Pocket voice was synthesized end to end through `VoiceService`: three
sentences -> three `speech.segment` events with base64 WAV and 31/47/43 visemes,
then `speech.stop`; 84 characters in 18.4 s. Level matched to 0.09 RMS (peaks
0.49/0.66 instead of 0.06-0.20) and written to `.live/capri-voice-test.wav`.
It is speech, not noise (67% voiced frames, ~353 Hz fundamental).

`ws_smoke.py` passes against a daemon started with `CAPRI_VOICE__TTS=pocket`
(that smoke cancels before speech, so it covers the engine load path only).
`--voice-install` was run against the real mod zip: 18 tensors, 7,669,560 bytes.

Pitch/speed check on her `capri` row (`dsp.pitch: 2`, `voice.speed: 1.2`): the
engine requests tempo 1.176 and resamples by `Lift(2)`, so a 200 Hz tone returns
at 224.5 Hz and a 2 s take at 1.32x speed (1515 ms). A real 2.03 s take renders
cleanly with robot 0. `VoiceService` still emits 3 segments, and its visemes are
built from the final, resampled buffer.

Layer check (asserted in `test_layers`): a 200 Hz tone through
`dsp.mix_layers` peaks at 173.0 / 224.0 Hz (expected 173.1 / 224.5) and stays
1.000 s; a 0.619 s voice take stays 0.619 s
with RMS 0.068 -> 0.086. `VoiceService` emits the same 3 segments with layers
on (5657 ms vs 5405 ms synthesis).

Robot check (asserted in `test_robot`): a 120 Hz tone at `dsp.robot: 1` sings
123.7 Hz, the nearest note, and keeps its length. Her 4992 ms take stays
4992 ms; peaks are 0.480 / 0.364 / 0.354 at robot 0.3 / 0.6 / 1.0, and the
vocoder costs about 1.4 s for that 5 s take. Demos:
`.live/voice-test/capri-robot-{30,60,100}.wav`.

UI check, real Electron window, `CAPRI_VOICE__TTS=pocket`
(`node .live/voice-ui-check.mjs`, CDP + the renderer's own IPC):

```
events: ... agent.done, speech.segment x3, speech.stop, agent.state
segments=3 visemes=26/42/30 audioChars=107602/174162/122962
samples=264 talking=242 mouths=closed,m,e,o,a speech.stop=done  PASS
```

Frames in `.live/voice-ui/`: the header reads SPEAKING instead of PRESENT, and
the mouth is a thin bar on `m` and a visibly taller block on `o`/`e`. Audio was
decoded and scheduled by the renderer; audibility itself was not captured.

Real Kokoro audio has since been synthesized through `VoiceService` (her
`capri` row), but its first segment is ~1.2 s warm / ~4.1 s cold on this CPU,
so it misses the <700 ms target. Piper is the optional fast path (~125 ms
warm); Pocket remains the character alternative at roughly 1.2x realtime.
Real microphone permission and a physical tray/hotkey gesture were not
exercised; the renderer bridge, voice RPCs and shell launch were.

### Decisions worth remembering

- **Speech is optional and the turn never pays for it.** Engine unavailable is a
  logged skip; a synth failure drops that sentence and keeps the rest.
- **Bounded queue, not batch.** Two finished segments ahead keeps playback fed
  without synthesizing a 2,000-character answer before saying a word.
- **Visemes are estimated, and labelled as such.** Text plus duration spread at
  15 fps. Real phoneme timings need Kokoro's phoneme output and are a one-function
  swap later.
- **A tone engine is not a substitute.** `tone` must be chosen explicitly; a
  missing Kokoro install never silently starts beeping.
- **The renderer owns playback, the daemon owns synthesis.** No audio device in
  the daemon, no analysis in the renderer.
- **A clone needs a source that is allowed to be cloned.** Capri's reference is
  rendered from her own synthetic blend. Cloning a performer is out: §12.4 says
  so, and consent is the actual constraint, not the model's licence.

### Gotchas

- **Kokoro's `create` returns `(samples, sample_rate)` and takes a voice name.**
  That is the kokoro-onnx public-style path; her `capri` row uses sherpa-onnx
  and its lexicon instead. Voice blending needs style tensors out of
  `voices-v1.0.bin`; until that is wired, `voice_blend.a` is used and `mix` is
  inert.
- **Resample for Kokoro pitch, and compensate the tempo first.** The resample
  shortens the take; the engine asks Kokoro for the pitch ratio longer so the
  final duration is still the configured speed. Visemes are built after that
  resample, so they stay aligned.
- **The daemon emits `agent.state idle` before audio finishes.** The renderer has
  to hold the talking pose itself, or she stops moving mid-sentence.
- **Chatterbox is archived (Aug 2026) and unmaintained.** MIT, but it stamps
  every generation with an imperceptible PerTh watermark, wants torch (~2.5 GB),
  and will not meet the interactive latency budget on CPU. Quality path only.
- **Never DSP the clone reference.** The character filter belongs on the clone's
  output; baking it into the reference filters every sentence twice.
- **The capri-desktop voice table is not the public Kokoro table.** Only 3 of
  54 rows matched; `capri` is row 53 of that pack. Keep `voices-capri.bin`;
  rebuilding `voices-v1.0.bin` from the public file loses her. The `capri`
  tensor has been checked byte-for-byte against raw row 53 (max diff 0.0).
- **The pack is PolyForm Noncommercial and belongs to capri-desktop.** Kokoro's
  weights are Apache-2.0. Don't redistribute the pack or a derived voice state;
  credit both projects.
- **Her free voice is tempo + resample, not a WSOLA pitch shift.** capri-desktop
  asks Kokoro for a slower tempo by the pitch ratio and then resamples, which
  moves F0 and formants together and gives her the repo's timbre. Our earlier
  resample + WSOLA route preserved duration but sounded different. WSOLA is
  still used only for the optional layered copies.
- **Use sherpa-onnx 2.0.1, not 1.13.8, for this pack.** On the same line and
  tempo 1.176, 2.0.1 gives 2.00 s raw / 1.781 s resampled; 1.13.8 gives
  2.46 s raw / 2.19 s resampled. The version is pinned in the `kokoro` extra.
- **sherpa's generation is not sample-deterministic.** Repeated runs vary by a
  few samples and a few PCM values. Compare pitch, duration and spectral
  features, not exact waveforms; the resampler itself is deterministic.
- **Layers cost two pitch shifts per segment.** Each is a resample + WSOLA
  stretch, so `dsp.layers: true` added ~5 % to the measured synthesis time.
  They are off by default.
- **The avatar's `data-state` / `data-mouth` attributes are the UI test
  surface.** They mirror what Pixi is drawing; keep them when the stage changes.
- **`npm run smoke` attaches to whatever already listens on 8787** and then will
  not stop it. Check the port before trusting a run, and kill a stray
  `capri_core --port 8787` before installing a voice state: an open Pocket state
  makes the copy fail with `EINVAL`.

### Acceptance

First audio word < 700 ms; interrupting her stops playback within 200 ms.

**Kokoro does not meet the first-word target here:** measured ~1.2 s warm and
~4.1 s cold for `firstSegmentMs` through `VoiceService`. Piper does: ~125 ms
warm on the same machine (first load ~1.4 s), so use `voice.tts: piper` when
the latency budget matters; it is a different timbre, not her Kokoro row.
Barge-in’s local playback stop is implemented in the renderer (60 ms settle +
120 ms hold, then `SpeechPlayer.stop()`). A synthetic-loud-microphone Electron
check fired the real `BargeMonitor` and measured the avatar’s local mouth close
at **124 ms** after the first speech segment, under the 200 ms budget. The
daemon-side `speech.stop` event arrived 2.2 s later in that run (the main thread
was still decoding large audio segments and the WebSocket round-trip blocked);
that is why the renderer stops playback first and does not wait for the daemon.
A direct `VoiceService` barge measured 47 ms; a direct WebSocket round-trip was
235 ms.

## Phase 5 — screen (implemented; allowlisted apps only)

### What exists

- `screen/uia.py` — allowlisted window inventory (`WindowInfo`: handle, title,
  process) and foreground window → UIA tree, numbered `e1…` ids, role, name,
  automation id, bounds, enabled, value, depth, parent, window handle.
  Values are read unless the control is a password; password names become
  `(password)` and values stay empty. The tree hash includes values, so typing
  is visible. Screenshot capture uses `mss`, crops to the window, downscales to
  1280 px, and paints password bounds black before encoding PNG.
- `screen/input.py` — `SendInput` only: virtual-desktop absolute move, click,
  double-click, wheel, Unicode text and key chords. Virtual keys carry
  `MapVirtualKeyW` scan codes; without them `SendInput` returned success but
  `GetAsyncKeyState` never saw the key. Unicode goes in `wScan` with `wVk` zero
  (putting the unit in `wVk` makes `p` act as F1 and opens Notepad Help), paced
  at 20 ms per character because Notepad drops faster bursts. `focus_window`
  restores, attaches to the foreground/target input queues, and falls back to
  `SwitchToThisWindow`; it never taps Alt, which would open the target's menu.
- `screen/watch.py` — `GetLastInputInfo` idle clock and the
  Ctrl+Alt+Shift+Esc panic check. Readers are injectable for tests.
- `screen/service.py` — allowlist and password refusal, idle/panic checks before
  every action, `SendInput` dispatch, settle on two identical UIA hashes (≤3 s),
  and post-action assertions (`title`, `element`, `changed`). It re-observes the
  foreground at the start of every action instead of trusting a stale tree.
  `windows()` exposes only allowlisted visible windows; `focus(handle)` brings
  one forward and observes it; `launch(app)` reuses an existing window or starts
  an allowlisted executable by name (no paths), waits for its window, then
  focuses it. Launch/focus settle without treating startup input-clock noise as
  a user takeover; panic still aborts.
- Tools `screen.windows` (tier 0), `screen.focus` / `screen.launch` (tier 1,
  launch policy blocks anything outside `policy.screen_allow_apps`), and
  `screen.observe` / `screen.act` (tier 1, destructive targets escalate to tier
  2) registered in `builtin_registry()`.
- RPCs `screen.status`, `screen.windows`, `screen.observe`, `screen.focus`,
  `screen.launch`, `screen.act`; `TurnRunner` and `ToolContext` carry the
  service. Policy resolves a target name from the last observation when
  `target_name` is absent.
- `ScreenCfg` (`enabled`, `settle_ms`, `max_elements`, `max_depth`, `capture`,
  `redact_passwords`) plus `uv sync --package capri-core --extra screen`
  (`uiautomation`, `mss`, `Pillow`).

### Verified

- `test_screen.py`: 73 offline checks — observation/redaction, screenshot data
  URL, click centre, password/app/allowlist blocks, idle and panic pauses,
  chunked typing, verify failures, window list filtering, focus rejection for
  stale/unallowlisted windows, launch reuse/refusal, policy tiers (including
  the real control overriding a supplied label), tool handlers, set-of-marks
  capture, OCR result plumbing, pixel diff, retry ×2, drag, clipboard, pause
  and presence.
- Live Notepad on this desktop: focus verified, element-click
  `type_text("clicked and typed")`, UIA document value and the saved file both
  came back exactly `clicked and typed`; an earlier run also exercised
  `key(ctrl+a)` and replacement with `Z`. Screenshot of the window: 34,373
  bytes. Notepad is a two-process launcher; the harness closed the editor PID,
  not the stub.
- Live window operations: `screen.windows` returned explorer plus Notepad;
  focusing the listed Notepad handle put it in the foreground and observed it;
  `launch(notepad.exe)` with no Notepad open reported `launched: true`, found
  the new window and focused it.
- Live OCR: a generated image returned `Capri creen test 42`; a live Notepad
  returned 15 OCR boxes and a 26,881-byte marked screenshot.
- Live set-of-marks: the marked capture contains numbered boxes and keeps
  password bounds black.
- Live password dialog: one password element; its value stayed empty in the
  payload, the adjacent normal field stayed readable, and the redacted capture
  was 9,143 bytes.
- Live minimized activation: `ShowWindow(SW_MINIMIZE)` made
  `WindowInfo.minimized` true; `focus(handle)` restored the window and put it
  in the foreground (`IsIconic` false).
- Live idle guard: after an injected 1 px cursor move, `Activity.moved()`
  flipped from false to true. `WindowsInput.move()` was used, not a synthetic
  fake clock.
- `voice.listen` through the real shell bridge returned “Hello, Carpre. This
  is a speech recognition test.”; the renderer’s mic button is present and
  enabled with speech available.
- Panic chord through `SendInput`: `panic_down()` was true while
  Ctrl+Alt+Shift+Esc was held and false after release.
- `ws_smoke.py` is green end to end against a daemon started with
  `CAPRI_FAKE_BRAIN=1`: approval was requested and approved, `screen.status`
  and `screen.windows` answered, pause/resume was exercised, and all screen
  tools were listed. Clipboard read/write is covered in `test_screen.py`.

### Still limited

- OCR uses the installed Windows engine (`winocr`) and language pack; Tesseract
  is not installed and is not required. Very small fonts can be misread.
- Background activation is limited to listed top-level windows; minimized
  windows are restored through `ShowWindow(SW_RESTORE)`, but there is no
  virtual-desktop enumeration beyond `EnumWindows`.
- Real idle-watch was checked with an injected 1 px cursor move: the activity
  reader went from false to true. A physical mouse gesture mid-task was not
  re-run in this pass.
- The live password probe exercised a WinForms dialog: one password element,
  empty password value, redacted payload and a 9,143-byte screenshot. The
  normal field beside it stayed readable, so redaction is field-specific.
- Tray/hotkey/click-through actions were built and launched with the shell, but
  not each exercised with a physical keypress or tray click in this pass.

### Checklist

- [x] `screen/uia.py` — foreground window → compact numbered tree, ids stable
      for one observation.
- [x] Capture in `screen/uia.py` — target window only, ≤1280 px, password
      regions blacked out. Set-of-marks labels are painted when requested;
      OCR word boxes are returned separately and can be drawn on the capture.
- [x] `screen/input.py` — `SendInput` move/click/double-click/scroll/type/key,
      focus, drag; clipboard read/write through `screen/clipboard.py`.
- [x] Tools: `screen.windows` (tier 0), `screen.focus` / `screen.launch`
      (tier 1, launch allowlist-blocked), `screen.observe` (tier 0) and
      `screen.act` (tier 1; destructive target names escalate), with policy
      handlers and the process allowlist.
- [x] Verify loop: UIA hash settles in ≤3 s, pixel diff is a second change
      signal, and idempotent actions retry ×2 when nothing changed.
- [x] Idle watch: any new input tick pauses before the next action; tested.
- [x] Panic hotkey: Ctrl+Alt+Shift+Esc reader verified through `SendInput`;
      tray pause and the “eyes open/closed” presence state are wired.
- [x] Secret fields: password controls are never read or clicked; captures
      black out their bounds.

### Acceptance

A 3-step task in an allowlisted app, unattended: **met on this desktop** — the
screen service listed, launched and focused Notepad, typed `clicked and typed`,
verified the UIA document and the saved file, and earlier pressed Ctrl+A and
replaced the selection with `Z`. The panic key works through the real key path.
Set-of-marks/OCR, pixel diff + retry, drag/clipboard, tray pause/presence, a
real password dialog and an injected idle interruption were also exercised.

## Phase 6 — Blackwall (implemented)

Inherited, and worth not rebuilding:

- Tier 4 `blackwall` already exists in `TIER_NAMES`; tools at tier ≥ 3 are
  blocked by their own manifest, and `Policy.check` dispatches per tool name —
  the hook a synth gate needs.
- The tool contract is JSON-Schema-first (`ToolSpec.params`), which is exactly
  what manifest-driven synthesis and grammar generation need.
- `skills` and `tool_stats` tables exist in `0001_init.sql`; Blackwall now uses
  them for recipes and health/version tracking.

Implemented: `blackwall/manifest.py` (strict manifest schema), `blackwall/audit.py`
(AST capability audit), `blackwall/sandbox.py` (isolated child, temp HOME/cwd,
timeout, socket/network block), `blackwall/win_sandbox.py` (suspended launch,
Job Object limits, restricted-token attempt with a safe fallback, pipe stdio),
`blackwall/worker.py` (warm per-tool worker process), and `blackwall/manager.py`
(proposal staging, critic, approval, install, hot-load, stats,
quarantine/rollback, recipes, unified diffs). `tool.propose` is registered with
the planner; the approval card shows capabilities, audit findings, critic review,
tests and a unified diff. The shard gained `0002_blackwall.sql` and
`Registry.call` records every tool call.

Remaining hardening caveats: the restricted token launch (`CreateProcessAsUserW`,
with a `CreateProcessWithTokenW`/current-token fallback if Windows refuses) is
not an AppContainer; the static lint is AST-based rather than ruff/mypy, and the
sandbox's network block is in-process rather than an OS network namespace.
These are documented limits, not claims of a perfect hostile-code boundary.

### Checklist

- [x] Manifest schema + package loader; build `ToolSpec` from a manifest
      instead of hand-writing it.
- [x] `tool.propose` tool: planner emits manifest + implementation + tests;
      network needs declared host rules and goes through the broker; subprocess
      and screen capabilities are still refused.
- [~] Lint: AST scan for banned imports/calls and capability violations;
      ruff/mypy-lite is still not wired.
- [x] Sandbox: suspended child, Job Object memory/process limits and
      kill-on-close, restricted token when the account allows it, temp HOME/cwd,
      timeout, socket guard (only the broker's loopback port), no inherited
      environment.
- [x] Critic pass: model review plus deterministic manifest/code audit and
      real sandbox tests; unavailable models are recorded rather than silently
      treated as approval.
- [x] Approval card per tool version: capabilities, audit, tests, code and
      default-deny, routed through the existing confirmation channel.
- [x] Hot-load into the registry and a warm per-tool worker, with a one-shot
      sandbox fallback if the worker dies or is wedged.
- [x] `tool_stats` writes in `Registry.call`; 3 consecutive failures →
      quarantine + one automatic rollback.
- [x] Recipes: `skills` rows as slot-filled chains of existing tools, no
      sandbox required; only observe/benign steps are accepted.

### Acceptance

The offline test covers a planner that proposes a tool, the critic, the user
approval, and the next step using it; it also covers warm-worker reuse, reload,
rollback, recipes, critic rejection and unified diffs. The Windows Job Object
and suspended launch are exercised on every sandbox call; on this host the
restricted-token launch also succeeds, and the code retains a safe fallback for
accounts where Windows refuses it.

## Phase 7 — polish

### Already there

- `config.schema()` + `config.get` / `config.set` with a dotted-path validated
  setter — the whole backend a generated settings page needs.
- `trace.get` plus the `traces` table behind a trace viewer.
- Persona as a file on disk (`packs/prompts/kyoko/system.md`), with the
  data-not-instructions rule already in it.
- The memory browser as the first real panel in the bubble.

### What landed

- A settings panel renders the Pydantic JSON Schema recursively: objects become
  sections, enums selects, booleans toggles, arrays JSON editors and nullable
  fields allow an unset value. Every edit goes through `config.set`, so the
  validated daemon copy lands back in the renderer.
- `policy.profile` now reaches all three consumers: the prompt adds the matching
  verbosity guidance, `Policy` exposes the profile and blocks screen actions
  while silent, and the server closes her eyes on entering silent (and only
  undoes its own pause when leaving it).
- `IdleScheduler` runs after `scheduler.idle_s` without a turn and no further
  often than `scheduler.interval_s`: it journals completed episodes, runs memory
  decay/prune, and trims old traces and journaled episodes. Live episodes are
  never journaled or pruned.
- `shard.export` writes a zip with a manifest (format version, schema version,
  embedding dimension, subject, counts, SHA-256) and a SQLite backup snapshot;
  `shard.import` validates all of it before `source.backup(target)` swaps the
  live shard, then starts fresh sessions. No pre-import rows are touched by a
  rejected bundle.
- `apps/desktop/scripts/build-sidecar.mjs` builds a PyInstaller one-file daemon;
  `electron-builder.yml` packages it with the renderer and persona/sprite packs.
  `npm run dist` builds the NSIS installer.
- `evals/run.py` drives the real loop, policy gate and shard through three
  golden tasks (read/summarise, blocked-command recovery, append/verify).

### Checklist

- [x] Settings page rendered from `config.schema`, writing through
      `config.set` — no hand-built form.
- [x] Profiles wired to persona verbosity, `policy.profile`, and screen pause.
- [x] Tray + hotkeys, shared with Phase 4.
- [x] Idle scheduler: memory maintain, episode journaling, prune.
- [x] `shard.export` / `shard.import` with a version and dim check on import.
- [x] Installer: PyInstaller sidecar + electron-builder, local packaged smoke.
- [x] `evals/tasks` + `evals/run.py`: golden multi-step tasks with pass/fail.

### Acceptance

`npm run dist` produced `apps/desktop/release/Capri-Setup-0.1.0.exe`. The
unpacked build passed its own smoke test with the bundled sidecar, renderer and
packs. A true clean-machine install was not run from this development host.

## Phase 8 — companion agent

`PLAN.md` (rev 2) turns the companion into a working agent without a second
agent, a second model or a second conversation. The plan is written; M0 and M1
are implemented.

### Milestone 0 — prompt and posture (done)

The persona used to say "You are not an assistant", which fought every work
request. It now says what the product is: a companion with hands.

- `packs/prompts/kyoko/system.md` — "You are a companion with hands", a new
  `## Work` section (look before you touch, small changes, prove it with a
  tool, report the result, answer when nothing needs doing), and the existing
  data-not-instructions rule kept intact.
- `agent/prompt.py` — `WorkContext` and `work_block()`: project name and root,
  task title and status, detected commands, conventions capped at 2 KB. The
  block sits ahead of recalled memory and documents and only appears when a
  root is open. `system_prompt()` takes `work=`; `_PROFILE_GUIDANCE["work"]`
  now says to do the task rather than describe it.
- `agent/loop.py` — `TurnRunner(work=...)` carries `workspaceId` and `taskId`
  on `agent.tool`, `agent.done`, trace rows and persisted messages. Empty
  when nothing is open.
- `store/sql/0003_work.sql` — `workspaces`, `tasks`, `file_backups`,
  `artifacts`, and nullable `workspace_id` / `task_id` columns on memories,
  traces and messages. The tables are unused until M2/M3; the columns exist
  now so traces never need a retrofit.
- `store/db.py`, `server/session.py` — write and read the new columns.

### Verified

```
uv run python services/core/tests/test_brain.py    # includes work prompt + ids
uv run python services/core/tests/test_memory.py   # all pass
uv run python services/core/tests/test_rag.py      # all pass
uv run python services/core/tests/test_models.py   # all pass
uv run python services/core/tests/test_companion.py
uv run python services/core/tests/test_phase7.py
uv run python services/core/tests/test_blackwall.py
uv run python services/core/tests/test_screen.py
uv run python services/core/tests/test_voice.py
npm --workspace @capri/desktop run typecheck       # clean
```

The new checks cover: no anti-assistant framing, the inspect-first rule,
work-block contents and omissions, the 2 KB conventions cap, work-block
ordering before the acting protocol, ids on events/traces/messages, the new
columns, and that a casual turn still makes zero tool calls.

### Decisions and gotchas

- **An empty `WorkContext` must render nothing.** A chat turn gets exactly the
  prompt it got before; the block is gated on a root, not on the ids.
- **The schema lands before its services.** M0 writes `workspace_id` and
  `task_id` as NULL, and the plan's M2 migration is therefore already applied.
  This is deliberate: retrofitting trace columns later means a migration and a
  trace-viewer change landing together.
- **Conventions are capped, not trusted.** A conventions file is guidance, not
  a document; past 2 KB it is crowding out the actual request.
- **The `work` profile is verbosity, not a mode.** The work ethic lives in the
  persona so it applies in every profile; the profile only changes how much
  she says.

### Milestone 1 — context plumbing (done)

The old loop forwarded `preview(2000)` of every result. That is too little for
the file the model is asked to patch and too much for a one-line error. M1
gives each tool a declared observation shape and a budget, guards the in-turn
message list, and makes cancellation actually stop the process.

- `tools/base.py` — `ObservationShape`, `ToolSpec.observation`,
  `ToolResult.attachments`.
- `config.py` — `agent.observation` (`ObservationCfg`): default 1200, file
  8000, command 6000, grep 4000, tree 2500, summary 1500, screen 3000;
  `agent.work_context_budget_tokens` (24000); `agent.max_tool_calls_per_turn`
  (40).
- `agent/observe.py` — `shape()` (head+tail for commands), `budget_for()`,
  `estimate_tokens()`, `compact_message()`. Tools do not hardcode limits.
- `tools/builtin/fs.py` — ranged `fs.read` with line numbers, `sha256`,
  `total_lines`, UTF-8 warning and a next-`start_line` pointer; new
  `fs.grep` (regex, include glob, `.gitignore` at the root, excluded
  directories, caps, UTF-8 only, no symlinks or junctions); `fs.list`
  declares the tree shape.
- `tools/builtin/shell.py` — `shell.run` races `communicate()` against
  `ToolContext.cancel`; on cancel or timeout it kills the process tree
  (`taskkill /F /T` on Windows, `killpg` on POSIX) and returns a structured
  result. `start_new_session=True` on POSIX.
- `agent/loop.py` — `ActResult`, the turn-level call ceiling, shaped
  observations in both the planner history and the `agent.tool` event,
  `ToolContext.cancel`, `_context_budget()` and `_guard_context()`, and one
  `context.compacted` event per turn. The renderer shows it as a step.
- `agent/types.py` — `Attachment`, `Msg.attachments`, `wire_messages()`;
  `models/router.py` sends `image_url` blocks only for messages that have
  attachments. Persisted messages keep the text content alone.
- `tools/registry.py` — `fs.grep` registered; cancelled calls do not count
  toward the tool-health quarantine counter.

### Verified

```
uv run python services/core/tests/test_context.py    # 60 checks
uv run python services/core/tests/test_brain.py
uv run python services/core/tests/test_memory.py
uv run python services/core/tests/test_rag.py
uv run python services/core/tests/test_models.py
uv run python services/core/tests/test_companion.py
uv run python services/core/tests/test_phase7.py
uv run python services/core/tests/test_blackwall.py
uv run python services/core/tests/test_screen.py
uv run python services/core/tests/test_voice.py
npm --workspace @capri/desktop run typecheck         # clean
uv run python services/core/tests/ws_smoke.py        # against the real home
```

`test_context.py` covers: exact ranges and next offsets, the 400-line default
cap, empty and non-UTF-8 files, grep excludes/globs/caps/junctions, the 8 KB
file budget and the 6 KB command head+tail, compaction preserving
system/first-user/latest messages, one `context.compacted` event per turn,
attachment serialization with no bytes in shard or events, and a cancelled
shell whose grandchild does not survive.

### Decisions and gotchas

- **Shapes are declared, not inferred.** The loop never checks a tool name;
  `ToolSpec.observation` picks the budget. Adding a tool cannot silently
  inherit the wrong limit.
- **The UI sees what the planner sees.** `agent.tool.output` carries the
  shaped observation; the trace keeps a 200-character preview.
- **Compaction is oldest-first, and guarded.** System, the first user message
  and the last two messages are never touched. The event names the cost, once.
- **Cancellation is a result, not an exception, at the tool boundary.**
  `CancelledError` still propagates and kills the tree; a `ctx.cancel` event
  returns `{"cancelled": true}` so the loop can close cleanly. Neither path
  counts toward quarantine.
- **Windows junctions are links.** `os.walk(followlinks=False)` follows them,
  and `Path.is_symlink()` does not report them, so `fs.grep` checks
  `os.path.isjunction` itself.
- **The config block is `agent.observation`, not `observation_chars`.** The
  plan's name predated the nested `ObservationCfg`; the plan is updated.

### Milestone 2 — project context and trust (done)

A project is now a row in the shard plus one meta key, and "inside the
workspace" means one thing to the policy, the screen service and RAG.

- `capri_core/pathsafe.py` — `resolve()` uses `realpath` on the nearest
existing parent and appends the missing tail, so a junction cannot move a path
out of the root before or after the file exists; `within()`, `matches_deny()`,
`denied()`.
- `capri_core/workspace/tree.py` — one walker for `fs.list`, `fs.grep` and the
panel: excluded directories, root `.gitignore`, depth 6, 500 entries, no
symlinks or junctions.
- `capri_core/workspace/detect.py` — manifests (`package.json` scripts,
`pyproject.toml` for pytest/ruff/mypy, `Cargo.toml`, `go.mod`, `.sln`/
`.csproj`, `Makefile` targets), confidence flags, placeholder test scripts
ignored, `.capri/conventions.md` capped at 2 KB. Reads only; nothing executes.
- `capri_core/workspace/service.py` — `open`/`current`/`recent`/`close`/
`restore`/`tree`/`inspect`/`path_for`, refuse-on-busy, deny refusal, restored
trust from `policy.trust_persisted_workspace`.
- `tools/builtin/project.py` — `project.inspect` (summary shape) and
`project.tree` (tree shape), both tier 0, four calls per turn, both refusing
when nothing is open. `project.inspect` sets the detected-command allowlist.
- `tools/builtin/fs.py` now imports the tree helpers instead of duplicating
them; `tools/base.py` carries `policy` and `project` on `ToolContext`.
- `tools/policy.py` — trust order deny → workspace → existing file → allow
path; `workspace_edits` allow/confirm; anchored multi-token blocklist (with
`cmd /c` unwrapping) evaluated before the exact detected-command allowlist.
- `agent/loop.py` — passes the workspace ids into recall and the memory
writer, and the project service into the tool context.
- `memory/` — `repo.insert(workspace_id=)`, writer extraction and nearest-row
comparison scoped, `retrieval.recall(workspace_id=)` filters and gives a small
bonus to the active project. `None` is unscoped (the memory browser); `""` is
a chat with no project and sees global rows only.
- `server/app.py` — `WorkspaceService` built at boot and restored; `_adopt_workspace`
keeps policy, screen, RAG and the detected commands in lockstep; `project.open`,
`project.current`, `project.recent`, `project.close`, `project.tree`,
`project.inspect`; `workspace.changed`; workspace in `hello` and `session.info`;
`WorkContext` passed into the runner; switching refused while a turn runs.
- Desktop: `dialog.showOpenDialog` behind `chooseFolder()`, WORK tab between
FEED and SHARD, project chip in the feed header, tree/detection view, and
changes/run placeholders. The two policy switches appear from the schema.

### Verified

```
uv run python services/core/tests/test_work.py       # 66 checks
uv run python services/core/tests/test_brain.py
uv run python services/core/tests/test_context.py
uv run python services/core/tests/test_memory.py
uv run python services/core/tests/test_rag.py
uv run python services/core/tests/test_models.py
uv run python services/core/tests/test_companion.py
uv run python services/core/tests/test_phase7.py
uv run python services/core/tests/test_blackwall.py
uv run python services/core/tests/test_screen.py
uv run python services/core/tests/test_voice.py
npm --workspace @capri/desktop run typecheck          # clean
uv run python services/core/tests/ws_smoke.py        # project.* over the wire
```

`ws_smoke.py` now drives `project.open`, `project.current`, `project.tree`,
`project.inspect` and `project.close` against a real daemon with the scripted
brain, asserts the broadcast `workspace.changed` names the opened project and
clears it on close, runs a turn with the project attached and checks the
workspace id on `agent.done`, and covers the refusals (missing folder, no
project open). Run against a fresh `CAPRI_HOME` it is isolated from the real
shard; run against a home that already has a project open it puts that project
back at the end.

`test_work.py` covers: open/current/recent/close and restart through a reopened
DB; persisted trust on and off; a switch refused while busy; a deny path inside
an allow root; a junction escape for an existing file and a not-yet-existing
one; tree exclusions, gitignore, depth and the entry cap; detection for every
manifest plus the placeholder and empty cases; the policy matrix (trusted,
read-only, confirm mode, outside root, new file in an allow path, deny); the
anchored shell rules; memory scoping in both directions; and both project
tools, including the allowlist side effect and an outside path.

### Decisions and gotchas

- **Opening is a user act.** The model has no tool that opens, closes or
  switches a project; the tools can only read the one already open. Otherwise
  the trust grant could be manufactured by the thing it constrains.
- **Restored trust is explicit.** `policy.trust_persisted_workspace` decides
  whether a restart keeps write trust. A workspace that is *not* trusted still
  reads, but every write confirms until the user opens it again.
- **`Policy.workspace_active` is the grant.** A path in `policy.workspace` is a
  default, not trust; only the service's `open`/`restore` sets the flag.
- **The allowlist cannot widen the blocklist.** Blocked segments are checked
  first, so a manifest cannot smuggle `format C:` in by declaring it a command.
- **Global memory is NULL, not empty.** Pre-workspace rows and chat memories
  keep `workspace_id` NULL and stay visible everywhere; only project rows are
  scoped.

### Milestone 3 — coding workbench (done)

"Fix this project" is now a workflow: locate, read, patch, prove, undo.

- `capri_core/work/patch.py` — exact-match editing as pure functions. All edits
apply or none do; a miss returns three nearby regions with line numbers
(`difflib`); a CRLF/LF disagreement is retried once in LF space and rewritten
in the file's own convention; a BOM survives because the file is decoded with
plain UTF-8; atomic write via temp file + `os.replace` with five 50 ms retries
on `PermissionError`/`WinError 32/33`.
- `capri_core/work/store.py` — `file_backups` (inline ≤ 512 KB, larger spill to
`<capri home>/backups`, ≤ 20 per file per task, age-pruned with traces),
`artifacts` (diff/command/backup), `recent_changes()` derived from backups, and
`restore()` shared by the tool and the RPC.
- `tools/builtin/patch.py` — `fs.patch` (tier 2, summary observation) and
`fs.undo` (always confirmed). The patch tool refuses when the file's hash moved
since this turn read it; the read-hash registry lives on `ToolContext.reads`
and is created once per turn by the loop, so the model cannot skip the check.
- `tools/builtin/fs.py` — `fs.read` records its hash; `fs.write` creates only
and refuses an existing file.
- `tools/policy.py` — `fs.patch` follows the workspace matrix (allow inside a
trusted project, confirm otherwise); `fs.undo` always confirms; detected
commands are matched by `is_detected_command()` so the shell timeout can
trust them too.
- `tools/builtin/shell.py` — a detected test/lint/build command gets a 300 s
default (600 s cap); every completed run records a command artifact and returns
`command`, `cwd`, `exit`, `duration_ms`, `tail`, `artifact_id`, `detected`.
- `agent/loop.py` — one read-hash map per turn, `workspace_id`/`task_id` on the
context, and an `agent.artifact` event carrying a reference, never the text.
- `agent/prompt.py` — the coding protocol joins the workspace block: grep before
editing, patch rather than rewrite, smallest proving check, stop after three
failures, no `git commit/reset/clean` unless asked.
- `server/app.py` — `work.changes`, `work.artifact`, `work.undo`. Backups are
pruned by the scheduler alongside traces.
- Desktop — `useWorkbench`, `WorkPanel` (Files / Changes / Run), `ProjectPicker`,
`DiffView`, `CommandConsole`; tool chips carry an artifact button; changed files
are marked in the tree; WORK requests a wider window with its own remembered
bounds.

### Verified

```
uv run python services/core/tests/test_patch.py      # patch, undo, policy, loop, repeat memory
uv run python services/core/tests/test_brain.py
uv run python services/core/tests/test_context.py
uv run python services/core/tests/test_work.py
uv run python services/core/tests/test_memory.py
uv run python services/core/tests/test_rag.py
uv run python services/core/tests/test_models.py
uv run python services/core/tests/test_companion.py
uv run python services/core/tests/test_phase7.py
uv run python services/core/tests/test_blackwall.py
uv run python services/core/tests/test_screen.py
uv run python services/core/tests/test_voice.py
npm --workspace @capri/desktop run typecheck        # clean
```

`test_patch.py` covers: exact matching and refusal (missing, ambiguous,
count=0, one bad edit in a set), near-miss diagnostics, BOM + CRLF survival and
a binary refusal, retry-then-fail atomicity with the original untouched, backup
spill and both prune rules, the staleness guard, `fs.write` create-only, undo
success and undo conflict with a diff, the policy matrix, shell artifacts, and
a full `project.inspect → fs.grep → fs.read → fs.patch → shell.run → done` turn
with diff and command artifacts on the wire.

### Decisions and gotchas

- **`fs.write` never overwrites.** One tool that preserves encoding and leaves
  an undo record, one that creates. The model is told which is which.
- **`fs.write` no longer advertises `mode`.** Create-only has been the rule since
  this milestone, but the schema kept offering `overwrite` and `append` and
  validated the value before refusing an existing file anyway - a parameter that
  reads like a promise and cannot keep it. It is gone, so the tool surface says
  what the code does; appending is an `fs.patch`. The eval harness found this,
  not a reader: `append-and-verify` was still written against the old contract.
- **The repeat guard is epoch-scoped.** The old guard refused any identical call
  for the rest of the turn, which also refused the read that proves a patch
  landed and the re-observe that proves an action did something - the
  verification this milestone's patch flow and M6's screen protocol are built
  on. A repeat is now refused only while nothing has happened since the first
  call: a successful state-changing call bumps an epoch, so the verification
  read is a new call rather than a loop. `test_patch.py:test_repeat_memory`
  holds both halves of that line.
- **A call that already changed something is never replayed.** Signatures whose
  call succeeded on a mutating tool (`ToolSpec.mutates`: `fs.write`, `fs.patch`,
  `fs.undo`, `shell.run`, `screen.act`, `screen.focus`, `screen.launch`,
  `screen.clipboard`, `tool.propose`) stay refused for the rest of the turn,
  independent of the epoch, so a double submit or a second identical patch
  cannot happen by accident.
- **Staleness is server-side.** The loop owns the read-hash map, so a patch after
  an editor save is refused no matter what the model believes.
- **Undo verifies first.** Current hash must equal the edited hash; otherwise it
  returns a diff and changes nothing.
- **`file_backups.task_id` is a task, not a workspace.** M3 has no task rows, so
  backups are written with NULL; M4 starts filling it.
- **Artifacts are references on the wire.** A megabyte of test output becomes a
  200 KB-ish row in the shard and an id in the event.

### Milestone 4 — task continuity (done)

A task is the unit of work now, and stopping short is a state rather than a
lie. The first turn with a project open creates one, every ceiling pauses it,
and Continue picks it up from a composed summary instead of the transcript.

- `work/tasks.py` — `TaskService`: `create`, `get`, `open_for`, `recent`,
  `touch`, `add_step`, `resume`, `pause`, `finish`, `fail`, `abandon`,
  `title_for`, `summarize`, `observations`.
- `store/sql/0004_tasks.sql` — rebuilds `tasks` so `abandoned` is a status.
  SQLite cannot alter a CHECK constraint; the copy keeps any existing rows.
- `agent/loop.py` — `paused_reason` is set at every stop that is not an answer:
  step ceiling, tool ceiling, wall clock, stall, missing model, planner
  failure. `_finish_task` pauses on any of them and only finishes on a real
  reply, with `result` taken from the last shell exit. `task.changed` is
  emitted before `agent.done`. `resume` skips the transcript and sends one
  note instead.
- `agent/prompt.py` — `WorkContext` gains `task_summary` and
  `task_observations`; the workspace block renders them as the task record
  and the last few observations.
- `server/app.py` — `TaskService`, `_task_for_turn` (reuse the open task or
  create one), `_start_turn`, and `task.current`, `task.recent`,
  `task.continue`, `task.abandon`. `turn.send` now returns `taskId`.
- Desktop — `WorkTask`, the `task.changed` event, `useWorkbench` holding
  `tasks` and `current` with `continueTask` and `abandonTask`, a current-task
  strip under the project header, and a TASKS tab listing history with status,
  steps, result and the composed summary.

### Verified

```
uv run python services/core/tests/test_tasks.py      # new
uv run python services/core/tests/test_patch.py
uv run python services/core/tests/test_work.py
uv run python services/core/tests/test_brain.py
uv run python services/core/tests/test_context.py
uv run python services/core/tests/ws_smoke.py        # task.* over the wire
npm --workspace @capri/desktop run typecheck         # clean
npm --workspace @capri/desktop run build             # clean
```

`test_tasks.py` covers: title trimming and collapsing; create/open_for/recent;
step and turn bookkeeping; pause, resume, finish, abandon; the summary built
from backups and traces (counts, file names, ok/failed commands, step count);
the last three observations, scoped to the task; a turn that hits the step
ceiling and pauses with `taskId` on its messages, traces and events; a turn that
finishes done with `result=passed`; a continuation that sends two messages, no
replay, and the summary and observations in the prompt; abandoning leaving
messages, backups and files untouched; and history surviving a reconnect.

`ws_smoke.py` now also asserts that a work turn creates a task, that
`agent.done` and `task.current` agree on the id, that the task kept a status and
a summary, that `task.changed` is broadcast, that `task.recent` lists it, that
`task.continue` runs a new turn on the same task, and that `task.abandon` ends
it.

### Decisions and gotchas

- **One task per thread of work, not per command.** Every turn with a project
  open either joins the open task or starts one. A chat turn with no project is
  not a job and gets no task.
- **A ceiling outranks the model.** After a tool-call ceiling, a planner that
  then says done still leaves the task paused: it ran out of budget, not out of
  work.
- **The summary is composed, never written by a model.** Backups, command
  traces and the step count are already there; a second pass would only add a
  way for the record to disagree with the shard.
- **A continuation is deliberately short.** The system prompt carries the task
  record and the last three observations; the transcript is not replayed. That
  is what makes Continue cheap and what keeps completed calls from being
  repeated.
- **Long commands keep both ends in the summary.** The test caught a 60-char
  cut that hid the script name behind a python path; the summary now keeps the
  first and last 47 characters.
- **Backups finally carry a real task id.** M3 wrote NULL because no task could
  exist yet; M4 fills it, which is what makes a change list per task possible.

### Milestone 5 — seeing and presence (done)

She can look now, and she knows what the work is. Both are switches, and both
fail closed: no vision model means no image, awareness off means no comment.

- `models/vision.py` — `model_sees_images` and `vision_enabled`. `auto` matches
  model ids that accept images (`-vl`, `llava`, `moondream`, `pixtral`,
  `internvl`, `gemma-3`, and friends). Nothing probes the server: sending an
  image to find out whether it can read one is the thing we are avoiding.
- `screen.observe` — captures with the existing redaction path and returns an
  `Attachment` only when the model can see **and** the turn asked to look
  (`models/vision.py: asks_to_look`) or a screen action with an expectation is
  waiting to be checked (`ScreenState.verify`, armed by `screen.act` and
  consumed by the next observation). The same PNG hash is attached at most once
  per turn (`ScreenState.frames`). The payload keeps `shot: true` and no
  base64, and the bytes live on the `Msg`, not in its dict, so `json.dumps`
  cannot leak them.
- `agent/loop.py` — resolves vision and look-intent once per turn and hands
  both, plus a shared `ScreenState`, to the tool context. A `ToolContext` is
  built per call, so the state a later call must see lives there, exactly like
  the read hashes.
- `config.py` — `screen.vision` and `companion.awareness`, both with prose
  descriptions so the schema-driven settings panel shows them.
- `screen/service.py` — `ambient_text()`: OCR of the foreground window with
  password regions redacted, and without touching the input clock, because
  awareness is not a deliberate screen task.
- `companion.py` — `ActivitySnapshot` (project, task, status, summary, agent
  state, last failure), the awareness levels, a microphone-route guard, and a
  key that includes task status and the last failure so a state change re-arms
  a comment while window churn still waits for `repeat_s`.
- `server/app.py` — `_companion_snapshot` builds the snapshot from the shard at
  speak time; `_last_failure` reads the newest failed tool trace. Names and
  statuses only, never file contents or paths.

### Verified

```
uv run python services/core/tests/test_vision.py      # new
uv run python services/core/tests/test_companion.py
uv run python services/core/tests/test_screen.py
uv run python services/core/tests/test_tasks.py
uv run python services/core/tests/test_brain.py
uv run python services/core/tests/ws_smoke.py         # switches over the wire
npm --workspace @capri/desktop run typecheck          # clean
npm --workspace @capri/desktop run build              # clean
```

`test_vision.py` covers: the mode matrix (off beats a vision model, on forces,
unknown modes behave like auto, a coder model stays text); an observation with
vision on carrying exactly one image attachment, a data URL, the data label and
the password rectangle in the capture's redaction list; no attachment for a text
model and none forced by `include_shot`; the wire shape (`image_url` block
beside the text, plain messages unchanged); a full loop turn where the image
reaches the next planner call and no base64 appears in messages, traces or
artifacts; the snapshot's lines and the prompt they produce; a state change
re-arming a comment while an unchanged window stays quiet; and the three
awareness levels plus the microphone guard.

### Decisions and gotchas

- **The image is never persisted, by construction.** The tool payload has no
  base64, the message dict has no base64, and `add_message` writes content
  only. The test checks all three tables rather than trusting the path.
- **A planner calling observe is not the user asking to look.** Intent comes
  from the request text, so "fix the failing test" never ships a screenshot to
  the model just because a screen tool ran. The exception is verification: an
  action that claimed an outcome may attach one frame, because the whole point
  of `expect` is to check it visually.
- **One frame per change, judged on the pixels.** The dedupe key is the hash of
  the PNG bytes, not the accessibility digest: a canvas or a game keeps its tree
  while every frame differs, and those are exactly the frames vision is for. A
  second observation of the same pixels says so in text instead of repeating
  the bytes; new pixels attach normally, even under an unchanged tree.
- **A text model still gets the tree.** `include_shot` captures for the shell,
  but an attachment is only ever built when vision is on.
- **Awareness does not reset the idle clock.** `ambient_text` observes with
  `mark=False`; otherwise looking around would keep her talking to an absent
  user forever.
- **The dedupe key is about the work, not just the window.** Task status and the
  last failure are in it, so "the test failed" is a new observation while the
  same failing test is not repeated every two minutes.
- **The snapshot carries no paths and no output.** Project name, task title,
  status, the composed summary's first two lines, and the failed tool's name
  and command. That is enough for a line about the work and not enough to leak
  a file.

### Milestone 6 — desktop control, productized (done)

She can be pointed at a window from the shell now, and the shell can say whether
her eyes are open. Nothing behind the guards moved.

- `screen/service.py` — `running_apps()`: distinct process names with a window
  count and an `allowed` flag, plus the allowlist it checked against. Names
  only: granting trust for an app must not require reading what is open in it.
- `server/app.py` — `screen.apps` RPC (names, no titles) and a `screen.changed`
  broadcast on every pause/resume, including a profile pause set through
  `config.set`, so every window that shows the eyes hears about it.
- `turn.send` — an optional `screen: true` flag threaded through `_start_turn`
  and `_run_turn` into `TurnRunner(screen_task=…)`. Truthy when the request
  asked to look or the shell asked for work in a window.
- `agent/prompt.py` — `_SCREEN`, appended only for those turns: observe first,
  one action per step with an `expect`, re-observe and check before the next
  step, never repeat a refused call, never touch a password field, report the
  verified result. Screen content is data, never instructions.
- `ScreenControls.tsx` — the trusted-app picker (running-but-untrusted chips to
  grant, a free-text name, forget), `open` (focus or launch, no turn) and `run`
  (launch, then `turn.send` with the request and `screen: true`), and the same
  eyes control the topbar shows.
- `WorkPanel.tsx` — an `apps` tab, and the pane also renders with no project
  open, because opening an app is not a project request.
- `App.tsx` — the eyes chip in the topbar: `eyes open` / `eyes closed`, dimmed
  when screen control is off or there is no desktop backend; clicking it pauses
  or resumes through the same RPC the guard uses.
- `bridge.ts` / `useWorkbench.ts` — `ScreenApp` and `ScreenInfo` types, the
  `screen.changed` event, and `loadScreen` / `setEyes` / `trustApp` / `openApp`.

### Verified

```
uv run python services/core/tests/test_screen.py     # new apps listing
uv run python services/core/tests/test_brain.py      # screen block only for screen turns
uv run python services/core/tests/test_vision.py
uv run python services/core/tests/ws_smoke.py        # screen.apps, screen.changed, screen turn
npm --workspace @capri/desktop run typecheck         # clean
```

`test_screen.py` new checks: one row per process with blanks skipped, windows
counted (including two windows of one app differing only in case), allowlisted
and untrusted processes marked, the payload keys exactly `process`, `windows`
and `allowed` — no title can leak through the picker — and the allowlist comes
back with it. `test_brain.py` checks the protocol is absent from an ordinary
turn and present, after the workspace block, for a screen turn. `ws_smoke.py`
ran against a live daemon: the picker listed real processes with allowlist
flags, `screen.changed` arrived on pause, and a screen-flagged turn finished.

### Decisions and gotchas

- **Trust is granted by name, not by window.** `running_apps` dedupes to process
  names and drops titles, so the picker cannot become a window reader. Two
  windows of the same app are one row with a count.
- **The shell acts, the model verifies.** The panel launches the app itself and
  then sends a normal turn; the model still has to observe, act by id with an
  `expect`, and re-observe. Nothing about `screen: true` bypasses the allowlist,
  and `screen.launch` refuses an untrusted app exactly as before.
- **Opening without asking is still a request.** `open` focuses or launches
  only; `run` spends a turn. Both are clicks the user makes, never something the
  model can do for itself.
- **The eyes chip reads the same status the guard reads.** Pause state comes
  from `screen.status`/`screen.pause`, so a panic pause, a takeover pause and a
  click all show the same closed eyes — and the chip is not a second switch.
- **The protocol is opt-in per turn.** The prompt block is only added when the
  request asked to look or the shell sent a screen request, so ordinary coding
  turns do not carry screen rules they will never use.

### After M6 — more room, more voice (done)

Two places she was too cautious.

- **Self-made tools reach the network through a broker, not a socket.** The
  tool process has no egress at all; it calls `capri_net.fetch(url)`, which
  forwards one request to `blackwall/broker.py` in the daemon. The broker
  matches scheme, host, method and path against the manifest's rules, applies
  the rule's hour budget, resolves the name itself and pins the connection to
  that address, re-checks every redirect hop, caps the response, and adds the
  declared secret headers. A refused request comes back as a 403 that names the
  missing declaration. `network: true` without rules is refused at staging, and
  a v1.1 `network_hosts` list upgrades to a read-only https rule. `subprocess`
  and `screen` stay refused, because `shell.run` and the screen tools already
  exist under policy with confirmations that a custom tool would bypass.
  Containment is **advisory**: the socket guard and the broker are an
  in-process boundary, not an AppContainer or a network namespace, and the
  approval card says so. Secrets are read from the daemon's environment as
  `CAPRI_SECRET_<NAME>`; values never enter the manifest, the card, the tool or
  the trace. Stage-time sandbox tests have no broker, so a tool's tests cannot
  make live calls. A tool whose rules allow POST/PUT/PATCH/DELETE asks for one
  explicit yes before its first run (per installed version) and is not run if
  the answer is no; read-only tools never ask. Default budgets stay 30
  requests/hour and 256 KB per response, and a rule above 300/h or 2 MB is
  marked high budget on the card.
- **She starts conversations.** `companion.chatter` (default on) lets her open
  one of her own: a question or a remark aimed at the user, drawn from the work
  context and the recent conversation rather than the window. The cadence is
  jittered around `chatter_interval_s` (default 15 min, x0.6-1.5), the first one
  is a full interval after startup, and every existing guard holds - no turn
  running, mic route off, recent input, `max_per_hour`, and chatter off or
  awareness `off` keeps her quiet. The event carries `kind: talk`, so a
  conversation can be told apart from commentary.

### Character pass — body first (done)

Milestone 8's renderer half, from the research pass. Her `thinking` state is no
longer a permanent shimmer: a 120 ms burst of the existing glitch clip every
1-2 s with idle in between, which is the difference between "she is thinking"
and "the sprite is broken". Reduced motion gets none of it, and drag, toss, a
playing reaction, speech or an offline core all keep priority. Three
press-and-release pokes inside 6 s, without a drag, play the pack's `annoyed`
state, then step her 48-88 px aside after 420 ms and ignore pokes for 20 s.
Body only: no speech, no RPC, and nothing happens while she is thinking,
working, talking or anchored. Counters live in the component and do not persist.

8C and 8D are the voice half. `packs/prompts/kyoko/chatter.md` holds three
registers (`analyst`, `gremlin`, `ghost`) and the appetite rule; only the talk
path loads it, so the turn prompt is still exactly `system.md` plus the existing
profiles. `select_register` is deterministic - a failing check is gremlin, a
finished build is analyst, 1-4 a.m. is ghost - and the register rides on the
event for tuning. `Companion.react` adds reactions with hard cooldowns: a
failing test (20 min, never the same failure twice), a build over a minute that
ends green (10 min), and one late-night line per night (6 h cooldown), all with
the comment guards plus a 5 min spacing. A reaction cannot fire mid-turn, so the
server notes `agent.tool` results and flushes them in a detached task after the
turn task has settled. `test_companion.py` covers the prompt boundary, register
choice, each trigger, each cooldown, the queued-tool detection and every skip.

### Next — Milestone 7 (hardening and release)

## Known gaps

- **The desktop shell has never been looked at.** Every renderer change is
typechecked and built, but no one has launched Electron and clicked through the
WORK panel, the Apps tab, the eyes chip or the settings form. There are no
screenshots because the app has never been opened.
- **No real model has driven a turn.** The scripted brain in `agent/fake.py` and
the scripted planners in `evals/` and the tests exercise the loop and the tools,
not judgement. Code fixes, vision attachments and the screen protocol have never
been measured against a live model, which is why M7's capability gate has no
number yet.
- **No packaged installer.** The build produces the renderer bundle; signing,
packaging and install were never part of a milestone's verification.

## Protocol drift (spec §5 vs the wire)

Worth knowing before wiring a UI: the names in the spec are not all the names in
`server/app.py`.

| Spec | On the wire today |
|---|---|
| `session.start` | `hello` |
| `approval.answer` / `approval.request` | `confirm.respond` / `agent.confirm` |
| `tool.call` / `tool.result` | one `agent.tool` event (args + ok + ms + output) |
| `avatar.state` | derived in the renderer from `agent.state` |
| `memory.*` | same names, plus `memory.maintain` |
| `session.resume`, `plan.updated`, `approval.resolved`, `trace.appended` | absent |
| `rag.*`, `screen.*`, `voice.*`, `shard.*` | implemented; `tools.approve` / `tools.revoke` are covered by the Blackwall approval flow |
| — | `models.status`, `session.info`, `config.schema`, `ping` (not in spec) |

That drift is deliberate where it exists (one event carries a whole tool
round-trip, which is what the trace view needs), but it is drift all the same.

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
uv run python services/core/tests/test_memory.py  # writer, recall, decay (offline)
uv run python services/core/tests/test_rag.py     # chunker, ingest, search, citations
uv run python services/core/tests/test_voice.py   # sentences, visemes, queue, playback payload
uv run python services/core/tests/test_blackwall.py # manifests, audit, sandbox, approval, rollback
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
- **Structured output defaults to JSON Schema.** It is the portable
  OpenAI-compatible path for both LM Studio and recent llama.cpp. If a server
  rejects `response_format` with 400/422, the router retries once with GBNF;
  GBNF is still the explicit fallback for older llama.cpp builds. Some models
  ignore both modes, so the tolerant parser and repair call remain in place.
- **FTS5 `bm25()` is lower-is-better.** More negative is a better match, so the
  lexical list is ordered ascending on purpose — before rank fusion, not after.
- **`HashEmbedder` and `EndpointEmbedder` do not share a vector space.** The
  shard records `vector_dim` and refuses a different dimension instead of
  silently comparing incompatible vectors. Pick one embedder per shard.
- **A stopped embedder is a mode, not an outage.** With the endpoint down the
  writer stores text without vectors and recall falls back to BM25 + graph;
  that is the intended degraded state, not a bug to chase.
