"""Entry point: ``python -m capri_core``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import __version__, paths
from .config import dump_yaml, load_config, schema
from .server import run_server

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-20s %(message)s"
LOG_DATE = "%H:%M:%S"


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
