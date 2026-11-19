# Project Capricorn — Companion Fix Plan

**Status:** Steps 1–8 implemented (§1, §2, §3, §4, §5, §6, §7, §8, §9 — see §10
and the matrix in §11). The perch art is wired, not merely fetched: the clips,
`StateDef.frame` and the `shift` are in `pack.json`, the states are chosen in
`Pet.tsx`, and `npm run smoke` asserts the seated pose and captures both perch
frames. §9.3b records that the 7,600-char ceiling was not reachable and that the
tests pin 8,600 / 9,220 instead.
**Date:** 18 November 2026
**Scope:** Nine reported defects in the movement/pose layer, the agent loop, model
config, and the voice path.

| # | Report | Root cause (verified) | Size |
|---|---|---|---|
| 1 | Movement and poses should reflect what she's doing | Poses are chosen from agent state only; there is no sitting/perched set, so idle, thinking and sleeping all read as "standing" above the floor | M |
| 2 | Get the perch PNGs from Lachy's repo | Not present. Source art fetched, not wired | S–M |
| 3 | Leaves jobs unfinished; needs prompting to continue after saying she'll start | `_drive` treats *any* `tool: "done"` reply as the end of the turn, so "I'll write it now" ends the turn with zero tool calls | S |
| 4 | Drop harness temperature, leave sampling to the server | `models.temperature` is a first-class config field, sent on every request and shown in Settings | S |
| 5 | TTS doesn't finish full sentences | Two causes: (a) out-of-turn speech (the "Hm?" cue, the wake ack) preempts a running reply mid-sentence; (b) unpunctuated fragments are handed to the engine, which clips their tail | S–M |
| 6 | "mm" is pronounced "Emma" | `voice/wake.py` writes `Mm? Go on.` / `Mm. You're awake too.` and `clean_for_speech` passes them through | XS |
| 7 | Long wait after the wake word; should detect end of a sentence | Fixed 700 ms trailing-silence window, then a whole-file Whisper pass, then an acknowledgements TTS pass before the floor opens | M |
| 8 | Per-request injection is quite big | Measured: **8,809 chars (~2,200 tokens)** of system prompt per turn for a work request, plus up to 800 tokens of memory and the RAG block | M |
| 9 | Only "Capri" is detected, not the rest of the sentence | The segmenter cuts on 700 ms of silence, so a short pause after her name splits the utterance; the request half is then judged as a wake candidate without her name and dropped | S–M |

---

## 0. Baseline measurements

Taken from the current tree, not estimated:

```
built-in tools            19
tool index (prompt)       2,243 chars
system prompt, work       8,809 chars   ≈ 2,200 tokens
system prompt, chat       9,217 chars   (adds the "nothing to do yet" block)
persona system.md         4,354 chars
_PROTOCOL                 2,057 chars
_CODING (work block)        655 chars
_SCREEN (screen turns)      685 chars
```

Voice defaults today: `voice.wake.silence_ms = 700`, `follow_up_ms = 6000`,
`voice.sentence_max_chars = 240`, `voice.queue_depth = 2`, wake gate
`min_confidence = 0.35`.

---

## 1. Perch sprites and pose wiring

### 1.1 What the source project has

From `lachydotmcg/capri-desktop` at the pinned commit already recorded in
`ASSET_LICENSES.md` (`e02579f1…`), `assets/sprites/`:

| File | Frames | FPS | Cell | Source constant |
|---|---|---|---|---|
| `perch.png` | 60 | 30 | 196×384 | `Clip perch` |
| `perch_talk.png` | 60 | 30 | 196×384 | `Clip perchTalk` (mouth already drawn) |
| `perch_blink.png` | 8 | 30 | 196×384 | `Clip perchBlink`, shut-eye frame is **4** (`PerchSleepCell`) |
| `perch_blink_glitch.png` | 8 | 30 | 196×384 | unused here |
| `perch_glitch.png` | 10 | 15 | 288×384 | `Clip perchGlitch` |
| `perch_music.png` | 60 | 30 | 196×384 | `Clip perchDance` |
| `perch_arrive.png` | 14 | 15 | 288×384 | `Clip perchArrive` |
| `perch_leave.png` | 12 | 15 | 288×384 | `Clip perchLeave` |

`app/Capri.cs:2153–2176` also declares `PerchShiftX = -26/384` and
`PerchShiftY = +36/384`. Their canvas is 384-normalised; our renderer scales by
`pack.canvas` = `[116, 384]`, and cell widths differ per strip (196 or 288), so
the shift has to be expressed per clip, in canvas pixels, not assumed.

**Already done:** all seven PNGs are downloaded into
`packs/sprites/capri-neon/sprites/` (untracked; `git status` shows them). They are
not referenced by `pack.json`, `sprite.ts`, or the licence file yet.

### 1.2 Work

1. **`packs/sprites/capri-neon/pack.json`**
   - Add clips with the counts and FPS above. `cutStrip` derives the cell from
     `sheet.width / count`, which is correct for every strip (196 px for
     perch/talk/blink/music, 288 px for glitch/arrive/leave), so no cell hint is
     needed. What *does* need adding is a per-clip `shift: [x, y]` in canvas
     pixels (default `[0, 0]`). **Settled value: `[-26, 228]` on every perch
     clip** — derivation in §1.3.
   - Add states: `perched`, `perched_listening`, `perched_thinking`,
     `perched_working`, `perched_talking`, `perched_sleeping`,
     `perched_dancing`, `perched_arriving`, `perched_leaving`. `perched_talking`
     uses `perch_talk` as its body clip (the artist already drew the mouth), so
     it needs no `mouthClip`; keep the idle mouth overlay for the floor state.
   - `perched_sleeping` needs a *held* frame, so add `frame?: number` to
     `StateDef` and freeze the clock at `perch_blink` frame 4.

2. **`apps/desktop/src/renderer/avatar/sprite.ts`**
   - Import the new strips into `SHEETS`.
   - Honour `def.frame`: when set, show that index and skip frame advance in
     `update()` (a `frozen` flag), so the sleeping perch does not blink.
   - Apply `clip.shift` as an offset on `body`, `blink` and `mouth`. The bob
     currently writes `body.y` directly; change it to `body.y = bob + shiftY` and
     keep `blink.y = mouth.y = body.y`.

3. **`apps/desktop/src/renderer/avatar/pack.ts`**
   - `StateDef.frame?: number`, `ClipDef.shift?: [number, number]`.

4. **`ASSET_LICENSES.md`**
   - Extend the "Files used" list with the seven perch strips and note that the
     frame counts and shift come from `app/Capri.cs`. No licence change: the same
     PolyForm Noncommercial terms and the same required notices apply.

