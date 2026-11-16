"""MCP: servers that speak the Model Context Protocol, exposed as Capri tools.

Capri is the client. A server is a subprocess (or, later, an HTTP endpoint) that
advertises tools over JSON-RPC; this package launches it, lists what it offers,
and registers each tool in the same registry the built-ins live in, so policy,
confirmation, tracing and the planner see one kind of thing.

Only ``tools/*`` is used. Resources, prompts, sampling and roots are not part of
the contract yet: a tool is the unit the loop can actually act on.
"""

from .client import McpClient, McpError, flatten_result
from .service import McpService, clean_schema, tool_name

__all__ = [
    "McpClient",
    "McpError",
    "McpService",
    "clean_schema",
    "flatten_result",
    "tool_name",
]
