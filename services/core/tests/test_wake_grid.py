"""Wake grid: the arithmetic that turns clips into gate limits.

    uv run python services/core/tests/test_wake_grid.py

No model, no microphone, no clips on disk: the cases are made up so the sweep
and the recommendation can be checked on their own.
"""

from __future__ import annotations

import sys

from capri_core.voice.grid import CURRENT, Case, recommend, sweep

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def main() -> int:
    good = Case("capri-open", wake=True, matched=True, confidence=0.70, no_speech=0.05)
    shaky = Case("capri-quiet", wake=True, matched=True, confidence=0.40, no_speech=0.10)
    missed = Case("capri-missed", wake=True, matched=False, confidence=0.90, no_speech=0.05)
    talk = Case("chatting", wake=False, matched=False, confidence=0.80, no_speech=0.05)
    room = Case("room", wake=False, matched=False, confidence=0.10, no_speech=0.80)

    print("cases")
    check(good.passes(0.35, 0.60), "a clear name passes the shipped limits")
    check(not shaky.passes(0.50, 0.60), "a quieter take fails a stricter confidence")
    check(shaky.passes(0.35, 0.60), "and passes the shipped one")
    check(not missed.passes(0.0, 1.0), "a clip the matcher refused never passes")
    check(not talk.passes(0.35, 0.60), "a sentence the matcher refused never passes a limit")
    falsed = Case("television", wake=False, matched=True, confidence=0.90, no_speech=0.02)
    check(falsed.passes(0.35, 0.60), "a negative the matcher matched is what a false wake is")

    print("sweep")
    cells = sweep([good, shaky, missed, talk, room], confidences=(0.3, 0.5), tails=(0.5, 0.9))
    check(len(cells) == 4, f"one cell per pair of limits: {len(cells)}")
    check(
        all(cell.positives == 3 and cell.negatives == 2 for cell in cells),
        "every cell scores the same clip set",
    )
    by_limits = {(cell.min_confidence, cell.max_no_speech): cell for cell in cells}
    strict = by_limits[(0.5, 0.5)]
    loose = by_limits[(0.3, 0.9)]
    check(strict.accepted == 1, f"the strict cell keeps only the clear name: {strict.accepted}")
    check(loose.accepted == 2, f"the loose cell keeps both real wakes: {loose.accepted}")
    check(
        strict.accepted <= loose.accepted,
        "raising the confidence limit never accepts more clips",
    )
    check(
        by_limits[(0.3, 0.5)].accepted == 2,
        "the no-speech limit does not touch takes with a low value",
    )

    print("recommend")
    best = recommend(cells)
    check(best is not None, "a clean cell is found")
    if best is not None:
        check(best.woke == 0, "the recommendation never wakes on a negative")
        check(best.accepted == 2, f"and keeps the most wakes: {best.accepted}")
        check(
            best.min_confidence == 0.3,
            f"among equals it stays closest to the shipped confidence: {best.min_confidence}",
        )

    noisy = sweep([good, talk, falsed], confidences=(0.3,), tails=(0.9,))
    check(noisy[0].woke == 1, f"a falsely matched negative is counted as a wake: {noisy[0].woke}")
    check(recommend(noisy) is None, "a set where a negative wakes has no clean cell")

    empty = sweep([], confidences=(0.3,), tails=(0.9,))
    check(empty[0].accept_rate == 0.0 and empty[0].false_rate == 0.0, "an empty set scores zero, not a crash")
    check(CURRENT == (0.35, 0.60), "the shipped limits are what the report compares against")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
