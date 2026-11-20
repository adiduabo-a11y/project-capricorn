"""Speech input: WAV in, text out.

Recognition is optional for the same reason synthesis is: the default install
should not pull a model runtime and weights just to boot the daemon. The engine
is lazy, so ``voice.status`` answers immediately and a missing package or model
is reported as a sentence rather than a stack trace.

The browser records push-to-talk audio and sends a mono WAV data URL. Faster
Whisper's Silero VAD trims silence before decoding, so a held button that starts
a little early does not turn into hallucinated words.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import math
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .. import paths
from ..config import CapriConfig

log = logging.getLogger("capri.voice.stt")

DEFAULT_MODEL = "small"
DEFAULT_COMPUTE = "int8"
MAX_AUDIO_S = 120.0


class SttUnavailable(RuntimeError):
    """Recognition cannot run: missing package, model, or invalid audio."""


@dataclass
class Transcript:
    text: str
    language: str = ""
    language_probability: float = 0.0
    duration_ms: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)
    # Mean exp(avg_logprob) over the segments that carried words. 0.0 when
    # nothing was heard, so an empty transcript is never "confident".
    confidence: float = 1.0
    # The strongest "there was no speech here" the decoder reported.
    no_speech_prob: float = 0.0


def model_dir(config: CapriConfig) -> Path:
    raw = (config.voice.stt_model_dir or "").strip()
    return Path(raw).expanduser() if raw else paths.models_dir() / "whisper"


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode the mono/stereo PCM WAV the renderer sends into float32 mono."""
    if not data:
        raise SttUnavailable("the recording was empty")
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = max(1, handle.getnchannels())
            width = handle.getsampwidth()
            rate = int(handle.getframerate())
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise SttUnavailable("the recording was not a readable WAV") from exc
    if not frames:
        raise SttUnavailable("the recording contained no audio")
    if rate < 8_000 or rate > 96_000:
        raise SttUnavailable(f"unsupported sample rate: {rate}")

    if width == 1:
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        samples = values.astype(np.float32) / 8388608.0
    elif width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise SttUnavailable(f"unsupported WAV sample width: {width} bytes")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    duration = samples.size / max(1, rate)
    if duration < 0.12:
        raise SttUnavailable("the recording was too short")
    if duration > MAX_AUDIO_S:
        raise SttUnavailable(f"the recording is longer than {MAX_AUDIO_S:.0f}s")
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-4:
        raise SttUnavailable("the recording was silent")
    return samples, rate


