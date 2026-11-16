"""Cloning from a reference clip.

Chatterbox is the permissive local option (MIT, zero-shot from 5-10 s of audio,
runs on CPU though it wants torch). It is an *optional* engine: the spec's
no-torch rule exists so the default path dodges the AMD-Windows mess, and that
rule still holds for Kokoro.

The reference clip must be one the user has the rights to. Capri's own reference
is rendered here from the synthetic Kokoro blend — never from a performer's
recording. ``render_reference`` refuses to invent a voice: with no Kokoro
weights it says so and stops.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import numpy as np

from .. import paths
from ..config import CapriConfig
from .tts import Audio, KokoroEngine, TtsUnavailable, active_voice
from .wav import to_wav

log = logging.getLogger("capri.voice.clone")

CHATTERBOX_SAMPLE_RATE = 24_000
DEFAULT_REFERENCE = "capri-reference.wav"
SILENCE_MS = 140

# Written to be read aloud in one register, with no borrowed dialogue in it.
REFERENCE_TEXT = (
    "Signal's clean. I am Capri: a construct with no body of my own, projecting "
    "myself for you. Ten seconds of a voice is enough to keep it. Everything you "
    "say stays on this machine."
)


def reference_path(config: CapriConfig) -> Path:
    raw = (config.voice.reference_clip or "").strip()
    if raw:
        return Path(raw).expanduser()
    return paths.models_dir() / "chatterbox" / DEFAULT_REFERENCE


class ChatterboxEngine:
    """Zero-shot clone of whatever is in the reference clip."""

    name = "chatterbox"
    sample_rate = CHATTERBOX_SAMPLE_RATE

    def __init__(self, config: CapriConfig) -> None:
        self.config = config
        self.reference = reference_path(config)
        self._model: Any = None
        self._error: str | None = None
        self._class: Any = None

    def reason(self) -> str:
        if self._error:
            return self._error
        if importlib.util.find_spec("chatterbox") is None:
            return "chatterbox-tts is not installed: uv add chatterbox-tts (pulls PyTorch)"
        if not self.reference.is_file():
            return (
                f"no reference clip at {self.reference}. Render Capri's own with "
                "`python -m capri_core --voice-reference` (needs Kokoro weights), or "
                "point voice.reference_clip at a WAV you have the rights to."
            )
        return ""

    def available(self) -> bool:
        return not self.reason()

    def voices(self) -> list[str]:
        return ["reference"] if self.available() else []

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        reason = self.reason()
        if reason:
            raise TtsUnavailable(reason)

        loaders: list[tuple[str, str]] = [
            ("chatterbox.tts_turbo", "ChatterboxTurboTTS"),
            ("chatterbox.tts", "ChatterboxTTS"),
        ]
        last: Exception | None = None
        for module_name, class_name in loaders:
            try:
                module = __import__(module_name, fromlist=[class_name])
                factory = getattr(module, class_name)
                self._model = factory.from_pretrained(device="cpu")
                self._class = class_name
                self.sample_rate = int(getattr(self._model, "sr", CHATTERBOX_SAMPLE_RATE))
                log.info("loaded %s for voice cloning", class_name)
                return self._model
            except Exception as exc:  # try the next entry point
                last = exc
        self._error = f"Chatterbox failed to load: {last}"
        raise TtsUnavailable(self._error)

    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio:
        model = self._load()
        try:
            generated = model.generate(text, audio_prompt_path=str(self.reference))
        except Exception as exc:
            raise TtsUnavailable(f"Chatterbox synthesis failed: {exc}") from exc

        array = generated
        if hasattr(array, "detach"):
            array = array.detach().cpu().numpy()
        samples = np.asarray(array, dtype=np.float32)
        samples = np.squeeze(samples)
        if samples.ndim > 1:  # (frames, channels) -> mono
            samples = samples.mean(axis=1)
        samples = np.ascontiguousarray(samples.reshape(-1))
        if samples.size == 0:
            raise TtsUnavailable("Chatterbox returned no audio")
        return Audio(samples, self.sample_rate)


def render_reference(config: CapriConfig, out: Path | None = None) -> dict[str, Any]:
    """Render Capri's reference clip from the synthetic blend.

    No DSP here: the clone should learn the clean timbre, and the character
    filter is applied to whatever the clone produces.
    """
    engine = KokoroEngine(config)
    reason = engine.reason()
    if reason:
        raise TtsUnavailable(
            f"cannot render a reference without Kokoro: {reason}"
        )

    from .service import split_sentences

    sentences = split_sentences(REFERENCE_TEXT, config.voice.sentence_max_chars)
    if not sentences:
        raise TtsUnavailable("reference text produced no sentences")

    pieces: list[np.ndarray] = []
    rate = engine.sample_rate
    voice = active_voice(config)
    for sentence in sentences:
        audio = engine.synthesize(sentence, voice=voice, speed=config.voice.speed)
        rate = audio.sample_rate
        pieces.append(audio.samples)
        pieces.append(np.zeros(max(1, int(rate * SILENCE_MS / 1000)), dtype=np.float32))

    samples = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
    target = out or reference_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(to_wav(samples, rate))
    seconds = round(samples.size / max(1, rate), 2)
    log.info("wrote %s (%.2fs)", target, seconds)
    return {"path": str(target), "seconds": seconds, "voice": voice, "sampleRate": rate}
