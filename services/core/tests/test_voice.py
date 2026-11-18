"""Voice tests: sentence splitting, visemes, WAV, streaming queue, loop wiring.

Offline: the tone engine stands in for Kokoro, so the pipeline is exercised with
no model files and no audio device.

    uv run python services/core/tests/test_voice.py
"""

from __future__ import annotations

import asyncio
import base64
import importlib.machinery
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import wave
from pathlib import Path
from typing import Any

import numpy as np

from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision
from capri_core.config import CapriConfig
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, builtin_registry
from capri_core.voice import (
    ToneEngine,
    VoiceService,
    acknowledge,
    address_only,
    match_command,
    match_wake,
    schedule,
    split_sentences,
)
from capri_core.voice import dsp
from capri_core.voice import wav as wav_mod
from capri_core.voice.stt import SttUnavailable, Transcript
from capri_core.voice.tts import KokoroEngine
from capri_core.voice.calibrate import barge_threshold, confidence_gate, measure
from capri_core.voice.visemes import VISEMES

FAILURES: list[str] = []
_CONNS: list[Any] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def _fundamental(samples: Any, rate: int = 24_000) -> float:
    """Rough f0 by autocorrelation, enough to see a semitone shift."""
    import numpy as np

    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    if x.size == 0:
        return 0.0
    window = x[:4096] if x.size >= 4096 else x
    window = window - window.mean()
    ac = np.correlate(window, window, "full")[window.size - 1 :]
    low, high = rate // 400, min(rate // 70, ac.size - 1)
    lag = low + int(np.argmax(ac[low:high]))
    return rate / lag if lag else 0.0


def config_for(workspace: Path) -> CapriConfig:
    config = CapriConfig()
    config.models.embed.dim = 64
    config.memory.embed = "hash"
    config.voice.tts = "tone"
    config.voice.enabled = True
    config.voice.queue_depth = 2
    config.voice.sentence_max_chars = 120
    config.policy.allow_paths = [str(workspace)]
    return config


class Sink:
    def __init__(self, delay: float = 0.0) -> None:
        self.events: list[dict[str, Any]] = []
        self.delay = delay

    async def __call__(self, event: dict[str, Any]) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append(event)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("type") == kind]


class FakeStt:
    name = "fake-stt"

    def available(self) -> bool:
        return True

    def reason(self) -> str:
        return ""

    def status(self) -> dict[str, Any]:
        return {"engine": self.name, "available": True, "vad": True}

    def transcribe(self, wav: bytes, *, language: str | None = None) -> Transcript:
        return Transcript(
            text="turn on the workshop lights",
            language=language or "en",
            language_probability=0.98,
            duration_ms=1400,
            segments=[{"start": 0.0, "end": 1.4, "text": "turn on the workshop lights"}],
        )


class UnavailableStt:
    name = "off"

    def available(self) -> bool:
        return False

    def reason(self) -> str:
        return "speech input is off"

    def status(self) -> dict[str, Any]:
        return {"engine": self.name, "available": False, "reason": self.reason()}

    def transcribe(self, wav: bytes, *, language: str | None = None) -> Transcript:
        raise SttUnavailable(self.reason())


class Brain:
    async def available(self) -> bool:
        return True

    async def decide(self, msgs, specs, persona_pack: str = "kyoko") -> Decision:
        return Decision(tool="done", final="One line. Two lines.", usage=ChatResult(text=""))

    async def args(self, spec, msgs, feedback=None):
        return {}, ChatResult(text="")

    async def extract(self, msgs, schema, **kwargs) -> dict[str, Any]:
        return {"items": []}


async def _always_true(request: dict[str, Any]) -> bool:
    return True


# --------------------------------------------------------------------------- tests


def test_sentences() -> None:
    sentences = split_sentences("First sentence. Second one! Third? And a fourth.")
    check(len(sentences) == 4, f"four sentences: {sentences}")

    fenced = split_sentences("Run this: ```python print('secret')``` then stop.")
    check(all("secret" not in sentence for sentence in fenced), "fenced code is not spoken")

    markup = split_sentences("**Bold** and `inline code` only.")
    check(
        markup and "Bold" in markup[0] and "inline code" in markup[0] and "**" not in markup[0],
        f"markup is stripped: {markup}",
    )

    long_text = "word " * 200
    pieces = split_sentences(long_text, max_chars=120)
    check(all(len(piece) <= 120 for piece in pieces), "long text is split to the limit")
    check(len(pieces) > 1, "long text produced multiple segments")

    check(split_sentences("   ") == [], "empty text yields no sentences")


def test_visemes() -> None:
    frames = schedule("Hello there, construct.", 1200, fps=15)
    check(len(frames) >= 10, f"{len(frames)} viseme frames for 1.2 s")
    check(all(frame["v"] in VISEMES for frame in frames), "visemes are from the fixed set")
    times = [int(frame["t"]) for frame in frames]
    check(times == sorted(times), "viseme times are monotonic")
    check(times[-1] < 1200, "last frame lands inside the audio")
    check(frames[-1]["v"] == "closed", "mouth closes at the end")

    empty = schedule("...", 400)
    check(empty and empty[0]["v"] in VISEMES, "silence still yields a frame")


def test_wav() -> None:
    audio = np.zeros(2400, dtype=np.float32)
    audio[100:200] = 0.5
    raw = wav_mod.to_wav(audio, 24_000)
    with wave.open(io.BytesIO(raw)) as handle:
        check(handle.getnchannels() == 1, "mono")
        check(handle.getsampwidth() == 2, "16-bit")
        check(handle.getframerate() == 24_000, "24 kHz")
        check(handle.getnframes() == 2400, "frame count matches the samples")

    url = wav_mod.to_data_url(raw)
    check(url.startswith("data:audio/wav;base64,"), "segment payload is a WAV data URL")
    check(base64.b64decode(url.split(",", 1)[1]) == raw, "data URL round-trips")


def test_dsp() -> None:
    config = CapriConfig()
    audio = np.sin(np.linspace(0, 200, 4000)).astype(np.float32) * 0.5
    crushed = dsp.bitcrush(audio, 0.5)
    check(crushed.shape == audio.shape, "bitcrush preserves length")
    check(not np.array_equal(crushed, audio), "bitcrush changes the signal")

    banded = dsp.radio(audio, 0.5, 24_000)
    check(banded.shape == audio.shape, "radio preserves length")
    check(not np.array_equal(banded, audio), "radio changes the signal")

    untouched = dsp.apply(
        audio, type(config.voice.dsp)(bitcrush=0.0, radio=0.0, layers=False), 24_000
    )
    check(np.array_equal(untouched, audio), "zero amounts leave audio alone")


