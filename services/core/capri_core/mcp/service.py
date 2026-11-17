"""MCP servers as Capri tools.

One process per configured server, started once with the daemon. Each advertised
tool is registered in the ordinary registry under ``mcp.<server>.<tool>`` with a
declared tier, so the policy engine, the confirmation card, tracing and the
planner all treat it like any other capability. A server that cannot start is
logged and skipped; it never stops the daemon from booting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Mapping

from ..config import CapriConfig, McpServerCfg
from ..tools.base import ToolContext, ToolResult, ToolSpec
from ..tools.registry import Registry
from .client import McpClient, McpError

log = logging.getLogger("capri.mcp")

_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object"}
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def tool_name(server: str, tool: str) -> str:
    """``mcp.<server>.<tool>``, with anything the registry cannot carry cut out."""
    clean_server = _NAME.sub("_", server).strip("._-") or "server"
    clean_tool = _NAME.sub("_", tool).strip("._-") or "tool"
    return f"mcp.{clean_server}.{clean_tool}"[:96]


def clean_schema(raw: Any) -> dict[str, Any]:
    """A tool's ``inputSchema`` as something Capri's grammar builder can compile.

    Server schemas are arbitrary JSON Schema: ``$ref``, unions, nullable types,
    nested objects. The grammar generator supports a narrow subset, so anything
    outside it is reduced to a string rather than being dropped — the model still
    gets a field to fill, and the server still validates what arrives.
    """
    spec = dict(raw) if isinstance(raw, Mapping) else {}
    if "type" not in spec:
        spec["type"] = "object" if "properties" in spec else "string"
    cleaned = _schema(spec, 0)
    if cleaned.get("type") != "object":
        return dict(_EMPTY)
    return cleaned


def _schema(raw: Any, depth: int) -> dict[str, Any]:
    spec = raw if isinstance(raw, Mapping) else {}
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next(
            (item for item in kind if isinstance(item, str) and item in _JSON_TYPES and item != "null"),
            None,
        )
    if kind not in _JSON_TYPES:
        kind = "string"
    out: dict[str, Any] = {"type": kind}
    description = spec.get("description") or spec.get("title")
    if isinstance(description, str) and description.strip():
        out["description"] = " ".join(description.split())[:300]
    enum = spec.get("enum")
    if kind == "string" and isinstance(enum, list) and enum:
        values = [str(item)[:120] for item in enum if isinstance(item, (str, int, float))]
        if values:
            out["enum"] = values[:50]
    if kind == "array":
        out["items"] = _schema(spec.get("items"), depth + 1) if depth < 3 else {"type": "string"}
    elif kind == "object":
        properties: dict[str, Any] = {}
        raw_props = spec.get("properties")
        if isinstance(raw_props, Mapping) and depth < 3:
            for key, value in raw_props.items():
                if not isinstance(key, str) or not key.strip() or len(key) > 64:
                    continue
                properties[key] = _schema(value, depth + 1)
        out["properties"] = properties
        required = spec.get("required")
        keep = (
            [str(name) for name in required if isinstance(name, str) and name in properties]
            if isinstance(required, list)
            else []
        )
        if keep:
            out["required"] = keep
        out["additionalProperties"] = False
    return out


def _explain(reason: str, client: McpClient) -> str:
    """A failure reason carrying the last thing the server printed itself.

    The protocol layer only ever sees a closed pipe or a timeout; the sentence
    that names the cause — a missing key, a traceback, a package it could not
    resolve — is on stderr. Three lines are enough to say why and short enough
    for one settings row.
    """
    lines = [line for line in client.stderr_tail[-3:] if line.strip()]
    if not lines:
        return reason
    return f"{reason} — stderr: " + " | ".join(lines)[:600]


def same_row(before: Mapping[str, Any] | None, after: McpServerCfg) -> bool:
    """Whether a save leaves the stored server exactly as it was.

    The panel sends the whole row, so re-clicking the tier it already has, or
    pressing ``save env`` on an untouched map, would otherwise write the file
    and cold-start the server for no change at all.
    """
    return isinstance(before, Mapping) and dict(before) == after.model_dump(mode="json")


class McpService:
    def __init__(self, config: CapriConfig, registry: Registry) -> None:
        self.config = config
        self.registry = registry
        self.clients: dict[str, McpClient] = {}
        # Qualified name -> "server/tool", for logs and the status call.
        self.tools: dict[str, str] = {}
        # Server name -> why it is not running. Empty when everything is fine.
        self.errors: dict[str, str] = {}

    @property
    def cfg(self) -> Any:
        return self.config.mcp

    async def start(self) -> int:
        """Start every enabled server. Returns how many came up."""
        if not self.cfg.enabled:
            return 0
        servers = [
            (name, server)
            for name, server in self.cfg.servers.items()
            if server.enabled and server.command.strip()
        ]
        if not servers:
            return 0
        results = await asyncio.gather(
            *(self._start_one(name, server) for name, server in servers),
            return_exceptions=True,
        )
        return sum(1 for result in results if result is True)

    async def _start_one(self, name: str, server: McpServerCfg) -> bool:
        client = McpClient(
            server.command, server.args, env=server.env, cwd=server.cwd, name=name
        )
        try:
            await client.start(self.cfg.startup_timeout_s)
            tools = await client.list_tools(timeout_s=self.cfg.startup_timeout_s)
        except Exception as exc:  # noqa: BLE001 - a bad server must not be fatal
            # The reason the protocol gave is rarely the whole story; what the
            # server itself said is the part a person can act on.
            await client.settle_stderr()
            self.errors[name] = _explain(str(exc), client)
            log.warning("mcp server %s is not usable: %s", name, self.errors[name])
            try:
                await client.close()
            except Exception:
                log.debug("mcp server %s did not close after failing", name, exc_info=True)
            return False

        self.clients[name] = client
        self.errors.pop(name, None)
        registered = 0
        for tool in tools:
            if self._register(name, server, client, tool):
                registered += 1
        log.info("mcp server %s exposes %d tool(s)", name, registered)
        return True

    async def probe(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str = "",
        name: str = "",
    ) -> dict[str, Any]:
        """Start a server once, list what it offers, and stop it again.

        Nothing is written to the config and nothing joins the registry: this is
        how a command and its token are checked before they become a row, and
        the only way to check a tier-3 server, which is saved switched off and
        never started. The failure is the shape a save would report, stderr and
        all, so a typo is found here rather than in a broken row.
        """
        if not self.cfg.enabled:
            # The switch governs every process the daemon starts, and a try
            # starts one. Say so rather than doing nothing quietly.
            return {
                "ok": False,
                "tools": [],
                "error": "MCP is switched off in the config, so nothing was started",
            }
        client = McpClient(
            command, list(args or []), env=env, cwd=cwd, name=name or command
        )
        try:
            await client.start(self.cfg.startup_timeout_s)
            tools = await client.list_tools(timeout_s=self.cfg.startup_timeout_s)
        except Exception as exc:  # noqa: BLE001 - a try must report, not raise
            await client.settle_stderr()
            return {"ok": False, "tools": [], "error": _explain(str(exc), client)}
        finally:
            await client.close()
        return {
            "ok": True,
            "tools": [
                {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or "")[:160],
                }
                for tool in tools
                if str(tool.get("name") or "").strip()
            ],
            "error": "",
        }

    def _register(
        self, server_name: str, server: McpServerCfg, client: McpClient, tool: Mapping[str, Any]
    ) -> bool:
        raw_name = str(tool.get("name") or "").strip()
        if not raw_name:
            return False
        qualified = tool_name(server_name, raw_name)
        description = str(tool.get("description") or tool.get("title") or raw_name).strip()
        spec = ToolSpec(
            name=qualified,
            description=f"{server_name} (MCP): {description}"[:600],
            params=clean_schema(tool.get("inputSchema")),
            tier=max(0, min(3, int(server.tier))),
            timeout_ms=int(server.timeout_s * 1000),
            max_calls_per_turn=server.max_calls_per_turn,
            version="mcp",
            source=f"mcp:{server_name}",
        )

        async def handler(
            ctx: ToolContext,
            _client: McpClient = client,
            _tool: str = raw_name,
            _qualified: str = qualified,
            **arguments: Any,
        ) -> ToolResult:
            return await self._call(_client, _tool, _qualified, arguments, server.timeout_s)

        try:
            self.registry.register(spec, handler)
        except ValueError:
            log.warning("mcp tool %s collides with an existing tool; skipped", qualified)
            return False
        self.tools[qualified] = f"{server_name}/{raw_name}"
        return True

    async def _call(
        self,
        client: McpClient,
        tool: str,
        qualified: str,
        arguments: Mapping[str, Any],
        timeout_s: float,
    ) -> ToolResult:
        try:
            text, failed = await client.call(tool, arguments, timeout_s=timeout_s)
        except McpError as exc:
            return ToolResult.error(f"{qualified}: {exc}")
        body = text or "(the server returned no text)"
        if failed:
            return ToolResult(ok=False, output=body, data={"mcp": qualified})
        return ToolResult(ok=True, output=body, data={"mcp": qualified})

    def status(self) -> list[dict[str, Any]]:
        """One row per configured server, for the settings panel.

        ``error`` is why it is not running: a command that does not exist, a
        server that spoke the wrong protocol, a handshake that timed out. It is
        kept from the boot attempt, so a server that failed before the window
        opened still explains itself when the panel is read, with the last
        lines it printed on stderr.

        Reading the rows is also where a server that died after boot is noticed:
        nothing else watches a child once it has started, so a crashed process
        used to keep its row green and its tools callable.
        """
        rows: list[dict[str, Any]] = []
        for name, server in self.cfg.servers.items():
            client = self.clients.get(name)
            if client is not None and not client.running:
                self._forget(name, client)
            prefix = f"mcp.{_NAME.sub('_', name).strip('._-') or 'server'}."
            rows.append(
                {
                    "name": name,
                    "command": server.command,
                    "args": list(server.args),
                    "cwd": server.cwd,
                    "env": dict(server.env),
                    "enabled": bool(server.enabled),
                    "tier": int(server.tier),
                    "running": name in self.clients,
                    "tools": sorted(q for q in self.tools if q.startswith(prefix)),
                    "error": self.errors.get(name, ""),
                }
            )
        return rows

    def _forget(self, name: str, client: McpClient) -> None:
        """A server whose process is gone keeps no tools and says so in its row.

        A capability that cannot run is not one she should be offered, so the
        tools are withdrawn from the registry together with the process; their
        handlers would only ever return ``not running``. The reason stays, so
        the row explains itself until a save or ``retry all`` starts it again.
        """
        self.clients.pop(name, None)
        self._unregister(name)
        code = client.exit_code
        reason = (
            f"the server exited after it started (exit code {code})"
            if code is not None
            else "the server exited after it started"
        )
        self.errors[name] = _explain(reason, client)
        log.warning("mcp server %s exited; its tools were withdrawn: %s", name, reason)

    def _unregister(self, name: str) -> None:
        """Take one server's tools out of the registry, and nothing else."""
        for qualified in [
            q for q, owner in self.tools.items() if owner.startswith(f"{name}/")
        ]:
            self.registry.unregister(qualified)
            self.tools.pop(qualified, None)

    async def _stop_one(self, name: str) -> None:
        """One server down: its tools, its process, and the reason it had."""
        self._unregister(name)
        self.errors.pop(name, None)
        client = self.clients.pop(name, None)
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            log.exception("mcp server %s did not close cleanly", name)

    async def reload(self, names: set[str] | None = None) -> int:
        """Restart the named servers, or every one of them. Returns how many came up.

        One save touches one row, so it restarts one server: closing the rest
        would cold-start each of them — an ``npx`` server takes seconds — and
        blink their tools out of the registry for work that did not involve
        them. Only the names given are stopped, and a name is unregistered
        before its process closes, so a removed or switched-off server stops
        being callable at once rather than at the next boot.
        """
        if names is None:
            self.errors.clear()
            targets = set(self.cfg.servers) | set(self.clients)
        else:
            targets = set(names)
        for name in targets:
            await self._stop_one(name)
        started = 0
        for name in targets:
            server = self.cfg.servers.get(name)
            if server is None or not server.enabled or not server.command.strip():
                continue
            if await self._start_one(name, server):
                started += 1
        return started

    async def aclose(self) -> None:
        for name, client in list(self.clients.items()):
            try:
                await client.close()
            except Exception:
                log.exception("mcp server %s did not close cleanly", name)
        self.clients.clear()
