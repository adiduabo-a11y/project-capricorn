"""Pixel-difference verification for screen actions.

A UIA hash is the cheapest signal, but canvas apps and games often change
without changing their accessibility tree. A second, visual signal lets a click
or scroll count as effective when only pixels moved. The comparison is bounded
and returns False when either capture is missing or unreadable; a missing
screenshot must never make a failed action look successful.
"""

from __future__ import annotations

import io

import numpy as np


def changed(before: bytes | None, after: bytes | None, threshold: float = 0.002) -> bool:
    if not before or not after:
        return False
    try:
        from PIL import Image

        first = Image.open(io.BytesIO(before)).convert("RGB")
        second = Image.open(io.BytesIO(after)).convert("RGB")
        if first.size != second.size:
            return True
        a = np.asarray(first, dtype=np.int16)
        b = np.asarray(second, dtype=np.int16)
        delta = np.abs(a - b)
        # Count pixels with a visible channel difference, not just any JPEG-ish
        # rounding. PNG captures are lossless, but mss output can still vary.
        visible = np.max(delta, axis=2) >= 8
        return float(np.count_nonzero(visible)) / visible.size > max(0.0, threshold)
    except Exception:
        return before != after
