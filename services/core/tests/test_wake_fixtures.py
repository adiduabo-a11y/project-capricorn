"""Voice input fixtures: synthetic audio, room noise, and recorded speech.

    uv run python services/core/tests/test_wake_fixtures.py

The synthetic and noise clips are generated here, so those checks need no model
and no microphone. Recorded clips are optional: drop them into
``fixtures/voice/`` (see its README) and this script transcribes them when
faster-whisper is installed. Without recordings the recorded section reports a
skip, not a failure.
"""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import numpy as np

from capri_core.config import CapriConfig
from capri_core.voice.stt import SttUnavailable, decode_wav, build_stt
from capri_core.voice.wake import match_wake

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "voice"
RATE = 16_000

FAILURES: list[str] = []
SKIPS: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def skip(label: str) -> None:
    print(f"  skip {label}")
    SKIPS.append(label)


def _tone(ms: int, rate: int = RATE, level: float = 0.4) -> np.ndarray:
    count = int(rate * ms / 1000)
    return (np.sin(np.linspace(0, 80, count)) * level).astype(np.float32)


def _noise(ms: int, rate: int = RATE, level: float = 0.05, seed: int = 7) -> np.ndarray:
    """Deterministic room noise: the TV, a fan, a street outside."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(rate * ms / 1000)) * level).astype(np.float32)


def _wav(samples: np.ndarray, rate: int = RATE, channels: int = 1) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def test_decoder() -> None:
    samples, rate = decode_wav(_wav(_tone(1000)))
    check(rate == RATE, f"a 16 kHz clip decodes at {rate} Hz")
    check(samples.dtype == np.float32, "samples are float32")
    check(abs(samples.size / rate - 1.0) < 0.02, f"duration survives the decode: {samples.size / rate:.2f} s")

    wide = decode_wav(_wav(_tone(1000), 44_100))
    check(wide[1] == 44_100, "44.1 kHz is accepted")
    high = decode_wav(_wav(_tone(1000), 48_000))
    check(high[1] == 48_000, "48 kHz is accepted")

    stereo, _ = decode_wav(_wav(_tone(500), channels=2))
    check(stereo.ndim == 1 and stereo.size == 8000, "stereo is mixed down to mono")

    refused = (
        ("an empty recording", b""),
        ("a file that is not a WAV", b"definitely not audio"),
        ("digital silence", _wav(np.zeros(RATE, dtype=np.float32))),
        ("a 60 ms click", _wav(_tone(60))),
    )
    for label, data in refused:
        reason = ""
        try:
            decode_wav(data)
        except SttUnavailable as exc:
            reason = str(exc)
        check(bool(reason), f"{label} is refused: {reason!r}")


def test_noise() -> None:
    samples, rate = decode_wav(_wav(_noise(2000)))
    check(samples.size == 2 * rate, "two seconds of room noise decodes")
    check(float(np.max(np.abs(samples))) > 0.01, "room noise is not mistaken for silence")

    # What whisper writes when there is nothing to hear. None of it is her.
    for hallucination in (
        "thank you.",
        "Thanks for watching!",
        "[BLANK_AUDIO]",
        "Subtitles by the Amara.org community",
        "I told Capri about the build yesterday.",
        "capricious weather today",
    ):
        check(not match_wake(hallucination).matched, f"does not wake: {hallucination!r}")


def test_short_burst_is_not_speech() -> None:
    """The frame gate ignores a click, so it never reaches the recogniser."""
    click = _tone(60, level=0.6)
    check(float(np.max(np.abs(click))) > 0.1, "the click is loud")
    refused = False
    try:
        decode_wav(_wav(click))
    except SttUnavailable:
        refused = True
    check(refused, "a loud click is still refused as too short")


def _expectation(clip: Path) -> tuple[str, str] | None:
    sidecar = clip.with_suffix(".txt")
    if not sidecar.is_file():
        return None
    # utf-8-sig: audio tools on Windows write a BOM, and a BOM before "no-wake"
    # used to read as a wake.
    lines = [line.strip() for line in sidecar.read_text(encoding="utf-8-sig").splitlines()]
    verdict = (lines[0] if lines else "").lower().replace("_", "-")
    return verdict, (lines[1] if len(lines) > 1 else "")


def test_sidecar_bom() -> None:
    """A sidecar saved by Notepad or PowerShell starts with a BOM."""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        clip = Path(folder) / "x.wav"
        clip.write_bytes(b"")
        clip.with_suffix(".txt").write_bytes(b"\xef\xbb\xbfno-wake\n-\n")
        found = _expectation(clip)
        check(found == ("no-wake", "-"), f"a BOM does not turn a negative into a wake: {found}")


def test_recorded_clips() -> None:
    clips = sorted(FIXTURES.glob("*.wav"))
    if not clips:
        skip(f"no recorded clips yet (see {FIXTURES})")
        return
    stt = build_stt(CapriConfig())
    if not stt.available():
        skip(f"{len(clips)} clip(s) present, but {stt.reason()}")
        return
    for clip in clips:
        expectation = _expectation(clip)
        if expectation is None:
            skip(f"{clip.name} has no .txt expectation")
            continue
        verdict, command = expectation
        try:
            transcript = stt.transcribe(clip.read_bytes())
        except (OSError, SttUnavailable) as exc:
            check(False, f"{clip.name} could not be transcribed: {exc}")
            continue
        hit = match_wake(transcript.text)
        check(
            hit.matched is (verdict != "no-wake"),
            f"{clip.name} ({transcript.text!r}) matched={hit.matched}, expected {verdict}",
        )
        if hit.matched and command and command != "-":
            check(
                command.lower() in hit.command.lower(),
                f"{clip.name} request {hit.command!r} contains {command!r}",
            )


def main() -> int:
    print("decoder")
    test_decoder()
    print("noise")
    test_noise()
    print("short burst")
    test_short_burst_is_not_speech()
    print("sidecars")
    test_sidecar_bom()
    print("recorded clips")
    test_recorded_clips()

    print()
    if SKIPS:
        print(f"{len(SKIPS)} skipped (not failures)")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
