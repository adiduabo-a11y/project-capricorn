"""Talking to her about the running turn.

A wake command is usually a request ("open the terminal"), but a few short
sentences are about the conversation itself: a pending confirmation, or a turn
that is still running. Only those are recognised here, and only when the whole
utterance is the phrase — "stop" is a command, "stop the server" is a request.
Whether a command applies is the renderer's decision, because only it knows
whether a card is open or a turn is live.

Kept pure and table-driven so it can be tested without audio or a daemon.
"""

from __future__ import annotations

import re

# intent -> the whole utterances that carry it. Nothing here is a prefix:
# "yes, use the other file" stays a request.
PHRASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "affirm": (
        ("yes",),
        ("yeah",),
        ("yep",),
        ("sure",),
        ("ok",),
        ("okay",),
        ("approve",),
        ("confirm",),
        ("go", "ahead"),
        ("do", "it"),
    ),
    "deny": (
        ("no",),
        ("nope",),
        ("deny",),
        ("cancel",),
        ("cancel", "that"),
        ("don't",),
        ("do", "not"),
    ),
    "stop": (
        ("stop",),
        ("stop", "talking"),
        ("quiet",),
        ("silence",),
    ),
    "never_mind": (
        ("never", "mind"),
        ("forget", "it"),
        ("forget", "that"),
    ),
}

# Politeness does not change what was said.
COURTESY = frozenset({"please", "thanks", "thank", "you"})

_TOKEN = re.compile(r"[A-Za-z0-9']+")


def _words(text: str) -> tuple[str, ...]:
    words = [found.group(0).lower().strip("'") for found in _TOKEN.finditer(text or "")]
    words = [word for word in words if word]
    while words and words[-1] in COURTESY:
        words.pop()
    return tuple(words)


def match_command(text: str) -> str | None:
    """The intent a whole utterance carries, or ``None`` when it is a request."""
    words = _words(text)
    if not words or len(words) > 3:
        return None
    for intent, phrases in PHRASES.items():
        if words in phrases:
            return intent
    return None
