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


PLUGIN = ROOT / "plugins" / "ipcam"


def test_plugin_version_matches_pyproject():
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "ipcam"
    assert plugin["version"] == project()["version"]


def test_plugin_runs_the_pypi_package_of_its_version():
    # Pinned, so updating the plugin also updates the server (uvx would keep a cached one).
    servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
    assert servers == {
        "ipcam": {"command": "uvx", "args": [f"{project()['name']}=={project()['version']}"]}
    }


def test_marketplace_lists_the_plugin():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    [entry] = market["plugins"]
    assert entry["name"] == "ipcam"
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()
    assert "version" not in entry  # plugin.json is the only place for it
