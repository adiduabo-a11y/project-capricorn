"""Live end-to-end check against a real model endpoint.

Unlike ``ws_smoke.py`` this expects a served model and asserts that the planner
actually completed a multi-step task. Point the daemon at ``llama-server`` or
LM Studio first, e.g.:

    $env:CAPRI_MODELS__ENDPOINT = "http://127.0.0.1:1234/v1"
    $env:CAPRI_MODELS__STRUCTURED = "json_schema"
    $env:CAPRI_MODELS__PLANNER__NAME = "google/gemma-4-e4b"
    uv run python -m capri_core

then:

    uv run python services/core/tests/live_brain.py "list the directory, ..."
"""

from __future__ import annotations

import asyncio
import json
import sys

from websockets.asyncio.client import connect

from capri_core import paths

URL = "ws://127.0.0.1:8787"
FAILURES: list[str] = []

TASK = (
    "Look at the files in the current directory, then write a file named "
    "live-check.txt containing the single line 'linked', then tell me in one "
    "sentence what you did."
)


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


async def rpc(ws, request_id: str, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == request_id:
            return msg


async def main() -> int:
    task = " ".join(sys.argv[1:]).strip() or TASK
    token = paths.token(create=False)
    if not token:
        print("no token at", paths.token_path())
        return 2

    async with connect(URL, max_size=8 * 1024 * 1024) as ws:
        hello = await rpc(ws, "h", "hello", {"token": token})
        if not hello.get("ok"):
            print("hello failed:", hello)
            return 1

        models = await rpc(ws, "m", "models.status")
        status = models.get("result", {})
        print(f"  .... endpoint={status.get('endpoint')} planner={status.get('planner')}")
        print(f"  .... online={status.get('online')} structured={status.get('structured')}")
        check(bool(status.get("online")), "endpoint is serving models")

        sent = await rpc(ws, "t", "turn.send", {"text": task})
        turn_id = sent["result"]["turnId"]
        print(f"  .... turn {turn_id}")

        steps: list[str] = []
        tools: list[tuple[str, bool]] = []
        confirms: list[str] = []
        answers: list[str] = []

        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
            kind = ev.get("type")
            if kind == "agent.step" and ev.get("tool") != "done":
                steps.append(ev["tool"])
                print(f"  ..   step {ev['step']}: {ev['tool']} — {ev['thought']}")
            elif kind == "agent.tool":
                tools.append((ev["name"], ev["ok"]))
                print(f"  ..   tool {ev['name']} ok={ev['ok']} {ev['ms']}ms tier={ev['tierName']}")
            elif kind == "agent.confirm":
                confirms.append(ev["tool"])
                print(f"  ..   confirm {ev['tool']}: {ev['reason']}")
                await rpc(ws, "a", "confirm.respond",
                          {"confirmId": ev["confirmId"], "approved": True})
            elif kind == "agent.delta" and ev.get("turnId") == turn_id:
                answers.append(ev["text"])
            elif kind == "agent.done" and ev.get("turnId") == turn_id:
                print(f"\n  capri> {ev['text']}\n")
                print(f"  .... usage {ev['usage']}")
                break
            elif kind == "error":
                print("  !!  ", ev)
                break

        check(len(tools) >= 2, f"ran {len(tools)} tools: {[t[0] for t in tools]}")
        check(all(ok for _, ok in tools), "every tool succeeded")
        check(bool("".join(answers).strip()), "answer streamed")

        check(bool(ev.get("text", "").strip()), "answer is non-empty")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("live check passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
