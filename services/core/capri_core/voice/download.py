"""Voice model download helper with atomic temp file replacement."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger("capri.voice.download")


def download_file(url: str, dest: Path) -> None:
    """Download a remote file to dest atomically using a .part temporary file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(dest.suffix + f".part-{Path(dest).name}")
    try:
        log.info("downloading %s to %s", url, dest)
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(temp, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        temp.replace(dest)
        log.info("download finished: %s", dest.name)
    except Exception as exc:
        if temp.exists():
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise exc
