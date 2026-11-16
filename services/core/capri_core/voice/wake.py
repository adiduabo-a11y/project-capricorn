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


def match_wake(text: str) -> Wake:
    """Decide whether ``text`` opens with her name, and return what follows."""
    entries = _tokens(text)
    start = 0
    while start < len(entries) and entries[start][2] in LEAD_INS:
        start += 1
    if start >= len(entries) or not _is_name(entries[start][2]):
        return Wake(False)
    command = text[entries[start][1] :].strip(" \t,.;:!?-—")
    return Wake(True, command=command)
