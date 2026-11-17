"""MCP: the stdio client, the schema sanitiser, and tools in the registry.

    uv run python services/core/tests/test_mcp.py

The server is a real subprocess (``tests/fixtures/mcp_echo_server.py``), so the
handshake, framing, pagination, error paths and shutdown are exercised over the
wire rather than mocked. No network.
"""

from __future__ import annotations

import asyncio
import json
import os
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
    same_row,
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

    # The environment a save writes. A pasted config and the panel's save are
    # judged by one rule, so the checks sit next to the writer rather than in
    # the RPC handler that calls it.
    check(
        importing.env_pairs({"TOKEN": "abc", "EMPTY": "", "NULL": None})
        == {"TOKEN": "abc", "EMPTY": "", "NULL": ""},
        "env values are text, and a JSON null is an empty value",
    )
    for label, raw, wanted in (
        ("a blank name", {"  ": "x"}, "is not an environment variable name"),
        ("'=' in a name", {"BAD=NAME": "x"}, "is not an environment variable name"),
        ("two spellings of one name", {"A": "1", " A ": "2"}, "is set twice"),
        ("a non-object env", ["A"], "env must be an object of NAME: value"),
        ("a null byte in a value", {"A": "\x00"}, "holds a null byte"),
    ):
        try:
            importing.env_pairs(raw)
            check(False, f"{label} is refused")
        except importing.ConfigError as exc:
            check(wanted in str(exc), f"{label} is refused: {exc}")

    # Windows compares environment names without case, so the two spellings are
    # one variable there and one of them would silently win at spawn. Both are
    # named, because the person has to pick which value they meant.
    try:
        importing.env_pairs({"TOKEN": "1", "token": "2"}, windows=True)
        check(False, "two case spellings are refused on Windows")
    except importing.ConfigError as exc:
        check(
            "TOKEN and token" in str(exc) and "on Windows" in str(exc),
            f"two case spellings are refused, naming both: {exc}",
        )
    check(
        importing.env_pairs({"TOKEN": "1", "token": "2"}, windows=False)
        == {"TOKEN": "1", "token": "2"},
        "elsewhere the two spellings are two variables",
    )
    if os.name == "nt":
        try:
            importing.env_pairs({"Path": "1", "PATH": "2"})
            check(False, "the Windows rule is on by default")
        except importing.ConfigError as exc:
            check(
                "Path and PATH" in str(exc),
                f"the Windows rule is on by default: {exc}",
            )

    check(
        importing.merge_env({"TOKEN": "abc"}, None) == {"TOKEN": "abc"},
        "a save that does not mention env keeps the stored one",
    )
    check(
        importing.merge_env({"TOKEN": "abc"}, {"OTHER": "x"}) == {"OTHER": "x"},
        "a new map replaces the old one whole",
    )
    check(importing.merge_env({"TOKEN": "abc"}, {}) == {}, "an empty map clears it")

    # A pasted server with a broken env is skipped for that reason, not
    # silently started without the variable it needed.
    broken_env = importing.parse_config(
        json.dumps({"mcpServers": {"odd": {"command": "npx", "env": {"A=B": "x"}}}})
    )
    check(
        broken_env["servers"] == []
        and "not an environment variable name" in broken_env["skipped"][0]["reason"],
        f"a pasted env that cannot work is a skip with a reason: {broken_env['skipped']}",
    )


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
    config.mcp.servers["fake"] = McpServerCfg(
        command=sys.executable, args=[str(FIXTURE)], env={"FAKE_TOKEN": "s3cret"}
    )
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
        check(
            rows["fake"]["env"] == {"FAKE_TOKEN": "s3cret"},
            f"a row shows the environment it is started with: {rows['fake']['env']}",
        )
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


