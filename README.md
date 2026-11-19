# Project Capricorn

Project Capricorn is the application; Capri is the companion you see. It is a
local-first agent harness with persistent memory, document RAG, real tools,
self-authored tools and desktop control — fronted by a Capri-styled projection.

An Arasaka personality construct with no body of her own, built from everything
she has read and everything you have told her. The daemon is the mind; the app
is only her projection.

She is company before she is a tool. A greeting gets an answer in her own voice,
not a question about what needs fixing: when a turn carries no request
(`agent/prompt.py:looks_like_conversation` — whole-message pleasantries,
acknowledgements and corrections), the prompt says there is nothing to do yet
and not to reach for a tool, and `packs/prompts/kyoko/system.md` says the same
in every turn. Work shaped under a request — "ok, now add a footer" — is
unaffected.

---

## Install

Download the latest `Project-Capricorn-Setup-<version>.exe` from
[Releases](https://github.com/adiduabo-a11y/project-capricorn/releases) and run
it. Windows x64. The installer is unsigned, so Windows SmartScreen will ask you
to confirm — "More info" → "Run anyway". Python, the sidecar and the required
runtime extras are bundled; there is nothing else to install.

## Updates

The packaged app updates itself. Shortly after launch — and every six hours
after that — it asks GitHub for the newest release; if there is one, it
downloads the installer named in `latest.yml`, checks its SHA-512 and runs it
silently **in place**: the app closes, the files are replaced, and it comes back
up on the new version. Nothing is downloaded when there is nothing newer, and a
dev run or an unpacked build never checks at all, because there is no installed
app to replace.

The header says where it is: `update 0.5.2 · 42%` while downloading,
`restart for 0.5.2` when it is ready, `up to date` when the feed has nothing for
you. Quitting installs it anyway — the chip only saves the wait.

Verified end to end on Windows 11 from 0.5.1 to 0.5.2: the app found the
release, downloaded the 256 MB installer in the background, and after an ordinary
window close the installed `app.asar` reported 0.5.2.

Releases before 0.5.2 predate the feed and cannot update themselves; if you are
on one of those, install the current release by hand once.

---

## Status

Phases 1–6 are implemented: the daemon routes to a local model, runs plan →
act → observe with grammar-constrained tool calls, gates every action through
permission tiers, remembers and cites documents, speaks with streaming audio,
observes allowlisted Windows apps, and can stage, lint, model-review,
sandbox-test, approve and hot-load a new declarative tool. The avatar
idle-animates from a data-driven procedural pack.

| Subsystem | State |
|---|---|
| Daemon (WS RPC, sessions, traces) | working |
| Shard (SQLite, migrations, vectors) | working |
| Config (layered, schema-validated) | working |
| Shell (Electron, tray, hotkeys, window chrome) | working |
| Avatar (procedural placeholder pack) | working; no atlas |
| Brain (one local model, router, loop, policy, tools) | working |
| Trace UI (steps, tool chips, approvals) | working |
| Memory / RAG | working |
| Voice (Kokoro/Piper/Pocket, STT, barge-in) | working; latency caveat below |
| Screen (UIA, marks, OCR, pixel verify, clipboard) | working on allowlisted apps |
| Blackwall (dynamic tools, recipes, health) | working; suspended Job Object containment, warm workers, critic, unified diff, brokered network with per-host rules, quotas and secret substitution |

Phase 4 latency: the voice is Pocket. Loading it (her state plus the base model)
takes ~14 s here, and starts in the background the moment the daemon is up: the
socket accepts ~2 s after launch and answers RPCs while the load runs, so a
restart spends that time before her first reply rather than inside it (~54 s on
the very first run, while the weights download). Measured end to end over the
RPC the shell uses, the first audio segment arrives 2.2 s after `voice.speak`
for a 46-character sentence, and ~1.1 s for a short line at the engine itself
(her DSP layers add ~0.07 s), so the <700 ms acceptance target is **not met on
the default path**. A line asked for in the first few seconds still waits for
the load to finish. Warm, it runs at a real-time factor of **~0.55** — 1.6 s of
CPU for 3.1 s of speech, 5.8 s for an 11 s sentence — so each segment is ready
in about half the time it takes to play. That is what long replies get from it: the queue (`voice.queue_depth`, 2
segments deep) refills faster than playback drains it, so a long answer streams
end to end without a gap in the middle; the price is about a second before her
first word, not a stall later on. Kokoro stays selectable and runs nearer 1.0 (a
second of CPU per second of speech); the optional Piper path measured ~125 ms
warm (first load ~1.4 s): `CAPRI_VOICE__TTS=piper` with a voice in
`~/.capri/models/piper/`.

## Requirements

- Windows 11, Node 20+, Python 3.12, [uv](https://docs.astral.sh/uv/)
- A local OpenAI-compatible endpoint: `llama-server`
  (`CAPRI_MODELS__ENDPOINT=http://127.0.0.1:8080/v1`) or LM Studio
  (`CAPRI_MODELS__ENDPOINT=http://127.0.0.1:1234/v1`). Set
  `CAPRI_MODELS__MODEL` to the identifier shown by the server's `/v1/models`
  endpoint. Pick the structured mode that server speaks:
  `CAPRI_MODELS__STRUCTURED=grammar` (llama.cpp) or `json_schema` (LM Studio).
  The same model handles planning and background work; there are no bundled
  model presets. In the app, open **Settings → Brain** to set the URL, model
  ID, and structured mode; the same fields can be set with the env vars above.
  Set `CAPRI_FAKE_BRAIN=1` for a deterministic scripted planner (the smoke
  tests do).

## Run

```powershell
uv sync                      # python env (3.12, managed by uv)
npm install                  # electron + renderer deps

npm run dev                  # vite + electron, spawns the core automatically
```

Code layout, the daemon/shell wire, how tests are written and packaging are in `DEVELOPMENT.md`.

The shell starts `python -m capri_core` itself. To run them separately:

```powershell
npm run core                 # daemon only, ws://127.0.0.1:8787
$env:CAPRI_NO_SIDECAR="1"; npm run dev
```

Optional runtime extras (each is independent; the default startup degrades
cleanly when one is absent):

```powershell
uv sync --package capri-core --extra kokoro --extra stt --extra screen --extra ocr
# add --extra piper for the low-latency alternative voice, --extra pocket for
# Pocket TTS (her voice state, no lexicon next to the engine)
```

### Cloning a voice (Chatterbox)

### Words the voice can say

The pack's lexicon lists base words and only some inflections, and sherpa-onnx
drops a word it cannot find without a sound: "expects" is simply not spoken, and
a clause that loses a word sounds like a sentence that stopped halfway. About one
word in twenty of an ordinary reply was going missing. On first use the engine
writes `<capri home>/models/kokoro/lexicon-capri-merged-v2.txt`: the pack's file
plus the regular forms it implies — plural, third person, past, gerund,
possessive, `y` to `ies` — each built from the base word's own phonemes, with the
ending picked by the base's last sound (`expect` \u2192 `expects`, `stop` \u2192
`stopped`, `carry` \u2192 `carries`). Only spellings English writes go in: no
`danceed`, no `carrys`, and a base that is itself an inflection (`building` from
`build`) gets its plural but not `buildinging`. That is about 831k entries and
33 MB, built once in ~5 s. sherpa's tokenizer charges about 0.7 s per 100k
entries to read it, so the engine loads in ~7.5 s instead of ~3 s (it was ~10 s
with the unpruned 1.16M-entry file); her voice is warmed at startup, so that
happens behind the panel. Nothing is invented: a word that is neither listed
nor regular (acronyms, jargon) is still skipped, and sherpa logs it. Dashes are
spoken as commas for the same reason — a dash is not in the lexicon, and a
clause split on a dropped one is what "cut off midway" sounds like.

### Which engine says the words

That word table is the Kokoro path's, and a merged lexicon cannot hold what it
has never seen: proper nouns and jargon ("Kubernetes", "OAuth") are still
skipped, silently. Pocket TTS — her own voice state,
`~/.capri/models/pocket/capri_english.safetensors` — has no word table at all; it
synthesizes from the text and its voice state, so nothing can be skipped, and the
33 MB merge is never read. Measured on this CPU: ~14 s to load the model and her
state (54 s on the very first run, while the weights download; the daemon now
does that in the background as it starts, Phase 4 above), then ~2.2 s to a
sentence's first audio segment and a real-time factor near 0.55. Kokoro is 8.1 s
to the first line and 2.9 s per 3 s; Piper is 1.7 s and 0.3 s a line, but is a
different voice. The
state already sounds at her register: measured on the same passage it sits
within 0.6 semitones of Kokoro's +2 output, so the engine does not add
`voice.dsp.pitch` on top of it (that put her ~2.4 semitones high). The layered
mix still applies downstream, after whichever engine spoke.

```powershell
uv sync --package capri-core --extra pocket   # pulls CPU torch
```

```yaml
# ~/.capri/config.yaml
voice:
  tts: pocket
```

Then restart the core. `CAPRI_VOICE__TTS=pocket` does the same for one run, and
`tts: kokoro` (or `piper`) switches back.

Chatterbox can clone a voice from a few seconds of reference audio, but it
cannot be installed into this project's environment: it pins `numpy<2` (the
daemon needs `numpy>=2`) and `transformers==4.46.3`, which the `pocket` extra
cannot share. So it gets a venv of its own, and the daemon starts that
interpreter once and talks to it over stdio (`voice/clone_worker.py`):