### 1.3 The shift — decided: `[-26, 228]`

Two independent lines of evidence agree.

**Source arithmetic.** `Capri.cs:1644` exposes `ShiftX`/`ShiftY` per clip, and the
draw path places the art rectangle centred on that offset (`:15461`, `:15513`,
`:25009`: `left = -wide/2 + ShiftX*size`, `top = -tall/2 + ShiftY*size`). For a
384-tall perch clip with `(-26, +36)`:

```
art left   = anchor - 196/2 - 26 = anchor - 124 px
art right  = anchor - 196/2 + 72…  → anchor +  72 px
art top    = anchor - 384/2 + 36 = anchor - 156 px
art bottom = art top + 384       = anchor + 228 px
```

Our renderer anchors the cell's **bottom-centre** at `pos`: left `pos.x-98`,
right `pos.x+72`, bottom `pos.y`, top `pos.y-384`. The right edge already
coincides, so the art must move **26 px left**, and the source anchor (her hip /
seat line) sits 228 px above the art bottom, so the art must move **228 px down**
relative to our bottom anchor. `shift = [-26, 228]` in canvas pixels, scaled by
the stage like everything else. The cell width does not enter it: the source
centres by offset, not by cell.

**Pixel evidence** (alpha bounding boxes, measured on the fetched art):

```
idle.png        cell 116   bbox x  5..114   centre +1.5   bottom gap 17
walk.png        cell 240   bbox x 52..207   centre +9.5   bottom gap 19
perch.png       cell 196   bbox x  8..192   centre +2.0   bottom gap  2..6
perch_music.png cell 196   bbox x 11..193   centre +4.0   bottom gap  2..6
perch_glitch    cell 288   bbox x 19..248   centre -10.5  bottom gap  2..3
```

Every strip is cut tight to her outline (bottom gap ≤ 19 px everywhere), which is
why the art, not the cell, defines alignment: a 196 px perch cell is not 80 px of
padding, it is her seated silhouette. `-26` is therefore the same correction the
source applies, and it has the sign the source writes — **negative x is left**.
The vertical sign is the one a naive copy gets wrong: `+36` in the source becomes
**`+228` in ours**, because the two are measured from different points of the art
(centre vs bottom).

**Consequences to implement with the shift**

- The perch anchor is her **hip/seat**, not her feet. Perched art therefore hangs
  `228 * scale ≈ 176 CSS px` below the actor's bottom edge. Perch states are
  therefore only correct where that room exists: gate them on
  `pos.y <= baseY - 190` (see §2.2). This is separate from the movement code's
  existing `p.y < p.baseY - 1` perch test, which stays as it is.
- `perch_arrive` frame 0 is a thin ribbon sliver near the middle of the cell, not
  a figure; with the shift it appears higher than the seated frames. That is the
  clip's own composition (the figure assembles by frame 7) and is expected, not a
  shift error.
- `walk.png` shows the same effect unshifted today (+9.5 px); it is left alone,
  because it is not in scope and nothing has complained about it.

One visual confirmation is still worthwhile after wiring (does she look seated at
the height the shift implies), but the numbers are no longer a guess.

---

## 2. Poses that reflect what she is doing

### 2.1 Today

`Pet.tsx:72` `moodFor()` maps `thinking → thinking`, `acting → working`,
`speaking → talking`, `error → error`, everything else `idle`. `Pet.tsx:948–963`
then layers drag/reaction/walk on top. `pos.y < baseY` already means "perched"
(`Pet.tsx:23`, `:441`, `:466`, `:798`) but perching changes only *where* she is,
never *how she sits*: she stands on a ledge exactly as she stands on the floor.

The **listening** state exists in `pack.json` but nothing drives it, and the pet
window never learns that the microphone is hot.

### 2.2 Work

1. **Track the perch.** Add `const [perched, setPerched] = useState(false)` plus a
   `perchedRef`; set it from the movement loop only when it changes. The **pose**
   gate is `p.y <= p.baseY - 190` — the seat sits at `pos.y` and 176 CSS px of
   dangling legs hang below the actor box, so a perch lower than that would clip
   her legs at the window edge (§1.3). The movement code keeps its own
   `p.y < p.baseY - 1` test for "is she in the air"; the two answer different
   questions and must not be merged.
2. **Map state through the perch.** A pure helper next to `moodFor`:

   ```ts
   function perchedState(state: string): string {
     switch (state) {
       case 'thinking':   return 'perched_thinking'
       case 'working':    return 'perched_working'
       case 'talking':    return 'perched_talking'
       case 'sleeping':   return 'perched_sleeping'
       case 'dancing':    return 'perched_dancing'
       case 'arriving':   return 'perched_arriving'
       case 'leaving':    return 'perched_leaving'
       case 'annoyed':    return 'perched_thinking'   // her glitch, seated
       default:           return 'perched'
     }
   }
   ```

   Apply it only when `perched` and she is not `dragging`/falling and not in a
   one-shot `clicked`/`waking` reaction (a pat on a ledge still gets the grin).
3. **Listening — decided: one pet-only IPC channel, no daemon change.**
   Verified: the pet is its own `BrowserWindow` (`main.tsx` renders `<Pet />` for
   `?surface=pet`), so it shares no state with `App.tsx`, and the daemon emits no
   `voice.state` event at all (grep finds none). `main.mjs:678-690` forwards only
   `core.status`, `agent.state`, `error` and `avatar.place` to the pet window.
   Microphone state is renderer-owned (`voiceState`, `wakeArmed`, `floorOpen` in
   `App.tsx`), so it belongs in the shell's existing pet channels, not in the
   socket protocol, and **not** in `AgentState` — the daemon's turn state is not
   the microphone's state, and overloading it would make "listening" mean two
   things at once.

   Mirror `capri.petCue` exactly (preload `:135` → `ipcMain.on` `:1565` →
   `petWin.webContents.send`):

   | File | Add |
   |---|---|
   | `preload.mjs` | `petListening(active) { ipcRenderer.send('capri:pet-listening', !!active) }` and `onPetListening(handler)` |
   | `main.mjs` | `ipcMain.on('capri:pet-listening', (_e, active) => …send('capri:pet-listening', !!active))`, and include it in `sendPetSnapshot()` so a pet that boots late still sees the current value |
   | `bridge.ts` | `petListening(active: boolean): void` on the `capri` object + `onPetListening` |
   | `App.tsx` | one effect: `capri.petListening(voiceState === 'listening' \|\| wakeArmed \|\| floorOpen)` |
   | `Pet.tsx` | subscribe, store a boolean, map `listening`/`perched_listening` above the ambient state |

   **Decided: the listening state is visible as a rim light, not as a pose.** The
   pack's `listening` clip is the idle strip and `perched_listening` is the perch
   strip, so both states only changed `data-state` — a hot microphone was
   invisible. The three options: a glow, a held frame, or reusing the perch idle.
   A held frame has no listening art to hold (freezing the idle strip reads as a
   stutter, not attention), and reusing the perch idle is what the pack already
   did, i.e. nothing. So `pet.css` gives the two listening states a cyan rim
   (`drop-shadow`, the recording-indicator convention) with a slow opacity
   breath, `prefers-reduced-motion` respected, and the pet's canvas no longer
   clips so the light can spill past the sprite box. Cost is one composited
   filter on a live canvas — the same treatment `data-state='error'` already
   uses — and no new art.

   Four files, one boolean, no protocol version bump, and it dies with the pet
   window like every other pet channel.
