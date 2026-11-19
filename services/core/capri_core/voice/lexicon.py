"""The pack's lexicon, with the forms English builds from its entries.

sherpa's Kokoro tokenizer looks each word up in the pack's lexicon and, when it
is not there, drops it without a sound: ``OOV word skipped: 'expects'``. The pack
lists base words and only some inflections, so about one word in twenty of an
ordinary reply never reaches the engine, which is what a sentence that skips a
word - or seems to stop halfway through a clause - sounds like.

Nothing here invents a pronunciation. A regular English ending is the base
word's phonemes plus a suffix chosen by the class of the base's last sound, so
``expect`` (``ɪ k s p ˈ ɛ k t``) gives ``expects`` (``ɪ k s p ˈ ɛ k t s``) and
``carry`` gives ``carries`` (``k ˈ ɛ ɹ i z``). Irregular forms are already in the
lexicon, and a word that is neither listed nor regularly derived is left to be
skipped exactly as before.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("capri.voice.lexicon")

MERGED_NAME = "lexicon-capri-merged-v2.txt"

VOICELESS = frozenset({"p", "t", "k", "f", "θ"})
SIBILANT = frozenset({"s", "z", "ʃ", "ʒ", "tʃ", "dʒ"})
DONT_DROP_E = ("ee", "ye", "oe")
VOWELS = frozenset({"ɪ", "ɛ", "æ", "ə", "A", "a", "i", "u", "ʊ", "ɔ", "o", "e", "ɐ", "ᵻ"})


def read_lexicon(path: Path) -> dict[str, list[str]]:
    lex: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if parts:
            lex.setdefault(parts[0].lower(), parts[1:])
    return lex


def _plural(tokens: list[str]) -> list[str]:
    last = tokens[-1] if tokens else ""
    if last in SIBILANT:
        return tokens + ["ɪ", "z"]
    if last in VOICELESS:
        return tokens + ["s"]
    return tokens + ["z"]


def _past(tokens: list[str]) -> list[str]:
    last = tokens[-1] if tokens else ""
    if last in {"t", "d"}:
        return tokens + ["ɪ", "d"]
    if last in VOICELESS or last in {"s", "ʃ", "tʃ"}:
        return tokens + ["t"]
    return tokens + ["d"]


def _doubles(base: str) -> bool:
    """stop -> stopping: one vowel, then one consonant, is doubled."""
    if len(base) < 3:
        return False
    return (
        base[-1] not in "aeiouywx"
        and base[-2] in "aeiou"
        and base[-3] not in "aeiou"
    )


def _consonant_y(base: str) -> bool:
    return base.endswith("y") and len(base) > 2 and base[-2] not in "aeiou"


def forms(base: str, tokens: list[str], *, nouns_only: bool = False) -> dict[str, list[str]]:
    """Every regular form of one lexicon entry, keyed by how it is written.

    Only spellings English actually uses: "danceed" and "carrys" are not words,
    and each line costs load time in sherpa's tokenizer. ``nouns_only`` is for a
    base that is itself an inflection ("building" from "build"): it takes a
    plural, but nobody writes "buildinged".
    """
    out: dict[str, list[str]] = {}
    past = _past(tokens)
    gerund = tokens + ["ɪ", "ŋ"]
    plural = _plural(tokens)
    if base.endswith("e"):
        stem = base[:-1]
        if not base.endswith(DONT_DROP_E):
            out[f"{stem}ed"] = past
            out[f"{stem}ing"] = gerund
        else:
            out[f"{base}ing"] = gerund
        out[f"{base}s"] = plural
    elif _consonant_y(base):
        stem = tokens[:-1] if tokens and tokens[-1] == "i" else tokens
        out[f"{base[:-1]}ies"] = _plural(stem + ["i"])
        out[f"{base[:-1]}ied"] = _past(stem + ["i"])
        out[f"{base}ing"] = gerund
    else:
        out[f"{base}ed"] = past
        out[f"{base}ing"] = gerund
        out[f"{base}s"] = plural
    if _doubles(base):
        out[f"{base}{base[-1]}ed"] = past
        out[f"{base}{base[-1]}ing"] = gerund
    out[f"{base}'s"] = plural
    return {
        word: phonemes
        for word, phonemes in out.items()
        if phonemes and not (nouns_only and word.endswith(("ed", "ing")))
    }


def _candidates(word: str) -> list[str]:
    """The base words a regular ending could have turned into ``word``."""
    out: list[str] = []
    if word.endswith("'s"):
        out.append(word[:-2])
    if word.endswith(("ies", "ied")) and len(word) > 3:
        out.append(word[:-3] + "y")
    if word.endswith("ing") and len(word) > 4:
        stem = word[:-3]
        out += [stem, stem + "e"]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            out.append(stem[:-1])
    if word.endswith("ed") and len(word) > 3:
        stem = word[:-2]
        out += [stem, word[:-1]]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            out.append(stem[:-1])
    if word.endswith("es") and len(word) > 3:
        out += [word[:-2], word[:-1]]
    elif word.endswith("s") and len(word) > 2:
        out.append(word[:-1])
    return out


def inflection(word: str, known) -> str:
    """The listed base whose regular ending could have produced ``word``."""
    for candidate in _candidates(word):
        if candidate != word and candidate in known:
            return candidate
    return ""


def merge(source: Path, target: Path) -> int:
    """Write ``source`` plus the forms it implies to ``target``; return the count."""
    base_lex = read_lexicon(source)
    merged = dict(base_lex)
    added = 0
    for base, tokens in base_lex.items():
        # A base that is not a plain word ("don't", "e.g.") takes no endings we
        # can be sure of, and single letters are their own pronunciations.
        if len(base) < 2 or not base.isalpha() or not all(c.isascii() for c in base):
            continue
        # "building" is already an inflection of "build": its plural is worth an
        # entry, "buildinging" is not.
        nouns_only = bool(inflection(base, base_lex))
        for word, phonemes in forms(base, tokens, nouns_only=nouns_only).items():
            if word not in merged:
                merged[word] = phonemes
                added += 1
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        "".join(f"{word} {' '.join(phonemes)}\n" for word, phonemes in sorted(merged.items())),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return added


def merged_lexicon(source: Path, *, target: Path | None = None) -> Path:
    """The merged lexicon, built once and reused until the pack's file changes."""
    out = target or source.with_name(MERGED_NAME)
    try:
        fresh = out.is_file() and out.stat().st_mtime >= source.stat().st_mtime
        if fresh:
            return out
        started = time.monotonic()
        added = merge(source, out)
        log.info(
            "lexicon: %s forms added in %.1fs -> %s",
            added,
            time.monotonic() - started,
            out,
        )
        return out
    except OSError as exc:
        # A read-only models folder is not worth losing her voice over: the pack
        # lexicon still works, it just drops the words it does not list.
        log.warning("could not extend %s: %s", source, exc)
        return source
