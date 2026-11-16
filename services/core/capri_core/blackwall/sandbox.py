"""Subprocess isolation for Blackwall tools.

The implementation never imports into the daemon. It runs in a fresh Python
process with a temporary HOME/cwd, an isolated interpreter, no inherited
environment, a wall-clock ceiling, and (on Windows) no console window. The
runner blocks the common socket entry points; the static audit is still the
first gate.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .manifest import ToolManifest

log = logging.getLogger("capri.blackwall.sandbox")

_SENTINEL = "__CAPRI_BLACKWALL_RESULT__"


def _python_executable() -> str:
    """Skip the uv/venv trampoline so the Job Object can hold one process."""
    return str(getattr(sys, "_base_executable", "") or sys.executable)

# The socket policy both runners share. In v2 the tool has no direct egress at
# all: the only address it may open is the daemon's broker, which owns the
# per-host policy. This guard is a speed bump, not a network namespace - the
# audit, the broker and the approval card are the real gates - but a tool that
# ignores capri_net and reaches for a socket fails here.
_SOCKET_GUARD = r'''
import json as _capri_json
import socket as _capri_socket

_capri_net = _capri_json.loads(
    sys.argv[NET_ARG] if len(sys.argv) > NET_ARG else "{}"
) or {}
_capri_broker_host = str(_capri_net.get("host") or "").lower().strip("[]")
_capri_broker_port = int(_capri_net.get("port") or 0)


def _capri_blocked(*args, **kwargs):
    raise PermissionError(
        "the Blackwall sandbox has no direct sockets; use capri_net.fetch"
    )


def _capri_is_broker(host, port):
    if not _capri_broker_port:
        return False
    name = str(host or "").lower().strip("[]")
    return name in {_capri_broker_host, "localhost"} and int(port or 0) == _capri_broker_port


if not _capri_broker_port:
    _capri_socket.socket = _capri_blocked
    _capri_socket.create_connection = _capri_blocked
    _capri_socket.socketpair = _capri_blocked
    _capri_socket.getaddrinfo = _capri_blocked
else:
    _capri_real_socket = _capri_socket.socket
    _capri_real_create = _capri_socket.create_connection
    _capri_real_gai = _capri_socket.getaddrinfo

    class _CapriSocket(_capri_real_socket):
        def connect(self, address):
            host, port = address[0], address[1]
            if not _capri_is_broker(host, port):
                raise PermissionError(
                    "network egress to %s:%s is outside the broker" % (host, port)
                )
            return super().connect(address)

        def connect_ex(self, address):
            host, port = address[0], address[1]
            if not _capri_is_broker(host, port):
                raise PermissionError(
                    "network egress to %s:%s is outside the broker" % (host, port)
                )
            return super().connect_ex(address)

    def _capri_create_connection(address, *rest, **kwargs):
        host, port = address[0], address[1]
        if not _capri_is_broker(host, port):
            raise PermissionError(
                "network egress to %s:%s is outside the broker" % (host, port)
            )
        return _capri_real_create(address, *rest, **kwargs)

    def _capri_getaddrinfo(host, port, *rest, **kwargs):
        if not _capri_is_broker(host, port):
            raise PermissionError("DNS for %s is not the broker" % (host,))
        return _capri_real_gai(host, port, *rest, **kwargs)

    _capri_socket.socket = _CapriSocket
    _capri_socket.create_connection = _capri_create_connection
    _capri_socket.getaddrinfo = _capri_getaddrinfo
'''


def _guard_source(arg_index: int) -> str:
    return _SOCKET_GUARD.replace("NET_ARG", str(arg_index))


_RUNNER = r'''
import importlib.util
import json
import socket
import sys
import traceback
from pathlib import Path

package = Path(sys.argv[1])
mode = sys.argv[2]
payload = json.loads(sys.argv[3] or "{}")

__CAPRI_SOCKET_GUARD__

if len(sys.argv) > 6:
    sys.path.insert(0, sys.argv[6])
sys.path.insert(0, str(package))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

try:
    entry = sys.argv[4] if len(sys.argv) > 4 else "impl.py:run"
    module_file, function_name = entry.split(":", 1)
    module = load("capri_blackwall_impl", package / module_file)
    if mode == "test":
        test_module = load("capri_blackwall_test", package / "test_impl.py")
        test = getattr(test_module, "test", None)
        if test is None:
            raise RuntimeError("test_impl.py must define test()")
        test()
        result = {"ok": True, "output": "tests passed"}
    else:
        function = getattr(module, function_name)
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
    print("\n__CAPRI_BLACKWALL_RESULT__" + json.dumps(result, default=str))
except Exception:
    print("\n__CAPRI_BLACKWALL_RESULT__" + json.dumps({
        "ok": False,
        "output": traceback.format_exc(limit=8),
    }))
    raise
'''

_RUNNER = _RUNNER.replace("__CAPRI_SOCKET_GUARD__", _guard_source(5))


RUNTIME_DIR = Path(__file__).with_name("_runtime")


def _environment(home: Path, net: dict[str, Any] | None = None) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMP": str(home),
        "TEMP": str(home),
        "TMPDIR": str(home),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if net:
        env["CAPRI_NET"] = json.dumps(
            {
                "host": net.get("host"),
                "port": net.get("port"),
                "token": net.get("token"),
            },
            sort_keys=True,
        )
    for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATH"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _parse(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(_SENTINEL):
            try:
                return json.loads(line[len(_SENTINEL) :])
            except json.JSONDecodeError:
                return None
    return None


def run_process(
    package: Path,
    manifest: ToolManifest,
    *,
    mode: str,
    args: dict[str, Any] | None = None,
    net: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one test or tool call. Returns a JSON-ready result."""
    if mode not in ("test", "call"):
        return {"ok": False, "output": f"unknown sandbox mode {mode!r}"}
    timeout = max(1.0, manifest.timeout_ms / 1000 + 1.0)
    temp = Path(tempfile.mkdtemp(prefix="capri-bw-"))
    try:
        command = [
            _python_executable(),
            "-I",
            "-c",
            _RUNNER,
            str(package.resolve()),
            mode,
            json.dumps(args or {}, sort_keys=True),
            manifest.entry,
            json.dumps(net or {}, sort_keys=True),
            str(RUNTIME_DIR),
        ]
        env = _environment(temp, net)
        if os.name == "nt":
            try:
                from . import win_sandbox

                code, stdout, stderr, timed_out, _restricted, _jobbed = win_sandbox.run(
                    command,
                    cwd=temp,
                    env=env,
                    timeout_s=timeout,
                )
            except Exception:
                log.exception("contained Windows sandbox failed; using subprocess fallback")
            else:
                if timed_out:
                    return {"ok": False, "output": f"sandbox timed out after {timeout:.1f}s"}
                return _result(code, stdout[-20_000:], stderr[-8_000:])

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=flags,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": f"sandbox timed out after {timeout:.1f}s"}
        return _result(
            completed.returncode,
            (completed.stdout or "")[-20_000:],
            (completed.stderr or "")[-8_000:],
        )
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _result(code: int, output: str, error: str) -> dict[str, Any]:
    result = _parse(output)
    if result is None:
        return {
            "ok": False,
            "output": f"sandbox exited {code} without a result\n{error}",
        }
    if code != 0 and result.get("ok"):
        result = {"ok": False, "output": error or f"sandbox exited {code}"}
    result.setdefault("output", "")
    return result


def run_tests(package: Path, manifest: ToolManifest) -> dict[str, Any]:
    return run_process(package, manifest, mode="test")


def run_call(
    package: Path,
    manifest: ToolManifest,
    args: dict[str, Any],
    net: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return run_process(package, manifest, mode="call", args=args, net=net)
