"""The Blackwall egress broker.

A generated tool never opens a socket. It sends a request over a loopback
channel to this broker, which decides whether the manifest allows that exact
host, scheme, method and path, applies the tool's secret headers, and only then
makes the connection. Redirects are re-checked hop by hop, the DNS answer is
pinned to the connection, private addresses need an explicit literal rule, and
every rule carries an hour budget.

The broker lives in the daemon. The child gets a token and a port; it gets no
host list, no secret value and no way to ask for anything the manifest did not
declare.
"""

from __future__ import annotations

import fnmatch
import http.client
import ipaddress
import json
import logging
import os
import posixpath
import socket
import socketserver
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from .manifest import HostRule, SecretRule

log = logging.getLogger("capri.blackwall.broker")

MAX_BODY_BYTES = 1_024 * 1_024
MAX_REDIRECTS = 2
SECRET_ENV_PREFIX = "CAPRI_SECRET_"
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_UNSAFE_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class BrokerError(RuntimeError):
    """A refused or failed request, with the status the tool should see."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Grant:
    """One tool's policy for as long as its worker lives."""

    tool: str
    token: str
    rules: list[HostRule]
    secrets: list[SecretRule]
    hits: dict[int, deque[float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, index: int, rule: HostRule, now: float) -> None:
        with self.lock:
            window = self.hits.setdefault(index, deque())
            while window and window[0] < now - 3600:
                window.popleft()
            if len(window) >= rule.requests_per_hour:
                raise BrokerError(
                    429,
                    f"hourly budget spent for {rule.host} "
                    f"({rule.requests_per_hour} requests); the tool must wait",
                )
            window.append(now)


def normalize_host(raw: str | None) -> str:
    host = str(raw or "").strip().lower().rstrip(".")
    if not host:
        raise BrokerError(400, "the URL has no host")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if ":" in host:
        # urlsplit separates the port; a colon inside a name is malformed.
        raise BrokerError(400, f"{host!r} is not a valid host")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BrokerError(400, f"{host!r} cannot be encoded as a hostname") from exc
    return host


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host.endswith("." + suffix) and host.count(".") == suffix.count(".") + 1
    return host == pattern


def _path_matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP with the validated host but the resolved address.

    Host header stays the declared name, the TCP peer is the address the broker
    resolved itself, so a resolver that changes its answer mid-request cannot
    move the connection somewhere else.
    """

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def _create_connection(self, address, timeout=None, source_address=None):  # type: ignore[override]
        return socket.create_connection(
            (self._address, address[1]), timeout, source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def _create_connection(self, address, timeout=None, source_address=None):  # type: ignore[override]
        return socket.create_connection(
            (self._address, address[1]), timeout, source_address
        )


class Broker:
    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.records: deque[dict[str, Any]] = deque(maxlen=200)

    # ------------------------------------------------------------------ server

    def _ensure_server(self) -> None:
        if self._server is not None:
            return
        with self._lock:
            if self._server is not None:
                return
            broker = self

            class _Handler(socketserver.StreamRequestHandler):
                def handle(self) -> None:
                    self.request.settimeout(30.0)
                    raw = self.rfile.readline(2 * 1024 * 1024)
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        reply = {"ok": False, "status": 400, "error": "malformed request"}
                    else:
                        reply = broker.dispatch(payload if isinstance(payload, dict) else {})
                    self.wfile.write(json.dumps(reply, default=str).encode("utf-8") + b"\n")
                    self.wfile.flush()

            class _Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            server = _Server(("127.0.0.1", 0), _Handler)
            server.broker = broker  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
            log.info("blackwall broker listening on 127.0.0.1:%d", server.server_address[1])

    def endpoint(self) -> tuple[str, int]:
        self._ensure_server()
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def close(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            self._grants.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ------------------------------------------------------------------ grants

    def grant(
        self,
        tool: str,
        rules: list[HostRule],
        secrets: list[SecretRule] | None = None,
    ) -> dict[str, Any]:
        """Register one tool's policy and return what the child needs."""
        host, port = self.endpoint()
        token = os.urandom(24).hex()
        with self._lock:
            self._grants[token] = Grant(
                tool=tool,
                token=token,
                rules=list(rules),
                secrets=list(secrets or []),
            )
        return {"host": host, "port": port, "token": token}

    def revoke(self, token: str) -> None:
        with self._lock:
            self._grants.pop(token, None)

    # ------------------------------------------------------------------ request

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("token") or "")
        with self._lock:
            grant = self._grants.get(token)
        if grant is None:
            return {"ok": False, "status": 401, "error": "unknown broker token"}
        try:
            method = str(payload.get("method") or "GET").upper()
            url = str(payload.get("url") or "")
            headers = payload.get("headers") or {}
            body = payload.get("body")
            if not isinstance(headers, dict):
                raise BrokerError(400, "headers must be an object")
            clean_headers: dict[str, str] = {}
            seen_lower: dict[str, str] = {}
            for key, value in headers.items():
                k = str(key)
                v = str(value)
                kl = k.lower()
                if kl in _UNSAFE_HEADERS or any(ch in k for ch in ("\r", "\n", ":")):
                    continue
                if "\r" in v or "\n" in v:
                    continue
                prev = seen_lower.get(kl)
                if prev is not None:
                    clean_headers.pop(prev, None)
                seen_lower[kl] = k
                clean_headers[k] = v
            if body is not None and not isinstance(body, str):
                body = json.dumps(body)
            if body is not None and len(body.encode("utf-8")) > MAX_BODY_BYTES:
                raise BrokerError(413, f"request body is over the {MAX_BODY_BYTES} byte cap")
            result = self._fetch(grant, method, url, clean_headers, body)
        except BrokerError as exc:
            log.info("blackwall broker refused %s: %s", grant.tool, exc.message)
            return {"ok": False, "status": exc.status, "error": exc.message}
        except Exception as exc:  # noqa: BLE001 - never leak a stack to the child
            log.exception("blackwall broker failed a request for %s", grant.tool)
            return {"ok": False, "status": 502, "error": f"broker failed: {exc}"}
        self.records.append(
            {
                "tool": grant.tool,
                "host": result["host"],
                "url": result["url"],
                "status": result["status"],
                "bytes": len(result["body"].encode("utf-8")),
            }
        )
        return {"ok": True, **result}

    def _authorize(
        self, grant: Grant, method: str, url: str, now: float
    ) -> tuple[int, HostRule, Any, str]:
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"}:
            raise BrokerError(403, "only http(s) URLs can be fetched")
        if parts.username is not None or parts.password is not None:
            raise BrokerError(403, "URLs must not carry credentials")
        host = normalize_host(parts.hostname)
        raw_path = parts.path or "/"
        decoded_path = unquote(raw_path)
        if raw_path.startswith("//") or "\\" in raw_path or "\\" in decoded_path:
            raise BrokerError(400, "the path is not normalized")
        if any(part == ".." for part in decoded_path.split("/")):
            raise BrokerError(403, "path traversal segments are not allowed")
        path = posixpath.normpath(raw_path)
        if raw_path.endswith("/") and path != "/":
            path += "/"
        default_port = 443 if parts.scheme == "https" else 80
        matched: tuple[int, HostRule] | None = None
        for index, rule in enumerate(grant.rules):
            if rule.scheme != parts.scheme:
                continue
            if not host_matches(host, rule.host):
                continue
            if parts.port is not None and parts.port != default_port and not is_ip(rule.host):
                continue
            if method not in rule.methods:
                continue
            if not _path_matches(path, rule.paths):
                continue
            matched = (index, rule)
            break
        if matched is None:
            allowed = ", ".join(
                f"{rule.scheme}://{rule.host}{rule.paths[0]}" for rule in grant.rules[:4]
            )
            raise BrokerError(
                403,
                f"{method} {parts.scheme}://{host}{path} is not declared"
                + (f"; this tool may only use {allowed}" if allowed else ""),
            )
        index, rule = matched
        grant.take(index, rule, now)
        return index, rule, parts, host

    def _resolve(self, host: str, rule: HostRule, port: int) -> str:
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise BrokerError(502, f"could not resolve {host}: {exc}") from exc
        if not answers:
            raise BrokerError(502, f"{host} did not resolve")
        address = str(answers[0][4][0])
        literal = is_ip(rule.host)
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            ip = None
        if ip is not None and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ) and not (literal and rule.host == address):
            # A public name that resolves inward is the classic rebinding move.
            raise BrokerError(403, f"{host} resolves to a private address")
        return address

    def _fetch(
        self,
        grant: Grant,
        method: str,
        url: str,
        headers: dict[str, str],
        body: str | None,
    ) -> dict[str, Any]:
        now = time.time()
        current = url
        current_body = body
        for hop in range(MAX_REDIRECTS + 1):
            index, rule, parts, host = self._authorize(grant, method, current, now)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            address = self._resolve(host, rule, port)
            outgoing = dict(headers)
            outgoing.setdefault("Accept", "*/*")
            outgoing.setdefault("User-Agent", "capri-blackwall/2")
            for secret in grant.secrets:
                if not any(host_matches(host, pattern) for pattern in secret.hosts):
                    continue
                value = os.environ.get(SECRET_ENV_PREFIX + secret.name)
                if not value:
                    raise BrokerError(
                        424,
                        f"{secret.name} is not configured; set "
                        f"{SECRET_ENV_PREFIX}{secret.name} in the daemon's environment",
                    )
                secret_key = secret.header.lower()
                for existing_key in [k for k in outgoing if k.lower() == secret_key]:
                    del outgoing[existing_key]
                outgoing[secret.header] = secret.prefix + value
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            connection: http.client.HTTPConnection
            if parts.scheme == "https":
                connection = _PinnedHTTPSConnection(host, port, address, rule.timeout_s)
            else:
                connection = _PinnedHTTPConnection(host, port, address, rule.timeout_s)
            try:
                connection.request(method, target, body=current_body, headers=outgoing)
                response = connection.getresponse()
                status = response.status
                location = response.getheader("Location")
                raw = response.read(rule.max_response_bytes + 1)
                reply_headers = {
                    key.lower(): value for key, value in response.getheaders() if key
                }
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise BrokerError(502, f"request to {host} failed: {exc}") from exc
            finally:
                connection.close()
            if len(raw) > rule.max_response_bytes:
                raise BrokerError(
                    413,
                    f"response from {host} is over the {rule.max_response_bytes} byte cap",
                )
            if status in _REDIRECT_STATUS and location and hop < MAX_REDIRECTS:
                current = urljoin(current, location)
                if status == 303 or (status in {301, 302} and method == "POST"):
                    method = "GET"
                    current_body = None
                continue
            if status in _REDIRECT_STATUS and location:
                raise BrokerError(403, f"{host} redirected more than {MAX_REDIRECTS} times")
            body_text = raw.decode("utf-8", "replace")
            return {
                "status": status,
                "headers": reply_headers,
                "body": body_text,
                "url": current,
                "host": host,
            }
        raise BrokerError(403, "too many redirects")
