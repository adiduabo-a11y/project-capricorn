"""Run all offline unit and regression test scripts sequentially.

Run with:
    uv run python services/core/tests/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    tests_dir = Path(__file__).resolve().parent
    scripts = sorted(tests_dir.glob("test_*.py"))
    if not scripts:
        print("FAIL no test_*.py scripts found", flush=True)
        return 1

    failed: list[str] = []
    started = time.monotonic()

    for script in scripts:
        t0 = time.monotonic()
        print(f"=== {script.name} ===", flush=True)
        proc = subprocess.run([sys.executable, str(script)])
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            print(f"FAIL {script.name} exited {proc.returncode} ({elapsed:.1f}s)", flush=True)
            failed.append(script.name)
        else:
            print(f"ok   {script.name} ({elapsed:.1f}s)", flush=True)

    total = time.monotonic() - started
    passed = len(scripts) - len(failed)
    print(f"\n{passed}/{len(scripts)} test scripts passed in {total:.1f}s", flush=True)
    if failed:
        print("failed:", ", ".join(failed), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