class FasterWhisperEngine:
    """faster-whisper on CPU. ``small`` + int8 is the interactive default."""

    name = "faster-whisper"

    def __init__(self, config: CapriConfig) -> None:
        self.config = config
        self.directory = model_dir(config)
        self.model_name = _model_name(config.voice.stt)
        self.compute_type = config.voice.stt_compute_type or DEFAULT_COMPUTE
        self._model: object | None = None
        self._error: str | None = None
        # The wake gate warms the weights in a worker thread while a wake RPC
        # may already be transcribing; one load per process, not two.
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()

    def available(self) -> bool:
        return not self.reason() and self._error is None

    def reason(self) -> str:
        if self._error:
            return self._error
        if importlib.util.find_spec("faster_whisper") is None:
            return "faster-whisper is not installed: uv sync --package capri-core --extra stt"
        return ""

    def status(self) -> dict[str, Any]:
        return {
            "engine": self.name,
            "model": self.model_name,
            "computeType": self.compute_type,
            "available": self.available(),
            "reason": self.reason(),
            "loaded": self.loaded,
            "modelDir": str(self.directory),
            "vad": True,
        }

    @property
    def model_path(self) -> Path:
        return self.directory / self.model_name

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warm(self) -> None:
        """Load the weights now, so the first utterance does not pay for it."""
        self._load()

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            reason = self.reason()
            if reason:
                raise SttUnavailable(reason)
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - reason() already covers it
                self._error = "faster-whisper is not installed: uv sync --package capri-core --extra stt"
                raise SttUnavailable(self._error) from exc
            self.directory.mkdir(parents=True, exist_ok=True)
            log.info("loading faster-whisper %s (%s)", self.model_name, self.compute_type)
            dev = (self.config.voice.device or "auto").lower()
            target_device = "cpu"
            if dev == "cuda":
                target_device = "cuda"
            elif dev == "auto":
                try:
                    import torch

                    if torch.cuda.is_available():
                        target_device = "cuda"
                except Exception:
                    pass
            try:
                self._model = WhisperModel(
                    self.model_name,
                    device=target_device,
                    compute_type=self.compute_type,
                    download_root=str(self.directory),
                )
            except Exception as exc:
                self._error = f"Whisper failed to load {self.model_name}: {exc}"
                raise SttUnavailable(self._error) from exc
            return self._model

    def transcribe(self, wav: bytes, *, language: str | None = None) -> Transcript:
        samples, rate = decode_wav(wav)
        duration_ms = int(round(samples.size * 1000 / max(1, rate)))
        if rate != 16_000 and samples.size > 1:
            target_len = max(1, int(round(samples.size * 16_000 / rate)))
            positions = np.linspace(0.0, float(samples.size - 1), num=target_len)
            samples = np.interp(positions, np.arange(samples.size), samples).astype(np.float32)
        model = self._load()
        try:
            with self._infer_lock:
                segments, info = model.transcribe(  # type: ignore[attr-defined]
                    samples,
                    language=(language or self.config.voice.language or None),
                    beam_size=1,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 250},
                    condition_on_previous_text=False,
                )
                rows: list[dict[str, Any]] = []
                text_parts: list[str] = []
                scored: list[float] = []
                no_speech = 0.0
                for segment in segments:
                    text = str(getattr(segment, "text", "") or "").strip()
                    avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
                    no_speech_prob = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
                    no_speech = max(no_speech, no_speech_prob)
                    if text:
                        text_parts.append(text)
                        # A logprob is per token and negative; exp() puts it back in
                        # 0..1, which is a confidence a threshold can be set on.
                        scored.append(math.exp(avg_logprob))
                    rows.append(
                        {
                            "start": round(float(getattr(segment, "start", 0.0)), 3),
                            "end": round(float(getattr(segment, "end", 0.0)), 3),
                            "text": text,
                        }
                    )
        except Exception as exc:
            raise SttUnavailable(f"Whisper transcription failed: {exc}") from exc

        return Transcript(
            text=" ".join(text_parts).strip(),
            language=str(getattr(info, "language", "") or ""),
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
            duration_ms=duration_ms,
            segments=rows,
            confidence=(sum(scored) / len(scored)) if scored else 0.0,
            no_speech_prob=no_speech,
        )


class UnavailableStt:
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self._message = message

    def available(self) -> bool:
        return False

    def reason(self) -> str:
        return self._message

    def status(self) -> dict[str, Any]:
        return {
            "engine": self.name,
            "available": False,
            "reason": self.reason(),
            "loaded": False,
            "vad": False,
        }

    def transcribe(self, wav: bytes, *, language: str | None = None) -> Transcript:
        raise SttUnavailable(self._message)


def _model_name(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return DEFAULT_MODEL
    if raw.startswith("faster-whisper-"):
        raw = raw[len("faster-whisper-") :]
    for suffix in ("-int8", "_int8", ".int8"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    return raw or DEFAULT_MODEL


def build_stt(config: CapriConfig):
    raw = (config.voice.stt or "").strip().lower()
    if raw in ("", "off", "none", "disabled"):
        return UnavailableStt("off", "speech input is disabled in config")
    if raw.startswith("faster-whisper"):
        return FasterWhisperEngine(config)
    return UnavailableStt(
        raw,
        f"{raw} speech input is not implemented; set voice.stt to faster-whisper-small-int8",
    )
