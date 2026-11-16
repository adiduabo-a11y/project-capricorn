"""Which gate limits the clips actually justify.

The wake gate is two numbers: how sure the recogniser has to be, and how much
"there was no speech here" it may report. Both shipped as guesses. A clip set
with known answers turns them into a table: for every pair of limits, how many
of the takes it should accept does it accept, and how many it should ignore does
it wake on.

Pure: the caller transcribes, so this is arithmetic over (was it for her, did the
matcher agree, how sure was the decoder) and the tests can exercise it without a
model or a microphone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

# Dense enough to see a plateau, coarse enough to read in a terminal.
CONFIDENCE_STEPS: tuple[float, ...] = tuple(round(0.10 + 0.05 * index, 2) for index in range(11))
NO_SPEECH_STEPS: tuple[float, ...] = tuple(round(0.30 + 0.10 * index, 2) for index in range(7))
# What ships today, so a recommendation can be stated as a change or not.
CURRENT = (0.35, 0.60)


@dataclass(frozen=True)
class Case:
    """One clip, already transcribed."""

    name: str
    wake: bool
    matched: bool
    confidence: float
    no_speech: float

    def passes(self, min_confidence: float, max_no_speech: float) -> bool:
        return (
            self.matched
            and self.confidence >= min_confidence
            and self.no_speech <= max_no_speech
        )


@dataclass(frozen=True)
class Cell:
    """One pair of limits and what it does to the clip set."""

    min_confidence: float
    max_no_speech: float
    accepted: int
    positives: int
    woke: int
    negatives: int

    @property
    def accept_rate(self) -> float:
        return self.accepted / self.positives if self.positives else 0.0

    @property
    def false_rate(self) -> float:
        return self.woke / self.negatives if self.negatives else 0.0

    @property
    def clean(self) -> bool:
        """No clip that should be ignored was woken on."""
        return self.woke == 0


def sweep(
    cases: Iterable[Case],
    confidences: Sequence[float] = CONFIDENCE_STEPS,
    tails: Sequence[float] = NO_SPEECH_STEPS,
) -> list[Cell]:
    """Every pair of limits, scored against the same set of clips."""
    items = list(cases)
    positives = sum(1 for case in items if case.wake)
    negatives = len(items) - positives
    cells: list[Cell] = []
    for confidence in confidences:
        for tail in tails:
            cells.append(
                Cell(
                    min_confidence=confidence,
                    max_no_speech=tail,
                    accepted=sum(1 for case in items if case.wake and case.passes(confidence, tail)),
                    positives=positives,
                    woke=sum(
                        1 for case in items if not case.wake and case.passes(confidence, tail)
                    ),
                    negatives=negatives,
                )
            )
    return cells


def recommend(cells: Iterable[Cell], *, near: tuple[float, float] = CURRENT) -> Cell | None:
    """The cleanest cell that keeps the most wakes, closest to what ships.

    Accept rate comes first, because refusing her name is the failure people
    notice. Beyond that, moving the shipped defaults needs a reason, so the
    closest cell wins.
    """
    clean = [cell for cell in cells if cell.clean]
    if not clean:
        return None
    best = max(cell.accept_rate for cell in clean)
    shortlist = [cell for cell in clean if cell.accept_rate == best]
    return min(
        shortlist,
        key=lambda cell: (abs(cell.min_confidence - near[0]), abs(cell.max_no_speech - near[1])),
    )
