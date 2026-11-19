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
import base64
import json
import logging
import os
import subprocess
import threading
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
# Where a clone interpreter lives when the daemon's own environment cannot hold
# Chatterbox: <capri home>/models/chatterbox/venv.
VENV_DIR = "chatterbox"
WORKER_SCRIPT = Path(__file__).with_name("clone_worker.py")

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


def interpreter_path(config: CapriConfig) -> Path:
    """The Python that runs Chatterbox when the daemon's own environment cannot.

    Chatterbox pins numpy<2 and transformers==4.46.3; the daemon needs numpy>=2
    and its other voice engines want their own transformers. So the clone gets
    an interpreter of its own and talks to the daemon over stdio.
    """
    raw = (config.voice.chatterbox_python or "").strip()
    if raw:
        return Path(raw).expanduser()
    venv = paths.models_dir() / VENV_DIR / "venv"
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class _CloneWorker:
    """One long-lived Chatterbox process, asked for one line at a time.

    The first answer takes as long as the model takes to load (and to download,
    the first time), so the caller waits on a pipe rather than a timeout.
    """

    def __init__(self, python: Path, script: Path) -> None:
        self.python = python
        self.script = script
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _start(self) -> subprocess.Popen[bytes]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        log.info("starting chatterbox worker: %s", self.python)
        self._proc = subprocess.Popen(
            [str(self.python), "-u", str(self.script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return self._proc

    def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            proc = self._start()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                self.close()
                raise TtsUnavailable(
                    "Chatterbox worker stopped without answering; see its stderr"
                )
            return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


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
        self._worker: _CloneWorker | None = None
        # What the worker reported: which model, and whether it landed on a GPU.
        self.device = ""
        self.model_kind = ""

    def reason(self) -> str:
        if self._error:
            return self._error
        if not self.reference.is_file():
            return (
                f"no reference clip at {self.reference}. Render Capri's own with "
                "`python -m capri_core --voice-reference` (needs Kokoro weights), or "
                "point voice.reference_clip at a WAV you have the rights to."
            )
        if importlib.util.find_spec("chatterbox") is not None:
            return ""
        python = interpreter_path(self.config)
        if not python.is_file():
            return (
                f"chatterbox-tts is not installed: no clone interpreter at {python}. "
                "See 'Cloning a voice' in the README; uv add cannot install it into "
                "this environment because it pins numpy<2."
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
        if importlib.util.find_spec("chatterbox") is None:
            return self._synthesize_external(text)
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

    def _synthesize_external(self, text: str) -> Audio:
        """Ask the clone interpreter; it holds the model, this holds the pipe."""
        reason = self.reason()
        if reason:
            raise TtsUnavailable(reason)
        if self._worker is None:
            self._worker = _CloneWorker(interpreter_path(self.config), WORKER_SCRIPT)
        answer = self._worker.ask(
            {
                "text": text,
                "reference": str(self.reference),
                "device": self.config.voice.chatterbox_device or "auto",
            }
        )
        if not answer.get("ok"):
            # A worker that failed once is not restarted on the same sentence;
            # the next one starts it again with a clean model.
            self._worker = None
            raise TtsUnavailable(f"Chatterbox synthesis failed: {answer.get('error')}")
        self.device = str(answer.get("device") or "cpu")
        self.model_kind = str(answer.get("kind") or "")
        log.info("chatterbox: %s on %s", self.model_kind, self.device)
        samples = np.frombuffer(base64.b64decode(answer["pcm"]), dtype="<f4")
        if samples.size == 0:
            raise TtsUnavailable("Chatterbox returned no audio")
        rate = int(answer.get("rate") or CHATTERBOX_SAMPLE_RATE)
        self.sample_rate = rate
        return Audio(np.ascontiguousarray(samples, dtype=np.float32), rate)


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