async def check_crash() -> None:
    print("a server that dies")
    config = CapriConfig()
    config.mcp.servers["crashy"] = McpServerCfg(
        command=sys.executable, args=[str(FIXTURE)]
    )
    # A process that starts and then exits before the handshake: its reason is
    # the line it printed on stderr, not the closed pipe the protocol sees.
    config.mcp.servers["dieearly"] = McpServerCfg(
        command=sys.executable,
        args=[
            "-c",
            "import sys; sys.stderr.write('no CAPRI_TEST_KEY in the environment\\n'); sys.exit(2)",
        ],
    )
    registry = Registry()
    service = McpService(config, registry)
    try:
        await service.start()
        rows = {row["name"]: row for row in service.status()}
        check(rows["crashy"]["running"], "a live server's row says running")
        check(
            "mcp.crashy.echo" in registry.names(),
            "and its tools are callable",
        )
        check(
            "no CAPRI_TEST_KEY" in rows["dieearly"]["error"],
            f"a boot failure carries what the server printed: {rows['dieearly']['error']}",
        )

        client = service.clients["crashy"]
        try:
            # Not in tools/list: a real server dying is a transport event, and
            # this is the test's way to cause one.
            await client.call("crash", {}, timeout_s=5)
        except McpError:
            pass

        # The process is reaped asynchronously, so the row is read once it is
        # actually gone rather than on the turn that killed it.
        for _ in range(100):
            if not client.running and client.stderr_tail:
                break
            await asyncio.sleep(0.05)

        rows = {row["name"]: row for row in service.status()}
        row = rows["crashy"]
        check(not row["running"], "a server that died is not reported running")
        check(row["tools"] == [], f"its tools are gone from the row: {row['tools']}")
        check(
            "mcp.crashy.echo" not in registry.names(),
            "and from the registry, so nothing offers a tool that cannot run",
        )
        check(
            "exited after it started" in row["error"],
            f"the row says it exited: {row['error']}",
        )
        check(
            "widget factory exploded" in row["error"],
            f"and repeats what it printed: {row['error']}",
        )
    finally:
        await service.aclose()


async def check_targeted_reload() -> None:
    print("one save, one restart")
    config = CapriConfig()
    for name in ("one", "two"):
        config.mcp.servers[name] = McpServerCfg(command=sys.executable, args=[str(FIXTURE)])
    registry = Registry()
    service = McpService(config, registry)
    try:
        await service.start()
        one, two = service.clients["one"], service.clients["two"]
        check(one.running and two.running, "both servers are up")

        config.mcp.servers["one"].tier = 0
        started = await service.reload({"one"})
        check(started == 1, f"only the named server is started: {started}")
        check(
            service.clients["two"] is two and two.running,
            "the untouched server keeps its process",
        )
        check(not one.running, "the changed one was stopped")
        check(service.clients["one"] is not one, "and started again")
        spec = registry.get("mcp.one.echo")
        check(
            spec is not None and spec.tier == 0,
            "its tools came back with the new tier",
        )

        # Switched off: withdrawn on its own, without the other restarting.
        config.mcp.servers["one"].enabled = False
        started = await service.reload({"one"})
        check(started == 0 and "one" not in service.clients, "a switched-off server is stopped")
        check("mcp.one.echo" not in registry.names(), "and its tools go with it")
        check(
            service.clients["two"] is two and two.running,
            "the other still has its process",
        )

        # The decision the save handler asks before writing or restarting.
        row = config.mcp.servers["two"].model_dump(mode="json")
        check(same_row(row, config.mcp.servers["two"]), "an identical row is unchanged")
        check(
            not same_row(dict(row, tier=1), config.mcp.servers["two"]),
            "a changed tier is a change",
        )
        check(not same_row(None, config.mcp.servers["two"]), "a new server is a change")
    finally:
        await service.aclose()


async def check_probe() -> None:
    print("trying a server before saving it")
    config = CapriConfig()
    registry = Registry()
    service = McpService(config, registry)
    try:
        tried = await service.probe(sys.executable, [str(FIXTURE)], name="draft")
        check(tried["ok"], f"a working draft starts: {tried['error']!r}")
        check(
            [tool["name"] for tool in tried["tools"]] == ["echo", "boom"],
            "and lists the tools it advertises",
        )
        check(
            registry.names() == [] and service.clients == {},
            "nothing is registered and nothing is left running",
        )
        check(config.mcp.servers == {}, "and the config is untouched")

        missing = await service.probe("capri-no-such-binary", [], name="missing")
        check(
            not missing["ok"] and "could not start" in missing["error"],
            f"a bad command reports why: {missing['error']}",
        )

        early = await service.probe(
            sys.executable,
            ["-c", "import sys; sys.stderr.write('no key here\\n'); sys.exit(2)"],
            name="early",
        )
        check(
            not early["ok"] and "no key here" in early["error"],
            f"and a server that dies says what it printed: {early['error']}",
        )

        # The global switch governs every process, a try included. A command
        # that does not exist proves nothing was launched: the refusal comes
        # back instead of the spawn failure.
        config.mcp.enabled = False
        off = await service.probe("capri-no-such-binary", [], name="off")
        check(
            not off["ok"] and "switched off" in off["error"],
            f"a try says MCP is off: {off['error']}",
        )
        check(
            "could not start" not in off["error"],
            "and no process is launched to find out",
        )
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
    await check_crash()
    await check_targeted_reload()
    await check_probe()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