```powershell
uv venv "$HOME\.capri\models\chatterbox\venv" --python 3.12
uv pip install --python "$HOME\.capri\models\chatterbox\venv\Scripts\python.exe" chatterbox-tts
```

Then point the engine and the clip at it — Settings → Voice, or the config:

```json
"voice": { "tts": "chatterbox", "reference_clip": "C:/path/to/clip.wav" }
```

The reference wants **five seconds at minimum** — Turbo refuses anything shorter
and the worker falls back to the base model below that — and the model's
maintainer recommends **ten seconds**. 24 kHz mono WAV, one speaker, no music or
background noise, speaking the way you want her to. The weights download on the
first sentence (about 2 GB) and the model then stays loaded; on CPU a sentence
takes seconds rather than milliseconds. `voice.chatterbox_python` points the
engine at a different interpreter. Whatever `voice.dsp` says still applies on
top (pitch +2 and the layers by default), so what you hear is the cloned timbre
in her character rather than the raw recording.

Say **"Capri"** and then the request — "Capri, open my notes" — to talk to her
without a hotkey. The wake word runs on the same recogniser as push-to-talk, so
it needs the `stt` extra, and `voice.wake.enabled: false` turns it off. A bare
"Capri" leaves `voice.wake.follow_up_ms` for the sentence that follows. The
switch is also in the tray and on `Ctrl+Alt+Shift+W`; either one writes
`voice.wake.enabled`, so Settings always shows the same value. A dot beside the
bubble's header (and "wake on" beside the eyes in the full panel) shows whether
the microphone is armed for her name.