def test_tone_engine() -> None:
    config = CapriConfig()
    engine = ToneEngine(config)
    check(engine.available(), "tone engine is always available")
    short = engine.synthesize("Hi.", voice="tone", speed=1.0)
    long = engine.synthesize("Hi there, this is a longer line.", voice="tone", speed=1.0)
    check(short.duration_ms > 0, f"tone audio is {short.duration_ms} ms")
    check(long.duration_ms > short.duration_ms, "longer text is longer audio")
    check(short.samples.dtype == np.float32, "samples are float32")


async def test_stream(service: VoiceService) -> None:
    sink = Sink()
    stats = await service.speak("trn_v1", "One. Two. Three.", sink)
    segments = sink.of("speech.segment")

    check(stats["segments"] == 3, f"three segments emitted: {stats}")
    check(0 < stats["firstSegmentMs"] < 700, f"first segment is under 700 ms: {stats['firstSegmentMs']}")
    check([event["seq"] for event in segments] == [0, 1, 2], "sequence is ordered")
    check(segments[-1]["last"] is True, "only the last segment is marked last")
    check(all(event["visemes"] for event in segments), "every segment carries visemes")
    check(
        all(event["audioUrl"].startswith("data:audio/wav;base64,") for event in segments),
        "every segment carries audio",
    )
    check(segments[0]["turnId"] == "trn_v1", "segments carry the turn id")
    check(stats["queueDepth"] == 2, "queue depth comes from config")
    check(stats["peakQueue"] <= 2, f"queue stayed bounded: peak {stats['peakQueue']}")

    stops = sink.of("speech.stop")
    check(stops == [{"type": "speech.stop", "reason": "done"}], f"one stop, done: {stops}")

    raw = base64.b64decode(segments[0]["audioUrl"].split(",", 1)[1])
    with wave.open(io.BytesIO(raw)) as handle:
        check(handle.getframerate() == 24_000, "segment audio is 24 kHz")


async def test_cancel(service: VoiceService) -> None:
    sink = Sink(delay=0.06)
    task = asyncio.create_task(
        service.speak("trn_v2", "One. Two. Three. Four. Five. Six. Seven. Eight.", sink)
    )
    await asyncio.sleep(0.15)
    await service.stop()
    stats = await task

    check(stats["reason"] == "cancelled", f"cancelled run says so: {stats['reason']}")
    check(stats["segments"] < 8, f"not every segment was emitted: {stats['segments']}")
    check(
        sink.of("speech.stop") == [{"type": "speech.stop", "reason": "cancel"}],
        "cancellation emits speech.stop(cancel)",
    )


async def test_barge_in(service: VoiceService) -> None:
    sink = Sink(delay=0.05)
    task = asyncio.create_task(
        service.speak("trn_barge", "One. Two. Three. Four. Five. Six. Seven.", sink)
    )
    await asyncio.sleep(0.12)
    stopped = await service.barge_in()
    stats = await task
    check(stopped, "barge-in stops an active stream")
    check(stats["reason"] == "barge-in", f"barge-in reason is preserved: {stats['reason']}")
    stops = sink.of("speech.stop")
    check(
        stops == [{"type": "speech.stop", "reason": "barge-in"}],
        "barge-in emits its own stop reason",
    )
    check(not await service.barge_in(), "barge-in is a no-op when idle")


async def test_aside_never_preempts(service: VoiceService) -> None:
    """An aside cannot stop a reply that is already speaking."""
    sink = Sink(delay=0.05)
    answer = asyncio.create_task(
        service.speak("trn_answer", "One. Two. Three. Four. Five.", sink)
    )
    await asyncio.sleep(0.08)
    aside = await service.speak("trn_aside", "A little aside.", Sink(), preempt=False)
    check(aside.get("reason") == "busy", f"an aside is refused mid-reply: {aside}")
    check(aside.get("segments") == 0, "the refused aside emitted nothing")

    stats = await answer
    check(stats["reason"] == "done", f"the reply was not interrupted: {stats['reason']}")
    segments = sink.of("speech.segment")
    check(len(segments) == 5, f"every reply segment went out: {len(segments)}")

    # The default is what a turn needs: its own reply does preempt an aside.
    first_sink = Sink(delay=0.05)
    first = asyncio.create_task(
        service.speak("trn_first", "One. Two. Three. Four. Five.", first_sink)
    )
    await asyncio.sleep(0.08)
    await service.speak("trn_second", "Short.", Sink())
    first_stats = await first
    check(first_stats["reason"] == "cancelled", "a reply still preempts an aside")


class WakeStt:
    """A canned recogniser: the test controls what was heard, and how sure."""

    name = "canned"

    def __init__(
        self,
        text: str,
        *,
        confidence: float = 1.0,
        no_speech_prob: float = 0.0,
    ) -> None:
        self.text = text
        self.confidence = confidence
        self.no_speech_prob = no_speech_prob
        self.warmed = 0

    def available(self) -> bool:
        return True

    def reason(self) -> str:
        return ""

    def status(self) -> dict[str, Any]:
        return {"engine": self.name, "available": True, "vad": True}

    def warm(self) -> None:
        self.warmed += 1

    def transcribe(self, wav: bytes, *, language: str | None = None) -> Transcript:
        return Transcript(
            text=self.text,
            language=language or "en",
            language_probability=0.9,
            duration_ms=900,
            confidence=self.confidence,
            no_speech_prob=self.no_speech_prob,
        )


