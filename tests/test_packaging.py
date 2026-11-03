"""server.json (MCP Registry entry) must agree with pyproject.toml and the README."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def server() -> dict:
    return json.loads((ROOT / "server.json").read_text())


def test_server_json_matches_pyproject():
    proj, srv = project(), server()
    assert srv["version"] == proj["version"]
    [package] = srv["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == proj["name"]
    assert package["version"] == proj["version"]
    assert package["transport"] == {"type": "stdio"}


def test_server_json_description_fits_the_registry():
    assert 0 < len(server()["description"]) <= 100


def test_readme_carries_the_registry_ownership_marker():
    # The registry checks the PyPI description (the README) for this exact marker.
    name = server()["name"]
    assert name == "io.github.aidev-iprokszin/ipcam-mcp"
    assert re.search(rf"mcp-name: {re.escape(name)}(\s|-->)", (ROOT / "README.md").read_text())
