"""Viseme timing.

The model does not give per-phoneme timings through this interface, so the
schedule is estimated from the text the segment was synthesized from and spread
across the measured duration of the audio. It is deliberately cheap and
monotonic: a mouth that moves plausibly in time with the words, not a lip-sync
oracle. When Kokoro phonemes are wired in later, only ``schedule`` changes.
"""

from __future__ import annotations

VISEMES = ("closed", "a", "e", "i", "o", "u", "m")
DEFAULT_FPS = 15

_VOWELS = {"a": "a", "e": "e", "i": "i", "o": "o", "u": "u"}
_CLOSED = set("mbp")
# After a closed shape, open a little so the mouth does not sit still.
_ALTERNATE = {"closed": "e", "m": "a"}


def _viseme_for(char: str, previous: str) -> str:
    lowered = char.lower()
    if lowered in _VOWELS:
        return _VOWELS[lowered]
    if lowered in _CLOSED:
        return "m"
    if previous in ("closed", "m"):
        return _ALTERNATE[previous]
    return "closed"


def schedule(text: str, duration_ms: int, *, fps: int = DEFAULT_FPS) -> list[dict[str, int | str]]:
    """Return ``[{t: ms, v: viseme}]`` for one segment."""
    letters = [char for char in text if char.isalnum()]
    total = max(0, int(duration_ms))
    if total <= 0:
        return [{"t": 0, "v": "closed"}]
    if not letters:
        letters = [" "]

    step = max(1, int(1000 / max(1, fps)))
    frames = max(1, total // step)
    out: list[dict[str, int | str]] = []
    previous = "closed"
    for frame in range(frames):
        index = min(len(letters) - 1, int(frame * len(letters) / frames))
        shape = _viseme_for(letters[index], previous)
        previous = shape
        out.append({"t": frame * step, "v": shape})
    out.append({"t": max(0, total - step), "v": "closed"})
    return out
