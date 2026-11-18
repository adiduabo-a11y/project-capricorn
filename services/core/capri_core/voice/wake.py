"""Wake word: her name, then the request.

The microphone belongs to the renderer, so this module never opens a device. It
answers one question about a short utterance: was the user talking to Capri, and
if so, what did they ask? Recognition is the same faster-whisper engine as
push-to-talk; only the decision lives here, which keeps it testable without
audio.

Whisper has no training data for a name like this and writes what it hears:
"capri", "capree", "kapri", "capry". One letter of that is noise and counts;
anything further away is another word. The name has to come first (after sounds
like "hey" or "okay"): a sentence that merely mentions her is not a wake, so
talking about her does not open the microphone.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

# How the name is written down: "capri" as she is named, "capree" because a
# trailing /i/ comes back as "ee". Everything else is scored against these.
SPELLINGS = ("capri", "capree")

# Sounds that may precede the name without changing who is addressed.
LEAD_INS = frozenset({"hello", "hey", "hi", "oi", "ok", "okay", "uh", "um", "yo"})

_TOKEN = re.compile(r"[A-Za-z0-9']+")
# "capricious" is a different word; "capree" and "capry" are her.
_CLOSE = 0.78


@dataclass(frozen=True)
class Wake:
    """What a wake candidate turned out to be."""

    matched: bool
    command: str = ""
    # Her name and nothing else: this is someone getting her attention, so it
    # is answered rather than sent anywhere as a request.
    bare: bool = False


def _tokens(text: str) -> list[tuple[int, int, str]]:
    """Words with their spans, so the request can be cut from the original."""
    out: list[tuple[int, int, str]] = []
    for found in _TOKEN.finditer(text or ""):
        word = found.group(0).lower().strip("'")
        if word:
            out.append((found.start(), found.end(), word))
    return out


def _is_name(word: str) -> bool:
    return any(SequenceMatcher(None, word, spelling).ratio() >= _CLOSE for spelling in SPELLINGS)


def address_only(text: str) -> bool:
    """True when an utterance is only how she is addressed, with no request in it.

    "Hey, Capri", "Capri?" and a recogniser's doubled "Hey Capri, Capri" are
    the same thing. Nothing from these may be sent as a request.
    """
    return all(word in LEAD_INS or _is_name(word) for _, _, word in _tokens(text))


def match_wake(text: str) -> Wake:
    """Decide whether ``text`` opens with her name, and return what follows."""
    entries = _tokens(text)
    start = 0
    while start < len(entries) and entries[start][2] in LEAD_INS:
        start += 1
    if start >= len(entries) or not _is_name(entries[start][2]):
        return Wake(False)
    command = text[entries[start][1] :].strip(" \t,.;:!?-—")
    return Wake(True, command=command, bare=address_only(command))


# A name on its own is a greeting, not an instruction. She answers it out loud —
# one short line, in her voice — and no turn is built from it, so it can never
# become a work request. The hour picks the mood, the way her idle lines do.
_REPLIES_DAY = (
    "Yeah? I'm here.",
    "There you are.",
    "Hm? I'm listening.",
)
_REPLIES_EVENING = (
    "You have my attention.",
    "Hey. I was just thinking.",
    "Mm? Go on.",
)
_REPLIES_LATE = (
    "Still up? I'm here.",
    "It's late. I'm listening.",
    "Mm. You're awake too.",
)


def acknowledge(now: float | None = None) -> str:
    """The line for a wake that carried nothing but her name."""
    stamp = time.localtime(time.time() if now is None else now)
    if stamp.tm_hour >= 23 or stamp.tm_hour < 5:
        lines = _REPLIES_LATE
    elif stamp.tm_hour >= 18:
        lines = _REPLIES_EVENING
    else:
        lines = _REPLIES_DAY
    return lines[(stamp.tm_min // 7) % len(lines)]
