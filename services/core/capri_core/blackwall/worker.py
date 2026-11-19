"""Warm, contained workers for installed Blackwall tools.

A worker is still a separate process with its own temporary HOME and cwd, but
it imports the implementation once and then answers one JSON request per line.
That keeps ordinary tool calls fast without ever putting generated code in the
daemon's interpreter. A crashed or wedged worker is discarded; the manager can
fall back to the one-shot sandbox while a replacement starts.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .manifest import ToolManifest
from .sandbox import RUNTIME_DIR, _environment, _guard_source, _python_executable

log = logging.getLogger("capri.blackwall.worker")

SENTINEL = "__CAPRI_BLACKWALL_WORKER__"

_WORKER = r'''
import importlib.util
import json
import socket
import sys
import traceback
from pathlib import Path

package = Path(sys.argv[1])
entry = sys.argv[2]
module_file, function_name = entry.split(":", 1)

__CAPRI_SOCKET_GUARD__

if len(sys.argv) > 4:
    sys.path.insert(0, sys.argv[4])
sys.path.insert(0, str(package))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

try:
    module = load("capri_blackwall_impl", package / module_file)
    function = getattr(module, function_name)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            result = function(payload)
            if result is None:
                result = {"ok": True, "output": ""}
            elif isinstance(result, str):
                result = {"ok": True, "output": result}
            elif not isinstance(result, dict):
                result = {"ok": True, "output": str(result)}
            else:
                result = {
                    "ok": bool(result.get("ok", True)),
                    "output": str(result.get("output", "")),
                    "data": result.get("data"),
                }
        except Exception:
            result = {"ok": False, "output": traceback.format_exc(limit=8)}
        sys.stdout.write("\n__CAPRI_BLACKWALL_WORKER__" + json.dumps(result, default=str) + "\n")
        sys.stdout.flush()
except Exception:
    sys.stderr.write(traceback.format_exc(limit=8))
    raise
'''

_WORKER = _WORKER.replace("__CAPRI_SOCKET_GUARD__", _guard_source(3))


class WorkerError(RuntimeError):
    pass


class Worker:
    """One warm process for one installed package."""

    def __init__(
        self,
        package: Path,
        manifest: ToolManifest,
        net: dict[str, Any] | None = None,
    ) -> None:
        self.package = package.resolve()
        self.manifest = manifest
        self.net = net
        self._lock = threading.Lock()
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=30)
        self._process: Any = None
        self._windows: Any = None
        self._temp: Path | None = None
        self._reader: threading.Thread | None = None
        self._error_reader: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------------ lifecycle

    def _start(self) -> None:
        self._queue = queue.Queue()
        self._stderr.clear()
        self._temp = Path(tempfile.mkdtemp(prefix="capri-bw-worker-"))
        env = _environment(self._temp, self.net)
        command = [
            _python_executable(),
            "-I",
            "-c",
            _WORKER,
            str(self.package),
            self.manifest.entry,
            json.dumps(self.net or {}, sort_keys=True),
            str(RUNTIME_DIR),
        ]
        if os.name == "nt":
            from . import win_sandbox

            self._windows = win_sandbox.spawn(
                command,
                cwd=self._temp,
                env=env,
                want_stdin=True,
            )
            proc = self._windows
            stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        else:
            proc = subprocess.Popen(
                command,
                cwd=self._temp,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        if stdin is None or stdout is None or stderr is None:
            self._kill()
            raise WorkerError("worker pipes were not created")
        self._process = proc
        self._reader = threading.Thread(
            target=self._drain_stdout, args=(stdout,), daemon=True
        )
        self._error_reader = threading.Thread(
            target=self._drain_stderr, args=(stderr,), daemon=True
        )
        self._reader.start()
        self._error_reader.start()

    def _drain_stdout(self, stream: Any) -> None:
        try:
            while True:
                line = stream.readline(1_048_576)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    while True:
                        rest = stream.readline(1_048_576)
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
                if line.startswith(SENTINEL.encode("ascii")):
                    self._queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)

    def _drain_stderr(self, stream: Any) -> None:
        try:
            while True:
                line = stream.readline(65_536)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    while True:
                        rest = stream.readline(65_536)
                        if not rest or rest.endswith(b"\n"):
                            break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text[:400])
        except (OSError, ValueError):
            pass

    def _kill(self) -> None:
        process = self._process
        self._process = None
        if self._windows is not None:
            try:
                self._windows.terminate()
            except OSError:
                pass
            self._windows.close()
            self._windows = None
        elif process is not None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass
            for stream in (
                getattr(process, "stdin", None),
                getattr(process, "stdout", None),
                getattr(process, "stderr", None),
            ):
                try:
                    stream.close()
                except (AttributeError, OSError):
                    pass
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None
        self._queue.put(None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._kill()

    # ------------------------------------------------------------------ request

    def call(self, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise WorkerError("worker is closed")
            if self._process is None:
                self._start()
            assert self._process is not None
            try:
                stdin = (
                    self._process.stdin
                    if self._windows is not None
                    else self._process.stdin
                )
                if stdin is None:
                    raise WorkerError("worker stdin is closed")
                stdin.write((json.dumps(args, ensure_ascii=False) + "\n").encode("utf-8"))
                stdin.flush()
            except (OSError, ValueError) as exc:
                self._kill()
                raise WorkerError(f"could not send to worker: {exc}") from exc

            deadline = time.monotonic() + max(0.1, timeout_s)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill()
                    raise WorkerError(f"worker timed out after {timeout_s:.1f}s")
                try:
                    line = self._queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    self._kill()
                    tail = "\n".join(self._stderr)
                    raise WorkerError(f"worker exited before replying\n{tail[-2000:]}")
                try:
                    return json.loads(line[len(SENTINEL) :])
                except (ValueError, TypeError, IndexError) as exc:
                    self._kill()
                    raise WorkerError(f"worker returned an invalid result: {exc}") from exc