The energy gate and the shortest sound it accepts are calibrated per room:
Settings → **Wake calibration** records five seconds of room and three takes of
her name, then writes `voice.wake.threshold_rms` and
`voice.wake.min_utterance_ms` through the same `config.set` path as any other
setting. The same takes are transcribed, so it also writes
`voice.wake.min_confidence` at 70% of the worst take's confidence (0.15–0.6):
the energy gate and the decoder gate are measured in one pass, and a missing
speech model leaves the confidence limit untouched rather than failing.

A turn holds the device open for exactly as long as it is in flight, so barge-in
is listening from an open microphone when she begins to speak even with the wake
word off, a quick reply releases the microphone at once, and a slow model cannot
outlast the hold.

The push-to-talk handler opens the device before it does anything else, the
graph keeps the last 600 ms of audio for the segment that starts with the press,
and it stays open for four seconds after a turn instead of closing at once — so
the word said on the way down is not cut off.

Changing the input device does not close the microphone: the capture graph
keeps its `AudioContext` and worklet and swaps only the source node, so the
batches keep coming while the new device opens.

The same recording sets `voice.barge_threshold` from the room's transient tail
(99th percentile): three times the tail, but never below the value already in
config and never above 0.09. A quiet room therefore keeps the default, and a
noisy one raises the gate so a keypress cannot interrupt her. It measures through the microphone processing the gate itself uses, so
the numbers hold once the gate is armed, and it shows a live level meter with
the current threshold marked while it records.

