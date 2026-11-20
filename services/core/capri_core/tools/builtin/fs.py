"""Filesystem tools: read, list, grep, write.

These do not enforce policy themselves — the loop checks every call before it
is dispatched. What they *do* enforce is bounds: size caps so a stray read of a
2 GB log cannot eat the context window, and a line range so the model can ask
for the part of a file it actually needs.

``fs.read`` returns line numbers and a content hash. The hash is what makes an
exact-match patch safe: if the file changed since it was read, the patch is
refused instead of applied to stale offsets.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import time
from pathlib import Path

from ...workspace.tree import (
    EXCLUDED_DIRS,
    gitignore_rules as _gitignore_rules,
    is_ignored as _ignored,
    is_link as _is_link,
)
from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms
from ..policy import resolve_path

MAX_READ_BYTES = 400_000
MAX_READ_LINES = 400
MAX_RANGE_LINES = 2000
MAX_WRITE_BYTES = 2_000_000
MAX_LIST_ENTRIES = 500
MAX_GREP_FILES = 5000
MAX_GREP_FILE_BYTES = 1_000_000
MAX_GREP_LINE = 300
HASH_CHUNK = 1 << 20

READ_SPEC = ToolSpec(
    name="fs.read",
    description=(
        "Read a UTF-8 text file by line range. Returns numbered lines, the total "
        "line count and a sha256 of the whole file."
    ),
    tier=0,
    max_calls_per_turn=24,
    observation="file",
    params={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, absolute or workspace-relative."},
            "start_line": {
                "type": "integer",
                "description": "First line to return, 1-based. Default 1.",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to return, inclusive. Default start_line + 399.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Stop decoding after this many bytes of the file.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)

LIST_SPEC = ToolSpec(
    name="fs.list",
    description="List a directory: names, sizes and whether each entry is a directory.",
    tier=0,
    max_calls_per_turn=16,
    observation="tree",
    params={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)

GREP_SPEC = ToolSpec(
    name="fs.grep",
    description=(
        "Search text files for a regular expression. Returns path:line: text for "
        "each match, so a symbol can be found without guessing filenames."
    ),
    tier=0,
    max_calls_per_turn=16,
    observation="grep",
    params={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {
                "type": "string",
                "description": "Directory or file to search. Defaults to the workspace root.",
            },
            "include": {
                "type": "string",
                "description": "Glob filter on the filename or relative path, e.g. *.py.",
            },
            "case_sensitive": {"type": "boolean", "description": "Default false."},
            "max_matches": {
                "type": "integer",
                "description": "Stop after this many matches. Default 80, max 500.",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
)

WRITE_SPEC = ToolSpec(
    name="fs.write",
    mutates=True,
    description=(
        "Create a new text file, creating parent directories. An existing path is "
        "refused: use fs.patch to change a file that is already there."
    ),
    tier=2,
    params={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, absolute or workspace-relative."},
            "content": {"type": "string", "description": "Full text to write."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)


def _display(path: str, workspace: str) -> str:
    try:
        return os.path.relpath(path, workspace)
    except ValueError:
        return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(raw: bytes) -> tuple[str, str]:
    """UTF-8, BOM tolerated. Bad bytes come back flagged, not silently fixed."""
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), "unknown"


def _read_slice_sync(
    file: Path, first: int, last: int, cap: int
) -> tuple[int, str, list[str], int | None, str, int]:
    size = file.stat().st_size
    digest = _sha256(file)
    if size <= cap or first == 1:
        with file.open("rb") as handle:
            raw = handle.read(cap)
        text, encoding = _decode(raw)
        lines = text.splitlines()
        total_lines = len(lines) if len(raw) >= size else None
        if total_lines is not None:
            last = min(last, total_lines)
        sliced = [
            f"{number:5d}: {lines[number - 1]}"
            for number in range(first, min(last, len(lines)) + 1)
        ]
        return size, digest, sliced, total_lines, encoding, len(raw)

    # Large file with start_line > 1: stream past earlier lines instead of
    # slicing only the first `cap` bytes from byte 0.
    collected: list[str] = []
    collected_bytes = 0
    encoding = "utf-8"
    line_no = 0
    eof = False
    with file.open("rb") as handle:
        while True:
            raw_line = handle.readline()
            if not raw_line:
                eof = True
                break
            line_no += 1
            if line_no < first:
                continue
            if line_no > last:
                break
            if line_no == first and raw_line.startswith(b"\xef\xbb\xbf"):
                raw_line = raw_line[3:]
            stripped = raw_line.rstrip(b"\r\n")
            try:
                text_line = stripped.decode("utf-8")
            except UnicodeDecodeError:
                encoding = "unknown"
                text_line = stripped.decode("utf-8", errors="replace")
            if collected and collected_bytes + len(raw_line) > cap:
                break
            collected.append(f"{line_no:5d}: {text_line}")
            collected_bytes += len(raw_line)
    total_lines = line_no if eof else None
    actual_last = min(last, line_no) if eof else (first + len(collected) - 1 if collected else last)
    return size, digest, collected, total_lines, encoding, collected_bytes


def _extract_pdf_sync(path: Path) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = []
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                pages.append(f"--- Page {i} ---\n{t}")
        return "\n\n".join(pages)
    except Exception as exc:
        raise OSError(f"could not read PDF: {exc}") from exc


def _extract_docx_sync(path: Path) -> str:
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(str(path)) as z:
            tree = ET.fromstring(z.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            lines = []
            for p in tree.findall(".//w:p", ns):
                text = "".join(p.itertext()).strip()
                if text:
                    lines.append(text)
            return "\n".join(lines)
    except Exception as exc:
        raise OSError(f"could not read DOCX: {exc}") from exc


async def fs_read(
    ctx: ToolContext,
    *,
    path: str,
    start_line: int = 1,
    end_line: int = 0,
    max_bytes: int = MAX_READ_BYTES,
) -> ToolResult:
    started = time.monotonic()
    target = resolve_path(path, ctx.workspace)
    file = Path(target)

    if not file.exists():
        return ToolResult.error(f"fs.read: no such file: {_display(target, ctx.workspace)}")
    if file.is_dir():
        return ToolResult.error(
            f"fs.read: {_display(target, ctx.workspace)} is a directory; use fs.list"
        )

    cap = min(max(1, int(max_bytes or MAX_READ_BYTES)), MAX_READ_BYTES)
    first = max(1, int(start_line or 1))
    last = int(end_line or 0)
    if last <= 0:
        last = first + MAX_READ_LINES - 1
    last = max(first, min(last, first + MAX_RANGE_LINES - 1))

    suffix = file.suffix.lower()
    if suffix in (".pdf", ".docx"):
        try:
            if suffix == ".pdf":
                raw_text = await asyncio.to_thread(_extract_pdf_sync, file)
                doc_type = "PDF"
            else:
                raw_text = await asyncio.to_thread(_extract_docx_sync, file)
                doc_type = "DOCX"
        except OSError as exc:
            return ToolResult.error(f"fs.read: {exc}")

        all_lines = raw_text.splitlines()
        total_lines = len(all_lines)
        if total_lines > 0 and first > total_lines:
            return ToolResult.error(f"fs.read: start_line {first} is past the end of {total_lines}-line document")
        slice_lines = all_lines[first - 1 : last]
        numbered = [f"{first + i:6d}  {line}" for i, line in enumerate(slice_lines)]
        body = "\n".join(numbered) or "(empty document)"
        header = f"[{doc_type} document: {file.name} ({total_lines} lines extracted)]\n"
        if last < total_lines:
            body = f"{header}{body}\n[lines {first}-{min(last, total_lines)} of {total_lines}; continue with start_line={last + 1}]"
        else:
            body = f"{header}{body}"

        digest = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()
        if ctx.reads is not None:
            ctx.reads[target] = digest

        return ToolResult(
            ok=True,
            output=body,
            data={
                "path": target,
                "first": first,
                "last": min(last, total_lines),
                "total": total_lines,
                "documentType": doc_type,
            },
            ms=elapsed_ms(started),
        )

    try:
        size, digest, body_lines, total_lines, encoding, read_bytes = await asyncio.to_thread(
            _read_slice_sync, file, first, last, cap
        )
    except OSError as exc:
        return ToolResult.error(f"fs.read: {exc}")

    if total_lines is not None:
        last = min(last, total_lines)

    if total_lines is not None and total_lines > 0 and first > total_lines:
        return ToolResult.error(
            f"fs.read: start_line {first} is past the end of {total_lines}-line file"
        )

    body = "\n".join(body_lines) or "(empty file)"
    notes: list[str] = []
    if total_lines is None:
        notes.append(f"[file continues past {read_bytes} bytes; line count unknown]")
        shown_end = first + len(body_lines) - 1 if body_lines else last
        notes.append(f"[lines {first}-{shown_end}; continue with start_line={shown_end + 1}]")
    elif last < total_lines:
        notes.append(
            f"[lines {first}-{last} of {total_lines}; continue with start_line={last + 1}]"
        )
    if notes:
        body = "\n".join([body, *notes])
    if encoding != "utf-8":
        # First line, not last: shaping keeps the head of a long read.
        body = "[not valid UTF-8 - fs.patch will refuse this file]\n" + body

    # The hash the patch tool checks against is the whole file's, even for a
    # ranged read: a stale patch is stale regardless of which lines were seen.
    if ctx.reads is not None:
        ctx.reads[target] = digest

    return ToolResult(
        ok=True,
        output=body,
        data={
            "path": target,
            "bytes": size,
            "sha256": digest,
            "total_lines": total_lines,
            "start_line": first,
            "end_line": last,
            "encoding": encoding,
        },
        ms=elapsed_ms(started),
    )


async def fs_list(ctx: ToolContext, *, path: str) -> ToolResult:
    started = time.monotonic()
    target = resolve_path(path, ctx.workspace)
    directory = Path(target)

    if not directory.exists():
        return ToolResult.error(f"fs.list: no such directory: {_display(target, ctx.workspace)}")
    if not directory.is_dir():
        return ToolResult.error(f"fs.list: {_display(target, ctx.workspace)} is not a directory")

    lines: list[str] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return ToolResult.error(f"fs.list: {exc}")

    for entry in entries[:MAX_LIST_ENTRIES]:
        try:
            if entry.is_dir():
                lines.append(f"{entry.name}/")
            else:
                lines.append(f"{entry.name}  {entry.stat().st_size}B")
        except OSError:
            lines.append(f"{entry.name}  ?")
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"... {len(entries) - MAX_LIST_ENTRIES} more entries")

    return ToolResult(
        ok=True,
        output="\n".join(lines) or "(empty directory)",
        data={"path": target, "count": len(entries)},
        ms=elapsed_ms(started),
    )


# --------------------------------------------------------------------------- grep


def _walk_files(root: Path, include: str) -> list[Path]:
    rules = _gitignore_rules(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        keep: list[str] = []
        for name in dirnames:
            full = current / name
            if name in EXCLUDED_DIRS or _is_link(full):
                continue
            if _ignored(os.path.relpath(full, root), name, rules):
                continue
            keep.append(name)
        dirnames[:] = keep

        for name in filenames:
            full = current / name
            if _is_link(full):
                continue
            rel = os.path.relpath(full, root)
            if _ignored(rel, name, rules):
                continue
            if include and not (
                fnmatch.fnmatch(name, include) or fnmatch.fnmatch(rel.replace("\\", "/"), include)
            ):
                continue
            found.append(full)
            if len(found) >= MAX_GREP_FILES:
                return found
    return found


def _grep_sync(
    target: Path,
    workspace: str,
    expression: re.Pattern[str],
    include: str,
    limit: int,
) -> tuple[list[str], int, bool]:
    root = target if target.is_dir() else target.parent
    files = [target] if target.is_file() else _walk_files(root, include)

    matches: list[str] = []
    scanned = 0
    capped = False
    for file in files:
        if capped:
            break
        try:
            if file.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            raw = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # The plan says text files only. A replacement-decoded binary is a
            # source of confident nonsense.
            continue
        scanned += 1
        display = _display(str(file), workspace)
        for number, line in enumerate(text.splitlines(), 1):
            if not expression.search(line):
                continue
            matches.append(f"{display}:{number}: {line.strip()[:MAX_GREP_LINE]}")
            if len(matches) >= limit:
                capped = True
                break
    return matches, scanned, capped


async def fs_grep(
    ctx: ToolContext,
    *,
    pattern: str,
    path: str = "",
    include: str = "",
    case_sensitive: bool = False,
    max_matches: int = 80,
) -> ToolResult:
    started = time.monotonic()
    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        expression = re.compile(str(pattern), flags)
    except re.error as exc:
        return ToolResult.error(f"fs.grep: bad pattern: {exc}")

    target = Path(resolve_path(str(path or ctx.workspace), ctx.workspace))
    if not target.exists():
        return ToolResult.error(f"fs.grep: no such path: {_display(str(target), ctx.workspace)}")

    limit = max(1, min(int(max_matches or 80), 500))
    matches, scanned, capped = await asyncio.to_thread(
        _grep_sync, target, ctx.workspace, expression, include, limit
    )

    if not matches:
        return ToolResult(
            ok=True,
            output=f"(no matches in {scanned} files)",
            data={"matches": 0, "files": scanned},
            ms=elapsed_ms(started),
        )

    summary = (
        f"[showing first {limit} matches in {scanned} files; narrow the pattern]"
        if capped
        else f"[{len(matches)} matches in {scanned} files]"
    )
    return ToolResult(
        ok=True,
        output="\n".join([*matches, summary]),
        data={"matches": len(matches), "files": scanned, "capped": capped},
        ms=elapsed_ms(started),
    )


async def fs_write(ctx: ToolContext, *, path: str, content: str) -> ToolResult:
    """Create a new file.

    Overwriting is refused on purpose: a full-file write silently rewrites line
    endings, a BOM and anything else the model never saw. Existing files go
    through ``fs.patch``, which preserves all of that and leaves an undo record.
    """
    started = time.monotonic()
    target = resolve_path(path, ctx.workspace)
    data = str(content)

    if len(data.encode("utf-8")) > MAX_WRITE_BYTES:
        return ToolResult.error(f"fs.write: content exceeds {MAX_WRITE_BYTES} bytes")

    file = Path(target)
    if file.exists():
        if file.is_dir():
            return ToolResult.error(
                f"fs.write: {_display(target, ctx.workspace)} is a directory"
            )
        return ToolResult.error(
            f"fs.write: {_display(target, ctx.workspace)} already exists. "
            "Use fs.patch to edit it; fs.write only creates new files."
        )

    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(data, encoding="utf-8", newline="")
    except OSError as exc:
        return ToolResult.error(f"fs.write: {exc}")

    digest = await asyncio.to_thread(_sha256, file)
    if ctx.reads is not None:
        ctx.reads[target] = digest

    return ToolResult(
        ok=True,
        output=f"created {_display(target, ctx.workspace)}"
        f" ({len(data.encode('utf-8'))} bytes)",
        data={
            "path": target,
            "bytes": len(data.encode("utf-8")),
            "sha256": digest,
            "created": True,
        },
        ms=elapsed_ms(started),
    )