4. **Movement should follow state.** Today a walk is only refused while
   thinking/speaking. An `acting` (working) pet should also stand still instead of
   strolling off mid-tool-run, and a sleeping pet already does. Add
   `agentRef.current !== 'idle'` to the walk guard in `startMove` and the walk
   branch (the branch already checks it; `startMove` re-checks `agentRef` — make
   the two agree).
5. **Do not walk while perched** — already true (`Pet.tsx:466–476` hops or
   teleports). Keep it, and use `perched_arriving` after a landing so the sit-down
   reads as one motion.

### 2.3 Acceptance

**Landed** — `perchedState`, the `p.y <= p.baseY - PERCH_GATE` (190) gate, the
`capri:pet-listening` channel (preload → main → pet, plus the snapshot), and the
listening state above the ambient one. The movement guard already agreed between
`startMove` and the walk branch, so §2.2.4 needed no code change.

**Covered by `npm run smoke`.** `probePetListening()` sends the flag from the
panel through preload and the shell, reads the pet window's `data-state` and the
canvas's computed `filter`, reloads the pet to prove the snapshot restores the
light, and turns it off again. A missing link fails the run (`finishSmoke(1)`),
not just the log. The smoke's 25 s boot watchdog used to stay armed for the whole
run and was killing slow runs mid-check; it is now re-armed on renderer-ready.

**Her legs are grabbable.** The overhang was outside both the window's hit test
and the DOM surface, so a click at her knees went to the desktop. Two changes:
`hitTest` now extends `PERCH_ROOM` below the actor box while `perchedRef` is set
(the window stops being click-through there), and `.pet-actor::after` is an empty
180 px strip with `pointer-events: auto` only under
`data-perched='true'` — so the pointer lands on the actor and the existing drag
handlers run unchanged. The actor box itself stays 200×300, so bounds, the bubble
anchor and the walk floor are untouched. `probePerchArt` now drives real input
(`sendInputEvent`, not synthetic DOM events): it checks the window is reachable
over the legs, that `elementFromPoint` there is `.pet-actor`, that hovering away
restores click-through, and that a mouse drag from the legs lifts her 90 px. All
four are pass conditions of `npm run smoke`.

**Seen, not just asserted.** `npm run smoke` now drags her onto a ledge, asserts
the renderer chose `perched` and then `perched_sleeping`, and captures
`.smoke-perch.png` / `.smoke-perch-sleep.png` for a human. In that capture she is
seated with the legs crossed and dangling, the palette matches the standing art
(red hair, green accents, dark body), nothing is clipped, and the two frames
differ by ~10k pixels — so the held shut-eye cell is a different drawing, not a
still idle pose. Two things only the eye could tell: her legs hang *below* the
200×300 actor box, so they are not draggable, and the seat lands at the actor's
bottom edge rather than at `pos.y` — which is the same thing the shift was
specified for, but worth knowing before moving a perch.

**The canvas had to grow.** `[0, 228]` puts the seated art 176 CSS px below her
feet line, and the pet's stage canvas was exactly the 200×300 actor box with
`overflow: hidden`, so that art would have been clipped away. The pet's stage
canvas now grows downward in CSS, and `CapriStage` fits the pack to the actor box
it is given (`frame`), not to the grown element, so the floor states are
unchanged and she is not magnified. The 190 px pose gate is what guarantees the
window has room for the overhang above the taskbar. (An earlier `growBottom`
prop did the same job from JS; it became dead once `frame` existed and was cut in
review.)

- Floor: idle/walk/think/talk/sleep behave as today.
- Ledge: every ambient state shows its seated counterpart; teleporting off a
  ledge plays `perch_leave`, teleporting onto one plays `perch_arrive`.
- No frame-rate cost: `setPerched` fires only on a transition.

---

## 3. Unfinished jobs

### 3.1 Diagnosis

`services/core/capri_core/agent/loop.py:393`:

```python
if decision.is_final:
    self.planner_failed = decision.failed
    if decision.failed:
        self.paused_reason = "planner failed"
    return (decision.final or "Done."), notes
```

`done` is a *promise*, not a verdict. A small model that emits
`{"tool":"done","final":"I'll write the file now."}` ends the turn, marks the task
done (`_finish_task`), and nothing continues it until the user says "go on". The
persona asks her not to claim work she has not done, but it never forbids
*narrating* future work — so the exact failure the user describes is a legal move
in the current rules.

### 3.2 Fix: one nudge, twice at most

1. **`agent/prompt.py`** — add the predicate beside `looks_like_conversation`:

   ```python
   # A reply that announces the next action instead of taking it. "Let me know"
   # is a courtesy, not a promise, so it is removed before matching.
   _ANNOUNCE = re.compile(
       r"\b(?:"
       r"i'?ll (?:write|create|add|fix|update|make|build|run|open|read|check|"
       r"patch|edit|install|set up|start|start on|go ahead|handle|do that|get)"
       r"|i will (?:write|create|add|fix|update|run|open|start|handle|do that)"
       r"|i'?m going to|i am going to|i'?m about to"
       r"|let me (?:write|check|look|start|run|fix|open|read|take)"
       r"|next,? i|first,? i'?ll|starting (?:now|with|on)"
       r"|on it\.?$|one (?:moment|second)"
       r")",
       re.IGNORECASE,
   )

   def announces_action(text: str) -> bool:
       return bool(_ANNOUNCE.search(re.sub(r"\blet me know\b", " ", text or "")))
   ```

