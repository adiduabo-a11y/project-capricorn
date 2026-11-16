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
            self.errors[name] = str(exc)
            log.warning("mcp server %s is not usable: %s", name, exc)
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
        opened still explains itself when the panel is read.
        """
        rows: list[dict[str, Any]] = []
        for name, server in self.cfg.servers.items():
            prefix = f"mcp.{_NAME.sub('_', name).strip('._-') or 'server'}."
            rows.append(
                {
                    "name": name,
                    "command": server.command,
                    "args": list(server.args),
                    "cwd": server.cwd,
                    "enabled": bool(server.enabled),
                    "tier": int(server.tier),
                    "running": name in self.clients,
                    "tools": sorted(q for q in self.tools if q.startswith(prefix)),
                    "error": self.errors.get(name, ""),
                }
            )
        return rows

    async def reload(self) -> int:
        """Re-read the config and restart everything. Returns how many came up.

        The registry is the source of truth for what she can call, so the old
        tools go before the new ones arrive: a server that was removed in the
        window must stop being callable, not linger until the next boot.
        """
        for qualified in list(self.tools):
            self.registry.unregister(qualified)
        self.tools.clear()
        await self.aclose()
        self.errors.clear()
        return await self.start()

    async def aclose(self) -> None:
        for name, client in list(self.clients.items()):
            try:
                await client.close()
            except Exception:
                log.exception("mcp server %s did not close cleanly", name)
        self.clients.clear()
