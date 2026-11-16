"""Blackwall tests: manifests, audit, sandbox, approval, stats, recipes.

No model and no network. The sandbox tests run real child processes, so this is
also a check that generated tools never execute inside the daemon's interpreter.

    uv run python services/core/tests/test_blackwall.py
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from capri_core.agent.loop import TurnRunner
from capri_core.agent.types import ChatResult, Decision
from capri_core.blackwall import Blackwall
from capri_core.blackwall.audit import audit_source
from capri_core.blackwall.broker import Broker, BrokerError
from capri_core.blackwall.manager import PROPOSE_SPEC
from capri_core.blackwall.manifest import (
    Capabilities,
    HostRule,
    SecretRule,
    parse_manifest,
)
from capri_core.blackwall.sandbox import run_call
from capri_core.config import CapriConfig
from capri_core.server.session import Session
from capri_core.store import db
from capri_core.tools import Policy, ToolContext, builtin_registry

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def manifest(name: str = "text.echo", version: str = "0.1.0") -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "description": "Echo one string.",
        "params": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "tier": 0,
    }


GOOD_IMPL = "def run(args):\n    return {'ok': True, 'output': args['text']}\n"
GOOD_TEST = (
    "from impl import run\n\n"
    "def test():\n"
    "    assert run({'text': 'hello'})['output'] == 'hello'\n"
)


class ScriptedBrain:
    """Propose a tool, use it, then finish. No model involved."""

    def __init__(self, proposal: dict[str, Any]) -> None:
        self.proposal = proposal
        self.step = 0

    async def available(self) -> bool:
        return True

    async def decide(self, msgs, specs, persona_pack: str = "kyoko") -> Decision:
        steps = [
            {"thought": "Make a tool.", "tool": "tool.propose"},
            {"thought": "Use it.", "tool": "text.loop"},
            {"thought": "Done.", "tool": "done", "final": "It works."},
        ]
        decision = Decision(**steps[min(self.step, len(steps) - 1)], usage=ChatResult(text=""))
        self.step += 1
        return decision

    async def args(self, spec, msgs, feedback=None):
        if spec.name == "tool.propose":
            return self.proposal, ChatResult(text="")
        if spec.name == "text.loop":
            return {"text": "from the loop"}, ChatResult(text="")
        return {}, ChatResult(text="")


WRITE_SOURCES = {
    "os.open non-literal flags": (
        "import os\n\ndef run(args):\n    return os.open(args['path'], args['flags'])\n"
    ),
    "io.open non-literal mode": (
        "import io\n\ndef run(args):\n    return io.open(args['path'], args['mode'])\n"
    ),
    "open non-literal mode": (
        "def run(args):\n    return open(args['path'], args['mode'])\n"
    ),
    "Path.open non-literal mode": (
        "from pathlib import Path\n\ndef run(args):\n    return Path(args['path']).open(args['mode'])\n"
    ),
    "shutil.copyfile": (
        "import shutil\n\ndef run(args):\n    shutil.copyfile(args['src'], args['dst'])\n"
    ),
    "shutil.copytree": (
        "import shutil\n\ndef run(args):\n    shutil.copytree(args['src'], args['dst'])\n"
    ),
    "shutil.copy": (
        "import shutil\n\ndef run(args):\n    shutil.copy(args['src'], args['dst'])\n"
    ),
    "shutil.move": (
        "import shutil\n\ndef run(args):\n    shutil.move(args['src'], args['dst'])\n"
    ),
    "Path.touch": (
        "from pathlib import Path\n\ndef run(args):\n    Path(args['path']).touch()\n"
    ),
    "Path.mkdir": (
        "from pathlib import Path\n\ndef run(args):\n    Path(args['path']).mkdir()\n"
    ),
    "os.makedirs": (
        "import os\n\ndef run(args):\n    os.makedirs(args['path'])\n"
    ),
    "os.chmod": (
        "import os\n\ndef run(args):\n    os.chmod(args['path'], 0o600)\n"
    ),
    "os.remove": (
        "import os\n\ndef run(args):\n    os.remove(args['path'])\n"
    ),
    "os.rename": (
        "import os\n\ndef run(args):\n    os.rename(args['src'], args['dst'])\n"
    ),
    "os.write": (
        "import os\n\ndef run(args):\n    os.write(args['fd'], b'x')\n"
    ),
    "os.fdopen": (
        "import os\n\ndef run(args):\n    os.fdopen(args['fd'], 'w')\n"
    ),
    "another os write": (
        "import os\n\ndef run(args):\n    os.fchmod(args['fd'], 0o600)\n"
    ),
    "unknown os call": (
        "import os\n\ndef run(args):\n    os.some_future_write_api(args['path'])\n"
    ),
    "unknown shutil call": (
        "import shutil\n\ndef run(args):\n    shutil.some_future_write_api(args['path'])\n"
    ),
    "unknown Path method": (
        "from pathlib import Path\n\ndef run(args):\n    Path(args['path']).some_future_write_api()\n"
    ),
    "unknown object open": (
        "def run(args):\n    args['writer'].open()\n"
    ),
    "Path variable touch": (
        "from pathlib import Path\n\ndef run(args):\n    p = Path(args['path'])\n    p.touch()\n"
    ),
    "Path variable write_text": (
        "from pathlib import Path\n\ndef run(args):\n    p = Path(args['path'])\n    p.write_text('x')\n"
    ),
    "Path variable replace": (
        "from pathlib import Path\n\ndef run(args):\n    p = Path(args['src'])\n    p.replace(args['dst'])\n"
    ),
    "open function alias": (
        "def run(args):\n    f = open\n    return f(args['path'], args['mode'])\n"
    ),
    "os.open function alias": (
        "import os\n\ndef run(args):\n    f = os.open\n    return f(args['path'], args['flags'])\n"
    ),
    "shutil.copyfile function alias": (
        "import shutil\n\ndef run(args):\n    f = shutil.copyfile\n    return f(args['src'], args['dst'])\n"
    ),
    "Path.touch method alias": (
        "from pathlib import Path\n\ndef run(args):\n    f = Path.touch\n    return f(Path(args['path']))\n"
    ),
    "Path variable method alias": (
        "from pathlib import Path\n\ndef run(args):\n    p = Path(args['path'])\n    f = p.touch\n    return f()\n"
    ),
    "module alias os.open": (
        "import os\n\ndef run(args):\n    m = os\n    return m.open(args['path'], args['flags'])\n"
    ),
    "getattr write function": (
        "import os\n\ndef run(args):\n    return getattr(os, 'write')\n"
    ),
}

SAFE_READS = (
    "import io\n"
    "import os\n"
    "from pathlib import Path\n\n"
    "def run(args):\n"
    "    os.getcwd()\n"
    "    os.path.join('a', 'b')\n"
    "    os.open('x', os.O_RDONLY)\n"
    "    open('x')\n"
    "    open('x', 'r')\n"
    "    io.open('x', 'r')\n"
    "    Path('x').read_text()\n"
    "    text = 'abc'\n"
    "    getattr(text, 'replace')('a', 'b')\n"
    "    read_open = open\n"
    "    read_open('x', 'r')\n"
    "    read_os_open = os.open\n"
    "    read_os_open('x', os.O_RDONLY)\n"
    "    read_io_open = io.open\n"
    "    read_io_open('x', 'r')\n"
    "    read_module = os\n"
    "    read_module.open('x', os.O_RDONLY)\n"
    "    return True\n"
)


def test_write_capability_gate() -> None:
    for label, source in WRITE_SOURCES.items():
        denied = audit_source(source, Capabilities())
        allowed = audit_source(source, Capabilities(write_files=True))
        check(not denied.ok, f"{label} is refused without write_files")
        check(allowed.ok, f"{label} is allowed with write_files")
    readable = audit_source(SAFE_READS, Capabilities())
    check(readable.ok, "literal read-only opens and observation calls pass without write_files")


def test_windows_containment() -> None:
    if os.name != "nt":
        return
    from capri_core.blackwall import win_sandbox

    executable = str(getattr(sys, "_base_executable", "") or sys.executable)
    code, output, error, timed_out, restricted, jobbed = win_sandbox.run(
        [executable, "-I", "-c", "print('contained')"],
        cwd=Path.cwd(),
        env=os.environ.copy(),
        timeout_s=10,
    )
    check(
        code == 0 and "contained" in output and not timed_out,
        f"Windows sandbox launches a contained child: {error[:120]}",
    )
    check(jobbed, "Windows sandbox attaches a Job Object")
    print(f"  .... restricted token: {restricted}")


class _TestServer(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - handler name from http.server
        _TestServer.seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization") or ""}
        )
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if self.path == "/away":
            self.send_response(302)
            self.send_header(
                "Location", "http://localhost:%d/ok" % self.server.server_address[1]
            )
            self.end_headers()
            return
        body = b"x" * 4096 if self.path == "/big" else ("hello " + self.path).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


def _serve() -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TestServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_network_policy() -> None:
    parsed = parse_manifest(
        {
            **manifest("text.fetch"),
            "capabilities": {
                "network": {
                    "hosts": [
                        {
                            "host": "API.Example.com",
                            "methods": ["get", "POST"],
                            "paths": ["/repos/*"],
                            "requests_per_hour": 5,
                        }
                    ],
                    "secrets": [
                        {"name": "GITHUB_TOKEN", "header": "authorization", "prefix": "Bearer ", "hosts": ["api.example.com"]}
                    ],
                }
            },
        }
    )
    caps = parsed.capabilities
    rule = caps.network_rules[0]
    check(caps.network and len(caps.network_rules) == 1, "a network policy parses")
    check(rule.host == "api.example.com" and rule.scheme == "https", "hosts normalise and default to https")
    check(rule.methods == ["GET", "POST"], "methods are upper-cased")
    check(rule.paths == ["/repos/*"], "paths survive validation")
    check(caps.network_secrets[0].header == "Authorization", "secret headers normalise")
    card = caps.network_card() or {}
    check(card.get("containment") == "advisory", "the card states the containment level")
    check((card.get("hosts") or [{}])[0].get("requestsPerHour") == 5, "the card shows the budget")
    check("value" not in json.dumps(card).lower(), "the card never carries a secret value")

    legacy = parse_manifest(
        {**manifest("text.legacy"), "capabilities": {"network": True, "network_hosts": ["API.Example.com"]}}
    )
    check(
        legacy.capabilities.network_rules[0].methods == ["GET"]
        and legacy.capabilities.network_rules[0].scheme == "https",
        "v1.1 host lists upgrade to read-only https rules",
    )

    try:
        parse_manifest(
            {**manifest("text.wild"), "capabilities": {"network": {"hosts": [{"host": "a.*.example.com"}]}}}
        )
        check(False, "wildcards are only allowed as a leading label")
    except ValueError:
        check(True, "wildcards are only allowed as a leading label")

    try:
        parse_manifest(
            {**manifest("text.pathless"), "capabilities": {"network": {"hosts": [{"host": "x.example.com", "paths": ["repos"]}]}}}
        )
        check(False, "paths must start with a slash")
    except ValueError:
        check(True, "paths must start with a slash")

    wide = parse_manifest(
        {
            **manifest("text.wide"),
            "capabilities": {
                "network": {"hosts": [{"host": "api.example.com", "requests_per_hour": 400}]}
            },
        }
    )
    wide_host = ((wide.capabilities.network_card() or {}).get("hosts") or [{}])[0]
    check(wide_host.get("highBudget") is True, "a large budget is marked high budget")
    check(wide_host.get("writes") is False, "a read rule is marked read-only")
    normal_host = ((parsed.capabilities.network_card() or {}).get("hosts") or [{}])[0]
    check(normal_host.get("highBudget") is False, "the default budget is not marked")


def test_network_guard(root: Path) -> None:
    server, base = _serve()
    port = server.server_address[1]
    try:
        package = root / "egress"
        package.mkdir(parents=True, exist_ok=True)
        (package / "impl.py").write_text(
            "import socket\n\n"
            "def run(args):\n"
            "    try:\n"
            "        socket.create_connection(('127.0.0.1', int(args['port'])), timeout=0.3)\n"
            "    except PermissionError as exc:\n"
            "        return {'ok': True, 'output': 'blocked: ' + str(exc)[:60]}\n"
            "    return {'ok': False, 'output': 'connected'}\n",
            encoding="utf-8",
        )
        declared = parse_manifest(
            {
                **manifest("text.egress"),
                "capabilities": {
                    "network": {"hosts": [{"host": "127.0.0.1", "scheme": "http", "paths": ["/*"]}]}
                },
            }
        )
        without_broker = run_call(package, declared, {"port": port})
        check(
            bool(without_broker.get("ok")) and "blocked" in str(without_broker.get("output")),
            f"declared hosts still cannot be reached without a broker: {without_broker.get('output')}",
        )

        sealed = parse_manifest(manifest("text.sealed"))
        closed = run_call(package, sealed, {"port": port})
        check(
            bool(closed.get("ok")) and "blocked" in str(closed.get("output")),
            "without the capability every socket stays shut",
        )

        broker = Broker()
        try:
            grant = broker.grant(
                "text.egress",
                [HostRule(host="127.0.0.1", scheme="http", paths=["/*"])],
            )
            with_broker = run_call(package, declared, {"port": port}, grant)
            check(
                bool(with_broker.get("ok"))
                and "blocked" in str(with_broker.get("output"))
                and "broker" in str(with_broker.get("output")),
                f"a broker grant does not open a direct socket: {with_broker.get('output')}",
            )
        finally:
            broker.close()

        fetch_package = root / "fetch"
        fetch_package.mkdir(parents=True, exist_ok=True)
        (fetch_package / "impl.py").write_text(
            "from capri_net import fetch, NetworkError\n\n"
            "def run(args):\n"
            "    try:\n"
            "        fetch(args['url'])\n"
            "    except NetworkError as exc:\n"
            "        return {'ok': True, 'output': 'refused: ' + str(exc)[:40]}\n"
            "    return {'ok': False, 'output': 'fetched'}\n",
            encoding="utf-8",
        )
        no_grant = run_call(fetch_package, declared, {"url": base + "/ok"})
        check(
            bool(no_grant.get("ok")) and "no network broker" in str(no_grant.get("output")),
            f"capri_net without a broker refuses: {no_grant.get('output')}",
        )
    finally:
        server.shutdown()
        server.server_close()


def test_broker(root: Path) -> None:
    server, base = _serve()
    try:
        rules = [
            HostRule(
                host="127.0.0.1",
                scheme="http",
                methods=["GET"],
                paths=["/ok", "/redirect", "/big"],
                max_response_bytes=1024,
                requests_per_hour=20,
            )
        ]
        broker = Broker()
        try:
            grant = broker.grant("text.fetch", rules)
            token = str(grant["token"])

            ok = broker.dispatch({"token": token, "url": base + "/ok"})
            check(
                bool(ok.get("ok")) and ok.get("status") == 200 and "hello /ok" in str(ok.get("body")),
                f"broker fetches a declared url: {ok.get('error')}",
            )
            undeclared_path = broker.dispatch({"token": token, "url": base + "/secret"})
            check(undeclared_path.get("status") == 403, "an undeclared path is refused")
            undeclared_host = broker.dispatch({"token": token, "url": "http://example.com/ok"})
            check(undeclared_host.get("status") == 403, "an undeclared host is refused")
            https_mismatch = broker.dispatch({"token": token, "url": base.replace("http://", "https://") + "/ok"})
            check(https_mismatch.get("status") == 403, "https-only rules refuse plain http")
            check(
                broker.dispatch({"token": "nope", "url": base + "/ok"}).get("status") == 401,
                "an unknown token is refused",
            )

            redirect = broker.dispatch({"token": token, "url": base + "/redirect"})
            check(
                bool(redirect.get("ok")) and redirect.get("url", "").endswith("/ok"),
                f"a redirect inside the policy is followed: {redirect.get('error')}",
            )
            away = broker.dispatch({"token": token, "url": base.replace("127.0.0.1", "localhost") + "/away"})
            check(away.get("status") == 403, "a redirect elsewhere is refused")

            big = broker.dispatch({"token": token, "url": base + "/big"})
            check(big.get("status") == 413, "the response cap is enforced")

            tight = Broker()
            try:
                limited = tight.grant(
                    "text.tight",
                    [HostRule(host="127.0.0.1", scheme="http", paths=["/ok"], requests_per_hour=2)],
                )
                first = tight.dispatch({"token": limited["token"], "url": base + "/ok"})
                second = tight.dispatch({"token": limited["token"], "url": base + "/ok"})
                third = tight.dispatch({"token": limited["token"], "url": base + "/ok"})
                check(
                    first.get("ok") and second.get("ok") and third.get("status") == 429,
                    "the hourly budget stops a third request",
                )
            finally:
                tight.close()

            old_token = os.environ.get("CAPRI_SECRET_TEST_TOKEN")
            os.environ["CAPRI_SECRET_TEST_TOKEN"] = "s3cret"
            try:
                secret_grant = broker.grant(
                    "text.secret",
                    rules,
                    [SecretRule(name="TEST_TOKEN", header="authorization", prefix="Bearer ", hosts=["127.0.0.1"])],
                )
                _TestServer.seen.clear()
                sent = broker.dispatch({"token": secret_grant["token"], "url": base + "/ok"})
                check(bool(sent.get("ok")), "a request with a declared secret succeeds")
                check(
                    any(item["auth"] == "Bearer s3cret" for item in _TestServer.seen),
                    "the broker substitutes the secret header",
                )
                os.environ.pop("CAPRI_SECRET_TEST_TOKEN")
                _TestServer.seen.clear()
                missing = broker.dispatch({"token": secret_grant["token"], "url": base + "/ok"})
                check(missing.get("status") == 424 and not _TestServer.seen, "a missing secret stops the request")
            finally:
                if old_token is None:
                    os.environ.pop("CAPRI_SECRET_TEST_TOKEN", None)
                else:
                    os.environ["CAPRI_SECRET_TEST_TOKEN"] = old_token

            original = socket.getaddrinfo

            def inward(host, port, *rest, **kwargs):
                return original("127.0.0.1", port, *rest, **kwargs)

            socket.getaddrinfo = inward  # type: ignore[assignment]
            try:
                public = broker.grant(
                    "text.public",
                    [HostRule(host="public.example.com", scheme="http", paths=["/*"])],
                )
                rebound = broker.dispatch({"token": public["token"], "url": "http://public.example.com/ok"})
                check(rebound.get("status") == 403, "a public name resolving inward is refused")
            finally:
                socket.getaddrinfo = original  # type: ignore[assignment]

            package = root / "fetch"
            package.mkdir(parents=True, exist_ok=True)
            (package / "impl.py").write_text(
                "from capri_net import fetch\n\n"
                "def run(args):\n"
                "    reply = fetch(args['url'])\n"
                "    return {'ok': True, 'output': str(reply['status']) + ' ' + str(reply['body'])[:40]}\n",
                encoding="utf-8",
            )
            fetched = run_call(
                package,
                parse_manifest(
                    {
                        **manifest("text.fetch"),
                        "capabilities": {
                            "network": {"hosts": [{"host": "127.0.0.1", "scheme": "http", "paths": ["/*"]}]}
                        },
                    }
                ),
                {"url": base + "/ok"},
                {"host": grant["host"], "port": grant["port"], "token": grant["token"]},
            )
            check(
                bool(fetched.get("ok")) and str(fetched.get("output", "")).startswith("200 hello /ok"),
                f"a sandboxed tool fetches through capri_net: {fetched.get('output')}",
            )
        finally:
            broker.close()
    finally:
        server.shutdown()
        server.server_close()


def test_manifest_and_audit() -> None:
    parsed = parse_manifest(manifest())
    check(parsed.name == "text.echo" and parsed.version == "0.1.0", "manifest parses")
    check(parsed.capabilities.write_files is False, "capabilities default closed")

    try:
        parse_manifest({**manifest(), "name": "Bad.Name"})
        check(False, "bad names are rejected")
    except ValueError:
        check(True, "bad names are rejected")

    safe = audit_source(GOOD_IMPL, Capabilities())
    check(safe.ok, "safe source passes audit")

    shell = audit_source("import os\n\ndef run(args):\n    return os.system('whoami')\n", Capabilities())
    check(not shell.ok, "shell escape is rejected")

    network = audit_source("import socket\n\ndef run(args):\n    return {'ok': True}\n", Capabilities())
    check(not network.ok, "undeclared network import is rejected")

    declared = audit_source(
        "import socket\n\ndef run(args):\n    return {'ok': True}\n",
        Capabilities(network=True, network_rules=[{"host": "api.example.com"}]),
    )
    check(not declared.ok, "a declared tool still gets no raw socket import")
    check(
        any(finding.code == "import.network.raw" for finding in declared.findings),
        "the raw-socket refusal points at capri_net",
    )

    shim = audit_source(
        "from capri_net import fetch\n\n"
        "def run(args):\n"
        "    return fetch('https://api.example.com/user')['status']\n",
        Capabilities(network=True, network_rules=[{"host": "api.example.com"}]),
    )
    check(shim.ok, "capri_net is allowed with a declared host")
    shim_sealed = audit_source(
        "from capri_net import fetch\n\ndef run(args):\n    return fetch('https://x.example.com')['status']\n",
        Capabilities(),
    )
    check(not shim_sealed.ok, "capri_net needs the network capability")

    pure = audit_source(
        "import urllib.parse\n\ndef run(args):\n    return urllib.parse.quote(args['text'])\n",
        Capabilities(),
    )
    check(pure.ok, "pure URL parsing needs no network")

    write = audit_source(
        "from pathlib import Path\n\ndef run(args):\n    Path('x').write_text('x')\n",
        Capabilities(),
    )
    check(not write.ok, "undeclared file write is rejected")

    write_ok = audit_source(
        "from pathlib import Path\n\ndef run(args):\n    Path('x').write_text('x')\n",
        Capabilities(write_files=True),
    )
    check(write_ok.ok, "declared file write is allowed")


def test_proposal(conn: Any, root: Path, cfg: CapriConfig) -> None:
    registry = builtin_registry()
    blackwall = Blackwall(registry, conn, Policy(cfg.policy, str(root)), root=root / "bw")
    registry.register(PROPOSE_SPEC, blackwall.propose_tool)

    requests: list[dict[str, Any]] = []

    async def confirm(request: dict[str, Any]) -> bool:
        requests.append(request)
        return True

    ctx = ToolContext(
        conn=conn,
        turn_id="trn_blackwall",
        subject="test",
        workspace=str(root),
        confirm=confirm,
    )

    proposed = asyncio.run(
        registry.call(
            "tool.propose",
            ctx,
            {"manifest": manifest(), "implementation": GOOD_IMPL, "tests": GOOD_TEST},
        )
    )
    check(proposed.ok, f"proposal passed tests: {proposed.output}")
    check(len(requests) == 1, "one approval card was requested")
    if requests:
        request = requests[0]
        check(request["tierName"] == "blackwall", "approval is a Blackwall card")
        check(request["args"]["name"] == "text.echo", "approval names the tool")
        check("tests passed" in request["args"]["tests"], "approval carries test output")

    check(registry.get("text.echo") is not None, "approved tool is hot-loaded")
    result = asyncio.run(registry.call("text.echo", ctx, {"text": "hello"}))
    check(result.ok and result.output == "hello", "dynamic tool runs in the sandbox")
    check("text.echo" in blackwall._workers, "dynamic tool uses a warm worker")
    check(
        conn.execute("SELECT calls FROM tool_stats WHERE name = 'text.echo'").fetchone()["calls"] == 1,
        "dynamic call is counted",
    )

    restarted = builtin_registry()
    reloaded = Blackwall(restarted, conn, Policy(cfg.policy, str(root)), root=root / "bw")
    check(reloaded.load() >= 1 and restarted.get("text.echo") is not None, "installed tool survives a reload")


def test_rejections(conn: Any, root: Path, cfg: CapriConfig) -> None:
    registry = builtin_registry()
    blackwall = Blackwall(registry, conn, Policy(cfg.policy, str(root)), root=root / "bw2")

    open_net = blackwall.stage(
        {**manifest("text.opennet"), "capabilities": {"network": True}},
        GOOD_IMPL,
        GOOD_TEST,
    )
    check(not open_net.get("ok"), "network without declared hosts is refused")

    declared_net = blackwall.stage(
        {
            **manifest("text.fetch"),
            "capabilities": {
                "network": {
                    "hosts": [
                        {"host": "API.Example.com", "methods": ["get"], "paths": ["/user"]}
                    ],
                    "secrets": [
                        {"name": "GITHUB_TOKEN", "prefix": "Bearer ", "hosts": ["api.example.com"]}
                    ],
                }
            },
        },
        GOOD_IMPL,
        GOOD_TEST,
    )
    check(
        bool(declared_net.get("ok")),
        f"a declared network policy passes staging: {declared_net.get('output')}",
    )
    rules = (declared_net.get("capabilities") or {}).get("network_rules") or []
    check(
        bool(rules)
        and rules[0].get("host") == "api.example.com"
        and rules[0].get("methods") == ["GET"],
        "hosts and methods are normalised onto the approval record",
    )
    card = declared_net.get("network") or {}
    check(card.get("containment") == "advisory", "the approval card names the containment")
    check(
        "api.example.com" in json.dumps(card) and "GITHUB_TOKEN" in json.dumps(card),
        "the approval card lists hosts and secret names",
    )

    legacy_net = blackwall.stage(
        {
            **manifest("text.legacy"),
            "capabilities": {"network": True, "network_hosts": ["API.Example.com"]},
        },
        GOOD_IMPL,
        GOOD_TEST,
    )
    legacy_rules = (legacy_net.get("capabilities") or {}).get("network_rules") or []
    check(
        bool(legacy_net.get("ok"))
        and bool(legacy_rules)
        and legacy_rules[0].get("scheme") == "https"
        and legacy_rules[0].get("methods") == ["GET"],
        "a v1.1 host list upgrades to a read-only https rule",
    )

    orphan_rules = blackwall.stage(
        {**manifest("text.orphan"), "capabilities": {"network_rules": [{"host": "api.example.com"}]}},
        GOOD_IMPL,
        GOOD_TEST,
    )
    check(not orphan_rules.get("ok"), "network rules need the network capability")

    unsafe = blackwall.stage(
        manifest("text.shell"),
        "import os\n\ndef run(args):\n    return os.system('whoami')\n",
        GOOD_TEST,
    )
    check(not unsafe.get("ok"), "audit refusal is surfaced before tests")

    failing = blackwall.stage(
        manifest("text.failing"),
        "def run(args):\n    return {'ok': True, 'output': 'x'}\n",
        "from impl import run\n\ndef test():\n    assert False, 'nope'\n",
    )
    check(not failing.get("ok"), "failing sandbox test blocks installation")

    tampered = blackwall.stage(
        {**manifest("text.tamper"), "capabilities": {"write_files": True}},
        GOOD_IMPL,
        "from pathlib import Path\n\ndef test():\n    Path('impl.py').write_text('def run(args):\\n    return {}\n')\n",
    )
    check(not tampered.get("ok"), "tests cannot rewrite the approved implementation")

    async def deny(request: dict[str, Any]) -> bool:
        return False

    registry.register(PROPOSE_SPEC, blackwall.propose_tool)
    ctx = ToolContext(
        conn=conn,
        turn_id="trn_deny",
        subject="test",
        workspace=str(root),
        confirm=deny,
    )
    denied = asyncio.run(
        registry.call(
            "tool.propose",
            ctx,
            {"manifest": manifest("text.denied"), "implementation": GOOD_IMPL, "tests": GOOD_TEST},
        )
    )
    check(not denied.ok and "denied" in denied.output, "denial leaves the tool uninstalled")
    check(registry.get("text.denied") is None, "denied tool is not registered")


def test_critic(conn: Any, root: Path, cfg: CapriConfig) -> None:
    registry = builtin_registry()

    async def reject(package: dict[str, Any]) -> dict[str, Any]:
        return {
            "verdict": "reject",
            "reason": "implementation hides a file write",
            "concerns": ["write not visible in the manifest"],
            "capability_diff": ["write_files"],
        }

    blackwall = Blackwall(
        registry,
        conn,
        Policy(cfg.policy, str(root)),
        root=root / "bw-critic",
        critic=reject,
    )
    registry.register(PROPOSE_SPEC, blackwall.propose_tool)
    requests: list[dict[str, Any]] = []

    async def confirm(request: dict[str, Any]) -> bool:
        requests.append(request)
        return True

    ctx = ToolContext(
        conn=conn,
        turn_id="trn_critic",
        subject="test",
        workspace=str(root),
        confirm=confirm,
    )
    result = asyncio.run(
        registry.call(
            "tool.propose",
            ctx,
            {"manifest": manifest("text.critic"), "implementation": GOOD_IMPL, "tests": GOOD_TEST},
        )
    )
    check(not result.ok and "critic rejected" in result.output, "model critic can reject a proposal")
    check(not requests, "critic rejection never reaches the approval card")
    check(registry.get("text.critic") is None, "critic-rejected tool is not installed")
    diff = blackwall._proposal_diff("text.new", "0.1.0", GOOD_IMPL)
    check("--- /dev/null" in diff and "+def run" in diff, "proposal diff is unified")


def test_first_write(conn: Any, root: Path, cfg: CapriConfig) -> None:
    registry = builtin_registry()
    blackwall = Blackwall(registry, conn, Policy(cfg.policy, str(root)), root=root / "bw-write")
    impl = "def run(args):\n    return {'ok': True, 'output': 'wrote'}\n"
    tests = "from impl import run\n\ndef test():\n    assert run({})['output'] == 'wrote'\n"

    def install(name: str, methods: list[str]) -> bool:
        staged = blackwall.stage(
            {
                **manifest(name),
                "params": {"type": "object", "properties": {},"additionalProperties": False},
                "capabilities": {
                    "network": {"hosts": [{"host": "api.example.com", "methods": methods, "paths": ["/items"]}]}
                },
            },
            impl,
            tests,
        )
        if not staged.get("ok"):
            print(f"  .... staging {name} failed: {staged.get('output')}")
            return False
        return bool(blackwall.install(staged["proposalId"]).get("ok"))

    check(install("text.post", ["POST"]), "a writing tool installs")
    requests: list[dict[str, Any]] = []

    async def approve(request: dict[str, Any]) -> bool:
        requests.append(request)
        return True

    ctx = ToolContext(
        conn=conn,
        turn_id="trn_write",
        subject="test",
        workspace=str(root),
        confirm=approve,
    )
    first = asyncio.run(registry.call("text.post", ctx, {}))
    check(first.ok, f"the first write runs after approval: {first.output}")
    check(len(requests) == 1, "the first write asks once")
    if requests:
        check(
            requests[0]["tierName"] == "blackwall" and "POST" in requests[0]["reason"],
            "the write card names the method and host",
        )
        check(
            requests[0]["args"]["methods"] == ["POST"]
            and requests[0]["args"]["hosts"] == ["api.example.com"],
            "the write card carries the scope",
        )
    second = asyncio.run(registry.call("text.post", ctx, {}))
    check(second.ok and len(requests) == 1, "later writes do not ask again")

    check(install("text.denywrite", ["PUT"]), "a denied-write candidate installs")
    refusals: list[dict[str, Any]] = []

    async def deny(request: dict[str, Any]) -> bool:
        refusals.append(request)
        return False

    denied_ctx = ToolContext(
        conn=conn,
        turn_id="trn_deny_write",
        subject="test",
        workspace=str(root),
        confirm=deny,
    )
    denied = asyncio.run(registry.call("text.denywrite", denied_ctx, {}))
    check(not denied.ok and "denied" in denied.output, "a denied first write does not run")
    check(len(refusals) == 1, "the denial came from one card")
    check(registry.get("text.denywrite") is not None, "the tool stays installed but unrun")

    check(install("text.readonly", ["GET"]), "a read-only tool installs")
    quiet: list[dict[str, Any]] = []

    async def record(request: dict[str, Any]) -> bool:
        quiet.append(request)
        return True

    read_ctx = ToolContext(
        conn=conn,
        turn_id="trn_read",
        subject="test",
        workspace=str(root),
        confirm=record,
    )
    read = asyncio.run(registry.call("text.readonly", read_ctx, {}))
    check(read.ok and not quiet, "a read-only tool never asks")
    blackwall.close()


def test_quarantine_rollback(conn: Any, root: Path, cfg: CapriConfig) -> None:
    registry = builtin_registry()
    blackwall = Blackwall(registry, conn, Policy(cfg.policy, str(root)), root=root / "bw3")
    registry.register(PROPOSE_SPEC, blackwall.propose_tool)

    async def approve(request: dict[str, Any]) -> bool:
        return True

    ctx = ToolContext(
        conn=conn,
        turn_id="trn_rollback",
        subject="test",
        workspace=str(root),
        confirm=approve,
    )

    def install(version: str, impl: str, tests: str) -> bool:
        staged = blackwall.stage(manifest("text.rollback", version), impl, tests)
        if not staged.get("ok"):
            return False
        return bool(blackwall.install(staged["proposalId"]).get("ok"))

    good_tests = (
        "from impl import run\n\n"
        "def test():\n"
        "    assert run({'text': 'ok'})['output'] == 'ok'\n"
    )
    check(
        install("0.1.0", "def run(args):\n    return {'ok': True, 'output': 'ok'}\n", good_tests),
        "first version installed",
    )
    bad_tests = (
        "from impl import run\n\n"
        "def test():\n"
        "    assert run({'text': 'ok'})['output'] == 'broken'\n"
    )
    check(
        install(
            "0.2.0",
            "def run(args):\n    return {'ok': False, 'output': 'broken'}\n",
            bad_tests,
        ),
        "second version installed",
    )
    for _ in range(3):
        result = asyncio.run(registry.call("text.rollback", ctx, {"text": "x"}))
        check(not result.ok, "failing version reports failure")
    active = registry.get("text.rollback")
    check(active is not None and active.version == "0.1.0",
          "three failures quarantine and roll back")
    restored = asyncio.run(registry.call("text.rollback", ctx, {"text": "x"}))
    check(restored.ok and restored.output == "ok", "previous version is usable again")
    row = conn.execute("SELECT quarantined FROM tool_stats WHERE name = 'text.rollback'").fetchone()
    check(bool(row and row["quarantined"]), "quarantine is recorded")

    (blackwall.installed_dir / "text.rollback" / "current.txt").write_text(
        "9.9.9", encoding="utf-8"
    )
    restarted = builtin_registry()
    reloaded = Blackwall(restarted, conn, Policy(cfg.policy, str(root)), root=root / "bw3")
    check(
        reloaded.load() >= 1 and restarted.get("text.rollback").version == "0.1.0",
        "a bad current pointer falls back to the newest usable version",
    )


def test_recipe(conn: Any, root: Path, cfg: CapriConfig) -> None:
    registry = builtin_registry()
    blackwall = Blackwall(registry, conn, Policy(cfg.policy, str(root)), root=root / "bw4")
    target = root / "recipe.txt"
    target.write_text("recipe content", encoding="utf-8")
    recipe = {
        "name": "recipe.inspect",
        "description": "List a directory, then read a file.",
        "params": {
            "type": "object",
            "properties": {"dir": {"type": "string"}, "file": {"type": "string"}},
            "required": ["dir", "file"],
            "additionalProperties": False,
        },
        "steps": [
            {"tool": "fs.list", "args": {"path": "{dir}"}},
            {"tool": "fs.read", "args": {"path": "{file}"}},
        ],
    }
    installed = blackwall.install_recipe(recipe)
    check(installed.get("ok") is True, "recipe is installed as a skill")
    ctx = ToolContext(conn=conn, turn_id="trn_recipe", subject="test", workspace=str(root))
    result = asyncio.run(
        registry.call("recipe.inspect", ctx, {"dir": str(root), "file": str(target)})
    )
    check(result.ok, f"recipe runs its steps: {result.output}")
    check("recipe content" in result.output, "recipe substitution reaches the file step")

    destructive = {
        "name": "recipe.bad",
        "steps": [{"tool": "fs.write", "args": {"path": "{path}", "content": "x"}}],
    }
    check(not blackwall.install_recipe(destructive).get("ok"), "destructive recipe is refused")


def test_agent_loop_proposal(conn: Any, root: Path, cfg: CapriConfig) -> None:
    registry = builtin_registry()
    blackwall = Blackwall(registry, conn, Policy(cfg.policy, str(root)), root=root / "bw5")
    registry.register(PROPOSE_SPEC, blackwall.propose_tool)
    session = Session.create(conn)
    events: list[dict[str, Any]] = []
    confirms: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def confirm(request: dict[str, Any]) -> bool:
        confirms.append(request)
        return True

    brain = ScriptedBrain(
        {**{"manifest": manifest("text.loop"), "implementation": GOOD_IMPL, "tests": GOOD_TEST}}
    )
    runner = TurnRunner(
        config=cfg,
        conn=conn,
        session=session,
        emit=emit,
        confirm=confirm,
        brain=brain,
        registry=registry,
        policy=Policy(cfg.policy, str(root)),
        workspace=str(root),
    )
    asyncio.run(runner.run("trn_agent_bw", "make and use a tool"))
    tool_events = [event for event in events if event.get("type") == "agent.tool"]
    names = [event["name"] for event in tool_events]
    check(names == ["tool.propose", "text.loop"], f"agent proposed then used it: {names}")
    check(all(event["ok"] for event in tool_events), "both loop calls succeeded")
    check(len(confirms) == 1 and confirms[0]["tool"] == "tool.propose", "loop approval path")
    done = [event for event in events if event.get("type") == "agent.done"]
    check(bool(done) and done[-1]["text"] == "It works.", "turn completed after using the tool")


def main() -> int:
    print("blackwall")
    test_manifest_and_audit()
    test_write_capability_gate()
    test_network_policy()
    test_windows_containment()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_network_guard(root)
        test_broker(root)
        conn = db.connect(root / "shard.db")
        db.migrate(conn)
        cfg = CapriConfig()
        cfg.policy.allow_paths = [str(root)]
        test_proposal(conn, root, cfg)
        test_first_write(conn, root, cfg)
        test_rejections(conn, root, cfg)
        test_critic(conn, root, cfg)
        test_quarantine_rollback(conn, root, cfg)
        test_recipe(conn, root, cfg)
        test_agent_loop_proposal(conn, root, cfg)
        conn.close()
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s)")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