async def test_wake_confidence(config: CapriConfig) -> None:
    """An uncertain candidate is not her name, however it was spelled."""
    sure = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri, open it"))
    hit = await sure.wake(b"RIFF fake wav")
    check(hit["matched"], "a confident candidate still wakes her")
    check(hit["confidence"] == 1.0, f"the confidence comes back: {hit['confidence']}")

    unsure = VoiceService(
        config, ToneEngine(config), stt=WakeStt("Capri, open it", confidence=0.2)
    )
    rejected = await unsure.wake(b"RIFF fake wav")
    check(not rejected["matched"], "an uncertain candidate does not wake her")
    check(
        rejected["reason"] == "low-confidence",
        f"the reason names confidence: {rejected['reason']!r}",
    )
    check(rejected["text"] == "Capri, open it", "the transcript is still reported for tuning")
    last = unsure.status()["wake"]["lastRejection"]
    check(last is not None and last["reason"] == "low-confidence", f"status names the last rejection: {last}")
    check(last is not None and last["confidence"] == 0.2, "the rejection keeps the confidence")

    quiet = VoiceService(
        config, ToneEngine(config), stt=WakeStt("Capri, open it", no_speech_prob=0.9)
    )
    silent = await quiet.wake(b"RIFF fake wav")
    check(not silent["matched"], "a candidate that sounds like no speech is rejected")
    check(silent["reason"] == "no-speech", f"the reason names no-speech: {silent['reason']!r}")
    check(
        quiet.status()["wake"]["lastRejection"]["reason"] == "no-speech",
        "status names the silence rejection",
    )

    loose = config.model_copy(deep=True)
    loose.voice.wake.min_confidence = 0.1
    permissive = VoiceService(
        loose, ToneEngine(config), stt=WakeStt("Capri, open it", confidence=0.2)
    )
    check(
        (await permissive.wake(b"RIFF fake wav"))["matched"],
        "the threshold comes from config",
    )

    strict = config.model_copy(deep=True)
    strict.voice.wake.max_no_speech_prob = 0.95
    following = VoiceService(
        strict, ToneEngine(config), stt=WakeStt("Capri, open it", no_speech_prob=0.9)
    )
    check(
        (await following.wake(b"RIFF fake wav"))["matched"],
        "the silence limit comes from config",
    )

    check(config.voice.wake.min_confidence == 0.35, "confidence default is 0.35")
    check(config.voice.wake.max_no_speech_prob == 0.6, "no-speech default is 0.6")


def test_commands() -> None:
    """A command is the whole utterance, never the start of a longer request."""
    for text in ("yes", "Yes please", "yeah", "sure", "ok", "do it", "go ahead"):
        check(match_command(text) == "affirm", f"affirm: {text!r}")
    for text in ("no", "No thanks", "nope", "cancel that", "don't"):
        check(match_command(text) == "deny", f"deny: {text!r}")
    for text in ("stop", "STOP!", "stop talking", "quiet"):
        check(match_command(text) == "stop", f"stop: {text!r}")
    for text in ("never mind", "Never mind.", "forget it"):
        check(match_command(text) == "never_mind", f"never mind: {text!r}")

    for text in (
        "stop the server",
        "yes, use the other file",
        "no thanks I'm fine",
        "don't touch the build folder",
        "cancel the download and retry",
        "never mind the tests, run the build",
        "",
        "Capri",
    ):
        check(match_command(text) is None, f"a request, not a command: {text!r}")


async def test_wake_intent(config: CapriConfig) -> None:
    """`voice.wake` reports the intent and still returns the request text."""
    yes = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri, yes"))
    hit = await yes.wake(b"RIFF fake wav")
    check(hit["matched"] and hit["intent"] == "affirm", f"yes carries an intent: {hit.get('intent')}")

    stop = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri, never mind"))
    check((await stop.wake(b"RIFF fake wav"))["intent"] == "never_mind", "never mind carries an intent")

    request = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri, stop the server"))
    answer = await request.wake(b"RIFF fake wav")
    check(answer["intent"] is None, "a longer sentence is not a command")
    check(answer["command"] == "stop the server", f"the request survives: {answer['command']!r}")

    bare = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri."))
    greeting = await bare.wake(b"RIFF fake wav")
    check(greeting["intent"] is None, "a bare name carries no intent")
    check(greeting["command"] == "", "and no request: it cannot become a turn")
    check(bool(greeting["reply"]), f"a bare name is answered with a line: {greeting.get('reply')!r}")
    check("fix" not in greeting["reply"].lower(), "the answer does not assume work")

    doubled = VoiceService(config, ToneEngine(config), stt=WakeStt("Hey, Capri, Capri."))
    repeated = await doubled.wake(b"RIFF fake wav")
    check(
        repeated["matched"] and repeated["command"] == "" and repeated["reply"],
        "a doubled name is still a greeting, not a request",
    )

    named = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri, open the terminal."))
    request_hit = await named.wake(b"RIFF fake wav")
    check(request_hit["reply"] == "", "a real request gets no greeting line")
    check(request_hit["command"] == "open the terminal", "and still carries the request")

    unsure = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri, yes", confidence=0.1))
    check((await unsure.wake(b"RIFF fake wav"))["intent"] is None, "a rejected candidate has no intent")


