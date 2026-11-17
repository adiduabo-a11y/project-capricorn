"""MCP: the stdio client, the schema sanitiser, and tools in the registry.

    uv run python services/core/tests/test_mcp.py

The server is a real subprocess (``tests/fixtures/mcp_echo_server.py``), so the
handshake, framing, pagination, error paths and shutdown are exercised over the
wire rather than mocked. No network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from capri_core.config import CapriConfig, McpServerCfg
from capri_core.mcp import (
    McpClient,
    McpError,
    McpService,
    clean_schema,
    flatten_result,
    importing,
    tool_name,
)
from capri_core.models import gbnf
from capri_core.tools.base import ToolContext
from capri_core.tools.policy import Policy
from capri_core.tools.registry import Registry

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "mcp_echo_server.py"

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def client(env: dict[str, str] | None = None) -> McpClient:
    return McpClient(sys.executable, [str(FIXTURE)], env=env, name="fake")


def check_flatten() -> None:
    print("flatten")
    text, failed = flatten_result(
        {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "image", "mimeType": "image/png"},
                {"type": "text", "text": "second"},
            ]
        }
    )
    check(text == "first\n[image image/png]\nsecond", f"text blocks join, media is marked: {text!r}")
    check(not failed, "a normal result is not an error")
    text, failed = flatten_result({"content": [{"type": "text", "text": "no"}], "isError": True})
    check(failed, "isError is carried through")
    text, _ = flatten_result({"content": [], "structuredContent": {"a": 1}})
    check('"a": 1' in text, "structured content is readable when there is no text")
    text, _ = flatten_result({})
    check(text == "", "an empty result is empty, not a crash")


def check_schema() -> None:
    print("schema")
    cleaned = clean_schema(
        {
            "type": "object",
            "properties": {
                "payload": {"$ref": "#/definitions/thing"},
                "mode": {"type": ["string", "null"], "enum": ["a", "b"]},
                "tags": {"type": "array", "items": {"type": "string"}},
                "nested": {
                    "type": "object",
                    "properties": {"deep": {"type": "number"}},
                    "required": ["deep"],
                },
                "anything": {},
            },
            "required": ["payload", "missing"],
        }
    )
    props = cleaned["properties"]
    check(cleaned["type"] == "object", "the top level stays an object")
    check(props["payload"]["type"] == "string", "$ref properties degrade to string")
    check(props["mode"]["type"] == "string" and props["mode"]["enum"] == ["a", "b"], "nullable enums keep the string arm")
    check(props["tags"]["items"]["type"] == "string", "array items are kept")
    check(props["nested"]["properties"]["deep"]["type"] == "number", "nested objects survive one level")
    check(cleaned["required"] == ["payload"], "required only lists properties that exist")
    check(cleaned["additionalProperties"] is False, "additional properties are closed")
    check(clean_schema(None)["properties"] == {}, "a missing schema is an empty object")
    check(clean_schema({"properties": {"a": {"type": "boolean"}}})["properties"]["a"]["type"] == "boolean", "a schema without a type is read as an object")
    check(tool_name("my server", "do/thing") == "mcp.my_server.do_thing", "tool names are made registry-safe")
    # The cleaned schema has to compile into a grammar, or the planner cannot
    # ask for arguments at all.
    grammar = gbnf.from_schema(cleaned)
    check(bool(grammar) and "root" in grammar, "the cleaned schema compiles to GBNF")
    decision = gbnf.decision_schema(["mcp.fake.echo"])
    check("mcp.fake.echo" in str(decision), "an MCP tool name fits the decision grammar")


def check_importing() -> None:
    print("importing")

    # Two pasted names that clean to one key would overwrite each other in
    # config.yaml, so the later one is suffixed rather than dropped.
    dupes = importing.parse_config(
        json.dumps(
            {
                "mcpServers": {
                    "My Server": {"command": "npx", "args": ["-y", "pkg"]},
                    "My_Server": {"command": "node", "args": ["server.js"]},
                }
            }
        )
    )
    names = [row["name"] for row in dupes["servers"]]
    check(names == ["My_Server", "My_Server_2"], f"both colliding names survive: {names}")
    check(any("was already taken" in note for note in dupes["notes"]), "and the rename says why")

    web = importing.parse_config(
        json.dumps({"mcpServers": {"web": {"url": "https://example.com/mcp"}}})
    )
    check(web["servers"] == [], "a url server is not a config row")
    reason = web["skipped"][0]["reason"]
    check("url" in reason and "stdio" in reason, f"and says why: {reason}")

    try:
        importing.parse_config('{\n  "mcpServers": {\n    "fs": {,}\n  }\n}')
        check(False, "malformed JSON raises ConfigError")
    except importing.ConfigError as exc:
        check("line 3" in str(exc), f"malformed JSON points at the position: {exc}")

    # Trust is never read from the paste: a config carrying a tier is not one
    # Claude wrote, and the decision belongs to the person.
    sneaky = importing.parse_config(
        json.dumps(
            {
                "mcpServers": {
                    "sneaky": {
                        "command": "npx",
                        "args": [],
                        "tier": 0,
                        "enabled": True,
                    }
                }
            }
        )
    )
    row = sneaky["servers"][0]
    check(row["tier"] == importing.DEFAULT_TIER == 2, f"a pasted tier is ignored: {row['tier']}")
    check(row["enabled"] is True, "and so is a pasted switch")
    check(
        any("sneaky" in note and "ignored" in note for note in sneaky["notes"]),
        "the paste is told its tier was ignored",
    )

    # The one writer both doors use: the switch follows the tier, so a tier-3
    # server can never be saved running.
    saved = importing.row_config(row, tier=3)
    check(saved["tier"] == 3 and saved["enabled"] is False, "tier 3 is written switched off")
    check(importing.row_config(row, tier=2)["enabled"] is True, "tier 2 is written on")
    check(importing.row_config(row, tier=9)["tier"] == 3, "a tier above the range is clamped")
    check(importing.row_config(row, tier="nope")["tier"] == 2, "so is one that is not a number")
    check(
        set(saved) == {"command", "args", "env", "cwd", "tier", "enabled"},
        f"the row has exactly the config keys: {sorted(saved)}",
    )
    check(saved["env"] == {} and saved["cwd"] == "", "missing env and cwd are written as empty")


async def check_legacy_client() -> None:
    print("legacy client")
    fake = client()
    await fake.start(timeout_s=15)
    try:
        check(fake.running, "the server process is up")
        check(fake.protocol == "2025-11-25", f"the handshake version is recorded: {fake.protocol}")
        check(fake.server_info.get("name") == "fake", "serverInfo comes back")
        check(not fake.modern, "the legacy era is not marked modern")

        tools = await fake.list_tools()
        check([tool["name"] for tool in tools] == ["echo", "boom"], "tools/list follows nextCursor")

        text, failed = await fake.call("echo", {"text": "hi", "count": 2})
        check("echo:" in text and '"text": "hi"' in text, f"arguments reach the server: {text!r}")
        check(not failed, "a normal call is not an error")

        text, failed = await fake.call("boom", {})
        check(failed and text == "it broke", "an application error is reported as a failed result")

        try:
            await fake.call("nope", {})
            check(False, "an unknown tool raises")
        except McpError as exc:
            check("unknown tool" in str(exc), f"an unknown tool raises McpError: {exc}")

        try:
            await fake.request("slow", timeout_s=0.2)
            check(False, "a slow call times out")
        except McpError as exc:
            check("timed out" in str(exc), f"a slow call times out: {exc}")
    finally:
        await fake.close()
    check(not fake.running, "close stops the process")


async def check_modern_client() -> None:
    print("modern client")
    fake = client({"MCP_FAKE_MODERN": "1"})
    await fake.start(timeout_s=15)
    try:
        check(fake.modern, "a server without initialize is spoken to as 2026-07-28")
        tools = await fake.list_tools()
        check(len(tools) == 2, "requests carrying _meta are accepted")
        text, _ = await fake.call("echo", {"text": "meta"})
        check("meta" in text, "calls work in the modern era")
    finally:
        await fake.close()


async def check_service() -> None:
    print("service")
    config = CapriConfig()
    config.mcp.servers["fake"] = McpServerCfg(command=sys.executable, args=[str(FIXTURE)])
    config.mcp.servers["broken"] = McpServerCfg(command="capri-no-such-binary", args=[])
    # The same server, trusted less: its tools should be blocked, not confirmed.
    config.mcp.servers["strict"] = McpServerCfg(
        command=sys.executable, args=[str(FIXTURE)], tier=3
    )
    registry = Registry()
    service = McpService(config, registry)
    try:
        started = await service.start()
        check(started == 2, f"the working servers come up: {started}")
        check("broken" in service.errors, f"the failure is recorded: {service.errors.get('broken', '')[:60]}")
        check("mcp.fake.echo" in registry.names(), f"its tools are registered: {registry.names()}")
        spec = registry.get("mcp.fake.echo")
        check(spec is not None and spec.tier == 2, "tools confirm by default")
        check(spec is not None and spec.source == "mcp:fake", "the source names the server")
        check(spec is not None and spec.version == "mcp", "and the version is mcp")
        check(
            spec is not None and spec.params["required"] == ["text"],
            "the schema survives into the manifest",
        )

        policy = Policy(config.policy, ".")
        verdict = policy.check(spec, {"text": "hi"})
        check(verdict.verdict == "confirm" and verdict.tier == 2, "policy asks before an MCP tool runs")

        ctx = ToolContext(conn=None, turn_id="t1", subject="default", workspace=".")
        result = await registry.call("mcp.fake.echo", ctx, {"text": "hello"})
        check(result.ok and "echo:" in result.output, f"the tool runs through the registry: {result.output!r}")
        check(result.data == {"mcp": "mcp.fake.echo"}, "the result is attributed to the MCP tool")

        result = await registry.call("mcp.fake.boom", ctx, {"payload": "x"})
        check(not result.ok, "a failing MCP tool is a failed tool call")

        result = await registry.call("mcp.fake.echo", ctx, {"text": "x", "surprise": 1})
        check(result.ok, "extra arguments are passed through rather than rejected")

        strict = registry.get("mcp.strict.echo")
        check(strict is not None and strict.tier == 3, "a server's trust tier lands on its tools")
        blocked = Policy(config.policy, ".").check(strict, {"text": "hi"})
        check(blocked.verdict == "block", "a tier-three MCP tool is blocked")
    finally:
        await service.aclose()
    check(not service.clients, "closing the service closes its servers")


async def check_settings() -> None:
    print("settings panel")
    config = CapriConfig()
    config.mcp.servers["fake"] = McpServerCfg(command=sys.executable, args=[str(FIXTURE)])
    config.mcp.servers["broken"] = McpServerCfg(command="capri-no-such-binary", args=[])
    registry = Registry()
    service = McpService(config, registry)
    try:
        await service.start()
        rows = {row["name"]: row for row in service.status()}
        check(set(rows) == {"fake", "broken"}, "every configured server has a row")
        check(rows["fake"]["running"] and not rows["broken"]["running"], "a row says which came up")
        check(
            rows["broken"]["error"] != "",
            f"a failed server explains itself: {rows['broken']['error'][:50]!r}",
        )
        check(
            rows["fake"]["tools"] and rows["fake"]["tools"][0].startswith("mcp.fake."),
            "a healthy row lists its tools",
        )
        check(rows["fake"]["tier"] == 2 and rows["fake"]["enabled"], "and its tier and switch")
        check(rows["broken"]["tools"] == [], "a failed server lists none")

        # What the panel's changes mean: the registry follows without a restart.
        config.mcp.servers["broken"] = McpServerCfg(
            command=sys.executable, args=[str(FIXTURE)], tier=3
        )
        config.mcp.servers["fake"].enabled = False
        started = await service.reload()
        check(started == 1, f"reload starts what is enabled now: {started}")
        check("mcp.fake.echo" not in registry.names(), "a switched-off server stops being callable")
        check("mcp.broken.echo" in registry.names(), "a repaired one becomes callable")
        rows = {row["name"]: row for row in service.status()}
        check(not rows["fake"]["running"] and rows["fake"]["enabled"] is False, "its row says off")
        check(rows["broken"]["error"] == "" and rows["broken"]["running"], "the error clears")
        repaired = registry.get("mcp.broken.echo")
        check(repaired is not None and repaired.tier == 3, "the tier set in the panel is enforced")

        del config.mcp.servers["broken"]
        await service.reload()
        check("mcp.broken.echo" not in registry.names(), "a removed server's tools are gone")
        check(len(service.status()) == 1, "and so is its row")
    finally:
        await service.aclose()


async def main() -> int:
    check_flatten()
    check_schema()
    check_importing()
    await check_legacy_client()
    await check_modern_client()
    await check_service()
    await check_settings()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
