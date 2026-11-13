"""End-to-end smoke test for the daemon.

Starts nothing itself — point it at a running `python -m capri_core`.

    uv run python services/core/tests/ws_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from capri_core import paths

URL = "ws://127.0.0.1:8787"
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


async def rpc(ws, request_id: str, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("id") == request_id:
                return msg
    except ConnectionClosed:
        return {"id": request_id, "ok": False, "error": {"code": "closed"}}


async def main() -> int:
    token = paths.token(create=False)
    if not token:
        print("no token at", paths.token_path())
        return 2

    async with connect(URL, max_size=8 * 1024 * 1024) as ws:
        hello = await rpc(ws, "r0", "hello", {"token": token})
        check(bool(hello.get("ok")), "hello accepted")
        if not hello.get("ok"):
            print(hello)
            return 1

        result = hello["result"]
        print(f"  .... version={result['version']} session={result['sessionId']}")
        print(f"  .... vector backend={result['vectorBackend']}")

        ping = await rpc(ws, "r1", "ping")
        check(ping["result"]["pong"] is True, "ping/pong")

        sent = await rpc(ws, "r2", "turn.send", {"text": "hello capri"})
        turn_id = sent["result"]["turnId"]
        check(bool(turn_id), "turn accepted")

        deltas: list[str] = []
        done = None
        states: list[str] = []
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if ev.get("type") == "agent.state":
                states.append(ev["state"])
            elif ev.get("type") == "agent.delta" and ev.get("turnId") == turn_id:
                deltas.append(ev["text"])
            elif ev.get("type") == "agent.done" and ev.get("turnId") == turn_id:
                done = ev
                break

        check(len(deltas) > 1, f"streamed {len(deltas)} deltas")
        check(done is not None, "turn completed")
        check("hello capri" in (done or {}).get("text", ""), "echo contains input")
        check(states[:1] == ["thinking"], f"state order {states}")
        if done:
            print(f"  .... {done['usage']}")

        traces = await rpc(ws, "r3", "trace.get", {"limit": 5})
        rows = traces["result"]["traces"]
        check(any(r["span"] == "turn" for r in rows), "turn traced")

        cfg = await rpc(ws, "r4", "config.get")
        check(cfg["result"]["server"]["port"] == 8787, "config.get")

        mem = await rpc(ws, "r5", "memory.list")
        check(isinstance(mem["result"]["items"], list), "memory.list")

        info = await rpc(ws, "r6", "session.info")
        check(len(info["result"]["messages"]) >= 2, "messages persisted")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1

    # bad token must be rejected on a fresh connection (server closes it)
    async with connect(URL) as ws2:
        bad = await rpc(ws2, "b1", "hello", {"token": "nope"})
        check(not bad.get("ok"), "bad token rejected")

    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