2. **`loop.py`** — in `_drive`, count nudges (`nudges = 0`) and change the final
   branch:

   ```python
   if decision.is_final:
       self.planner_failed = decision.failed
       if decision.failed:
           self.paused_reason = "planner failed"
           return (decision.final or "Done."), notes
       reply = (decision.final or "").strip()
       if (
           nudges < 2
           and not prompt.looks_like_conversation(self.request)
           and prompt.announces_action(reply)
       ):
           nudges += 1
           db.trace(self.conn, span="turn", turn_id=turn_id,
                    workspace_id=self.workspace_id, task_id=self.task_id,
                    name="turn.nudge", payload={"attempt": nudges, "said": reply[:300]})
           messages.append(Msg("assistant", reply))
           messages.append(Msg("user", _NUDGE))
           continue
       return (reply or "Done."), notes
   ```

   A nudge on the last allowed step is answered, not retried: the reply stands
   with the truth appended (*"… (That was my last step, so nothing actually ran:
   tell me to keep going and I'll do it.)"*), because a `continue` there leaves
   the loop through the generic step-ceiling line and drops what she just said.

   `_NUDGE` (module constant):

   > `[system] You said you would do that, but no tool ran. Do it now, in this turn: pick a tool and act. If something is genuinely blocking you, say exactly what it is and stop.`

3. **`packs/prompts/kyoko/system.md`** and `_PROTOCOL` — one rule, phrased in her
   voice: *if your reply describes a next action, take it in this same turn;
   announcing work is not doing it.* This is the durable fix; the nudge is the
   safety net for a model that ignores it.

### 3.3 Why not "never finish without a tool call"

Because conversation is a valid turn end, and the existing
`looks_like_conversation` already separates that case. The nudge is gated on
`not looks_like_conversation(self.request)`, so "hey" can never be nudged into
work. Two attempts is the ceiling: after that, a model that still will not act is
a model problem, and burning the step budget makes it worse.

### 3.4 Tests (`services/core/tests/test_brain.py`)

- Fake brain scripted as `done("I'll write the file now")` then `done("Done")`:
  assert one `turn.nudge` trace and that the turn continued.
- `done("I'll be here if you need me")` on request "how are you" produces no
  nudge.
- A work request answered with `done("Done.")` produces no nudge.
- Two consecutive announcements produce exactly two nudges and then finish.
- An announcement on the last allowed step keeps her reply, states that nothing
  ran, and does not spend a second planner call.

---

## 4. Temperature belongs to the server

### 4.1 Sites to remove

| File | What |
|---|---|
| `config.py:80–86` | `ModelsCfg.temperature` field |
| `config.py:677–681` | legacy planner-temperature carry-over in `_migrate_model_config` |
| `config.py` `_drop_retired_config` | `models.pop("temperature", None)`, so an existing `config.yaml` still loads (`extra="forbid"`) |
| `models/router.py:39` | `_REPAIR_TEMPERATURE` |
| `router.py:248,261` | `complete(..., temperature=...)` parameter and the `"temperature"` body key |
| `router.py:378,423,444` | three call sites (`decide` repair, `extract`, `args`) |
| `companion.py:1103` | `temperature=0.8` on the ambient line call |
| `SettingsPanel.tsx:493–503` | the Temperature `Field` in Brain → Advanced (leave `disable_thinking`, timeouts) |
| `tests/test_models.py:160,174,402` | assertions that temperature is preserved/sent |

### 4.2 Notes

- The JSON body simply omits `temperature`; llama.cpp and LM Studio then apply the
  server's own default (or the model's `generation_config`), which is exactly what
  was asked for.
- Removing the field is a **config-schema change**. Full-dump `config.yaml` files
  will contain `models: { temperature: … }`; `_drop_retired_config` must drop it
  before validation or the app refuses to start. That makes the migration test the
  most important test in this section.
- Update the request-shape assertion from `bodies[0]["temperature"] == 0.6` to
  `"temperature" not in bodies[0]` — with the comment that sampling is the
  server's decision.

### 4.3 Decided: no temperature anywhere, reflex included

Every remaining call site gets the same treatment — planner, repair, `extract`
(memory writer), `args` (tool arguments and the destructive-call reviewer) and the
ambient companion line. The reasoning is not "consistency for its own sake":

- **Determinism already comes from the grammar, not the temperature.** `extract`
  passes `gbnf.from_schema(schema)`; `args` passes the tool's own params schema;
  the reviewer goes through `brain.args` with `REVIEW_SPEC`. A 0.1 temperature was
  never what made those reliable — constrained decoding is.
- **Two owners of one knob is the defect being fixed.** Leaving a harness default
  on some paths means the server is authoritative for the planner and the harness
  is authoritative for extraction, which is exactly the split state that produced
  the complaint.
- **`VoiceService` and the memory writer already degrade cleanly** when the server
  picks unusual sampling; there is no retry or score threshold that depends on it.

If memory extraction gets noisier after this, that is a prompt/schema problem
(fix it in `memory/`), not a reason to reintroduce a harness-side default. Do not
add a replacement "reflex temperature" setting under another name.

---

## 5. Speech that finishes its sentences

### 5.1 Two independent causes

**(a) Preemption.** `VoiceService.speak` stops whatever is playing when a new call
arrives with `preempt=True` (`voice/service.py`, the `if self._busy:` block), and
`_voice_speak` (`server/app.py:1031`) always uses the default. The renderer issues
two out-of-turn `voice.speak` calls:

- the "Hm?" cue for a low-confidence wake candidate (`App.tsx:470–486`), and
- the acknowledgement for a bare "Capri" (`App.tsx:1053–1064`).

The cue is guarded by `activeTurn.current`, but that ref is cleared at
`agent.done` — *before* her reply is spoken (the loop emits `agent.done`, then
awaits `voice.speak`). So a wake candidate captured just before she starts talking
comes back from Whisper during her reply, the guard passes, `Hm?` preempts, and
the reply is cut mid-sentence. This is a concrete, reproducible race.

**(b) Tail clipping.** `split_sentences` hands fragments to the engine as-is. A
fragment can end with no terminal punctuation (the last sentence of a reply that
did not end with a stop, or a 240-char wrap), and unpunctuated text makes a
neural TTS drop the final phoneme and flatten the final contour — heard as a
sentence that stops early.

### 5.2 Fix

1. **Plumb `preempt` through the RPC.** `_voice_speak` reads
   `params.get("preempt", True)` and passes it to `voice.speak`. The renderer sends
   `preempt: false` for the ack and the cue — a cue must never cut an answer, and
   "busy" is a correct answer for a cue.
2. **Guard the cue on speech as well as the turn.** Add `speakingRef` (a mirror of
   the `speaking` state) and return early from `noteRejectedWake` while it is true.
   Belt and braces with (1), but it also stops the dot from flashing mid-answer.
