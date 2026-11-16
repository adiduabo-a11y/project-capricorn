# Voice input fixtures

Recorded clips are the only way to check the wake path against a real
microphone. They are regression clips, not training data: a handful of short
takes of the same phrases, checked in so a change to the gate, the matcher or
the model can be measured instead of guessed.

They are optional. `test_wake_fixtures.py` passes without them and reports a
skip; `--wake-check` needs at least one file to do anything.

## Recording

- One take per file, 1–4 seconds, leaving ~200 ms of silence before and after.
- Same microphone, same room, same distance you normally talk from — the point
  is to catch problems on *your* setup, not a studio.
- WAV is preferred (16 kHz or 48 kHz, mono or stereo; the decoder accepts 8–96
  kHz and mixes stereo down). MP3/M4A should be converted first, for example
  `ffmpeg -i take.m4a -ac 1 -ar 16000 01-wake-command.wav`.

On Windows with ffmpeg installed, one take is one command — press enter, say the
line, wait for it to finish:

```powershell
ffmpeg -f dshow -i audio:"Microphone (2- Realtek(R) Audio)" -t 4 -ac 1 -ar 16000 `
  services/core/tests/fixtures/voice/01-wake-command.wav
```

`ffmpeg -list_devices true -f dshow -i dummy` prints the exact device names.
Then write the `.txt` beside it (line one `wake` or `no-wake`, line two the
request or `-`).
- Speak naturally. Do not slow down for the recorder, and do not shout.
- No music, TV or other voices in the same file — those get their own clip.

## Files to record

Each `.wav` needs a `.txt` beside it with the same name:

```
line 1:  wake      (or: no-wake)
line 2:  the request she should extract, or - when there is none
```

Line 2 is matched as a case-insensitive substring of what the matcher returns,
so punctuation and capitalisation do not matter.

| File | Say | Sidecar |
|---|---|---|
| `01-wake-command.wav` | "Capri, open the terminal." | `wake`<br>`open the terminal` |
| `02-wake-bare.wav` | "Capri?" — nothing after it | `wake`<br>`-` |
| `03-hey-capree.wav` | "Hey Capree, what's on my screen?" | `wake`<br>`what's on my screen` |
| `04-mid-sentence.wav` | "I told Capri about the build yesterday." | `no-wake`<br>`-` |
| `05-room-noise.wav` | say nothing for 5 s (fan, TV, street — whatever is really there) | `no-wake`<br>`-` |
| `06-clipped-start.wav` | Start the recorder and say "start recording" immediately, then "Capri, start recording" once more in the same take if your recorder allows it; the first word must be cut into by the start | `wake`<br>`start recording` |
| `07-quiet-voice.wav` | "Capri, pause the music" at your normal distance, a little quieter than usual | `wake`<br>`pause the music` |
| `08-fast-speech.wav` | "Capri, what time is it" spoken quickly | `wake`<br>`what time is it` |

`05` is the one that matters most: it is what stops the gate waking on a room.

## Checking them

```powershell
uv run python services/core/tests/test_wake_fixtures.py
uv run python -m capri_core --wake-check services/core/tests/fixtures/voice
uv run python -m capri_core --wake-grid services/core/tests/fixtures/voice
```

`--wake-grid` transcribes each clip once and then sweeps
`min_confidence` against `max_no_speech`, printing the share of clips that
should wake her that still do, and how many it should ignore still wake her, for
every pair of limits. It ends with the cleanest pair that keeps the most wakes,
and says whether that differs from the shipped defaults. It exits non-zero when
no pair rejects every negative, which is a real answer about the gate, not a
crash. Use it on any folder of
`.wav` + `.txt` pairs, not just this one.

`05-room-noise.wav` is recorded on the machine this was set up on and is the
only clip so far: the spoken takes need a person at the microphone, and the
grid's first readings came from synthesised speech (Kokoro's public voices and
Windows' two desktop voices) instead.

The CLI prints what was heard, whether it opened with her name, and the request
it extracted, then compares each file with its sidecar. It exits non-zero on a
mismatch, so it can be run before and after a change to the matcher, the
silence gap or the model.

The recordings themselves are the expected churn: re-record rather than edit a
sidecar to match a wrong transcript.
