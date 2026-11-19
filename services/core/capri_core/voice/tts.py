"""Speech synthesis engines.

``KokoroEngine`` is the real one: Kokoro-82M through onnxruntime, no torch, CPU
by default. ``ToneEngine`` is a deterministic stand-in that exercises the whole
pipeline — chunking, the bounded queue, WAV encoding, visemes, playback — with
no model files at all. It is opt-in (``voice.tts: tone``) and never substitutes
itself for a missing Kokoro install.

Everything here is synchronous and CPU-bound; the service runs it in a thread.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .. import paths
from ..config import CapriConfig
from . import dsp
from . import lexicon

log = logging.getLogger("capri.voice.tts")

KOKORO_SAMPLE_RATE = 24_000
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
# capri-desktop's "Her own" preset is speaker 53 at speed 1.1. Its speed
# slider multiplies that style speed. Public Kokoro styles use 1.0.
KOKORO_CAPRI_STYLE_SPEED = 1.1
KOKORO_CAPRI_SPEAKER = 53
# sherpa-onnx needs the pack's raw table and its phonemizer data. The named
# archive above is only for kokoro-onnx's public voices and fallback path.
KOKORO_SHERPA_VOICES = "voices-capri.bin"
KOKORO_TOKENS = "tokens.txt"
KOKORO_LEXICON = "lexicon-us-en.txt"
KOKORO_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
)


class TtsUnavailable(RuntimeError):
    """The engine cannot synthesize: missing package, missing weights, bad config."""


@dataclass
class Audio:
    samples: np.ndarray
    sample_rate: int

    @property
    def duration_ms(self) -> int:
        if self.sample_rate <= 0 or self.samples.size == 0:
            return 0
        return int(round(self.samples.size * 1000 / self.sample_rate))


class TtsEngine(Protocol):
    name: str
    sample_rate: int

    def available(self) -> bool: ...
    def reason(self) -> str: ...
    def voices(self) -> list[str]: ...
    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio: ...


def model_dir(config: CapriConfig) -> Path:
    raw = (config.voice.model_dir or "").strip()
    return Path(raw).expanduser() if raw else paths.models_dir() / "kokoro"


class KokoroEngine:
    name = "kokoro"
    sample_rate = KOKORO_SAMPLE_RATE

    def __init__(self, config: CapriConfig) -> None:
        self.config = config
        self.directory = model_dir(config)
        self._kokoro: object | None = None
        self._sherpa: object | None = None
        self._error: str | None = None

    # ------------------------------------------------------------------ paths

    @property
    def model_path(self) -> Path:
        return self.directory / KOKORO_MODEL

    @property
    def voices_path(self) -> Path:
        return self.directory / KOKORO_VOICES

    @property
    def sherpa_voices_path(self) -> Path:
        return self.directory / KOKORO_SHERPA_VOICES

    @property
    def tokens_path(self) -> Path:
        return self.directory / KOKORO_TOKENS

    @property
    def lexicon_path(self) -> Path:
        return self.directory / KOKORO_LEXICON

    @property
    def speech_lexicon_path(self) -> Path:
        """What the engine reads: the pack's lexicon plus the forms it omits.

        The pack lists base words and few inflections, and sherpa drops a word
        it cannot find without saying anything - which is a sentence that skips
        a word. The merged file adds the regular forms once, and is reused.
        """
        source = self.lexicon_path
        if not source.is_file():
            return source
        return lexicon.merged_lexicon(source)

    def missing_files(self) -> list[Path]:
        return [path for path in (self.model_path, self.voices_path) if not path.is_file()]

    def missing_sherpa_files(self) -> list[Path]:
        paths = (
            self.model_path,
            self.sherpa_voices_path,
            self.tokens_path,
            self.lexicon_path,
        )
        return [path for path in paths if not path.exists()]

    def sherpa_reason(self) -> str:
        if importlib.util.find_spec("sherpa_onnx") is None:
            return "sherpa-onnx is not installed: uv sync --extra kokoro"
        missing = self.missing_sherpa_files()
        if missing:
            names = ", ".join(path.name for path in missing)
            return (
                f"capri-desktop voice pack files missing ({names}) in {self.directory}. "
                "Install the capri-voice-1 pack there, including tokens.txt, "
                "lexicon-us-en.txt and espeak-ng-data."
            )
        return ""

    def reason(self) -> str:
        if self._error:
            return self._error
        # Her pack has its own engine and files. Do not make it depend on
        # kokoro-onnx, which is only used for the public styles.
        if self.config.voice.voice_blend.a == "capri":
            return self.sherpa_reason()
        if importlib.util.find_spec("kokoro_onnx") is None:
            return "kokoro-onnx is not installed: uv add kokoro-onnx"
        missing = self.missing_files()
        if missing:
            names = ", ".join(path.name for path in missing)
            return (
                f"Kokoro weights missing ({names}) in {self.directory}. Download "
                f"{KOKORO_MODEL} and {KOKORO_VOICES} from {KOKORO_RELEASE}/ into that folder."
            )
        return ""

    def available(self) -> bool:
        return not self.reason()

    # ------------------------------------------------------------------ model

    def _load(self) -> object:
        if self._kokoro is not None:
            return self._kokoro
        reason = self.reason()
        if reason:
            raise TtsUnavailable(reason)
        try:
            from kokoro_onnx import Kokoro  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - covered by reason()
            self._error = "kokoro-onnx is not installed: uv add kokoro-onnx"
            raise TtsUnavailable(self._error) from exc

        log.info("loading kokoro from %s", self.directory)
        try:
            self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
        except Exception as exc:  # a bad download should read like a bad download
            self._error = f"Kokoro failed to load: {exc}"
            raise TtsUnavailable(self._error) from exc
        return self._kokoro

    def _load_sherpa(self) -> object:
        """Load the same sherpa-onnx engine capri-desktop uses for her pack."""
        if self._sherpa is not None:
            return self._sherpa
        reason = self.sherpa_reason()
        if reason:
            raise TtsUnavailable(reason)
        try:
            import sherpa_onnx  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - covered by sherpa_reason()
            raise TtsUnavailable(
                "sherpa-onnx is not installed: uv sync --extra kokoro"
            ) from exc

        log.info("loading sherpa-onnx kokoro from %s", self.directory)
        try:
            kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(self.model_path),
                voices=str(self.sherpa_voices_path),
                tokens=str(self.tokens_path),
                lexicon=str(self.speech_lexicon_path),
            )
            model = sherpa_onnx.OfflineTtsModelConfig(
                kokoro=kokoro,
                num_threads=max(1, min(6, (os.cpu_count() or 2) // 2)),
                provider="cpu",
                debug=False,
            )
            config = sherpa_onnx.OfflineTtsConfig(model=model, max_num_sentences=1)
            if hasattr(config, "validate") and not config.validate():
                raise ValueError("invalid Kokoro configuration")
            self._sherpa = sherpa_onnx.OfflineTts(config)
        except Exception as exc:
            self._error = f"sherpa-onnx Kokoro failed to load: {exc}"
            raise TtsUnavailable(self._error) from exc
        return self._sherpa

    def voices(self) -> list[str]:
        """List style names without loading either 325 MB ONNX model."""
        try:
            with np.load(self.voices_path, allow_pickle=False) as archive:
                return sorted(str(name) for name in archive.files)
        except (OSError, ValueError):
            return []

    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio:
        if voice == "capri":
            return self._synthesize_capri(text, speed)
        return self._synthesize_public(text, voice, speed)

    def _synthesize_capri(self, text: str, speed: float) -> Audio:
        """Her pack through sherpa-onnx, then the repo's pitch resample."""
        tts = self._load_sherpa()
        pitch = float(self.config.voice.dsp.pitch)
        lift = 2.0 ** (pitch / 12.0)
        tempo = max(0.5, min(3.0, KOKORO_CAPRI_STYLE_SPEED * float(speed) / lift))
        try:
            generated = tts.generate(  # type: ignore[attr-defined]
                text, sid=KOKORO_CAPRI_SPEAKER, speed=tempo
            )
        except Exception as exc:
            raise TtsUnavailable(f"sherpa-onnx synthesis failed: {exc}") from exc
        samples = np.asarray(generated.samples, dtype=np.float32).reshape(-1)
        rate = int(generated.sample_rate)
        if samples.size == 0:
            raise TtsUnavailable("sherpa-onnx returned no audio")
        if abs(pitch) > 1e-6:
            samples = dsp.resample_repo(samples, lift)
        self.sample_rate = rate
        return Audio(samples, rate)

    def _synthesize_public(self, text: str, voice: str, speed: float) -> Audio:
        """Public Kokoro styles through kokoro-onnx, with the same pitch math."""
        kokoro = self._load()
        pitch = float(self.config.voice.dsp.pitch)
        lift = 2.0 ** (pitch / 12.0)
        tempo = max(0.5, min(3.0, float(speed) / lift))

        try:
            samples, rate = kokoro.create(  # type: ignore[attr-defined]
                text, voice=voice, speed=tempo, lang="en-us"
            )
        except Exception as exc:
            raise TtsUnavailable(f"Kokoro synthesis failed: {exc}") from exc
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            raise TtsUnavailable("Kokoro returned no audio")
        if abs(pitch) > 1e-6:
            audio = dsp.resample(audio, lift)
        self.sample_rate = int(rate)
        return Audio(audio, int(rate))