Voice input runs on one capture graph. Push-to-talk, barge-in, her name and
calibration all subscribe to the same AudioWorklet tap, so the device is opened
once and one echo canceller covers all of them; the graph closes when the last
listener leaves. Settings → **Voice microphone** chooses the input device. Like
the mic route, that choice is stored on this PC (`localStorage`, not
`config.yaml`), because a device id means nothing on another machine; a saved
device that has been unplugged falls back to the system default.

That window also reopens after each command: once she has finished answering,
the next sentence needs no name, and each accepted sentence holds it again. A
**your turn** chip sits beside the wake switch (and in the bubble header) while
the window is open — click it, or press **Esc**, to give the floor back. The
same confidence and no-speech gate guards the window, so a sound it is unsure
about closes it instead of extending it, and the television cannot keep it
alive.

A destructive action is reviewed before it is put to you. The reviewer is the
same model, shown what you asked for and one question with a schema rather than
prose: does this call lose anything that cannot be brought back. Because it sees
the request, a call that does exactly what you asked can be approved — closing
the app you named is reversible in its terms, since the app can be started again;
a forced kill is on the irreversible list below and always asks. It skips the
card only when it says yes on both counts, and never for a command on the
irreversible list
(`rm -rf /`, `git push --force`, `format`, `shutdown` and the like), never for a
tier-three call, and never when the model is missing, slow or unclear — those
all ask as before. `policy.confirm.review: off` turns it off;
`policy.confirm.review_timeout_s` (default 6) is how long the answer may take.
Every decision is traced as `tool.review`.

`process.close` is the case that shaped this: a polite close can be approved
because the app raises its own save prompt, while `force: true` is vetoed
statically — the reviewer never sees it, and it always asks you.

While a confirmation card is open, **"Capri, yes"** and **"Capri, no"** answer
it; while a turn is running, **"Capri, stop"** and **"Capri, never mind"**
cancel it. These are whole-sentence commands only — anything with more words in
it is an ordinary request, so "stop the server" still goes to her.

STT uses `faster-whisper` int8 and downloads its model on first use. OCR uses
the installed Windows language pack; no Tesseract install is needed. Kokoro
weights live in `~/.capri/models/kokoro/`, Piper voices in
`~/.capri/models/piper/`, and the Whisper cache in `~/.capri/models/whisper/`.

Desktop hotkeys:

| Keys | Action |
|---|---|
| `Ctrl+Shift+Space` | push-to-talk toggle (hold the on-screen mic for hold-to-talk) |
| `Ctrl+Alt+Shift+P` | pause / resume screen control |
| `Ctrl+Alt+Shift+H` | show / hide the chat bubble |
| `Ctrl+Alt+Shift+M` | mute / unmute speech |
| `Ctrl+Alt+Shift+W` | listen for "Capri" on / off |
| `Ctrl+Alt+Shift+Esc` | panic: stop screen input |

The tray carries the same pause, mute and push-to-talk actions.