3. **Terminal punctuation before synthesis** in `voice/service.py`:

   ```python
   _VOICED_END = re.compile(r"[A-Za-z0-9\"')\]]$")

   def _spoken(sentence: str) -> str:
       """Text handed to the engine. A missing stop is what a clipped tail
       sounds like, so give the engine one."""
       return sentence + "." if _VOICED_END.search(sentence) else sentence
   ```

   Applied in `produce()`; visemes keep using the clean sentence. Do **not** add a
   stop to a fragment that ends on a comma — that is a deliberate clause break.
4. **Keep the queue honest.** No change needed: the renderer already waits for
   playback to drain on `speech.stop{done}` and the daemon's `last` flag is correct.
   Worth an explicit regression check because it is easy to "fix" this again.

### 5.3 Verify

- `test_voice.py`: a stubbed engine that records text receives a terminal stop on
  an unpunctuated fragment and none on a comma-ended one.
- Manual (owner): let a reply play uninterrupted; trigger a wake candidate during
  it (speak near the mic) and confirm the reply is not cut and no "Hm?" preempts.
- `voice.speak` with `preempt: false` during an active stream returns
  `{"reason": "busy"}` and leaves the running stream alone (unit test).

---

**Where the cut actually came from (19 Nov).** Not a preempt, and not a missing
terminal stop: sherpa's Kokoro tokenizer looks every word up in the pack's
lexicon and, on a miss, drops it in silence (`OOV word skipped: 'expects'`). That
lexicon lists base words and few inflections, so 5.8% of the words in ordinary
text never reached the engine - "it expects the tool to answer" was spoken as
"at the tool to answer" - and a dash is OOV as well, which is what made a clause
sound like it stopped halfway. Two changes: `voice/lexicon.py` merges the pack's
lexicon with the regular forms its entries imply (plural, third person, past,
gerund, possessive, `y` to `ies`), built once into
`<capri home>/models/kokoro/lexicon-capri-merged.txt` (45 MB, ~5 s to build,
engine load 2.9 s -> 8.8 s, hidden behind `voice.warm` at startup), and

