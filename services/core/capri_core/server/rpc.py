"""Shared WebSocket RPC primitives used by CapriServer and its route mixins."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from . import protocol

if TYPE_CHECKING:
    from .session import Session

Handler = Callable[["Client", dict[str, Any]], Awaitable[Any]]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validation_note(exc: ValidationError) -> str:
    """One line from a pydantic error, without the URL half."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid"))
    return f"{where}: {message}" if where else message


class RpcError(Exception):
    def __init__(self, code: str, message: str | None = None, fatal: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.fatal = fatal


class Client:
    """One connected shell."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.session: Session | None = None
        self.authed = False
        # The renderer owns the local mic route; this is the daemon's copy of
        # its state for the current shell client.
        self.mic_route_active = False

    async def send(self, message: dict[str, Any]) -> None:
        try:
            await self.ws.send(protocol.encode(message))
        except ConnectionClosed:
            pass

    @property
    def peer(self) -> str:
        addr = getattr(self.ws, "remote_address", None)
        return f"{addr[0]}:{addr[1]}" if addr else "?"