class ToneEngine:
    """Speech-shaped noise with a per-character mouth. Offline and deterministic."""

    name = "tone"
    sample_rate = KOKORO_SAMPLE_RATE

    def __init__(self, config: CapriConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return True

    def reason(self) -> str:
        return ""

    def voices(self) -> list[str]:
        return ["tone"]

    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio:
        rate = self.sample_rate
        pace = max(0.4, min(2.0, float(speed or 1.0)))
        pieces: list[np.ndarray] = []
        cursor = 0.0
        for char in text or " ":
            if char.isspace():
                length = int(rate * 0.035 / pace)
                pieces.append(np.zeros(length, dtype=np.float32))
                continue
            # Vowels get a lower, rounder tone; everything else a click.
            base = 170.0 if char.lower() in "aeiou" else 240.0
            length = max(1, int(rate * 0.055 / pace))
            t = np.arange(length, dtype=np.float32) / rate
            envelope = np.sin(np.pi * np.arange(length) / max(1, length - 1)).astype(np.float32)
            wave = np.sin(2 * np.pi * (base + 12.0 * cursor) * t).astype(np.float32)
            pieces.append((0.18 * wave * envelope).astype(np.float32))
            cursor += 1.0
        samples = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
        return Audio(samples, rate)


class UnavailableEngine:
    """A named engine that is not implemented yet, with a reason worth reading."""

    sample_rate = KOKORO_SAMPLE_RATE

    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self._message = message

    def available(self) -> bool:
        return False

    def reason(self) -> str:
        return self._message

    def voices(self) -> list[str]:
        return []

    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio:
        raise TtsUnavailable(self._message)


def build_engine(config: CapriConfig) -> TtsEngine:
    if config.voice.tts == "tone":
        log.info("voice engine: tone (offline placeholder)")
        return ToneEngine(config)
    if config.voice.tts == "kokoro":
        return KokoroEngine(config)
    if config.voice.tts == "piper":
        from .piper import PiperEngine

        return PiperEngine(config)
    if config.voice.tts == "chatterbox":
        # Imported here so the default path never touches torch-adjacent modules.
        from .clone import ChatterboxEngine

        return ChatterboxEngine(config)
    if config.voice.tts == "pocket":
        from .pocket import PocketEngine

        return PocketEngine(config)
    return UnavailableEngine(
        config.voice.tts,
        f"{config.voice.tts} is not implemented yet; set voice.tts to kokoro, piper, pocket, chatterbox or tone",
    )


def active_voice(config: CapriConfig) -> str:
    """Voice name for this turn. Blending needs style tensors; that lands later."""
    return config.voice.voice_blend.a
