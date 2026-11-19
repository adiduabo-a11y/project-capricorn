"""Chatterbox in its own interpreter, spoken to over stdin and stdout.

Chatterbox pins ``numpy<2`` and ``transformers==4.46.3``; the daemon needs
``numpy>=2`` and its other optional engines want a different transformers again,
so the clone cannot live in the daemon's environment. This script runs under a
separate interpreter (``<capri home>/models/chatterbox/venv``) and answers one
JSON object per line:

    in   {"text": "Say it again.", "reference": "C:/voice/hello-miss.wav"}
    out  {"ok": true, "kind": "base", "rate": 24000, "pcm": "<base64 float32>"}

The model loads once, on the first request, and stays; the engine keeps the
process for as long as it lives. Nothing here is imported by the daemon.
"""

from __future__ import annotations

import base64
import json
import sys
import wave
from pathlib import Path
from typing import Any

# Turbo asserts its reference is longer than this; the base model takes shorter.
TURBO_MIN_SECONDS = 5.0


def clip_seconds(path: Path) -> float:
    """Length of a plain PCM WAV, or 0.0 when it cannot be read that way."""
    try:
        with wave.open(str(path)) as handle:
            return handle.getnframes() / max(1, handle.getframerate())
    except Exception:
        return 0.0


def device_for(requested: str) -> str:
    """Where to run: what was asked for, or the best this install can use."""
    import torch

    want = (requested or "auto").strip().lower()
    if want in ("cpu", "cuda"):
        return want
    return "cuda" if torch.cuda.is_available() else "cpu"


def load(reference: Path, device: str) -> tuple[Any, str]:
    """The best model for this reference clip, and which one it is."""
    from chatterbox.tts import ChatterboxTTS
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    if clip_seconds(reference) >= TURBO_MIN_SECONDS:
        try:
            return ChatterboxTurboTTS.from_pretrained(device=device), "turbo"
        except Exception as exc:  # weights missing, no torch backend, ...
            print(f"turbo unavailable: {exc}", file=sys.stderr, flush=True)
    return ChatterboxTTS.from_pretrained(device=device), "base"


def render(model: Any, text: str, reference: Path) -> dict[str, Any]:
    import numpy as np

    generated = model.generate(text, audio_prompt_path=str(reference))
    if hasattr(generated, "detach"):
        generated = generated.detach().cpu().numpy()
    samples = np.asarray(generated, dtype=np.float32).squeeze()
    if samples.ndim > 1:  # (frames, channels) -> mono
        samples = samples.mean(axis=1)
    samples = np.ascontiguousarray(samples.reshape(-1))
    return {
        "ok": True,
        "rate": int(getattr(model, "sr", 24_000)),
        "pcm": base64.b64encode(samples.astype("<f4").tobytes()).decode("ascii"),
        "seconds": round(float(samples.size) / max(1, int(getattr(model, "sr", 24_000))), 2),
    }


def main() -> int:
    model: Any = None
    kind = ""
    device = "cpu"
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
            body = str(request.get("text") or "")
            reference = Path(str(request.get("reference") or "")).expanduser()
            if not body:
                raise ValueError("no text to say")
            if not reference.is_file():
                raise ValueError(f"no reference clip at {reference}")
            if model is None:
                device = device_for(str(request.get("device") or "auto"))
                model, kind = load(reference, device)
                print(f"loaded {kind} on {device}", file=sys.stderr, flush=True)
            answer = render(model, body, reference)
            answer["kind"] = kind
            answer["device"] = device
        except Exception as exc:  # reported, never fatal: the next line may work
            answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(answer), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