def test_engine_loads_once() -> None:
    """Four simultaneous warms must load the weights once, and none may hang."""
    from capri_core.voice.stt import FasterWhisperEngine

    stub = types.ModuleType("faster_whisper")
    stub.__spec__ = importlib.machinery.ModuleSpec("faster_whisper", loader=None)
    built: list[tuple[Any, ...]] = []

    def fake_model(*args: Any, **kwargs: Any) -> object:
        built.append(args)
        time.sleep(0.05)
        return object()

    stub.WhisperModel = fake_model  # type: ignore[attr-defined]
    previous = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = stub
    try:
        engine = FasterWhisperEngine(CapriConfig())
        threads = [threading.Thread(target=engine.warm, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        check(
            not any(thread.is_alive() for thread in threads),
            "four concurrent warms finish instead of blocking each other",
        )
        check(len(built) == 1, f"the weights load once, not per caller: {len(built)}")
        check(engine.loaded, "the engine reports loaded after warm")
        engine.warm()
        check(len(built) == 1, "warming again reuses the loaded weights")
    finally:
        if previous is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = previous


async def test_wake_rejection_trace(config: CapriConfig) -> None:
    """A dropped candidate leaves a trace row: why, and how sure."""
    conn = db.open_shard("test-wake-gate", dim=64)
    _CONNS.append(conn)
    stt = WakeStt("Capri, open it", confidence=0.1)
    service = VoiceService(config, ToneEngine(config), stt=stt, conn=conn)

    result = await service.wake(b"RIFF fake wav")
    check(result["reason"] == "low-confidence", "the gate rejected the candidate")
    rows = [
        row
        for row in db.recent_traces(conn, limit=10)
        if row["name"] == "voice.wake.rejected"
    ]
    check(len(rows) == 1, f"one rejection was traced: {len(rows)}")
    payload = json.loads(rows[0]["payload"]) if rows else {}
    check(payload.get("reason") == "low-confidence", f"the trace names the reason: {payload}")
    check(payload.get("confidence") == 0.1, f"the trace carries the confidence: {payload}")
    check(payload.get("noSpeechProb") == 0.0, "the trace carries the no-speech probability")
    check("text" not in payload, "the trace does not store what was overheard")

    stt.confidence = 1.0
    await service.wake(b"RIFF fake wav")
    again = [
        row
        for row in db.recent_traces(conn, limit=10)
        if row["name"] == "voice.wake.rejected"
    ]
    check(len(again) == 1, "a matched wake adds no rejection trace")
    check(
        service.status()["wake"]["lastRejection"]["reason"] == "low-confidence",
        "status keeps the last rejection after a later match",
    )


async def test_warm(config: CapriConfig) -> None:
    stt = WakeStt("hello")
    service = VoiceService(config, ToneEngine(config), stt=stt)
    result = await service.warm()
    check(stt.warmed == 1, "warm loads the recogniser")
    check(result["loaded"] is True, f"warm reports loaded: {result}")

    off = VoiceService(config, ToneEngine(config), stt=UnavailableStt())
    raised = False
    try:
        await off.warm()
    except SttUnavailable as exc:
        raised = "off" in str(exc)
    check(raised, "warm refuses with the STT engine's reason")

    from capri_core.voice.stt import FasterWhisperEngine

    engine = FasterWhisperEngine(config)
    check(engine.loaded is False, "an engine reports unloaded before warm")
    check(engine.status()["loaded"] is False, "status carries the loaded flag")


def _at_hour(hour: int) -> float:
    """A timestamp whose local hour is `hour`, without depending on DST."""
    stamp = time.time()
    for _ in range(48):
        if time.localtime(stamp).tm_hour == hour:
            return stamp
        stamp -= 1800
    return stamp


def test_wake_matching() -> None:
    hit = match_wake("Capri, open the terminal.")
    check(hit.matched, "her name at the start wakes")
    check(hit.command == "open the terminal", f"the request survives the name: {hit.command!r}")

    check(match_wake("hey capri what time is it").command == "what time is it", "a lead-in is ignored")
    bare = match_wake("Capri.")
    check(bare.matched and bare.command == "", "a bare name wakes with no request")
    check(bare.bare, "and is flagged as only her name")
    addressed = match_wake("Hey, Capri.")
    check(
        addressed.matched and addressed.command == "" and addressed.bare,
        "the recogniser's comma does not turn her name into a request",
    )
    doubled = match_wake("Hey, Capri, Capri")
    check(doubled.bare, "a doubled name is still only her name")
    check(not match_wake("Capri, open the terminal.").bare, "a request after the name is not bare")
    check(
        address_only("hey capri") and not address_only("capri, open it"),
        "address_only separates a greeting from an instruction",
    )

    line = acknowledge()
    check(0 < len(line) <= 40, f"the answer to a bare name is one short line: {line!r}")
    check("fix" not in line.lower(), "and it never assumes there is something to fix")
    for hour in (3, 12, 21):
        check(len(acknowledge(_at_hour(hour))) <= 40, f"hour {hour} still answers in one line")
    check(not match_wake("I told capri to stop").matched, "a name mid-sentence does not wake")
    check(match_wake("capree, play something").matched, "whisper's spelling of the name wakes")
    check(match_wake("kapri, lights").matched, "a K spelling wakes")
    check(match_wake("Capry are you there").matched, "a one-letter misspelling wakes")
    check(not match_wake("").matched, "silence does not wake")
    check(not match_wake("okay").matched, "filler alone does not wake")
    check(not match_wake("cappuccino please").matched, "an unrelated word does not wake")
    check(not match_wake("capricious").matched, "a word that only starts alike does not wake")


def _wav_bytes(samples: Any, rate: int = 16_000) -> bytes:
    import numpy as np

    pcm = (np.clip(np.asarray(samples, dtype=np.float64), -1, 1) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _tone(seconds: float, level: float, rate: int = 16_000) -> Any:
    import numpy as np

    count = int(rate * seconds)
    return level * np.sin(np.arange(count) * 0.3)


def _take(voiced_s: float, level: float, tail_s: float = 1.1) -> bytes:
    import numpy as np

    return _wav_bytes(np.concatenate([_tone(voiced_s, level), _tone(tail_s, 0.0)]))


def test_calibration() -> None:
    """The gate's numbers come from a room and a voice, not from a guess."""
    room = _wav_bytes(_tone(4.0, 0.02))
    takes = [_take(0.7, 0.3) for _ in range(3)]
    measured = measure([room], takes)

    check(
        0.0 < measured.noise_floor < measured.threshold_rms < measured.speech_level,
        f"the threshold sits between room and voice: {measured.as_dict()}",
    )
    check(
        abs(measured.noise_floor - 0.0141) <= 0.002,
        f"the floor is the room's loud end: {measured.noise_floor}",
    )
    check(
        abs(measured.speech_level - 0.2121) <= 0.01,
        f"the voice level is the take's middle: {measured.speech_level}",
    )
    check(
        measured.threshold_rms == round(measured.threshold_rms, 4),
        "the threshold is a plain number, not a float tail",
    )
    check(
        measured.takes_ms == [700, 700, 700],
        f"each take's voiced length is measured: {measured.takes_ms}",
    )
    check(
        measured.min_utterance_ms == 350,
        f"the shortest utterance is half a take: {measured.min_utterance_ms}",
    )
    check(measured.noise_ms == 4000, f"the room recording is measured in full: {measured.noise_ms}")

    payload = measured.as_dict()
    check(
        payload["thresholdRms"] == measured.threshold_rms and "noiseFloor" in payload,
        "the result is JSON-shaped for the shell",
    )

    mismatch = ""
    try:
        measure([room], [_take(0.7, 0.01)])
    except SttUnavailable as exc:
        mismatch = str(exc)
    check("not louder than the room" in mismatch, f"a take below the room is refused: {mismatch}")

    missing = ""
    try:
        measure([], takes)
    except SttUnavailable as exc:
        missing = str(exc)
    check("recording of the room" in missing, f"no room recording is refused: {missing}")

    missing_takes = ""
    try:
        measure([room], [])
    except SttUnavailable as exc:
        missing_takes = str(exc)
    check('spoken "Capri"' in missing_takes, f"no takes are refused: {missing_takes}")

    # A loud room must not push the threshold past what a voice can reach.
    loud = measure([_wav_bytes(_tone(4.0, 0.13))], [_take(0.7, 0.3)])
    check(loud.threshold_rms <= 0.09, f"the threshold is capped: {loud.threshold_rms}")

    short = measure([room], [_take(0.04, 0.9)])
    check(
        short.min_utterance_ms == 120,
        f"a very short take keeps a usable minimum: {short.min_utterance_ms}",
    )

    # The recogniser gate: below the worst take, inside a usable band.
    check(
        confidence_gate([0.8, 0.7, 0.75]) == 0.49,
        f"the gate sits below the worst take: {confidence_gate([0.8, 0.7, 0.75])}",
    )
    check(confidence_gate([0.99]) == 0.6, "a confident voice does not push the gate past 0.6")
    check(confidence_gate([0.1]) == 0.15, "a quiet mumble keeps the gate at the floor")
    check(
        confidence_gate([0.5]) == round(0.5 * 0.7, 2),
        "the margin is proportional, not a fixed subtraction",
    )

    # Barge-in: the room can raise its gate, never lower it.
    check(
        abs(measured.noise_tail - 0.0141) <= 0.001,
        f"the room's transient tail is measured: {measured.noise_tail}",
    )
    check(
        barge_threshold(0.0035, floor=0.018) == 0.018,
        "a quiet room leaves the barge gate alone",
    )
    check(
        barge_threshold(0.02, floor=0.018) == 0.06,
        f"a room with louder transients raises it: {barge_threshold(0.02, floor=0.018)}",
    )
    check(barge_threshold(0.05, floor=0.018) == 0.09, "the derived gate is capped")
    check(
        barge_threshold(0.05, floor=0.2) == 0.2,
        "a deliberately high setting is never lowered by calibration",
    )


async def test_follow_up(config: CapriConfig) -> None:
    """An open window takes the next sentence, but only if it is a sentence."""
    conn = db.open_shard("test-follow-up", dim=64)
    _CONNS.append(conn)
    service = VoiceService(
        config, ToneEngine(config), stt=WakeStt("and open the notes", confidence=0.9), conn=conn
    )
    hit = await service.follow_up(b"RIFF fake wav")
    check(hit["accepted"], f"a clear sentence is accepted: {hit.get('reason')}")
    check(hit["text"] == "and open the notes", "the sentence comes back without her name")
    check(not hit["bare"], "an ordinary sentence is not just her name")
    check(hit["reply"] == "", "so it gets no greeting line")

    # Her name again inside an open window is still a greeting, not a request.
    named = VoiceService(config, ToneEngine(config), stt=WakeStt("Hey, Capri."), conn=conn)
    again = await named.follow_up(b"RIFF fake wav")
    check(again["accepted"], "a repeated name is heard")
    check(again["bare"], "and flagged as only her name")
    check(bool(again["reply"]), f"and answered, not run: {again.get('reply')!r}")
    check(again["text"] == "Hey, Capri.", "the transcript is still reported")

    # The television cannot hold the floor: the same gate that guards a wake.
    tv = VoiceService(
        config, ToneEngine(config), stt=WakeStt("mumble mumble", confidence=0.1), conn=conn
    )
    refused = await tv.follow_up(b"RIFF fake wav")
    check(
        not refused["accepted"] and refused["reason"] == "low-confidence",
        f"an unsure follow-up is refused: {refused.get('reason')}",
    )
    check(
        tv.status()["wake"]["lastRejection"]["reason"] == "low-confidence",
        "the refusal is reported like a wake rejection",
    )
    rows = [
        row
        for row in db.recent_traces(conn, limit=10)
        if row["name"] == "voice.follow_up.rejected"
    ]
    check(len(rows) == 1, f"one follow-up refusal was traced: {len(rows)}")
    payload = json.loads(rows[0]["payload"]) if rows else {}
    check(payload.get("reason") == "low-confidence", f"the trace names the gate: {payload}")
    check("text" not in payload, "the trace does not store what was overheard")

    quiet = VoiceService(
        config,
        ToneEngine(config),
        stt=WakeStt("something", confidence=0.9, no_speech_prob=0.95),
        conn=conn,
    )
    silent = await quiet.follow_up(b"RIFF fake wav")
    check(
        not silent["accepted"] and silent["reason"] == "no-speech",
        f"a no-speech follow-up is refused: {silent.get('reason')}",
    )

    blank = VoiceService(config, ToneEngine(config), stt=WakeStt("   ", confidence=0.9))
    empty = await blank.follow_up(b"RIFF fake wav")
    check(
        not empty["accepted"] and empty["reason"] == "empty",
        f"a confident transcript with no words is still refused: {empty.get('reason')}",
    )

    off = config.model_copy(deep=True)
    off.voice.wake.enabled = False
    disabled = await VoiceService(off, ToneEngine(config), stt=WakeStt("hello")).follow_up(
        b"RIFF fake wav"
    )
    check(
        not disabled["accepted"] and disabled["reason"] == "disabled",
        "a disabled gate has no follow-up window",
    )

    raised = ""
    try:
        await VoiceService(config, ToneEngine(config), stt=UnavailableStt()).follow_up(
            b"RIFF fake wav"
        )
    except SttUnavailable as exc:
        raised = str(exc)
    check(bool(raised), f"a missing recogniser refuses the follow-up: {raised}")


async def test_calibration_confidence(config: CapriConfig) -> None:
    """The same takes also tell the recogniser gate where this voice sits."""
    room = _wav_bytes(_tone(4.0, 0.02))
    takes = [_take(0.7, 0.3) for _ in range(3)]

    heard = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri", confidence=0.8))
    measured = await heard.calibrate([room], takes)
    check(
        measured["minConfidence"] == 0.56,
        f"the takes set the confidence gate: {measured['minConfidence']}",
    )
    check(measured["thresholdRms"] > 0, "the energy gate is still measured in the same pass")
    check(
        measured["bargeThreshold"] == round(measured["noiseTail"] * 3, 4),
        f"the tone room raises the barge gate: {measured['bargeThreshold']}",
    )

    quiet_room = _wav_bytes(_tone(4.0, 0.002))
    calm = await heard.calibrate([quiet_room], takes)
    check(
        calm["bargeThreshold"] == config.voice.barge_threshold,
        f"a quiet room keeps the configured barge gate: {calm['bargeThreshold']}",
    )
    check(measured["minUtteranceMs"] == 350, "the length gate comes from the same takes")

    mute = VoiceService(config, ToneEngine(config), stt=UnavailableStt())
    check(
        (await mute.calibrate([room], takes))["minConfidence"] is None,
        "without a recogniser the confidence gate is left alone",
    )

    silent = VoiceService(config, ToneEngine(config), stt=WakeStt("   ", confidence=0.9))
    check(
        (await silent.calibrate([room], takes))["minConfidence"] is None,
        "takes that transcribe to nothing do not set a gate",
    )

    payload = await heard.calibrate([room], takes)
    check(payload["takesMs"] == [700, 700, 700], "the response still carries the takes")


async def test_wake_listen(config: CapriConfig) -> None:
    service = VoiceService(
        config, ToneEngine(config), stt=WakeStt("Capri, turn on the workshop lights")
    )
    result = await service.wake(b"RIFF fake wav")
    check(result["matched"], "voice.wake reports the match")
    check(
        result["command"] == "turn on the workshop lights",
        f"voice.wake returns the request: {result['command']!r}",
    )
    check(result["text"].startswith("Capri"), "the whole transcript comes back")
    check(service.status()["wake"]["enabled"], "voice.status reports the wake settings")
    check(service.status()["wake"]["thresholdRms"] == 0.012, "status carries the gate threshold")
    check(service.status()["wake"]["minUtteranceMs"] == 300, "status carries the shortest utterance")

    ordinary = VoiceService(config, ToneEngine(config), stt=WakeStt("hello there"))
    check(not (await ordinary.wake(b"RIFF fake wav"))["matched"], "an ordinary sentence does not wake")

    muted = config.model_copy(deep=True)
    muted.voice.wake.enabled = False
    quiet = VoiceService(muted, ToneEngine(config), stt=WakeStt("Capri, hello"))
    disabled = await quiet.wake(b"RIFF fake wav")
    check(
        disabled["matched"] is False and disabled["reason"] == "disabled",
        "a disabled wake does not listen at all",
    )


async def test_wake_barge(config: CapriConfig) -> None:
    """Only a sentence that actually opened with her name interrupts her."""
    ordinary = VoiceService(config, ToneEngine(config), stt=WakeStt("hello there"))
    sink = Sink(delay=0.05)
    answer = asyncio.create_task(
        ordinary.speak("trn_wake_1", "One. Two. Three. Four. Five.", sink)
    )
    await asyncio.sleep(0.08)
    candidate = await ordinary.wake(b"RIFF fake wav")
    check(not candidate["matched"], "the candidate was not for her")
    stats = await answer
    check(stats["reason"] == "done", f"an unmatched candidate leaves the reply alone: {stats['reason']}")

    matched = VoiceService(config, ToneEngine(config), stt=WakeStt("Capri, stop."))
    sink = Sink(delay=0.05)
    answer = asyncio.create_task(
        matched.speak("trn_wake_2", "One. Two. Three. Four. Five.", sink)
    )
    await asyncio.sleep(0.08)
    await matched.wake(b"RIFF fake wav")
    stats = await answer
    check(stats["reason"] == "barge-in", f"a matched wake interrupts the reply: {stats['reason']}")

    quiet = config.model_copy(deep=True)
    quiet.voice.barge_in = False
    no_barge = VoiceService(quiet, ToneEngine(config), stt=WakeStt("Capri, stop."))
    sink = Sink(delay=0.05)
    answer = asyncio.create_task(
        no_barge.speak("trn_wake_3", "One. Two. Three. Four. Five.", sink)
    )
    await asyncio.sleep(0.08)
    await no_barge.wake(b"RIFF fake wav")
    stats = await answer
    check(stats["reason"] == "done", f"voice.barge_in=false keeps the reply: {stats['reason']}")


async def test_stt(config: CapriConfig) -> None:
    service = VoiceService(config, ToneEngine(config), stt=FakeStt())
    result = await service.listen(b"RIFF fake wav")
    check(result["text"] == "turn on the workshop lights", "voice.listen returns a transcript")
    check(result["language"] == "en" and result["durationMs"] == 1400, "transcript metadata survives")
    check(result["confidence"] == 1.0, "listen reports the decoder's confidence")
    check(result["noSpeechProb"] == 0.0, "listen reports the no-speech probability")
    check(service.last_transcript == result, "last transcript is retained for diagnostics")
    check(service.status()["stt"]["available"], "voice.status reports the STT engine")

    off = VoiceService(config, ToneEngine(config), stt=UnavailableStt())
    raised = False
    try:
        await off.listen(b"RIFF fake wav")
    except SttUnavailable as exc:
        raised = "off" in str(exc)
    check(raised, "unavailable STT refuses with its reason")


def test_piper_engine(config: CapriConfig, tmp: Path) -> None:
    from capri_core.voice import build_engine
    from capri_core.voice.piper import PiperEngine

    config.voice.tts = "piper"
    config.voice.piper_voice = str(tmp / "missing.onnx")
    engine = build_engine(config)
    check(isinstance(engine, PiperEngine), "piper is a real engine, not a stub")
    check(not engine.available(), "piper reports missing voice files")
    check(
        "Piper voice files missing" in engine.reason(),
        f"piper names the missing files: {engine.reason()[:60]}",
    )


async def test_unavailable(config: CapriConfig, tmp: Path) -> None:
    config.voice.tts = "kokoro"
    config.voice.model_dir = str(tmp / "no-models")
    engine = KokoroEngine(config)
    check(not engine.available(), "kokoro reports unavailable without weights")
    check(bool(engine.reason()), f"reason is actionable: {engine.reason()[:60]}")

    service = VoiceService(config, engine)
    sink = Sink()
    stats = await service.speak("trn_v3", "This should not speak.", sink)
    check(stats["segments"] == 0, "unavailable engine emits nothing")
    check(not sink.events, "nothing is emitted when the engine is unavailable")


def test_clone_engine(config: CapriConfig, tmp: Path) -> None:
    from capri_core.voice import build_engine
    from capri_core.voice.clone import ChatterboxEngine, reference_path

    config.voice.tts = "chatterbox"
    config.voice.reference_clip = str(tmp / "my-clip.wav")
    engine = build_engine(config)
    check(isinstance(engine, ChatterboxEngine), "chatterbox is a real engine, not a stub")
    check(not engine.available(), "unavailable without chatterbox-tts")
    check("chatterbox-tts" in engine.reason(), f"reason names the package: {engine.reason()[:50]}")
    check(
        str(reference_path(config)).endswith("my-clip.wav"),
        f"reference path honours voice.reference_clip: {reference_path(config)}",
    )

    config.voice.reference_clip = ""
    default = reference_path(config)
    check(default.name == "capri-reference.wav", f"default reference name: {default.name}")
    check(default.parent.name == "chatterbox", "default reference lives under models/chatterbox")

    # A fresh engine, so it picks up the default path rather than the earlier one.
    engine = ChatterboxEngine(config)
    service = VoiceService(config, engine)
    status = service.status()
    check(status.get("reference") == str(default), "status reports the reference path")
    check(status.get("referenceReady") is False, "status says the reference is missing")


async def test_reference_guard(config: CapriConfig, tmp: Path) -> None:
    from capri_core.voice import TtsUnavailable
    from capri_core.voice.clone import render_reference

    config.voice.tts = "kokoro"
    config.voice.model_dir = str(tmp / "no-kokoro")
    raised = False
    message = ""
    try:
        render_reference(config)
    except TtsUnavailable as exc:
        raised = True
        message = str(exc)
    check(raised, "reference rendering refuses without Kokoro")
    check("Kokoro" in message, f"refusal explains why: {message[:60]}")


def test_pitch(config: CapriConfig) -> None:
    rate = 24_000
    lift = 2.0 ** (2.0 / 12.0)
    tone = np.sin(2 * np.pi * 200 * np.arange(rate) / rate).astype(np.float32)

    shifted = dsp.resample(tone, lift)
    check(abs(shifted.size - round(tone.size / lift)) <= 2, "resample length follows the ratio")
    check(
        abs(_fundamental(shifted) - 200 * lift) < 8,
        f"resample lifts 200 Hz to {_fundamental(shifted):.0f} Hz",
    )

    stretched = dsp.time_stretch(tone, 1.5)
    check(abs(stretched.size - round(tone.size * 1.5)) <= 2, "time stretch follows the ratio")
    check(
        abs(_fundamental(stretched) - 200) < 8,
        f"time stretch keeps 200 Hz at {_fundamental(stretched):.0f} Hz",
    )

    repo_shifted = dsp.resample_repo(tone, lift)
    check(
        abs(repo_shifted.size - round(tone.size / lift)) <= 2,
        "repo-resample length follows the pitch ratio",
    )
    check(
        abs(_fundamental(repo_shifted) - 200 * lift) < 8,
        f"repo resample lifts 200 Hz to {_fundamental(repo_shifted):.0f} Hz",
    )

    check(
        CapriConfig().voice.dsp.pitch == 2.0,
        "pitch starts at her tuned +2 semitones, not neutral",
    )

    class FakeGenerated:
        def __init__(self, samples: Any, sample_rate: int) -> None:
            self.samples = samples
            self.sample_rate = sample_rate

    class FakeSherpa:
        speed = 0.0
        sid = -1

        def generate(self, text: str, sid: int, speed: float):
            self.sid = sid
            self.speed = speed
            count = int(rate * 2.0 / speed)
            samples = np.sin(2 * np.pi * 200 * np.arange(count) / rate).astype(np.float32)
            return FakeGenerated(samples, rate)

    config.voice.dsp.pitch = 2.0
    engine = KokoroEngine(config)
    fake = FakeSherpa()
    engine._sherpa = fake  # type: ignore[attr-defined]
    audio = engine.synthesize("x", voice="capri", speed=1.2)

    # capri-desktop: style 1.1 x speed 1.2 / pitch lift, then a resample.
    check(fake.sid == 53, "engine asks for speaker 53 from her pack")
    check(abs(fake.speed - 1.1 * 1.2 / lift) < 1e-6, "engine asks for the pitch-compensated tempo")
    check(
        abs(audio.duration_ms - round(2000 / (1.1 * 1.2))) <= 20,
        f"style and slider speed multiply: {audio.duration_ms} ms",
    )
    check(
        abs(_fundamental(audio.samples) - 200 * lift) < 8,
        f"engine lifts 200 Hz to {_fundamental(audio.samples):.0f} Hz",
    )


def test_layers(config: CapriConfig) -> None:
    rate = 24_000
    cfg = config.voice.dsp
    cfg.bitcrush = 0.0
    cfg.radio = 0.0
    tone = (0.1 * np.sin(2 * np.pi * 200 * np.arange(rate) / rate)).astype(np.float32)

    defaults = CapriConfig().voice.dsp
    check(defaults.layers is True, "the two voice layers are on by default")
    check(
        (defaults.layer_a_pitch, defaults.layer_a_delay) == (2.0, 25)
        and (defaults.layer_b_pitch, defaults.layer_b_delay) == (-2.5, 25),
        "layer defaults match her preset",
    )
    check(defaults.layer_level == 0.4, "layers sit 8 dB down by default")

    cfg.layers = False
    check(
        np.array_equal(dsp.apply(tone, cfg, rate), tone),
        "layers off leaves the dry voice alone",
    )

    cfg.layers = True
    cfg.layer_a_pitch = 0.0
    cfg.layer_a_delay = 25
    cfg.layer_b_pitch = 0.0
    cfg.layer_b_delay = 0
    cfg.layer_level = 0.25
    mixed = dsp.apply(tone, cfg, rate)
    check(mixed.size == tone.size, "layers keep the duration")
    shift = 600
    expected = 1.25 * tone[shift:] + 0.25 * tone[:-shift]
    check(
        float(np.max(np.abs(mixed[shift:] - expected))) < 1e-6,
        "the 25 ms layer lands where it should",
    )

    # Spectral check: the copies must sit on their own semitone offsets, not
    # merely exist. Difference out the dry tone, then look for a peak near each
    # layer's expected fundamental.
    cfg.layer_a_pitch = 2.0
    cfg.layer_b_pitch = -2.5
    cfg.layer_a_delay = 0
    cfg.layer_b_delay = 0
    cfg.layer_level = 0.5
    layered = dsp.mix_layers(tone, cfg, rate)
    difference = layered - tone
    spectrum = np.abs(np.fft.rfft(difference * np.hanning(difference.size)))
    freqs = np.fft.rfftfreq(difference.size, 1 / rate)

    def peak_near(target: float) -> float:
        mask = (freqs > 120) & (freqs < 300) & (np.abs(freqs - target) <= 6)
        return float(freqs[mask][int(np.argmax(spectrum[mask]))])

    high = peak_near(200 * 2 ** (2 / 12))
    low = peak_near(200 * 2 ** (-2.5 / 12))
    check(abs(high - 224.5) <= 2.5, f"+2 semitone layer peaks at {high:.1f} Hz")
    check(abs(low - 173.1) <= 2.5, f"-2.5 semitone layer peaks at {low:.1f} Hz")


def test_robot(config: CapriConfig) -> None:
    rate = 24_000
    cfg = config.voice.dsp
    cfg.bitcrush = 0.0
    cfg.radio = 0.0
    cfg.layers = False
    tone = (0.2 * np.sin(2 * np.pi * 120 * np.arange(rate) / rate)).astype(np.float32)

    check(cfg.robot == 0.0, "robot is off by default")
    check(
        np.array_equal(dsp.apply(tone, cfg, rate), tone),
        "robot off leaves the dry voice alone",
    )

    cfg.robot = 1.0
    out = dsp.apply(tone, cfg, rate)
    check(out.size == tone.size, "robot keeps the duration")
    note = _fundamental(out)
    check(abs(note - 123.47) < 3, f"robot sings 120 Hz on the nearest note: {note:.1f} Hz")


def test_pocket_engine(config: CapriConfig, tmp: Path) -> None:
    import json as jsonlib
    import struct
    import zipfile

    from capri_core.voice import build_engine
    from capri_core.voice.pocket import (
        PocketEngine,
        install_voice_state,
        pocket_voice_path,
        state_info,
    )

    from capri_core.config import VoiceCfg

    check(VoiceCfg(tts="pocket").tts == "pocket", "config accepts the pocket engine")
    config.voice.tts = "pocket"
    config.voice.voice_file = ""
    config.voice.language = "en"
    engine = build_engine(config)
    check(isinstance(engine, PocketEngine), "pocket is a real engine, not a stub")
    check(not engine.available(), "unavailable without a voice state in this home")
    engine_reason = engine.reason()
    check(
        "pocket-tts" in engine_reason or "voice state" in engine_reason,
        f"reason is actionable: {engine_reason[:60]}",
    )
    default = pocket_voice_path(config)
    check(default.name == "capri_english.safetensors", f"default state name: {default.name}")
    check(default.parent.name == "pocket", "default state lives under models/pocket")

    header = {"w": {"dtype": "F32", "shape": [0], "data_offsets": [0, 0]}}
    blob = jsonlib.dumps(header).encode()
    data = struct.pack("<Q", len(blob)) + blob
    check(state_info(data)["tensors"] == 1, "counts tensors without torch")

    src = tmp / "voice.safetensors"
    src.write_bytes(data)
    config.voice.voice_file = str(tmp / "installed.safetensors")
    result = install_voice_state(src, config)
    check(Path(result["path"]) == tmp / "installed.safetensors", "installs a bare state file")

    mod = tmp / "mod.zip"
    with zipfile.ZipFile(mod, "w") as archive:
        archive.writestr("red4ext/plugins/x/CapriVoz/voces/capri_english.safetensors", data)
    config.voice.voice_file = str(tmp / "from-zip.safetensors")
    result = install_voice_state(mod, config)
    check(result["from"] == "capri_english.safetensors", "picks Capri's state out of a mod zip")
    check((tmp / "from-zip.safetensors").is_file(), "the state lands where config points")

    empty = tmp / "empty.zip"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("readme.txt", "nothing here")
    raised = False
    try:
        install_voice_state(empty, config)
    except ValueError:
        raised = True
    check(raised, "a zip without a voice state is refused")

    service = VoiceService(config, PocketEngine(config))
    status = service.status()
    check(status.get("voiceFile") == str(tmp / "from-zip.safetensors"), "status reports the state file")
    check(status.get("voiceFileReady") is True, "status sees the installed state")


async def test_loop(config: CapriConfig, workspace: Path, tmp: Path) -> None:
    conn = db.open_shard("test-voice", dim=64)
    _CONNS.append(conn)
    service = VoiceService(config, ToneEngine(config))
    sink = Sink()
    runner = TurnRunner(
        config=config,
        conn=conn,
        session=Session.create(conn),
        emit=sink,
        confirm=_always_true,
        brain=Brain(),
        registry=builtin_registry(),
        policy=Policy(config.policy, str(workspace)),
        workspace=str(workspace),
        voice=service,
    )
    await runner.run("trn_v4", "say something")

    kinds = [event.get("type") for event in sink.events]
    check("speech.segment" in kinds, "the turn speaks")
    check(kinds.index("agent.done") < kinds.index("speech.segment"), "speech follows the answer")
    check("speech.stop" in kinds, "speech is closed out")
    check(kinds[-1] == "agent.state", f"turn still ends on a state event: {kinds[-1]}")


async def main() -> int:
    raw = Path(tempfile.mkdtemp(prefix="capri-voice-"))
    try:
        workspace = raw / "workspace"
        workspace.mkdir()
        os.environ["CAPRI_HOME"] = str(raw / "home")
        config = config_for(workspace)

        print("sentences")
        test_sentences()
        print("visemes")
        test_visemes()
        print("wav")
        test_wav()
        print("dsp")
        test_dsp()
        print("tone engine")
        test_tone_engine()
        print("stream")
        await test_stream(VoiceService(config, ToneEngine(config)))
        print("cancel")
        await test_cancel(VoiceService(config, ToneEngine(config)))
        print("barge-in")
        await test_barge_in(VoiceService(config, ToneEngine(config)))
        print("aside vs reply")
        await test_aside_never_preempts(VoiceService(config, ToneEngine(config)))
        print("wake")
        test_wake_matching()
        await test_wake_listen(config_for(workspace))
        await test_wake_barge(config_for(workspace))
        print("wake confidence")
        await test_wake_confidence(config_for(workspace))
        print("commands")
        test_commands()
        await test_wake_intent(config_for(workspace))
        print("wake rejection trace")
        await test_wake_rejection_trace(config_for(workspace))
        print("calibration")
        test_calibration()
        await test_calibration_confidence(config_for(workspace))
        print("follow-up")
        await test_follow_up(config_for(workspace))
        print("warm")
        await test_warm(config_for(workspace))
        test_engine_loads_once()
        print("stt")
        await test_stt(config_for(workspace))
        print("piper")
        test_piper_engine(config_for(workspace), raw)
        print("unavailable")
        await test_unavailable(config_for(workspace), raw)
        print("clone")
        test_clone_engine(config_for(workspace), raw)
        print("reference guard")
        await test_reference_guard(config_for(workspace), raw)
        print("pocket")
        test_pocket_engine(config_for(workspace), raw)
        print("pitch")
        test_pitch(config_for(workspace))
        print("layers")
        test_layers(config_for(workspace))
        print("robot")
        test_robot(config_for(workspace))
        print("loop")
        await test_loop(config_for(workspace), workspace, raw)
    finally:
        for conn in _CONNS:
            try:
                conn.close()
            except Exception:
                pass
        shutil.rmtree(raw, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
