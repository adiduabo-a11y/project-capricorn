"""Wire protocol helpers.

Newline-delimited JSON over a loopback WebSocket.

Client -> server:  {"id": "r1", "method": "turn.send", "params": {...}}
Server -> client:  {"id": "r1", "ok": true, "result": {...}}
Server -> client:  {"type": "agent.delta", "turnId": "...", "text": "hi"}
"""

from __future__ import annotations

import json
from typing import Any

# Close codes
CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_REQUEST = 4402

ERROR_CODES = {
    "unauthorized": "missing or invalid token",
    "bad_request": "malformed request",
    "unknown_method": "no such method",
    "not_found": "not found",
    "invalid_args": "invalid arguments",
    "internal": "internal error",
    "cancelled": "cancelled",
}


def event(event_type: str, **fields: Any) -> dict[str, Any]:
    return {"type": event_type, **fields}


def ok(request_id: str | None, result: Any = None) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}


def err(
    request_id: str | None, code: str, message: str | None = None, fatal: bool = False
) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message or ERROR_CODES.get(code, code)},
        "fatal": fatal,
    }


def encode(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)


def decode(raw: str | bytes) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("frame must be a JSON object")
    return data


def state(value: str, detail: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": value}
    if detail:
        payload["detail"] = detail
    return event("agent.state", **payload)
