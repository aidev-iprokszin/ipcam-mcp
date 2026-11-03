"""Turning errors into what an MCP client may see."""

import logging
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError

from ipcam_mcp.errors import CameraMcpError

log = logging.getLogger("ipcam_mcp")


def client_message(e: Exception) -> str:
    """The error's message if it is safe to show; otherwise a generic one (details logged)."""
    if isinstance(e, (CameraMcpError, ValueError)):
        return str(e)
    if isinstance(e, OSError) and e.filename:  # a file error; socket errors have no filename
        log.error("%s in tool call: %s", type(e).__name__, e)
        return f"could not access {os_error_text(e)}"
    log.error("unexpected %s in tool call", type(e).__name__)
    return f"internal error ({type(e).__name__}); see server log"


def os_error_text(e: OSError) -> str:
    """``<path>: <reason>`` for a file error."""
    return f"{e.filename}: {e.strerror or type(e).__name__}"


async def guarded[T](fn: Callable[[], Awaitable[T]]) -> T:
    """Turn our errors into tool errors; hide details of unexpected ones."""
    try:
        return await fn()
    except Exception as e:  # noqa: BLE001 — never leak unexpected messages to the client
        raise ToolError(client_message(e)) from None
