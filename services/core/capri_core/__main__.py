"""Entry point: ``python -m capri_core``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__, paths
from .config import dump_yaml, load_config, schema
from .server import run_server

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-20s %(message)s"
LOG_DATE = "%H:%M:%S"


def _wake_expectation(clip: Path) -> tuple[str, str] | None:
    """`wake` / `no-wake` on line one, the expected request on line two."""
    sidecar = clip.with_suffix(".txt")
    if not sidecar.is_file():
        return None
    # utf-8-sig: Notepad and PowerShell both like to start a file with a BOM.
    lines = [line.strip() for line in sidecar.read_text(encoding="utf-8-sig").splitlines()]
    verdict = (lines[0] if lines else "").lower().replace("_", "-")
    return verdict, (lines[1] if len(lines) > 1 else "")


def _wake_check(config, target: str) -> int:
    """Answer, per clip, the only question that matters: was it for her?"""
    from .voice import build_stt
    from .voice.stt import SttUnavailable
    from .voice.wake import match_wake

    path = Path(target).expanduser()
    clips = sorted(path.glob("*.wav")) if path.is_dir() else [path]
    if not path.exists():
        print(f"[wake-check] no such file or folder: {path}", file=sys.stderr)
        return 1
    if not clips:
        print(f"[wake-check] no .wav files in {path}", file=sys.stderr)
        return 1

    stt = build_stt(config)
    if not stt.available():
        print(f"[wake-check] speech input unavailable: {stt.reason()}", file=sys.stderr)
        return 1

    failures = 0
    expected = 0
    for clip in clips:
        try:
            transcript = stt.transcribe(clip.read_bytes())
        except (OSError, SttUnavailable) as exc:
            print(f"  FAIL {clip.name}: {exc}")
            failures += 1
            continue
        hit = match_wake(transcript.text)
        mark = " ok  "
        note = ""
        expectation = _wake_expectation(clip)
        if expectation is not None:
            wanted, command = expectation
            expected += 1
            ok = hit.matched is (wanted != "no-wake")
            if ok and hit.matched and command and command != "-":
                ok = command.lower() in hit.command.lower()
            if not ok:
                failures += 1
                mark = " FAIL"
            note = f"  expected {wanted}" + (f" / {command}" if command and command != "-" else "")
        print(
            f" {mark} {clip.name}: matched={hit.matched} "
            f"confidence={transcript.confidence:.2f} command={hit.command!r}{note}"
        )
        print(f"       heard: {transcript.text!r}")

    print(
        f"[wake-check] {len(clips)} clip(s), {expected} with expectations, "
        f"{failures} failure(s)"
    )
    return 1 if failures else 0


def _wake_grid(config, target: str) -> int:
    """Sweep the gate's two limits over clips whose answers are known."""
    from .voice import build_stt
    from .voice.grid import CURRENT, Case, recommend, sweep
    from .voice.stt import SttUnavailable
    from .voice.wake import match_wake

    path = Path(target).expanduser()
    clips = sorted(path.glob("*.wav")) if path.is_dir() else [path]
    if not path.exists():
        print(f"[wake-grid] no such file or folder: {path}", file=sys.stderr)
        return 1
    if not clips:
        print(f"[wake-grid] no .wav files in {path}", file=sys.stderr)
        return 1

    stt = build_stt(config)
    if not stt.available():
        print(f"[wake-grid] speech input unavailable: {stt.reason()}", file=sys.stderr)
        return 1

    cases: list[Case] = []
    for clip in clips:
        expectation = _wake_expectation(clip)
        if expectation is None:
            continue
        verdict, _command = expectation
        try:
            transcript = stt.transcribe(clip.read_bytes())
        except (OSError, SttUnavailable) as exc:
            print(f"  FAIL {clip.name}: {exc}")
            continue
        hit = match_wake(transcript.text)
        cases.append(
            Case(
                name=clip.name,
                wake=verdict != "no-wake",
                matched=hit.matched,
                confidence=transcript.confidence,
                no_speech=transcript.no_speech_prob,
            )
        )
        mark = "ok  " if hit.matched is (verdict != "no-wake") else "WRONG"
        print(
            f" {mark} {clip.name:<34} want={'wake' if verdict != 'no-wake' else 'no-wake'} "
            f"matched={hit.matched} conf={transcript.confidence:.2f} "
            f"noSpeech={transcript.no_speech_prob:.2f} heard={transcript.text!r}"
        )

    if not cases:
        print("[wake-grid] no clips with expectations", file=sys.stderr)
        return 1

    cells = sweep(cases)
    tails = sorted({cell.max_no_speech for cell in cells})
    print()
    print("[wake-grid] min_confidence (rows) x max_no_speech (columns): kept%/false wakes")
    print(" " * 8 + "".join(f"{tail:>13.2f}" for tail in tails))
    for confidence in sorted({cell.min_confidence for cell in cells}):
        row = [cell for cell in cells if cell.min_confidence == confidence]
        print(
            f"  {confidence:>5.2f} "
            + "".join(
                f"{cell.accept_rate * 100:>9.0f}%/{cell.woke:<2}" for cell in row
            )
        )

    best = recommend(cells)
    if best is None:
        print("[wake-grid] no cell rejects every negative")
        return 1
    change = "" if (best.min_confidence, best.max_no_speech) == CURRENT else " (shipped: %s / %s)" % CURRENT
    print(
        f"[wake-grid] {len(cases)} clips; recommend min_confidence "
        f"{best.min_confidence:.2f}, max_no_speech {best.max_no_speech:.2f}: "
        f"{best.accept_rate * 100:.0f}% kept, {best.woke} false wakes{change}"
    )
    return 0


