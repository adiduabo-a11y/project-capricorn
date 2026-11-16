"""Structure-aware chunking.

Order of preference when choosing a boundary: heading, fenced code block,
paragraph, sentence, hard slice. A chunk is always an exact slice of the
source text, so ``text[start:end] == chunk.text`` and a citation can open the
file at the character the model actually saw.

Overlap is expressed as extra leading context on the next chunk, never as
rewritten text: the span still points at real characters.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Local models tokenize prose at roughly four characters per token. Exactness
# is not the point — stable, generous chunks are.
CHARS_PER_TOKEN = 4

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n{2,}")


@dataclass(frozen=True)
class Atom:
    """A boundary-respecting unit of text: a heading, a paragraph, a fence, a sentence."""

    start: int
    end: int
    heading: str


@dataclass
class Chunk:
    text: str
    heading_path: str
    start: int
    end: int

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def _split_block(text: str, start: int, end: int, heading: str, limit: int) -> list[Atom]:
    """Split an oversized paragraph at sentence boundaries, then hard-slice."""
    if end - start <= limit:
        return [Atom(start, end, heading)]

    body = text[start:end]
    pieces: list[Atom] = []
    cursor = start
    for match in _SENTENCE.finditer(body):
        cut = start + match.end()
        if cut - cursor >= limit:
            pieces.extend(_hard_slice(text, cursor, cut, heading, limit))
            cursor = cut
    if cursor < end:
        pieces.extend(_hard_slice(text, cursor, end, heading, limit))
    return pieces


def _hard_slice(text: str, start: int, end: int, heading: str, limit: int) -> list[Atom]:
    out: list[Atom] = []
    cursor = start
    while cursor < end:
        stop = min(end, cursor + limit)
        if stop < end:
            window = text[cursor:stop]
            space = window.rfind(" ")
            if space > limit // 2:
                stop = cursor + space + 1
        out.append(Atom(cursor, stop, heading))
        cursor = stop
    return out


def _atoms(text: str, limit: int) -> list[Atom]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)

    atoms: list[Atom] = []
    stack: list[tuple[int, str]] = []
    heading = ""
    # A heading belongs to the block that follows it, not to a chunk of its
    # own: a chunk that is just "# Deploy" embeds nothing useful.
    pending: int | None = None
    index = 0

    while index < len(lines):
        line = lines[index]
        start = offsets[index]

        match = _HEADING.match(line)
        if match:
            level = len(match.group(1))
            stack = [(depth, title) for depth, title in stack if depth < level]
            stack.append((level, match.group(2).strip()))
            heading = " > ".join(title for _, title in stack)
            if pending is None:
                pending = start
            index += 1
            continue

        block_start = pending if pending is not None else start

        if _FENCE.match(line):
            fence = line.strip()[:3]
            close = index + 1
            while close < len(lines) and not lines[close].lstrip().startswith(fence):
                close += 1
            end = offsets[close] + len(lines[close]) if close < len(lines) else len(text)
            atoms.extend(_split_block(text, block_start, min(end, len(text)), heading, limit))
            pending = None
            index = close + 1
            continue

        # Paragraph: up to a blank line, the next heading, or the next fence.
        close = index
        while (
            close < len(lines)
            and lines[close].strip()
            and not _HEADING.match(lines[close])
            and not _FENCE.match(lines[close])
        ):
            close += 1
        end = offsets[close] if close < len(lines) else len(text)
        if end > block_start:
            atoms.extend(_split_block(text, block_start, end, heading, limit))
            pending = None
        index = close if close > index else index + 1

    if pending is not None and text[pending:].strip():
        atoms.append(Atom(pending, len(text.rstrip()), heading))

    return atoms


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    body = text[start:end]
    stripped = body.strip()
    if not stripped:
        return start, start
    leading = len(body) - len(body.lstrip())
    trailing = len(body) - len(body.rstrip())
    return start + leading, end - trailing


def chunk(text: str, *, chunk_tokens: int = 700, overlap_tokens: int = 80) -> list[Chunk]:
    """Split ``text`` into citation-exact chunks."""
    if not text.strip():
        return []

    max_chars = max(200, chunk_tokens * CHARS_PER_TOKEN)
    overlap_chars = max(0, min(overlap_tokens * CHARS_PER_TOKEN, max_chars // 3))
    atoms = _atoms(text, max_chars)

    if not atoms:
        start, end = _trim(text, 0, len(text))
        return [Chunk(text[start:end], "", start, end)]

    out: list[Chunk] = []
    index = 0
    while index < len(atoms):
        first = index
        last = index
        while (
            last + 1 < len(atoms)
            and atoms[last + 1].heading == atoms[first].heading
            and atoms[last + 1].end - atoms[first].start <= max_chars
        ):
            last += 1

        start = atoms[first].start
        end = atoms[last].end
        if out and overlap_chars:
            # Carry a little of the previous chunk forward, but never past its start.
            start = max(atoms[first].start - overlap_chars, out[-1].start + 1, 0)
        start, end = _trim(text, start, end)
        if end > start:
            out.append(Chunk(text[start:end], atoms[first].heading, start, end))
        index = last + 1

    return out
