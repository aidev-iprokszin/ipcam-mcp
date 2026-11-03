"""MCP tool annotations: hints that tell clients what a tool does to its environment.

Every tool talks only to cameras on the local network and the local disk, so none is
open-world. Hints are advisory; the tools keep their own safeguards (e.g. delete_camera's
approval form).
"""

from typing import Any

from mcp.types import ToolAnnotations


def read_only(title: str) -> dict[str, Any]:
    """``@mcp.tool(**read_only(...))``: reads config or files, changes nothing."""
    return {
        "title": title,
        "annotations": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    }


def changes(title: str, *, destructive: bool, idempotent: bool) -> dict[str, Any]:
    """``@mcp.tool(**changes(...))``: changes files, config or the camera.

    destructive: may replace or delete data (else it only adds or moves things).
    idempotent: repeating the call with the same arguments has no further effect.
    """
    return {
        "title": title,
        "annotations": ToolAnnotations(
            read_only_hint=False,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=False,
        ),
    }