def setup_logging(level: str) -> None:
    paths.ensure_dirs()
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATE)
    root = logging.getLogger()
    root.setLevel(level.upper())

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file = RotatingFileHandler(
        paths.logs_dir() / "core.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file.setFormatter(formatter)
    root.addHandler(file)

    logging.getLogger("websockets").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capri-core", description="Capri agent daemon")
    parser.add_argument("--version", action="version", version=f"capri-core {__version__}")
    parser.add_argument("--host", default=None, help="bind address (default from config)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default from config)")
    parser.add_argument("--log-level", default="INFO", help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument("--print-token", action="store_true", help="print the loopback token and exit")
    parser.add_argument("--print-config", action="store_true", help="print resolved config and exit")
    parser.add_argument("--print-schema", action="store_true", help="print config JSON Schema and exit")
    parser.add_argument(
        "--voice-status",
        action="store_true",
        help="report the TTS engine and which model files are missing",
    )
    parser.add_argument(
        "--voice-reference",
        action="store_true",
        help="render Capri's clone reference from the synthetic Kokoro blend",
    )
    parser.add_argument(
        "--voice-install",
        metavar="PATH",
        default=None,
        help="install a Pocket voice state from a mod zip or a .safetensors file",
    )
    parser.add_argument(
        "--wake-grid",
        metavar="PATH",
        default=None,
        help=(
            "transcribe a folder of clips and sweep the confidence and no-speech "
            "limits, reporting the accept rate and false wakes per pair"
        ),
    )
    parser.add_argument(
        "--wake-check",
        metavar="PATH",
        default=None,
        help=(
            "transcribe a WAV file, or every .wav in a folder, and report whether "
            "it opened with her name; a .txt beside the clip states the answer"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.print_token:
        print(paths.token())
        return 0

    config = load_config()

    if args.print_config:
        sys.stdout.write(dump_yaml(config))
        return 0

    if args.print_schema:
        json.dump(schema(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.voice_reference:
        from .voice.clone import render_reference
        from .voice.tts import TtsUnavailable

        try:
            result = render_reference(config)
        except TtsUnavailable as exc:
            json.dump({"ok": False, "reason": str(exc)}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 1
        json.dump({"ok": True, **result}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.voice_install:
        from pathlib import Path

        from .voice.pocket import install_voice_state

        try:
            result = install_voice_state(Path(args.voice_install), config)
        except (OSError, ValueError) as exc:
            json.dump({"ok": False, "reason": str(exc)}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 1
        json.dump({"ok": True, **result}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.wake_check:
        return _wake_check(config, args.wake_check)

    if getattr(args, "wake_grid", None):
        return _wake_grid(config, args.wake_grid)

    if args.wake_grid:
        return _wake_grid(config, args.wake_grid)

    if args.voice_status:
        from .voice import build_engine, build_stt

        engine = build_engine(config)
        stt = build_stt(config)
        files = {}
        for attribute in ("model_path", "voices_path", "reference", "voice_file", "path"):
            path = getattr(engine, attribute, None)
            if path is not None:
                files[path.name] = str(path)
        json.dump(
            {
                "engine": engine.name,
                "available": engine.available(),
                "reason": engine.reason(),
                "sampleRate": engine.sample_rate,
                "modelDir": str(getattr(engine, "directory", "")),
                "files": files,
                "stt": stt.status(),
                # The gate's numbers, so a room's calibration can be checked
                # and compared without opening the app.
                "wake": {
                    "enabled": config.voice.wake.enabled,
                    "thresholdRms": config.voice.wake.threshold_rms,
                    "minUtteranceMs": config.voice.wake.min_utterance_ms,
                    "silenceMs": config.voice.wake.silence_ms,
                    "followUpMs": config.voice.wake.follow_up_ms,
                    "minConfidence": config.voice.wake.min_confidence,
                    "maxNoSpeechProb": config.voice.wake.max_no_speech_prob,
                },
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    setup_logging(args.log_level)
    log = logging.getLogger("capri.main")
    log.info("capri-core %s starting (home=%s)", __version__, paths.capri_home())

    try:
        asyncio.run(run_server(config, host=args.host, port=args.port))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