Capri's character now lives in her own always-on-top window, with a compact chat
bubble anchored above her. She walks across the desktop when idle, turns to face
her direction, and sometimes glitch-teleports to a new spot. The jump is wrapped
in her red glitch transformation and a large expanding energy burst on departure
and arrival. She uses her real walk, blink, talk, dance, sleep and glitch
animations, and shimmers while thinking or running tools. Drag her to pick her up
and toss her, click her for a reaction, double-click her to bring the chat back,
or right-click for wander, stay anchored, teleport, dance, sleep and quit
controls. Stay anchored keeps her where you drop her, clamped above the taskbar,
so she does not fall back to the floor. Outside her sprite the window is
click-through, so desktop icons and other apps stay usable.

Apps are closed as processes, not through their menus. `process.list` names what
is running and `process.close` asks an app to quit (tier 2, so you confirm
first); `force: true` terminates it when it sits on a save prompt, and the tool
reports "still running" rather than pretending. System processes and Capri
herself are refused.

She can move herself when asked. "Move to the left", "go over there" and "stand
in the middle" are about her body, not the workspace: `avatar.place` emits one
event, the shell walks her to that edge of the screen, and she stops wandering
until you turn it back on (right-click → wander). The tool is tier 1, so it runs
without a confirmation card, and it says so plainly when no shell is connected
instead of reporting a move that never happened.

The bubble follows her and is the normal chat surface. Its header expands to the
full console for settings, memory, the document archive and the complete tool
trace; collapse it with the arrow in the console's title bar.

She is also a little chatty by default. When the foreground app or window changes,
she makes one short observation about what you are doing, in the bubble and out
loud, and she checks in on a long unchanged session after about fifteen minutes.
A comment only lands if the keyboard or mouse has been used within the last two
minutes (`companion.active_within_s`), so an open window on an empty desk does
not get narrated. The watcher reads only the app and window title, never the
window contents; it skips credential-shaped windows, stays quiet while a turn is
running or screen control is paused, and is capped at eight lines an hour. Tune
or disable it under `companion` in Settings → Mainframe, or with
`CAPRI_COMPANION__ENABLED=false`. Set `companion.speak` to false for text only.

Settings → Mainframe → **Her voice in your mic** mixes your physical microphone
and Capri's speech into VB-CABLE, so Discord and other calls hear both of you.
Pick your mic and the cable's **CABLE Input**, turn the route on, then choose
**CABLE Output** as the microphone inside Discord. Capri still plays through
your speakers; the mixer only sends the combined signal to the cable. The device
choices are stored locally per PC, and the route stays on when the chat is
hidden. While the route is active, ambient commentary stays text-only so it is
not mixed into the call; only direct replies are spoken. Use headphones if your
mic picks up too much of Capri's speaker output.

## What she knows she can do

The prompt carries a capability index, not a manual: one line per tool with its
name, arguments, first sentence and how the call is approved (`asks the person
first`, `can ask first for a destructive target`, `blocked by policy`). The rest
of each contract — every argument's type, required or not, the enum values, what
each one means — is fetched with `tool.detail(name)` only when she needs it.
That keeps the catalogue at about 40% of its former size for the built-ins and
matters much more once MCP servers add tools with long descriptions.

The index is generated from the live registry on every turn, so a tool added
later — an MCP server, a Blackwall proposal — is in the next prompt and in
`tool.detail` without a restart.

## MCP servers

