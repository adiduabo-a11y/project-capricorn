"""Text extraction for ingest.

Text formats are read directly. PDF, DOCX and HTML go through optional
dependencies imported lazily, so the daemon runs without them and says so
plainly when a document needs one.

Nothing here decides *what* to index — callers do. This module only turns a
path into text plus a title, or refuses with a readable reason.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("capri.rag.loaders")

MAX_BYTES = 2_000_000

TEXT_SUFFIXES = {
    ".md", ".markdown", ".mdx", ".txt", ".rst", ".log",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".csv", ".tsv",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".cs", ".lua",
    ".sql", ".css", ".scss", ".sh", ".ps1", ".java", ".kt", ".rb", ".php",
}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
DOC_SUFFIXES = {".pdf", ".docx"}

SUPPORTED = TEXT_SUFFIXES | HTML_SUFFIXES | DOC_SUFFIXES

MIME = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".mdx": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/x-rst",
    ".log": "text/plain",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class LoadError(RuntimeError):
    """A file could not be turned into text."""


@dataclass
class Loaded:
    text: str
    title: str
    mime: str
    kind: str


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED


def mime_for(path: Path) -> str:
    return MIME.get(path.suffix.lower(), "text/plain")


def _title_for(path: Path, text: str) -> str:
    """First markdown heading if there is one, otherwise the filename."""
    for line in text.splitlines()[:20]:
        match = re.match(r"^#{1,6}\s+(.*\S)\s*$", line)
        if match:
            return match.group(1).strip()[:160]
    return path.stem[:160]


class _TextExtractor(HTMLParser):
    """Stdlib HTML → text, for when trafilatura is not installed."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK and not self._skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK and not self._skip_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if not self._skip_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _html_text(raw: str) -> str:
    try:
        import trafilatura  # type: ignore[import-not-found]
    except ImportError:
        trafilatura = None
    if trafilatura is not None:
        extracted = trafilatura.extract(raw, include_comments=False, include_tables=True)
        if extracted:
            return extracted
    parser = _TextExtractor()
    parser.feed(raw)
    return parser.text()


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LoadError("PDF ingest needs pypdf (pip install pypdf)") from exc
    from io import BytesIO

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise LoadError(f"could not read PDF: {exc}") from exc
    pages: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # a broken page must not kill the document
            log.warning("pdf page %d failed: %s", number, exc)
            continue
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def _docx_text(data: bytes) -> str:
    try:
        import docx  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LoadError("DOCX ingest needs python-docx (pip install python-docx)") from exc
    from io import BytesIO

    try:
        document = docx.Document(BytesIO(data))
    except Exception as exc:
        raise LoadError(f"could not read DOCX: {exc}") from exc
    return "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())


def load(path: Path, *, max_bytes: int = MAX_BYTES) -> Loaded:
    """Read one file into text. Raises ``LoadError`` with a usable message."""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise LoadError(f"unsupported file type {suffix or '(none)'}")

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LoadError(str(exc)) from exc
    if size > max_bytes:
        raise LoadError(f"file is {size} bytes, over the {max_bytes} byte cap")

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LoadError(str(exc)) from exc

    if suffix in DOC_SUFFIXES:
        text = _pdf_text(data) if suffix == ".pdf" else _docx_text(data)
        kind = suffix.lstrip(".")
    elif suffix in HTML_SUFFIXES:
        text = _html_text(data.decode("utf-8", errors="replace"))
        kind = "html"
    else:
        if b"\x00" in data[:4096]:
            raise LoadError("looks binary")
        text = data.decode("utf-8", errors="replace")
        kind = "code" if suffix not in {".md", ".markdown", ".mdx", ".txt", ".rst"} else "text"

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise LoadError("no extractable text")
    return Loaded(text=text, title=_title_for(path, text), mime=mime_for(path), kind=kind)


def html_strip(raw: str) -> str:
    """Public helper, used by tests that do not want a real HTML dependency."""
    return html.unescape(_html_text(raw))
