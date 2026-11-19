"""Streaming speech.

Sentences are synthesized ahead of the consumer, never all at once: a bounded
queue holds ``voice.queue_depth`` finished segments, so the first one is emitted
as soon as it exists and the daemon cannot run away synthesizing a long reply.

The renderer gets a WAV data URL plus viseme timings per segment. It plays audio
and moves the mouth; it never analyses audio itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable

from ..config import CapriConfig
from ..store import db
from . import dsp, wav as wav_mod
from .calibrate import barge_threshold, confidence_gate, measure
from .commands import match_command
from .stt import SttUnavailable, build_stt
from .tts import TtsEngine, TtsUnavailable, active_voice
from .visemes import DEFAULT_FPS, schedule
from .wake import acknowledge, address_only, match_wake

log = logging.getLogger("capri.voice")

Emit = Callable[[dict[str, Any]], Awaitable[None]]

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MARKUP = re.compile(r"[*_#>]+")
_SPACE = re.compile(r"\s+")
# A dash is not in the pack's lexicon: sherpa drops it, silently, and the clause
# runs on unpunctuated. A comma is what a spoken dash is.
_DASH = re.compile(r"\s*[\u2014\u2013]\s*|\s+-+\s+")
_HYPHEN = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")
# A hum is a sound, and the engine has no word for it: it reads "Mm" as the name
# Emma, and "Hm" close enough to it. So it never reaches the engine at all.
_HUM = re.compile(r"\b(?:[hH]+[mM]+|[mM]{2,}[hH]*[mM]*)\b")
# A quote is not something the engine says, so it can never be the reason a hum
# leaves a mark: it stays where it is, and a quote that held only the hum goes
# with it. Escaped, like the dashes above, to keep the source ASCII.
_QUOTES = "\"'\u201c\u201d\u2018\u2019"
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE = re.compile(r"(?<=[;:,])\s+")
# A fragment that ends on a letter, a digit or a closing quote is not closed.
_VOICED_END = re.compile(r"[A-Za-z0-9\"')\]]$")

def _drop_hums(text: str) -> str:
    """Take the hum out, with the punctuation that belonged to the hum.

    "Hmm, that's odd." says "That's odd.": the comma goes with the hum, and the
    clause keeps the capital it needs to start a sentence. Quote characters are
    silent, so they stay where they were: '"Hmm," she said.' keeps one pair, not
    the '\"\"' of a hum taken out from between them. "Mm." alone says nothing at
    all, so a reply that is only a hum is not spoken.
    """
    out = text
    while True:
        match = _HUM.search(out)
        if match is None:
            return out
        before = out[: match.start()].rstrip()
        # The comma, stop or dash after a hum was the hum's own phrase break.
        after = out[match.end() :].lstrip(" \t,;:-\u2014\u2013.!?\u2026")
        head = before.rstrip(_QUOTES).rstrip()
        # A quoted hum was the whole quote: there is nothing left to quote.
        quoted = len(head) < len(before) and after[:1] in _QUOTES
        if quoted:
            before = head.rstrip(" \t,;:-\u2014\u2013")
            after = after[1:].lstrip()
        elif not after:
            before = before.rstrip(_QUOTES).rstrip(" \t,;:-\u2014\u2013").rstrip(_QUOTES)
        if not head or head[-1] in ".!?\u2026":
            after = after[:1].upper() + after[1:] if after else after
        joiner = " " if before and after and after[0].isalnum() and before[-1] not in _QUOTES else ""
        out = f"{before}{joiner}{after}"


def clean_for_speech(text: str) -> str:
    """Drop what should not be read aloud: fences, markup, excess whitespace."""
    without_fences = _FENCE.sub(" ", text or "")
    without_code = _INLINE_CODE.sub(r"\1", without_fences)
    joined = _HYPHEN.sub(" ", _DASH.sub(", ", without_code))
    plain = _MARKUP.sub("", _drop_hums(joined))
    return _SPACE.sub(" ", plain).strip()


def _spoken(sentence: str) -> str:
    """The text handed to the engine.

    A fragment with no terminal stop is what a clipped tail sounds like: the
    engine has nothing to close the contour on and takes the last phoneme with
    it. A clause break (a trailing comma from ``_wrap``) is deliberate and is
    left alone, so the intonation still rises into the next segment.
    """
    sentence = sentence.strip()
    if sentence and _VOICED_END.search(sentence):
        return sentence + "."
    return sentence


def _wrap(sentence: str, limit: int) -> list[str]:
    if len(sentence) <= limit:
        return [sentence]

    out: list[str] = []
    current = ""
    for piece in _CLAUSE.split(sentence):
        candidate = f"{current} {piece}".strip()
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            out.append(current)
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        current = piece
    if current:
        out.append(current)
    return [item for item in out if item]


def split_sentences(text: str, max_chars: int = 240) -> list[str]:
    cleaned = clean_for_speech(text)
    if not cleaned:
        return []
    out: list[str] = []
    for sentence in _SENTENCE_END.split(cleaned):
        sentence = sentence.strip()
        if sentence:
            out.extend(_wrap(sentence, max(40, max_chars)))
    return out


class VoiceService:
    def __init__(
        self,
        config: CapriConfig,
        engine: TtsEngine,
        *,
        conn: sqlite3.Connection | None = None,
        stt: Any = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.conn = conn
        self.stt = stt if stt is not None else build_stt(config)
        self._stop = asyncio.Event()
        self._stop_reason = "cancel"
        self._busy = False
        self._active_done: asyncio.Event | None = None
        self.last: dict[str, Any] = {}
        self.last_transcript: dict[str, Any] = {}
        self.last_rejection: dict[str, Any] | None = None

    async def aclose(self) -> None:
        await self.stop()
        close = getattr(self.engine, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "engine": self.engine.name,
            "available": self.engine.available(),
            "reason": self.engine.reason(),
            "sampleRate": self.engine.sample_rate,
            "voice": active_voice(self.config),
            "speed": self.config.voice.speed,
            "queueDepth": self.config.voice.queue_depth,
            "visemeFps": self.config.voice.viseme_fps,
            "voices": self.engine.voices() if self.engine.available() else [],
            "bargeIn": self.config.voice.barge_in,
            "bargeThreshold": self.config.voice.barge_threshold,
            "bargeHoldMs": self.config.voice.barge_hold_ms,
            "wake": {
                "enabled": self.config.voice.wake.enabled,
                "silenceMs": self.config.voice.wake.silence_ms,
                "nameGraceMs": self.config.voice.wake.name_grace_ms,
                "followUpMs": self.config.voice.wake.follow_up_ms,
                "thresholdRms": self.config.voice.wake.threshold_rms,
                "minUtteranceMs": self.config.voice.wake.min_utterance_ms,
                "lastRejection": self.last_rejection,
            },
            "stt": self.stt.status() if self.stt is not None else {"available": False},
        }
        for attribute, key in (("reference", "reference"), ("voice_file", "voiceFile")):
            path = getattr(self.engine, attribute, None)
            if path is not None:
                payload[key] = str(path)
                payload[key + "Ready"] = bool(getattr(path, "is_file", lambda: False)())
        return payload

    # ------------------------------------------------------------------ speech

    async def speak(
        self,
        turn_id: str,
        text: str,
        emit: Emit,
        *,
        emotion: str = "neutral",
        preempt: bool = True,
    ) -> dict[str, Any]:
        if not self.config.voice.enabled:
            return {"segments": 0, "reason": "disabled"}

        reason = self.engine.reason()
        if not self.engine.available():
            log.warning("speech skipped: %s", reason)
            return {"segments": 0, "reason": reason}

        if self._busy:
            # A turn's own reply preempts an aside; an aside never preempts the
            # reply. The companion passes preempt=False, so a line that was
            # written during a turn cannot cut off the answer it missed.
            if not preempt:
                return {"segments": 0, "reason": "busy"}
            await self.stop()

        sentences = split_sentences(text, self.config.voice.sentence_max_chars)
        if not sentences:
            return {"segments": 0, "reason": "nothing to say"}

        # Each run owns its stop event. `stop()` sets the current one; a run
        # that was preempted keeps its own, already-set event and winds down,
        # instead of watching a flag the next run has just cleared.
        stop_event = asyncio.Event()
        run_done = asyncio.Event()
        self._stop = stop_event
        self._active_done = run_done
        self._busy = True
        self._stop_reason = "cancel"
        depth = max(1, self.config.voice.queue_depth)
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=depth)
        stats: dict[str, Any] = {
            "segments": 0,
            "sentences": len(sentences),
            "chars": sum(len(sentence) for sentence in sentences),
            "queueDepth": depth,
            "peakQueue": 0,
            "ms": 0,
            "firstSegmentMs": 0,
            "reason": "done",
        }
        started = time.monotonic()

        async def offer(item: Any) -> bool:
            """Put unless stopped; a full queue is backpressure, not a deadlock."""
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(queue.put(item), timeout=0.1)
                    return True
                except asyncio.TimeoutError:
                    continue
            return False

        async def produce() -> None:
            try:
                for index, sentence in enumerate(sentences):
                    if stop_event.is_set():
                        break
                    try:
                        audio = await asyncio.to_thread(
                            self._synthesize, _spoken(sentence)
                        )
                    except TtsUnavailable as exc:
                        log.warning("segment %d failed: %s", index, exc)
                        break
                    if not await offer((index, sentence, audio)):
                        break
                    stats["peakQueue"] = max(stats["peakQueue"], queue.qsize())
            finally:
                await offer(None)

        async def consume() -> None:
            while True:
                try:
                    # A short poll keeps barge-in from waiting behind a long
                    # queue timeout; the producer still has 100 ms slots.
                    item = await asyncio.wait_for(queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    if stop_event.is_set():
                        return
                    continue
                if item is None:
                    return
                if stop_event.is_set():
                    return
                index, sentence, audio = item
                # Pitch and the layers are numpy over the whole segment (~200 ms
                # with layers on). On the loop that stalls every event, the
                # barge-in poll and any pending confirmation behind her voice.
                samples = await asyncio.to_thread(
                    dsp.apply, audio.samples, self.config.voice.dsp, audio.sample_rate
                )
                payload = wav_mod.to_data_url(wav_mod.to_wav(samples, audio.sample_rate))
                if not stats["firstSegmentMs"]:
                    # A sub-millisecond segment is still a produced segment;
                    # report 1 ms so zero remains "not measured yet".
                    stats["firstSegmentMs"] = max(
                        1, int((time.monotonic() - started) * 1000)
                    )
                visemes = schedule(
                    sentence,
                    audio.duration_ms,
                    fps=self.config.voice.viseme_fps or DEFAULT_FPS,
                )
                await emit(
                    {
                        "type": "speech.segment",
                        "turnId": turn_id,
                        "seq": index,
                        "text": sentence,
                        "audioUrl": payload,
                        "sampleRate": audio.sample_rate,
                        "visemes": visemes,
                        "emotion": emotion,
                        "last": index == len(sentences) - 1,
                    }
                )
                stats["segments"] = index + 1

        producer = asyncio.create_task(produce())
        consumer = asyncio.create_task(consume())
        try:
            try:
                await asyncio.gather(producer, consumer)
            except asyncio.CancelledError:
                producer.cancel()
                consumer.cancel()
                raise
            finally:
                cancelled = stop_event.is_set()
                # Only the run that still owns the flag may clear it: a preempted
                # run must not hand a replacement stream a stale "not busy".
                if self._stop is stop_event:
                    self._busy = False

            stats["ms"] = int((time.monotonic() - started) * 1000)
            stop_reason = self._stop_reason if cancelled else "done"
            stats["reason"] = "cancelled" if stop_reason == "cancel" else stop_reason
            await emit({"type": "speech.stop", "reason": stop_reason})
        finally:
            run_done.set()
            if self._active_done is run_done:
                self._active_done = None
        if self.conn is not None:
            db.trace(
                self.conn,
                span="voice",
                name="voice.speak",
                turn_id=turn_id,
                payload={
                    "engine": self.engine.name,
                    "segments": stats["segments"],
                    "chars": stats["chars"],
                    "ms": stats["ms"],
                },
            )
        self.last = stats
        return stats

    async def stop(self, reason: str = "cancel") -> None:
        """Ask the active stream to stop. ``cancel`` and ``barge-in`` differ only
        in the reason the renderer and trace receive."""
        self._stop_reason = reason if reason in ("cancel", "barge-in") else "cancel"
        self._stop.set()
        done = self._active_done
        if done is not None and not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(0)  # let the producer/consumer notice

    async def barge_in(self) -> bool:
        """Stop speaking because the user started talking. No-op when idle."""
        if not self._busy:
            return False
        await self.stop("barge-in")
        return True

    # ------------------------------------------------------------------ hearing

    def _require_stt(self) -> None:
        if self.stt is None or not self.stt.available():
            reason = self.stt.reason() if self.stt is not None else "speech input is not configured"
            raise SttUnavailable(reason)

    async def _recognize(
        self, audio: bytes, *, language: str | None = None
    ) -> dict[str, Any]:
        """One WAV in, one transcript out.

        No effect on her speech: whether hearing this should interrupt her is
        the caller's decision, and a wake candidate has not been judged yet.
        """
        started = time.monotonic()
        transcript = await asyncio.to_thread(self.stt.transcribe, audio, language=language)
        result = {
            "text": transcript.text,
            "language": transcript.language,
            "languageProbability": transcript.language_probability,
            "durationMs": transcript.duration_ms,
            "segments": transcript.segments,
            "confidence": round(transcript.confidence, 4),
            "noSpeechProb": round(transcript.no_speech_prob, 4),
            "ms": int((time.monotonic() - started) * 1000),
        }
        self.last_transcript = result
        if self.conn is not None:
            db.trace(
                self.conn,
                span="voice",
                name="voice.listen",
                payload={
                    "engine": getattr(self.stt, "name", "stt"),
                    "chars": len(transcript.text),
                    "durationMs": transcript.duration_ms,
                    "ms": result["ms"],
                },
            )
        return result

    async def warm(self) -> dict[str, Any]:
        """Load the recogniser now instead of inside the first utterance."""
        self._require_stt()
        await asyncio.to_thread(self.stt.warm)
        return {"loaded": True}

    async def prewarm(self) -> dict[str, Any]:
        """Load the speech engine off the event loop, before anyone speaks.

        Pocket takes ~11 s to load its base model and her voice state on this
        CPU. At startup that is invisible; inside her first reply it is a
        silence the user hears. An engine that refuses, or a voice that is
        switched off, is left alone: a daemon may never speak at all.
        """
        if not self.config.voice.enabled:
            return {"loaded": False, "reason": "voice disabled"}
        reason = self.engine.reason()
        if reason:
            log.info("speech warm-up skipped: %s", reason)
            return {"loaded": False, "reason": reason}
        loader = getattr(self.engine, "load", None)
        if not callable(loader):
            return {"loaded": False, "reason": f"{self.engine.name} loads with its first line"}
        started = time.monotonic()
        try:
            await asyncio.to_thread(loader)
        except Exception as exc:  # a bad model must not take the daemon down
            log.warning("speech warm-up failed: %s", exc)
            return {"loaded": False, "reason": str(exc)}
        ms = int((time.monotonic() - started) * 1000)
        log.info("%s warm in %d ms", self.engine.name, ms)
        return {"loaded": True, "engine": self.engine.name, "ms": ms}

    async def calibrate(self, noise: list[bytes], speech: list[bytes]) -> dict[str, Any]:
        """Measure the room, the voice, and what the recogniser makes of it.

        The energy gate is arithmetic; the confidence gate depends on how this
        decoder hears this person through this microphone, so the takes are
        transcribed too. A missing or failing recogniser leaves the confidence
        limit alone instead of failing the whole calibration — the room and
        length measurements are still worth having.
        """
        measured = await asyncio.to_thread(measure, noise, speech)
        payload = measured.as_dict()
        payload["bargeThreshold"] = barge_threshold(
            measured.noise_tail, floor=self.config.voice.barge_threshold
        )
        payload["minConfidence"] = None
        if self.stt is not None and self.stt.available():
            payload["minConfidence"] = await asyncio.to_thread(
                self._confidence_gate_for, speech
            )
        return payload

    def _confidence_gate_for(self, takes: list[bytes]) -> float | None:
        levels: list[float] = []
        for take in takes:
            try:
                transcript = self.stt.transcribe(take)
            except SttUnavailable:
                return None
            if transcript.text.strip():
                levels.append(transcript.confidence)
        return confidence_gate(levels) if levels else None

    async def listen(
        self, audio: bytes, *, language: str | None = None
    ) -> dict[str, Any]:
        """Transcribe one push-to-talk WAV. The caller owns the microphone."""
        self._require_stt()
        # The watchword here is the button: pressing it means "stop talking".
        if self._busy:
            await self.barge_in()
        return await self._recognize(audio, language=language)

    async def wake(
        self, audio: bytes, *, language: str | None = None
    ) -> dict[str, Any]:
        """Transcribe a wake candidate and cut the request off the front.

        The microphone is the caller's; this is the same recogniser as
        push-to-talk with her name checked at the start of the sentence.
        """
        wake = self.config.voice.wake
        if not wake.enabled:
            return {"matched": False, "command": "", "intent": None, "reason": "disabled"}
        self._require_stt()
        # Every sentence the microphone hears is a candidate, so most of them
        # are not for her. Transcribe first and only then decide: barging on a
        # candidate would stop her reply for someone else's conversation.
        result = await self._recognize(audio, language=language)
        if result["confidence"] < wake.min_confidence:
            return self._reject_wake(result, "low-confidence")
        if result["noSpeechProb"] > wake.max_no_speech_prob:
            return self._reject_wake(result, "no-speech")
        hit = match_wake(result["text"])
        if hit.matched and self.config.voice.barge_in:
            await self.barge_in()
        # A name on its own is answered, not executed. The reply is a line she
        # says; the command stays empty, so nothing downstream can turn her name
        # into a turn. What follows the name may still not be a request at all:
        # "yes", "stop" carry an intent instead.
        reply = acknowledge() if hit.matched and hit.bare else ""
        return {
            **result,
            "matched": hit.matched,
            "command": "" if hit.bare else hit.command,
            "intent": match_command(hit.command) if hit.matched and not hit.bare else None,
            "reply": reply,
        }

    async def follow_up(
        self, audio: bytes, *, language: str | None = None
    ) -> dict[str, Any]:
        """One sentence said on an open follow-up window: her name is not needed.

        The window itself is the renderer's, because it owns the microphone.
        What is not the renderer's is the judgement: the same confidence and
        no-speech gate decides whether a sound is a sentence at all, so an open
        window cannot become a conversation with the television. A refusal is
        final for the caller — there is nothing here to retry.
        """
        wake = self.config.voice.wake
        if not wake.enabled:
            return {"accepted": False, "reason": "disabled"}
        self._require_stt()
        result = await self._recognize(audio, language=language)
        if result["confidence"] < wake.min_confidence:
            return self._reject_follow_up(result, "low-confidence")
        if result["noSpeechProb"] > wake.max_no_speech_prob:
            return self._reject_follow_up(result, "no-speech")
        if not str(result.get("text") or "").strip():
            return self._reject_follow_up(result, "empty")
        # Saying her name again is not a request, and an open window does not
        # make it one: it is the same greeting, answered the same way.
        bare = address_only(result["text"])
        return {
            **result,
            "accepted": True,
            "bare": bare,
            "reply": acknowledge() if bare else "",
        }

    @staticmethod
    def _rejection(result: dict[str, Any], reason: str) -> dict[str, Any]:
        """What the gate saw, without what it heard: a trace is not a recording.

        `chars` is the length of the transcript, not the words. It is the one
        fact that separates "she never heard the rest of the sentence" from
        "she heard it and the gate said no", which is otherwise unanswerable
        once the text is discarded.
        """
        text = result.get("text")
        return {
            "reason": reason,
            "confidence": result.get("confidence"),
            "noSpeechProb": result.get("noSpeechProb"),
            "durationMs": result.get("durationMs"),
            "chars": len(text) if isinstance(text, str) else 0,
            "ms": result.get("ms"),
            "at": int(time.time()),
        }

    def _reject_wake(self, result: dict[str, Any], reason: str) -> dict[str, Any]:
        """Record why a candidate was dropped.

        The transcript is discarded precisely when the gate fires, so without
        this "she ignored me" has no answer: nothing says whether the decoder
        was unsure or heard no speech at all.
        """
        rejection = self._rejection(result, reason)
        self.last_rejection = rejection
        log.debug("wake candidate rejected (%s): %s", reason, rejection)
        if self.conn is not None:
            db.trace(self.conn, span="voice", name="voice.wake.rejected", payload=rejection)
        return {**result, "matched": False, "command": "", "intent": None, "reason": reason}

    def _reject_follow_up(self, result: dict[str, Any], reason: str) -> dict[str, Any]:
        """A sentence refused inside a follow-up window; it does not hold the floor."""
        rejection = self._rejection(result, reason)
        self.last_rejection = rejection
        log.debug("follow-up rejected (%s): %s", reason, rejection)
        if self.conn is not None:
            db.trace(self.conn, span="voice", name="voice.follow_up.rejected", payload=rejection)
        return {**result, "accepted": False, "reason": reason}

    def _synthesize(self, sentence: str):
        return self.engine.synthesize(
            sentence,
            voice=active_voice(self.config),
            speed=self.config.voice.speed,
        )