Servers are added in **Settings → MCP servers**, not by editing `config.yaml`.
A row takes a name, a command and its arguments, an on/off switch and a trust
tier, and says what happened: `2 tools` when it is up, `failed` with the
server's own complaint when it is not (`could not start 'npx': the system cannot
find the file specified`). Every change is written to the config and the servers
are restarted in place, so a server added in the window is callable in the next
turn without restarting the daemon — and one switched off stops being callable
immediately, rather than at the next boot.

The tier is the same trust decision the tool list states: `0` runs without
asking, `1` runs and mentions it, `2` asks first, `3` never runs. It lands on
every tool the server advertises, including ones it grows later. A failed server
keeps its row and its error, so a typo in a command is visible instead of silent.


Capri is an MCP client. A server that speaks the Model Context Protocol over
stdio contributes tools: she launches it with the daemon, lists what it offers,
and registers each one as `mcp.<server>.<tool>` in the same registry as the
built-ins, so the policy engine, the confirmation card, tracing and the planner
treat them alike.

```yaml
mcp:
  servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/work"]
      tier: 2              # 2 asks before each call (the default), 3 blocks, 0/1 allow
      timeout_s: 30        # per call
      max_calls_per_turn: 4
```

- `tier` is the trust you place in the whole server, since the daemon cannot
  know what a third-party tool does. Everything confirms by default.
- A server that fails to start is logged and skipped; the daemon still boots.
- Schemas are narrowed to what the grammar can compile: `$ref` and unions
  become strings, enums on strings survive, objects nest three levels, and
  `additionalProperties` is closed. Text, images (as markers), resources and
  structured content all come back into the observation.
- Both protocol eras: the legacy `initialize` handshake (2025-11-25 and earlier)
  and the stateless 2026-07-28 form that carries the version and capabilities in
  `_meta` on every request.
- Tools are read at startup, so restart the daemon after editing the list.
- Not yet: HTTP transport, resources/prompts/sampling, and per-tool tier
  overrides.

## Obsidian notes

**Settings → Notes** points Capri at an Obsidian vault and indexes it, so your own
notes are part of what she knows. Three tools go with it: `note.search` finds the
passage, `note.read` returns a note by its vault-relative path, and `note.write`
creates, appends to or rewrites one — after the confirmation card, with the same
reviewer that can wave a safe destructive call through, so the ordinary "add this
to my inbox" does not need a click.

Only markdown is indexed (`note.search` reads what the index has), under a
`vault` collection: `.md` and `.markdown` outside Obsidian's own `.obsidian` and
`.trash` folders, pruned on every pass, so a note deleted in Obsidian stops being
recalled. The folder is a setting and never an argument — every path the tools take
is resolved inside it, so `../` and absolute paths are refused, and `.obsidian` is
not writable. Re-index from the panel after editing notes outside the app; it shows
how many notes are in the folder and how many are indexed.

Choosing the folder starts the first pass in the background: `notes.set` answers
at once with `indexing: {running, done, total}` instead of holding the call until
every note is read, and the panel polls `notes.status` while it runs
("indexing 128 of 400 notes..."). One pass runs at a time, a second request waits
for the first rather than racing it, and `notes.index` is still the on-demand
refresh that returns the added/changed/removed report. Unsetting the vault stops
a running pass before it forgets the collection.

The pass is also pushed, not only polled. `notes.index` carries
`{running, done, total, error}` — once when a pass starts, about once a second
while it runs, and once when it stops — so any open panel can say what the core
is doing. The app under the avatar shows a line under the composer: "reading your
notes (128 of 400)..." while it runs, then "your notes are indexed (400 notes)"
for a few seconds, or the error if the pass stopped on one. A panel that opens
mid-pass asks `notes.status` once to catch up, then follows the events.

With no vault chosen, the three note tools are left out of the prompt's tool
index (271 chars, measured) and the `## Your notes` section with them: a
capability she cannot use is budget spent for nothing. They stay registered, so a
stray call still answers with "choose the folder in Settings -> Notes" rather
than an unknown tool, and they appear in the index the moment a folder is set.

