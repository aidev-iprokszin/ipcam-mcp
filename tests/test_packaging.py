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


def plugin_manifest() -> dict:
    return json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_plugin_version_is_a_released_package_version():
    # The plugin follows a release: its lock can only pin a version that is on PyPI, so
    # during a release the package is ahead and a follow-up PR moves the plugin.
    plugin = plugin_manifest()
    assert plugin["name"] == "ipcam"
    assert version_tuple(plugin["version"]) <= version_tuple(project()["version"])


def plugin_project() -> dict:
    return tomllib.loads((PLUGIN / "pyproject.toml").read_text())


def test_plugin_project_pins_the_package_of_its_version():
    plugin_proj = plugin_project()
    assert plugin_proj["project"]["dependencies"] == [
        f"{project()['name']}=={plugin_manifest()['version']}"
    ]
    assert plugin_proj["project"]["requires-python"] == project()["requires-python"]
    assert plugin_proj["tool"]["uv"]["package"] is False  # only an environment, no build


def test_plugin_lock_pins_the_package_from_pypi():
    lock = tomllib.loads((PLUGIN / "uv.lock").read_text())
    [package] = [p for p in lock["package"] if p["name"] == project()["name"]]
    assert package["version"] == plugin_manifest()["version"]
    assert package["source"] == {"registry": "https://pypi.org/simple"}
    every_wheel = [w for p in lock["package"] for w in p.get("wheels", [])]
    assert every_wheel and all(w["hash"].startswith("sha256:") for w in every_wheel)


def test_plugin_lock_covers_linux_and_apple_silicon():
    # `uv run --frozen` refuses to start on a platform the lock doesn't cover.
    lock = tomllib.loads((PLUGIN / "uv.lock").read_text())
    assert sorted(lock["supported-markers"]) == sorted(
        [
            "platform_machine == 'x86_64' and sys_platform == 'linux'",
            "platform_machine == 'aarch64' and sys_platform == 'linux'",
            "platform_machine == 'arm64' and sys_platform == 'darwin'",
        ]
    )


def test_plugin_lock_fits_the_directory_file_limit():
    # The plugin directory holds any non-image file over 256 KiB for review.
    assert (PLUGIN / "uv.lock").stat().st_size < 256 * 1024


def test_plugin_runs_the_server_from_its_lock():
    # --frozen: install exactly what uv.lock pins. The environment lives in the plugin's
    # data folder, which survives updates; the versioned plugin folder stays untouched.
    servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
    assert servers == {
        "ipcam": {
            "command": "uv",
            "args": ["run", "--frozen", "--project", "${CLAUDE_PLUGIN_ROOT}", "ipcam-mcp"],
            "env": {"UV_PROJECT_ENVIRONMENT": "${CLAUDE_PLUGIN_DATA}/venv"},
        }
    }


def test_marketplace_lists_the_plugin():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    [entry] = market["plugins"]
    assert entry["name"] == "ipcam"
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()
    assert "version" not in entry  # plugin.json is the only place for it


def test_plugin_icon_fits_the_directory():
    # Anthropic's plugin directory: square PNG, 512-2048 px per side, under 2 MB.
    from PIL import Image

    icon = PLUGIN / ".claude-plugin" / "icon.png"
    assert icon.stat().st_size < 2 * 1024 * 1024
    with Image.open(icon) as im:
        assert im.format == "PNG"
        assert im.width == im.height and 512 <= im.width <= 2048


def test_plugin_has_its_own_readme():
    # The plugin directory wants at least 40 words in the plugin folder, code blocks excluded.
    text = re.sub(r"```.*?```", "", (PLUGIN / "README.md").read_text(), flags=re.DOTALL)
    assert len(text.split()) >= 40


def test_plugin_links_point_into_this_repo():
    plugin = plugin_manifest()
    blob = "https://github.com/aidev-iprokszin/ipcam-mcp/blob/main/"
    privacy = plugin["privacyPolicyUrl"]
    assert privacy.startswith(blob) and (ROOT / privacy.removeprefix(blob)).is_file()
    assert plugin["supportUrl"] == "https://github.com/aidev-iprokszin/ipcam-mcp/issues"
    assert plugin["documentationUrl"].startswith("https://github.com/aidev-iprokszin/ipcam-mcp")
