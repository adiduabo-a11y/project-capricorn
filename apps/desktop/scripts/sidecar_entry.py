"""PyInstaller entry point for the daemon sidecar."""

from __future__ import annotations

import multiprocessing

from capri_core.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
