"""Audio containers.

16-bit PCM mono WAV, handed to the renderer as a data URL. The shell owns the
socket and the renderer owns playback, so a file on disk would need a second
serving path for no benefit; a segment is at most a few hundred kilobytes over
loopback.
"""

from __future__ import annotations

import base64
import io
import wave

import numpy as np

WAV_MIME = "audio/wav"


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def to_data_url(wav: bytes) -> str:
    encoded = base64.b64encode(wav).decode("ascii")
    return f"data:{WAV_MIME};base64,{encoded}"
