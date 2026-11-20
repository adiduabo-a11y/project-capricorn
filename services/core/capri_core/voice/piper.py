"""Piper: the small, fast voice path.

Piper is optional and deliberately separate from Kokoro: it is useful when the
machine cannot synthesize a sentence before the user notices the pause, and it
keeps the same ``TtsEngine`` shape so the streaming queue and viseme schedule
do not care which engine produced the samples. Its voice files are not Capri's
synthetic Kokoro timbre; choose it explicitly.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from pathlib import Path

import numpy as np

from .. import paths
from ..config import CapriConfig
from .download import download_file
from .tts import Audio, TtsUnavailable

log = logging.getLogger("capri.voice.piper")

DEFAULT_VOICE = "en_US-lessac-medium"
PIPER_RELEASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"


def voice_path(config: CapriConfig) -> Path:
    raw = (config.voice.piper_voice or "").strip()
    if raw:
        return Path(raw).expanduser()
    return paths.models_dir() / "piper" / f"{DEFAULT_VOICE}.onnx"


class PiperEngine:
    name = "piper"
    sample_rate = 22_050

    def __init__(self, config: CapriConfig) -> None:
        self.config = config
        self.path = voice_path(config)
        self._voice: object | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    @property
    def config_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".json")

    def missing_files(self) -> list[Path]:
        return [path for path in (self.path, self.config_path) if not path.is_file()]

    def reason(self) -> str:
        if self._error:
            return self._error
        if importlib.util.find_spec("piper") is None:
            return "piper is not installed: uv sync --package capri-core --extra piper"
        missing = self.missing_files()
        if missing:
            names = ", ".join(path.name for path in missing)
            return (
                f"Piper voice files missing ({names}) in {self.path.parent}. Download "
                f"{DEFAULT_VOICE}.onnx and its .json from the Piper voices release, or "
                f"run `python -m piper.download_voices {DEFAULT_VOICE}` into that folder."
            )
        return ""

    def available(self) -> bool:
        return not self.reason()

    def voices(self) -> list[str]:
        return [self.path.stem] if self.available() else []

    def ensure_files(self) -> None:
        """Download default Piper voice files automatically if missing."""
        if self.path.stem != DEFAULT_VOICE:
            return
        missing = self.missing_files()
        if not missing:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for p in missing:
            filename = p.name
            url = f"{PIPER_RELEASE}/{filename}"
            log.info("auto-downloading missing Piper voice file %s from %s", filename, url)
            try:
                download_file(url, p)
            except Exception as exc:
                self._error = f"Failed to auto-download {filename}: {exc}"
                log.error("Piper download failed for %s: %s", filename, exc)
                break

    def load(self) -> object:
        return self._load()

    def _load(self) -> object:
        if self._voice is not None:
            return self._voice
        with self._lock:
            if self._voice is not None:
                return self._voice
            self.ensure_files()
            reason = self.reason()
            if reason:
                raise TtsUnavailable(reason)
            try:
                from piper.voice import PiperVoice  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - reason() covers it
                self._error = "piper is not installed: uv sync --package capri-core --extra piper"
                raise TtsUnavailable(self._error) from exc
            log.info("loading piper from %s", self.path)
            try:
                self._voice = PiperVoice.load(str(self.path), config_path=str(self.config_path))
            except Exception as exc:
                self._error = f"Piper failed to load: {exc}"
                raise TtsUnavailable(self._error) from exc
            return self._voice

    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio:
        piper = self._load()
        try:
            from piper.config import SynthesisConfig  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise TtsUnavailable("piper is not installed") from exc
        pace = max(0.5, min(2.0, float(speed or 1.0)))
        try:
            chunks = list(
                piper.synthesize(
                    text,
                    syn_config=SynthesisConfig(length_scale=1.0 / pace),
                )
            )
        except Exception as exc:
            raise TtsUnavailable(f"Piper synthesis failed: {exc}") from exc
        samples = [
            np.asarray(chunk.audio_float_array, dtype=np.float32).reshape(-1)
            for chunk in chunks
            if getattr(chunk, "audio_float_array", None) is not None
        ]
        if not samples:
            raise TtsUnavailable("Piper returned no audio")
        rate = int(getattr(chunks[0], "sample_rate", self.sample_rate) or self.sample_rate)
        self.sample_rate = rate
        return Audio(np.concatenate(samples), rate)
