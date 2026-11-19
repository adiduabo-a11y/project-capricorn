"""Capri's own voice, through Pocket TTS.

Pocket is Kyutai's CPU-first TTS (MIT code, CC BY 4.0 weights). A *voice state*
is a small safetensors file — the one shipped here was tuned from OmniVoice
output, with no recording of any performer in it.

Two ways in:
  * ``model.get_state_for_audio_prompt`` accepts a state file (fast, no cloning)
    or a WAV (clones it, then we can export the state so it is paid for once).
  * nothing here reaches the network unless Pocket has to fetch its own base
    model; ``HF_HOME`` stays inside the Capri home.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import struct
import threading
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

from .. import paths
from ..config import CapriConfig
from .tts import Audio, TtsUnavailable

log = logging.getLogger("capri.voice.pocket")

POCKET_SAMPLE_RATE = 24_000
DEFAULT_STATE = "capri_english.safetensors"
# Pocket's level drifts sentence to sentence (peak 0.06-0.20 for the same voice),
# which is audible when segments are queued back to back. Level to a target RMS.
LEVEL_RMS = 0.09
MAX_GAIN = 8.0
LANGUAGES = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "nl": "dutch",
}


def pocket_voice_path(config: CapriConfig) -> Path:
    raw = (config.voice.voice_file or "").strip()
    if raw:
        return Path(raw).expanduser()
    return paths.models_dir() / "pocket" / DEFAULT_STATE


def state_info(data: bytes) -> dict[str, Any]:
    """Read a safetensors header without importing torch."""
    if len(data) < 9:
        raise ValueError("too small to be a safetensors file")
    length = struct.unpack("<Q", data[:8])[0]
    if length <= 0 or 8 + length > len(data):
        raise ValueError("not a safetensors file (bad header length)")
    header = json.loads(data[8 : 8 + length])
    tensors = [key for key in header if key != "__metadata__"]
    if not tensors:
        raise ValueError("safetensors file holds no tensors")
    return {"tensors": len(tensors), "bytes": len(data), "first": tensors[:3]}


class PocketEngine:
    """Capri's voice file, synthesized on the CPU."""

    name = "pocket"
    sample_rate = POCKET_SAMPLE_RATE

    def __init__(self, config: CapriConfig) -> None:
        self.config = config
        self.voice_file = pocket_voice_path(config)
        # `reference` lets VoiceService.status report the file under one key.
        self.reference = self.voice_file
        code = (config.voice.language or "en")[:2].lower()
        self.language = LANGUAGES.get(code, "english")
        self._model: Any = None
        self._state: Any = None
        self._error: str | None = None
        # Startup warms this while a reply may already be asking for it: one
        # load, and a second caller waits for it rather than loading again.
        self._lock = threading.Lock()

    def reason(self) -> str:
        if self._error:
            return self._error
        if importlib.util.find_spec("pocket_tts") is None:
            return "pocket-tts is not installed: uv sync --extra pocket"
        if not self.voice_file.is_file():
            return (
                f"no Pocket voice state at {self.voice_file}. Install one with "
                "`python -m capri_core --voice-install <mod.zip|voice.safetensors>`."
            )
        return ""

    def available(self) -> bool:
        return not self.reason()

    def voices(self) -> list[str]:
        return [self.voice_file.stem] if self.available() else []

    def load(self) -> None:
        """Load the base model and her state without saying anything.

        ~11 s of weights on this CPU. Paid at daemon startup it is invisible;
        paid inside her first reply it is a silence the user hears.
        """
        self._load()

    def _load(self) -> tuple[Any, Any]:
        with self._lock:
            return self._open()

    def _open(self) -> tuple[Any, Any]:
        if self._model is None:
            reason = self.reason()
            if reason:
                raise TtsUnavailable(reason)
            # Keep the download beside the rest of Capri's models, and use plain
            # HTTP: Xet's downloader has a habit of dying halfway.
            os.environ.setdefault("HF_HOME", str(paths.models_dir() / "pocket" / "hf"))
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            try:
                from pocket_tts import TTSModel

                self._model = TTSModel.load_model(language=self.language)
                self.sample_rate = int(getattr(self._model, "sample_rate", POCKET_SAMPLE_RATE))
                log.info("Pocket %s loaded (%d Hz)", self.language, self.sample_rate)
            except Exception as exc:
                self._error = f"Pocket failed to load: {exc}"
                raise TtsUnavailable(self._error) from exc

        if self._state is None:
            try:
                self._state = self._model.get_state_for_audio_prompt(str(self.voice_file))
            except Exception as exc:
                self._error = f"Pocket rejected {self.voice_file.name}: {exc}"
                raise TtsUnavailable(self._error) from exc
        return self._model, self._state

    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio:
        model, state = self._load()
        try:
            audio = model.generate_audio(state, text)
        except Exception as exc:
            raise TtsUnavailable(f"Pocket synthesis failed: {exc}") from exc
        if hasattr(audio, "numpy"):
            audio = audio.numpy()
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            raise TtsUnavailable("Pocket returned no audio")
        # No pitch shift here: the state already carries her register. Measured
        # against the Kokoro path on the same passage, this take sits within 0.6
        # semitones of Kokoro's +2 output, so dsp.pitch would put her ~2.4
        # semitones above it. The layers downstream still apply.
        out = _levelled(samples)
        return Audio(np.ascontiguousarray(out, dtype=np.float32), self.sample_rate)


def _levelled(samples: np.ndarray) -> np.ndarray:
    """Match loudness across sentences, without letting a boost clip."""
    rms = float(np.sqrt(np.mean(np.square(samples))))
    if rms <= 1e-6:
        return samples
    out = samples * min(LEVEL_RMS / rms, MAX_GAIN)
    peak = float(np.max(np.abs(out)))
    if peak > 0.95:
        out = out * (0.95 / peak)
    return out.astype(np.float32)


def install_voice_state(
    source: Path,
    config: CapriConfig,
    *,
    language: str | None = None,
) -> dict[str, Any]:
    """Copy a Pocket voice state out of a mod zip (or a bare file)."""
    target = pocket_voice_path(config)
    code = (language or config.voice.language or "en")[:2].lower()
    member = ""

    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".safetensors") and "capri" in name.lower()
            ]
            if not candidates:
                raise ValueError(f"{source.name} has no Capri voice state in it")
            preferred = f"capri_{LANGUAGES.get(code, 'english')}.safetensors"
            chosen = next(
                (name for name in candidates if Path(name).name == preferred),
                next((name for name in candidates if Path(name).name == DEFAULT_STATE), candidates[0]),
            )
            member = Path(chosen).name
            max_state_bytes = 32 * 1024 * 1024
            if archive.getinfo(chosen).file_size > max_state_bytes:
                raise ValueError(f"{member} exceeds the {max_state_bytes} byte limit")
            with archive.open(chosen) as stream:
                data = stream.read(max_state_bytes + 1)
            if len(data) > max_state_bytes:
                raise ValueError(f"{member} exceeds the {max_state_bytes} byte limit")
    elif source.suffix.lower() == ".safetensors":
        data = source.read_bytes()
        member = source.name
    else:
        raise ValueError(f"{source.name}: expected a .zip or a .safetensors voice state")

    info = state_info(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    log.info("installed %s (%d tensors) at %s", member, info["tensors"], target)
    return {"path": str(target), "bytes": info["bytes"], "tensors": info["tensors"], "from": member}
