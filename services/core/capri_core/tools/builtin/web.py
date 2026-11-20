"""Web tools: fetch public pages as readable markdown/text."""

from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urlparse

import httpx

from ..base import ToolContext, ToolResult, ToolSpec, elapsed_ms

log = logging.getLogger("capri.tools.web")

FETCH_SPEC = ToolSpec(
    name="web.fetch",
    observation="web",
    description=(
        "Fetch the content of a public web page as clean text/markdown. "
        "Follows redirects. Rejects private/internal IP addresses and localhost."
    ),
    tier=0,
    timeout_ms=15_000,
    max_calls_per_turn=8,
    params={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The HTTP or HTTPS URL to fetch, e.g. https://example.com/docs.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters of text to return (default 8000, max 30000).",
                "default": 8000,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
)

_STRIP_TAGS = re.compile(
    r"<(script|style|nav|header|footer|svg|noscript)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_CLEAN_TAGS = re.compile(r"<[^>]+>")
_MULTIPLE_NEWLINES = re.compile(r"\n{3,}")


def _is_private_ip(host: str) -> bool:
    """Is this host on loopback, private RFC1918, link-local, or cloud metadata?"""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        pass
    try:
        info = socket.getaddrinfo(host, None)
        for item in info:
            ip_str = item[4][0]
            addr = ipaddress.ip_address(ip_str)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return True
    except (socket.gaierror, OSError):
        return False
    return False


def _html_to_text(body: str) -> str:
    cleaned = _STRIP_TAGS.sub("", body)
    cleaned = re.sub(r"<(?:br|p|div|li|tr|h[1-6])\b[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = _CLEAN_TAGS.sub(" ", cleaned)
    unescaped = html.unescape(cleaned)
    lines = [" ".join(line.split()) for line in unescaped.splitlines()]
    text = "\n".join(line for line in lines if line)
    return _MULTIPLE_NEWLINES.sub("\n\n", text).strip()


async def web_fetch(
    ctx: ToolContext,
    *,
    url: str,
    max_chars: int = 8000,
) -> ToolResult:
    started = time.monotonic()
    clean_url = (url or "").strip()
    parsed = urlparse(clean_url)
    if parsed.scheme.lower() not in ("http", "https"):
        return ToolResult.error("web.fetch: URL must begin with http:// or https://")
    if not parsed.hostname:
        return ToolResult.error("web.fetch: missing hostname in URL")

    if _is_private_ip(parsed.hostname):
        return ToolResult.error(
            f"web.fetch: blocked connection to private, loopback, or internal address: {parsed.hostname}"
        )

    cap = min(max(500, int(max_chars or 8000)), 30000)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0), follow_redirects=True) as client:
            resp = await client.get(clean_url, headers=headers)
    except httpx.HTTPError as exc:
        return ToolResult.error(f"web.fetch: cannot reach {parsed.hostname}: {exc}")
    except OSError as exc:
        return ToolResult.error(f"web.fetch: network error: {exc}")

    if resp.status_code >= 400:
        return ToolResult.error(f"web.fetch: server returned HTTP {resp.status_code}")

    ctype = resp.headers.get("content-type", "").lower()
    raw_content = resp.text
    if "html" in ctype or "<html" in raw_content[:200].lower():
        text = _html_to_text(raw_content)
    else:
        text = raw_content.strip()

    truncated = False
    if len(text) > cap:
        text = text[:cap] + f"\n\n[truncated: content exceeded {cap} characters]"
        truncated = True

    return ToolResult(
        ok=True,
        output=text or "(no readable text content)",
        data={"url": str(resp.url), "status": resp.status_code, "truncated": truncated},
        ms=elapsed_ms(started),
    )
