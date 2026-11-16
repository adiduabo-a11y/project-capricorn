"""A tiny stdio MCP server for the client tests.

Legacy era by default: an ``initialize`` handshake, then ``tools/list`` paginated
in two pages and ``tools/call``. With ``MCP_FAKE_MODERN=1`` it behaves like the
2026-07-28 revision instead: no initialize, per-request ``_meta`` required.
"""

from __future__ import annotations

import json
import os
import sys
import time

MODERN = os.environ.get("MCP_FAKE_MODERN") == "1"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the arguments back as text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "what to echo"},
                "count": {"type": "integer"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "boom",
        "description": "Fail at the application level.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payload": {"$ref": "#/definitions/thing"},
                "mode": {"type": ["string", "null"], "enum": ["a", "b", None]},
            },
            "required": ["payload", "missing"],
        },
    },
]


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def error(message_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}
    if message_id is None:
        continue  # notification, e.g. notifications/initialized

    if MODERN and "_meta" not in params:
        error(message_id, -32602, "missing _meta")
        continue

    if method == "initialize":
        if MODERN:
            error(message_id, -32601, "initialize was removed in 2026-07-28")
            continue
        send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1.0.0"},
                },
            }
        )
        continue

    if method == "tools/list":
        cursor = params.get("cursor")
        if cursor:
            send({"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS[1:]}})
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"tools": TOOLS[:1], "nextCursor": "page-2"},
                }
            )
        continue

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            text = "echo: " + json.dumps(arguments, sort_keys=True)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False},
                }
            )
        elif name == "boom":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"content": [{"type": "text", "text": "it broke"}], "isError": True},
                }
            )
        else:
            error(message_id, -32602, f"unknown tool {name}")
        continue

    if method == "slow":
        time.sleep(2)
        send({"jsonrpc": "2.0", "id": message_id, "result": {"content": []}})
        continue

    error(message_id, -32601, f"no such method {method}")
