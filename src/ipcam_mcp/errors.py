"""Error types and secret scrubbing.

Every message carried by an ``CameraMcpError`` is meant to be safe to show to the
MCP client: it must never contain credentials.
"""

from collections.abc import Iterable
from urllib.parse import quote


class CameraMcpError(Exception):
    """Base class for errors whose message is safe to show to the client."""


class ConfigError(CameraMcpError):
    """Invalid or missing configuration."""


class DeviceError(CameraMcpError):
    """A device could not be found, lacks a capability, or failed to respond."""


class MediaError(CameraMcpError):
    """Capturing a snapshot or recording failed."""


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Replace each non-empty secret (raw and URL-encoded) in ``text`` with ``***``."""
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, quote(secret, safe="")}:
            text = text.replace(form, "***")
    return text
