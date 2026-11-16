"""Measuring a room, so the wake gate stops guessing.

Two numbers come out of a calibration: the loudness of the room when nobody is
speaking, and the loudness of "Capri" when someone is. The gate's threshold
goes between them on a log scale — a room ten times louder needs ten times the
level to trip, so the midpoint of 0.01 and 0.1 is 0.03, not 0.055. The shortest
utterance is measured from the takes themselves, so a clipped "Capri" is not
cut off before the recogniser sees it.

The third number is the recogniser's: how sure it is of this voice through this
microphone, turned into the confidence gate. That part needs the transcripts, so
it lives in the service; only the arithmetic is here.

Pure: WAV bytes in, numbers out. The renderer captures and writes the config;
this module never touches the microphone, the config or the database, which is
what lets the tests feed it tones and noise instead of a person saying her name.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .stt import SttUnavailable, decode_wav

# Frame the energy is measured over. Matches the segmenter's sense of "a
# moment of audio" closely enough that a threshold means the same thing in
# both places.
FRAME_MS = 20
# The room's floor is its loud end, not its average: a fridge that hums for a
# tenth of the recording must not sit below the threshold.
NOISE_PERCENTILE = 95.0
# Barge-in cares about the room's transients, not its steady floor: a gate has
# to clear the occasional keypress or chair creak, which is the tail.
NOISE_TAIL_PERCENTILE = 99.0
# A take's own level is its middle, so one loud syllable does not decide it.
SPEECH_PERCENTILE = 50.0
# Speech has to clear the floor by this much before it counts as a word at
# all; closer than that is the room breathing.
VOICED_MARGIN = 2.0
# A microphone that is effectively silent still needs a positive reference,
# and a gate that opens on digital silence is no gate.
SILENT_FLOOR = 1e-5
MIN_THRESHOLD = 0.003
MAX_THRESHOLD = 0.09
# The shortest utterance is half the shortest real take, kept inside a range
# where it still filters clicks without eating a one-word request.
MIN_UTTERANCE_MS = 120
MAX_UTTERANCE_MS = 600
# The recogniser's confidence on a clean take is an upper bound: a real
# candidate is shorter, further from the microphone and over noise. The gate
# sits this far below the worst take, inside a band where a logprob still means
# something.
CONFIDENCE_MARGIN = 0.7
MIN_CONFIDENCE = 0.15
MAX_CONFIDENCE = 0.6
# Barge-in has no speech sample to compare against, so the room can only raise
# its gate: this far above the transient tail, and never above a level a voice
# close to the microphone could fail to reach.
BARGE_MARGIN = 3.0
MAX_BARGE = 0.09
MAX_AUDIO_PER_LIST = 8


@dataclass
class Calibration:
    threshold_rms: float
    min_utterance_ms: int
    noise_floor: float
    speech_level: float
    noise_ms: int
    noise_tail: float
    takes_ms: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "thresholdRms": self.threshold_rms,
            "minUtteranceMs": self.min_utterance_ms,
            "noiseFloor": self.noise_floor,
            "speechLevel": self.speech_level,
            "noiseMs": self.noise_ms,
            "noiseTail": self.noise_tail,
            "takesMs": list(self.takes_ms),
        }


def frame_levels(samples: np.ndarray, rate: int) -> np.ndarray:
    """RMS of each 20 ms window; a short tail is dropped, not averaged in."""
    width = max(1, int(rate * FRAME_MS / 1000))
    count = int(samples.size // width)
    if count <= 0:
        return np.zeros(0, dtype=np.float64)
    frames = samples[: count * width].astype(np.float64).reshape(count, width)
    return np.sqrt((frames**2).mean(axis=1))


def barge_threshold(noise_tail: float, *, floor: float) -> float:
    """The barge gate for this room.

    Calibration has no recording of speech over her playback, so it cannot say
    where "interrupting" starts; it can only say the room must not trip it. The
    gate therefore never goes below what the user configured and rises with the
    room's transient tail.
    """
    return round(max(floor, min(MAX_BARGE, noise_tail * BARGE_MARGIN)), 4)


def confidence_gate(levels: list[float]) -> float:
    """The recogniser gate, from what the takes scored on this machine.

    Pure on purpose: the caller transcribes, so the arithmetic that turns
    confidences into a threshold can be tested without a model.
    """
    if not levels:
        raise ValueError("confidence_gate needs at least one measured take")
    worst = min(levels)
    return round(min(MAX_CONFIDENCE, max(MIN_CONFIDENCE, worst * CONFIDENCE_MARGIN)), 2)


def _levels(blobs: list[bytes], what: str) -> list[np.ndarray]:
    levels: list[np.ndarray] = []
    for index, blob in enumerate(blobs, start=1):
        samples, rate = decode_wav(blob)
        measured = frame_levels(samples, rate)
        if measured.size == 0:
            raise SttUnavailable(f"{what} {index} was shorter than {FRAME_MS} ms")
        levels.append(measured)
    return levels


def measure(noise: list[bytes], speech: list[bytes]) -> Calibration:
    """Calibrate the gate from one room recording and a few spoken takes."""
    if not noise:
        raise SttUnavailable("calibration needs a recording of the room")
    if not speech:
        raise SttUnavailable("calibration needs at least one spoken \"Capri\"")
    if len(noise) > MAX_AUDIO_PER_LIST or len(speech) > MAX_AUDIO_PER_LIST:
        raise SttUnavailable(f"calibration takes at most {MAX_AUDIO_PER_LIST} recordings")

    room = np.concatenate(_levels(noise, "room recording"))
    floor = max(SILENT_FLOOR, float(np.percentile(room, NOISE_PERCENTILE)))
    voiced_cutoff = floor * VOICED_MARGIN

    levels: list[float] = []
    takes_ms: list[int] = []
    for index, take in enumerate(_levels(speech, "take"), start=1):
        voiced = take[take > voiced_cutoff]
        if voiced.size == 0:
            raise SttUnavailable(
                f"take {index} was not louder than the room; move closer or speak up"
            )
        levels.append(float(np.percentile(voiced, SPEECH_PERCENTILE)))
        takes_ms.append(int(round(voiced.size * FRAME_MS)))

    speech_level = float(np.median(levels))
    # Geometric, not arithmetic: loudness is perceived against what is already
    # there, so a ratio is the thing that stays honest across rooms.
    threshold = math.sqrt(floor * speech_level)
    threshold = min(MAX_THRESHOLD, max(MIN_THRESHOLD, threshold))

    shortest = min(takes_ms)
    shortest_utterance = int(min(MAX_UTTERANCE_MS, max(MIN_UTTERANCE_MS, shortest // 2)))

    return Calibration(
        threshold_rms=round(threshold, 4),
        min_utterance_ms=shortest_utterance,
        noise_floor=round(floor, 4),
        speech_level=round(speech_level, 4),
        noise_ms=int(room.size * FRAME_MS),
        noise_tail=round(float(np.percentile(room, NOISE_TAIL_PERCENTILE)), 4),
        takes_ms=takes_ms,
    )