**Where the load time goes (20 Nov).** Not the file read: 38 MB comes off disk in
0.28 s. Not the ONNX session either: a load with no lexicon at all still pays
~2.3-3 s for the 325 MB model. It is sherpa's tokenizer building its word table,
and it is linear in entries at ~6.8 us each, so ~0.7 s per 100k lines. Measured
best-of-three on this machine: pack alone 178k entries -> 2.98 s; a 250k-line
subset -> 3.58 s; 500k -> 5.28 s; the unpruned merge, 1.16M -> 10.17 s. The
lexicon is read whatever the text is, so the only lever that matters is how many
lines are in the file. "A smaller entry set" turned out to mean dropping lines
that are not spellings of anything: `danceed`, `carrys`, `boxs`, `danc's`, and
the `-ed`/`-ing` of bases that are themselves inflections (`buildinging`,
`coachedded`). Those are 329k of the 982k generated lines. Bases that are
inflections keep their plural (`buildings`, `meetings` are worth an entry), and
the ambiguous doubling spellings (`stoped`/`stopped`) are both kept, because
which one is right depends on stress and guessing would bring a dropped word
back. The pruned merge is 831k entries, 32.6 MB, and loads in 7.55 s: 2.6 s off
the 10.17 s, with no word lost that the old file could say (a corpus of the
repo's prose loses only `ms`/`ws`). Getting near the pack's 3 s would need the
merged forms gone altogether, which means either not covering plurals and past
tenses or replacing sherpa's word frontend with phoneme codepoints
(`GenerationConfig.phoneme_codepoints` exists in sherpa-onnx 2.0.1) - a different
frontend whose prosody is unverified here, so it is not what this does.

The rest of the fix stands as measured: the merge plus a dash spoken as a comma
took OOV skips from 3 to 0 on three long replies, and the transcriptions lose
only "and" (which whisper drops by itself).

**Superseded by a different engine (20 Nov, owner's call).** The merge can only
ever hold words somebody wrote down, and sherpa still skipped the ones nobody
did: `Kubernetes`, `OAuth`. Since a lexicon is the wrong shape for this problem,
her voice now runs on Pocket TTS (`voice.tts: pocket` in
`~/.capri/config.yaml`), which synthesizes from text plus her own state file and
has no word table to miss a word in. `capri_english.safetensors` sounds like her
by construction, and the 33 MB merge and the pack lexicon are not read at all.
Cost, measured here: 54 s to the first line on the first run (base weights
download), 15 s to the first line once cached, then ~3.7 s per ~4 s of speech -
similar real-time factor to Kokoro's 2.9 s per 3 s. `pocket` had to join the
shared venv: `uv sync --package capri-core --all-extras` (the `--extra pocket`
form drops the other extras from the environment). Kokoro and Piper remain
selectable, and `voice/lexicon.py` stays for the Kokoro path. A Pocket line -
"Kubernetes and OAuth were the two things it kept mispronouncing" - transcribed
back with no word missing ("OAuth" came out as "allWath", which is a
pronunciation question, not a drop). Words that are neither listed nor regular - acronyms, jargon - are
still skipped, and sherpa still logs them.

**Pocket keeps her register; the +2 lift does not apply to it (20 Nov).** The
first Pocket wiring also ran `dsp.pitch_shift(take, voice.dsp.pitch)` so she
would match the Kokoro path. Measured on one eight-sentence passage, 370-480
voiced frames per condition, f0 from a per-frame cepstrum median: Kokoro dry
199.9 Hz, Kokoro with the lift 222.2 Hz, Pocket dry 229.6 Hz, the same Pocket
take shifted 253.1 Hz. Both controls (the Kokoro lift, and the shift on Pocket's
own audio) read +1.8 st and +1.7 st against a true +2.0, so the estimator is
worth about 0.3 st; Pocket dry sits +0.57 st from Kokoro with the lift already
in it, not 2 st below. The state carries her pitch, and the shift put her ~2.4
st above the Kokoro path - so it was removed, with a fake-model test pinning
that `PocketEngine` returns the take unshifted. Duration is untouched either
way (the shift resamples then stretches back); Pocket is ~1.5x slower than
Kokoro here because it has no speed knob and Kokoro runs at tempo
1.1 x speed / lift.

---

## 6. "mm" is not "Emma"

`voice/wake.py:92,97` writes:

```python
"Mm? Go on.",
"Mm. You're awake too.",
```

Kokoro reads a bare `Mm` as the name "Emma". Two changes, both small:

1. **Rewrite the lines** to an unambiguous interjection: `"Hm? Go on."` and
   `"Hm. You're awake too."` (`Hm?` is already used elsewhere, including the cue,
   and it reads correctly).
2. **Generalise in `clean_for_speech`** *(superseded below: the fold did not
   change the sound, so hums are dropped instead)*: fold a standalone hum to `hm` before it
   reaches the engine —

   ```python
   _HUM = re.compile(r"\b[Mm]{2,}\b")
   plain = _HUM.sub("hm", _MARKUP.sub("", without_code))
   ```

   `\b` plus the length guard leaves words like "hmm" → "hm" but not "Emma" or
   "ummm" (which is fine either way). This protects any future line that starts
   with `Mm`.

Tests: `split_sentences("Mm. You're awake too.")` yields text containing `Hm`,
never `Mm`; `wake.acknowledge()` never returns a line beginning with "Mm".

**After listening (owner, 19 Nov).** `Hm` did not fix it: by ear the engine says
"em" / "Emma" for `Hm` too, so the spelling was never the problem, and the fold
above was treating a symptom. Final on this: the interjections are a word the
engine already says — `"Yeah? Go on."`, `"Yeah. You're awake too."`,
`"Yeah? I'm listening."`, and the low-confidence cue is `Yeah?` in `App.tsx` —
and `clean_for_speech` keeps hums out of the spoken text entirely: a standalone
hum is dropped with the punctuation that belonged to it, so `"Hmm, that's odd."`
says `"That's odd."` and a reply that is only a hum is not spoken at all. A name
that merely holds the letters (`Emma`) is untouched. Quote characters are silent,
so they stay where they were: `"Hmm," she said.` says `She said.` rather than
leaving the `""` a hum removed from between them would make. Her lines contain no
hum, and neither does anything handed to the engine. `test_hum` pins all of it:
no `mm`/`hm` in any wake line, the exact text the engine receives for a leading
and a mid-sentence hum, the quoted cases, and `Emma` unchanged.

**The voice is not the recording (same listening pass).** Nothing from
`capri-desktop/assets/voice/*.wav` (`hello-morning/miss/late.wav`, 2.2 s each)
reaches the engine. Her voice is the sherpa-onnx `capri` pack
(`voice.voice_blend.a`), and then `voice.dsp.pitch` (+2 semitones) and the
`layers` doubling (+2 / -2.5 semitones at level 0.4) move it further away. To
hear the pack alone: `voice.dsp.pitch: 0`, `voice.dsp.layers: false`.

**Wiring the clone (19 Nov).** `uv add chatterbox-tts` cannot work in this
project: it pins `numpy<2` where the daemon needs `numpy>=2`, and
`transformers==4.46.3`, which the `pocket` extra cannot share — uv resolves the
union of all extras, so one extra that cannot coexist blocks the lock. Chatterbox
therefore gets its own venv (`<capri home>/models/chatterbox/venv`, torch
2.6.0+cpu, 1.9 GB) and `voice/clone_worker.py` runs there: one JSON line in, one
base64 float32 line out, model loaded once per process. `ChatterboxEngine` uses
it when `chatterbox` is not importable in the daemon and falls back to the
in-process import when it is. `voice.chatterbox_python` overrides the
interpreter. The worker picks Turbo only when the reference is five seconds or
longer (Turbo asserts on shorter clips) and the base model otherwise.

---

## 7. Wake latency and end-of-sentence detection

### 7.1 Where the time goes after "Capri"

1. Trailing silence: `silence_ms = 700` before the utterance is even closed.
2. Whisper pass over the whole clip (`_recognize`), ~0.5–1.5 s on this CPU.
3. The acknowledgement is synthesized (Kokoro first-segment ≈1.2 s measured in
   `README.md`) and played.
4. Only when the line finishes does `finishWakeReply` open the floor
   (`App.tsx:425–435`, `:455`).

So the floor opens ~3 s after the name, and the user often keeps talking into a
window that has not opened.

### 7.2 Fixes

1. **Faster trailing cut.** `voice.wake.silence_ms` default `700 → 550`. The
   segmenter is the only consumer; calibration never writes it.
2. **Name grace, so a pause after the name is not the end.** New
   `voice.wake.name_grace_ms` (default `700`). In `segmenter.ts`:

   ```ts
   /** Below this much speech the utterance may be her name alone. */
   export const SHORT_MS = 1200
   ...
   const limit =
     this.speechMs < SHORT_MS ? this.silenceMs + this.graceMs : this.silenceMs
   if (this.quietMs < limit && this.voicedMs < MAX_MS) return null
   ```

   A full sentence is closed 550 ms after it stops; "Capri" alone keeps listening
   for 1.25 s. This is the same knob that fixes §8.
3. **Do not stack the ack in front of the request.** When the same utterance
   carries a command (`hit.command` non-empty), no ack is spoken — already true.
   The remaining cost is the bare-name case, where the ack *is* the answer, so it
   is not latency.
4. **Clean up the acknowledgement handshake.** `speakWakeReply` already falls back
   after 2.5 s if no segment arrives; keep it, and make the fallback also
   `openFloor()` on a rejected RPC (it currently does via `.catch`).
5. **Do not block the next utterance on ack playback** (see §8.3): the request
   that follows the name is routinely already in flight.

### 7.3 Verify

- `npm run test:voice` with the new grace case: short speech + a 500 ms pause does
  not end the utterance; 1.6 s of speech + 400 ms of silence does.
- Python: `config.voice.wake.silence_ms == 550`, `name_grace_ms == 700`, both
  exposed by `voice.status` (`nameGraceMs`) and `--voice-status`
  (`__main__.py:314`).
- Manual: time floor-open after saying "Capri" alone, then "Capri, open my notes".

---

## 8. Only "Capri" is heard, not the rest

### 8.1 Why the split happens

`WakeSegmenter` ends an utterance on `quietMs >= silenceMs` regardless of how
little was said. Say "Capri — open my notes" with a 700 ms pause after the name and
you get two utterances:

1. `"Capri"` → `voice.wake` matches, `bare = True` → she replies
   "Yeah? I'm here." and (later) opens the floor.
2. `"open my notes"` → a *separate* wake candidate. `match_wake` requires the name
   first, so `matched = False`, and the request is discarded — usually before
   `wakePending` has even been set, because that flag is only raised when the ack
   finishes.

Two more contributors:

- The wake monitor is paused while she speaks
  (`App.tsx:1230`: `setPaused(speaking || voiceState !== 'idle')`). Anything said
  during the ack is neither captured nor queued.
- Utterances are handled concurrently (`onUtterance: (url) => void handleWakeUtterance(url)`),
  so two segments can race and the second can be judged before the first has set
  the floor.

### 8.2 Fix

1. **The grace window (§7.2.2)** solves the common case: the whole sentence stays
   one utterance and `voice.wake` returns `command = "open my notes"` in one pass.
2. **Arm the floor at match time, not at playback end.** In the bare branch of
   `handleWakeUtterance`, set `wakePending.current = true` before
   `speakWakeReply(...)`. `openFloor` still owns the *UI* window and the timeout;
   this only means a continuation that arrives while the ack is queued is treated
   as the request instead of a stray sentence.
3. **Serialise wake utterances.** Keep a chain ref:

   ```ts
   const wakeChain = useRef<Promise<void>>(Promise.resolve())
   ...
   onUtterance: (audioUrl) => {
     wakeChain.current = wakeChain.current
       .then(() => handleWakeUtterance(audioUrl))
       .catch(() => undefined)
   }
   ```

   One judgement at a time, in the order the microphone produced them. This costs
   nothing when a sentence stays whole, and it makes the split case deterministic
   instead of a race.
4. **Do not drop audio spoken during the ack.** Options, in preference order:
   - Leave the *wake gate* running while she speaks but mute its own echo: the
     existing barge monitor already runs during speech and relies on echo
     cancellation, so the same cancellation covers the gate. Risk: her own reply
     can be transcribed as a request. Needs a guard (ignore a follow-up whose text
     matches the last spoken segment closely) — do this only if (1)+(2) prove
     insufficient in practice.
   - Otherwise: keep the pause, and make the ack shorter ("Mm?"-free, ≤1 s of
     audio) so the hole is small.

   **Recommendation:** ship (1) and (2) first and measure. The grace window means
   the continuation is inside the same utterance in every ordinary delivery.

5. **Make the rejection visible for diagnosis.** `voice.wake` already returns
   `reason` and keeps `lastRejection`; surface the transcript length in the trace
   (`chars`) so a future "she only heard her name" report is checkable.

   **Landed.** `_rejection()` now carries `chars` (the transcript length, never
   the words — `text` stays out of the trace on purpose), so both the
   `voice.wake.rejected` / `voice.follow_up.rejected` rows and
   `voice.status.wake.lastRejection` answer "did she hear the rest of the
   sentence and drop it, or never hear it?" without recording what was said.
   `test_wake_rejection_trace` asserts the count and that the text is absent.

### 8.3 Tests

- `wake.py` unit: `match_wake("Capri, open my notes")` → `command == "open my notes"`.
- Segmenter: the grace case above (shared with §7).
- Renderer logic: not unit-tested today. A serial-chain test would need React
  harness work; instead assert the pure part (grace) in `test:voice` and keep the
  chain small enough to review.

---

## 9. Shrinking the per-request injection

### 9.1 What is actually big (measured)

```
system prompt, work turn   8,809 chars ≈ 2,200 tokens
  persona                  4,354  (49%)
  tool index (19 tools)    2,243  (25%)
  _PROTOCOL                2,057  (23%)
  _CODING                    655   (7%)
  profile guidance           ~200
plus per turn: memory recall (budget 800 tokens) and the RAG block, and the
conversation history up to `history_messages = 30`.
```

2,200 tokens of system prompt is not pathological, but it is re-sent on every step
of a 24-step turn, and a local 4–8 k model feels it: ~27% of an 8 k window is gone
before the request is read. The measured, achievable target is **~1,860 tokens**
(§9.3) — the persona alone is 1,088 of them, so anything lower means cutting her
character or the capability index.

### 9.2 What to cut

1. **_PROTOCOL (2,057 → 1,407 measured).** It currently restates the JSON shape, the
   observation convention, and twelve rules in prose, several of which repeat each
   other ("never repeat the same call", "if you already read a file…", "keep the
   final reply short" twice). Rewrite as the shape (3 lines), the observation
   convention (2 lines), the tool list, and 6 bullet rules. Every behavioural
   guarantee in the current text must survive as a bullet — this is a compression,
   not a policy change. The "announcing work is not doing it" rule from §3 lands
   here, so this is where that sentence is paid for.
2. **_CODING (655 → 447 measured).** Items 1–7 are three ideas: locate before
   editing, make the smallest change, prove it with the narrowest check. Merge;
   keep the "do not claim a test passed" and "no git commit unless asked" lines
   verbatim — those are the ones that fail visibly when dropped.
3. **Tool index (2,243 → 1,515 measured).** `gist()` already truncates to 96
   chars + "…". Tighten `gist` to 72 chars and replace the seven repeated
   "asks the person first" suffixes with one legend line ("7 of these ask the
   person before they run"). Saves 728 chars with no loss of meaning; the
   per-tool marker survives as `[asks]` if the legend is not enough in practice.
4. **Persona.** Do not touch for size. It is the character, it is the one part the
   user chose, and cutting it to save 200 tokens is the wrong trade.
5. **Memory.** `memory.recall_items = 8` at 800 tokens is the other half of the
   budget. Only after the prompt trim, measure a few real turns; if recall is
   mostly noise, drop to 6 items / 600 tokens. Keep this out of the first patch so
   its effect is separable.
6. **Do not** drop `_SCREEN` or `apps_block` — they are already conditional on the
   turn.

### 9.3 Target — decided: 7,600 chars (~1,900 tokens), 8,100 with a workspace

The compressed blocks were prototyped and measured against the live registry, not
guestimated:

```
                        now        after       delta
persona                 4,354      4,354       0        (untouched, by decision)
tool index              2,243      1,515     -728        gist 72 + one legend line
_PROTOCOL               2,057      1,407     -650
_CODING                   655        447     -208
--- core prompt         8,809      7,431   -1,378        (~1,858 tokens)
with a workspace        9,466      7,880   -1,586        (~1,970 tokens)
```

**Ceiling for the test: 7,600 chars** for the no-workspace work prompt and
**8,100** with a workspace (≈100–200 chars of headroom over the measured
prototype, so a new tool or a reworded sentence does not fail the build for a
rounding error). Pin both in `test_brain.py`:

```python
specs = builtin_registry().specs()
plain = prompt.system_prompt(specs, "kyoko", profile="focus", request="fix the failing test")
check(len(plain) <= 7600, f"system prompt is {len(plain)} chars")
work = prompt.system_prompt(specs, "kyoko", profile="focus", request="fix the failing test",
                            work=prompt.WorkContext(workspace_id="w", task_id="t",
                                                    name="p", root="C:/p"))
check(len(work) <= 8100, f"work prompt is {len(work)} chars")
```

**Where the floor is, and why we stop there.** After this cut the persona is 59%
of the prompt. The next meaningful step would be trimming the persona's "Work"
and "What you do not do" sections, which overlap the protocol — but the overlap is
the *ethic* against the *mechanics* ("prove it with a tool" vs "observations
arrive as …"), so cutting one side saves ~150 tokens and costs either her voice or
a rule the small model actually follows. **Do not go below 7,400 chars**; below
that you are deleting character or capability documentation, and both cost more
than 1,860 tokens in an 8 k window is worth. Memory (`recall_items`/
`recall_budget_tokens`) stays on its own separate patch, measured on real turns
(§9.2.5).

### 9.3b Measured after implementation — the 7,600 ceiling was not reachable

The trim landed (gist 72, one consent legend, `_PROTOCOL` and `_CODING`
rewritten), and the sizes were measured against the live registry:

```
                        plan said   measured
persona                   4,354       4,492    (4,354 + the §3 rule)
profile guidance            ~200         158
tool index                1,515       2,026    gist 72 + legend, 19 tools
_PROTOCOL                 1,407       1,580
_CODING                     447         448
--- core prompt           7,431       8,253
with a workspace          7,880       8,878
```

Two arithmetic problems, both visible above. The plan's own columns sum to
**7,929**, not the 7,431 it claimed for the same rows — the prototype total was
miscalculated, and the real ceiling was therefore ~7,900 before §3 added 138
chars to the persona. And the tool-index row was optimistic: it assumed 7 consent
suffixes when the registry has **5**, and 96 → 72 chars only touches the **10**
rows long enough to truncate, so the same technique lands at 2,026, not 1,515.

Reaching 7,600 from here needs ~650 more chars, and there is nowhere left to take
them from that does not reverse a decision in §9.2: the persona (4,492) and the
tool index (2,026) are 79% of the prompt, and the protocol already carries every
rule as one bullet. 7,600 was never attainable with a 4,354-char persona and 19
documented tools. The tests pin **8,350 / 8,980** instead, and the real decision
for the next patch is persona size versus capability documentation, not prose.

**Raised again for the note tools.** The Obsidian vault added three tools
(`note.search`, `note.read`, `note.write`), which is 271 chars of index:
measured **8,524 / 9,149**, so the tests now pin **8,600 / 9,220**. A capability
she cannot discover is worse than ~68 tokens on every turn, and the index is still
the cheapest place to pay for it.

**And paid only where it buys something.** Those three lines are now hidden from
the index while `notes.vault` is empty: the turn measures **8,228 / 8,853**, and
the 271 chars are paid only by someone who has a vault for them to act on. The
pin stays at 8,600 / 9,220 because `test_brain.py` sizes a prompt built from the
whole registry, i.e. the vault-configured worst case; the tools stay registered
either way, so a call still answers with the setting to fix.

**Not raised for the wiki.** The vault read as her wiki adds a `## Your notes`
section (616 chars, measured by `test_notes.py`), but only for a turn that has a
vault behind it: `system_prompt(notes=…)` defaults to `None`, so the pinned ceilings
still describe every other turn. Notes the recall adds are a data block, which the
prompt's own budget accounts for alongside the document block it replaces, not
instruction text that has to be paid for on every turn forever.

### 9.4 Risk

Prompt edits change model behaviour more than they change code. Land §9 alone, with
`test_brain.py` persona/block tests and a scripted-mode run, so a regression can be
attributed to the trim rather than to the nudge (§3) landing in the same commit.

---

## 10. Sequencing

Ordered so each step is independently verifiable and nothing masks anything else:

| Step | Work | Why here |
|---|---|---|
| 1 | §6 "mm" lines + hum fold | Trivial, zero risk, immediate audible win |
| 2 | §5 preemption plumbing + punctuation | Small; fixes an intermittent bug that poisons every listening test |
| 3 | §7 + §8 voice gate (silence 550, grace, serial chain, arm-on-match) | The two reports are the same mechanism; must land together |
| 4 | §4 temperature removal + migration test | Config change, independent of the rest |
| 5 | §3 nudge + persona rule | Depends on the prompt work in §9 for the final wording |
| 6 | §9 prompt trim + budget test | After §3 so the rule text is settled |
| 7 | §2 pose mapping | Depends on §1 clips |
| 8 | §1 perch wiring + licence + visual check | Last: needs eyes on the art, not just tests |

## 11. Verification matrix

| Area | Command |
|---|---|
| Model config, migration | `uv run python services/core/tests/test_models.py` |
| Prompt, nudge, loop | `uv run python services/core/tests/test_brain.py` |
| Voice service, wake, sentences | `uv run python services/core/tests/test_voice.py` |
| Context budget | `uv run python services/core/tests/test_context.py` |
| Renderer types | `npm --workspace @capri/desktop run typecheck` |
| Segmenter (grace) | `npm --workspace @capri/desktop run test:voice` |
| Build | `npm --workspace @capri/desktop run build` |
| Perch, listening, poses (Electron) | `npm --workspace @capri/desktop run smoke` |
| Wire-level smoke | `CAPRI_FAKE_BRAIN=1 … uv run python services/core/tests/ws_smoke.py` |

Not covered by any of the above and therefore needing the owner's ears/eyes:
articulation of "Hm" vs "Mm", whether a reply survives a cue fired mid-answer, the
floor-open latency after a bare name, and perch alignment.

## 12. Decisions

All four questions from the previous revision are settled; none of them blocks
implementation any more.

| Question | Decision | Evidence |
|---|---|---|
| Perch shift sign | `shift = [-26, 228]` canvas px on every perch clip | source draw arithmetic + measured alpha boxes (§1.3) |
| Reflex temperature | removed everywhere, including extraction, arguments and the reviewer | those paths are grammar-constrained; two owners of one knob is the bug (§4.3) |
| Listening pose plumbing | new pet-only IPC `capri:pet-listening`; no `AgentState` change, no socket event | pet is a separate window; daemon emits no voice state (§2.2.3) |
| Injection target | ~~≤7,600 chars; floor 7,400~~ — not reachable with the persona and 19 documented tools. Pinned at 8,600 / 9,220 | measured after implementation (§9.3b) |

Deliberately left to the owner, because they are taste rather than fact:

1. **Does a seated pose in mid-air look right?** There is no ledge art in this app,
   so perch = sitting on an implied edge. If it reads badly, the fallback is to
   keep the floor sprite at perch heights and use the perch set only for
   `perched_arriving` — a two-line change in `Pet.tsx`, not a re-plan.
2. **Memory recall budget** (8 items / 800 tokens). Measure real turns after the
   prompt trim before touching it; it is a separate patch either way.