The vault is her wiki, not just one more index. With `notes.recall` on — the
default, and the "Read it every turn" switch in the panel — each turn starts with up
to four of your notes that match the request, in their own "From your notes"
section, labelled `[note:path]` by vault-relative path. That collection is left out
of the general document block on the same turn, so a passage cannot arrive twice
under two headings, and no query-rewrite call is spent on it: the vault is read on
every turn, and paraphrasing the question for it does not pay for itself. The
prompt tells her to answer from those notes and name them as `[[note name]]`, and to
write anything durable back with `note.write` instead of only saying she will
remember it. Switch it off and she only reads the vault when she searches it.

## Verify

```powershell
uv run python services/core/tests/test_mcp.py     # MCP client over a real subprocess
uv run python services/core/tests/test_brain.py   # offline: loop, policy, grammar, stall guard
uv run python services/core/tests/test_context.py # ranged reads, grep, budgets, compaction, cancel
uv run python services/core/tests/test_work.py    # workspace state, trust, paths, detection, policy
uv run python services/core/tests/test_patch.py   # patch matching, encodings, backups, undo, artifacts
uv run python services/core/tests/test_tasks.py   # task lifecycle, ceilings, summaries, continuation
uv run python services/core/tests/test_vision.py  # vision switch, attachments, no bytes persisted, awareness
$env:CAPRI_FAKE_BRAIN="1"; uv run python -m capri_core --port 8787  # daemon for the next line
uv run python services/core/tests/ws_smoke.py     # daemon over the wire (fake brain)
#   covers project.open/current/tree/close, the workspace.changed event and a
#   turn with the project attached; opens a temporary folder and puts back
#   whatever was already open. Point both commands at a throwaway CAPRI_HOME
#   to keep the smoke out of your shard (a home with no voice weights skips
#   the speak/barge-in pair and says so).
#   The scripted turn edits a fixture in the daemon's run directory, which is
#   outside any workspace, so the approval card it raises does not depend on
#   your allow_paths; the check also confirms the edit landed afterwards, and
#   reads policy.confirm.destructive rather than assuming a card (a daemon set
#   never to ask is expected to raise none).
uv run python services/core/tests/test_voice.py   # speech, STT, barge-in, Piper/engine checks
uv run python services/core/tests/test_screen.py  # UIA, marks/OCR, diff, redaction, tools
uv run python services/core/tests/test_companion.py # ambient commentary cadence, privacy, voice
uv run python services/core/tests/test_blackwall.py # manifests, audit, sandbox, approval, rollback
uv run python services/core/tests/test_phase7.py   # profiles, scheduler, shard bundles
uv run python evals/run.py                         # golden multi-step tasks
uv run python services/core/tests/live_brain.py   # real model + tools + approval (daemon must be running)
npm run smoke                                     # build + launch + rendered trace + approval
npm --workspace @capri/desktop run typecheck
npm --workspace @capri/desktop run build
npm --workspace @capri/desktop run sidecar        # PyInstaller daemon only
npm --workspace @capri/desktop run dist           # NSIS installer in release/
```

## Layout

```
apps/desktop          Electron shell: main process owns the socket, renderer is the view
services/core         Python daemon: agent, memory, rag, tools, screen, voice
packs/sprites         sprite packs (pack.json + sprite strips or generated provider)
packs/prompts         persona packs
```

## Where things live

Everything durable is under `~/.capri` (override with `CAPRI_HOME`):

```
~/.capri/config.yaml          user config (layered over schema defaults)
~/.capri/run/token            loopback auth token
~/.capri/shards/default/      the shard: SQLite db (memory, docs, traces)
~/.capri/logs/core.log        rotating daemon log
~/.capri/blackwall/           proposed, installed and quarantined dynamic tools
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
4. Avatar + voice: Kokoro blend, streaming speech, lip-sync, barge-in — implemented
5. Screen: UIA-first observation, marks/OCR, actions, verification — implemented
6. Blackwall: sandboxed tool synthesis with approval — implemented; suspended
   Job Object, restricted token, warm workers, critic and unified diff
7. Polish: generated settings UI, tray, scheduler, shard bundles, installer — implemented
8. Companion agent: work posture and context plumbing — M0/M1 implemented
