"""Test TTS fallback and voice download RPC."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from capri_core.config import CapriConfig
from capri_core.voice.service import VoiceService
from capri_core.voice.tts import Audio, ToneEngine, TtsUnavailable, UnavailableEngine
import numpy as np


class Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class DummyAvailableEngine:
    name = "dummy"
    sample_rate = 24_000

    def available(self) -> bool:
        return True

    def reason(self) -> str:
        return ""

    def voices(self) -> list[str]:
        return ["default"]

    def synthesize(self, text: str, *, voice: str, speed: float) -> Audio:
        return Audio(np.zeros(2400, dtype=np.float32), 24_000)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok   {message}")


async def test_tts_fallback() -> None:
    print("tts fallback")
    config = CapriConfig()
    config.voice.enabled = True
    config.voice.tts = "failing"

    primary = UnavailableEngine("failing", "weights missing")
    fallback = DummyAvailableEngine()

    service = VoiceService(config, primary)
    service._fallback_engine = fallback

    # Status should reflect effective engine
    st = service.status()
    check(st["engine"] == "dummy", f"effective engine in status is dummy: {st['engine']}")
    check(st["configuredEngine"] == "failing", "configured engine preserved")
    check(st["available"] is True, "service reports available via fallback")

    # Speak should synthesize via fallback
    sink = Sink()
    stats = await service.speak("trn_fallback", "Hello world through fallback.", sink)
    check(stats["segments"] > 0, f"speech segments produced: {stats['segments']}")
    check(any(e.get("type") == "speech.segment" for e in sink.events), "speech.segment event emitted")
    check(any(e.get("type") == "speech.stop" and e.get("reason") == "done" for e in sink.events), "speech.stop emitted")


async def test_download_models() -> None:
    print("download models")
    config = CapriConfig()
    config.voice.enabled = True

    # Engine without ensure_files
    service = VoiceService(config, ToneEngine(config))
    res = await service.download_models("tone")
    check(res["supported"] is False, "tone does not support auto-download")

    # Engine with ensure_files mock
    mock_eng = MagicMock()
    mock_eng.name = "mock_voice"
    mock_eng.ensure_files = MagicMock(return_value=None)
    mock_eng.available.return_value = True
    mock_eng.reason.return_value = ""

    service.engine = mock_eng
    res = await service.download_models("mock_voice")
    check(res["supported"] is True, "mock_voice supports download")
    check(res["downloaded"] is True, "mock_voice downloaded is True")
    check(mock_eng.ensure_files.called, "ensure_files called")


async def main() -> None:
    await test_tts_fallback()
    await test_download_models()
    print("\nall checks passed")


if __name__ == "__main__":
    asyncio.run(main())
