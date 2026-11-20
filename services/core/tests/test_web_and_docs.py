"""Tests for web.fetch and binary document reading in fs.read."""

import asyncio
import io
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from capri_core.tools.base import ToolContext
from capri_core.tools.builtin.web import FETCH_SPEC, web_fetch, _is_private_ip, _html_to_text
from capri_core.tools.builtin.fs import fs_read

FAILURES: list[str] = []


def check(predicate: bool, message: str) -> None:
    if predicate:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        FAILURES.append(message)


def test_ssrf_guards() -> None:
    print("ssrf guards")
    check(_is_private_ip("127.0.0.1"), "loopback IPv4 is private")
    check(_is_private_ip("localhost"), "localhost resolves to private/loopback")
    check(_is_private_ip("10.0.0.5"), "10.x.x.x is private")
    check(_is_private_ip("192.168.1.1"), "192.168.x.x is private")
    check(_is_private_ip("169.254.169.254"), "link-local/metadata is private")
    check(not _is_private_ip("8.8.8.8"), "8.8.8.8 is public")


def test_html_cleanup() -> None:
    print("html cleanup")
    raw = """
    <html>
      <head><script>alert('evil');</script><style>body { color: red; }</style></head>
      <body>
        <nav><a href="/">Home</a></nav>
        <h1>Documentation Title</h1>
        <p>This is a paragraph with <b>bold</b> text &amp; entities.</p>
        <footer>Copyright 2026</footer>
      </body>
    </html>
    """
    clean = _html_to_text(raw)
    check("alert" not in clean, "scripts are removed")
    check("color: red" not in clean, "styles are removed")
    check("Copyright" not in clean, "footers are removed")
    check("Documentation Title" in clean, "headings preserved")
    check("This is a paragraph with bold text & entities." in clean, "text unescaped")


async def test_web_fetch_tool() -> None:
    print("web.fetch tool")
    ctx = ToolContext(conn=None, turn_id="t1", subject="default", workspace="")
    res = await web_fetch(ctx, url="ftp://example.com")
    check(not res.ok and "http://" in res.output, "rejects non-http/https")

    res2 = await web_fetch(ctx, url="http://127.0.0.1:8080/secret")
    check(not res2.ok and "blocked connection" in res2.output, "blocks loopback request")

    res3 = await web_fetch(ctx, url="http://192.168.1.50/admin")
    check(not res3.ok and "blocked connection" in res3.output, "blocks private network request")


async def test_docx_extraction() -> None:
    print("docx extraction")
    with tempfile.TemporaryDirectory() as tmpdir:
        doc_path = Path(tmpdir) / "sample.docx"
        # Create a valid minimal DOCX zip
        doc_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p><w:r><w:t>First Assignment Paragraph</w:t></w:r></w:p>
            <w:p><w:r><w:t>Second Activity Line</w:t></w:r></w:p>
          </w:body>
        </w:document>"""
        with zipfile.ZipFile(doc_path, "w") as z:
            z.writestr("word/document.xml", doc_xml.encode("utf-8"))

        ctx = ToolContext(conn=None, turn_id="t2", subject="default", workspace=tmpdir)
        res = await fs_read(ctx, path=str(doc_path))
        check(res.ok, "fs.read succeeds on .docx")
        check("DOCX document: sample.docx" in res.output, "identifies as DOCX")
        check("First Assignment Paragraph" in res.output, "extracts first paragraph")
        check("Second Activity Line" in res.output, "extracts second paragraph")


async def main() -> int:
    test_ssrf_guards()
    test_html_cleanup()
    await test_web_fetch_tool()
    await test_docx_extraction()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
