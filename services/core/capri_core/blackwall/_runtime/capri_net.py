"""The only network a Blackwall tool gets.

This module is injected into the sandbox's import path by the runner. It has no
host list, no credentials and no socket policy of its own: it forwards one
request per call to the daemon's broker, which decides whether the manifest
allows it and adds any secret headers.

    from capri_net import fetch
    reply = fetch("https://api.github.com/user")   # {"status", "headers", "body", "url"}
"""

from __future__ import annotations

import json
import os
import socket


class NetworkError(RuntimeError):
    """The broker refused the request or could not complete it."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def _endpoint() -> dict[str, str]:
    raw = os.environ.get("CAPRI_NET") or ""
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict) or not value.get("port") or not value.get("token"):
        raise NetworkError(
            503, "no network broker is attached to this sandbox process"
        )
    return value


def _request(payload: dict[str, object], timeout: float) -> dict[str, object]:
    endpoint = _endpoint()
    line = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        connection = socket.create_connection(
            (str(endpoint.get("host") or "127.0.0.1"), int(endpoint["port"])),
            timeout=max(1.0, timeout),
        )
    except OSError as exc:
        raise NetworkError(503, f"could not reach the broker: {exc}") from exc
    try:
        connection.sendall(line)
        buffer = b""
        while b"\n" not in buffer:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
    finally:
        connection.close()
    try:
        reply = json.loads(buffer.decode("utf-8", "replace"))
    except ValueError as exc:
        raise NetworkError(502, "the broker returned an unreadable reply") from exc
    if not isinstance(reply, dict):
        raise NetworkError(502, "the broker returned a non-object reply")
    if not reply.get("ok"):
        raise NetworkError(
            int(reply.get("status") or 502), str(reply.get("error") or "request refused")
        )
    return reply


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    json_body: object | None = None,
    timeout: float = 60.0,
) -> dict[str, object]:
    """Ask the broker for one URL. Returns status, headers, body and the final URL.

    Only URLs the manifest declared are reachable; anything else raises
    ``NetworkError`` with the broker's status.
    """
    if json_body is not None:
        if body is not None:
            raise ValueError("pass either body or json_body, not both")
        body = json.dumps(json_body)
        headers = {**(headers or {}), "Content-Type": "application/json"}
    reply = _request(
        {
            "token": _endpoint()["token"],
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "body": body,
        },
        timeout,
    )
    return {
        "status": int(reply.get("status") or 0),
        "headers": reply.get("headers") or {},
        "body": str(reply.get("body") or ""),
        "url": str(reply.get("url") or url),
    }


def get(url: str, **kwargs: object) -> dict[str, object]:
    return fetch(url, method="GET", **kwargs)  # type: ignore[arg-type]


def post(url: str, **kwargs: object) -> dict[str, object]:
    return fetch(url, method="POST", **kwargs)  # type: ignore[arg-type]
