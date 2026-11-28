"""The MCP registry manifests launch a command the package actually installs.

server.json and smithery.yaml start the stdio server as `agami-core` (via uvx). If the console
script, the module function it points at, or the manifest versions drift apart, every registry
listing breaks at install time, after the listing is already public.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "packages" / "agami-core"


def _pyproject() -> dict:
    return tomllib.loads((PACKAGE / "pyproject.toml").read_text())


def test_console_script_points_at_the_stdio_server():
    import mcp_harness

    target = _pyproject()["project"]["scripts"]["agami-core"]
    module, func = target.split(":")
    assert module == "mcp_harness"
    assert callable(getattr(mcp_harness, func))


def test_server_json_pins_a_coherent_version_and_command():
    manifest = json.loads((REPO_ROOT / "server.json").read_text())
    package = manifest["packages"][0]
    assert package["identifier"] == _pyproject()["project"]["name"]
    assert package["version"] == manifest["version"]
    from_arg = next(a["value"] for a in package["runtimeArguments"] if a["name"] == "--from")
    assert from_arg == f"agami-core[model]=={manifest['version']}"
    # The registry caps the description at 100 characters and rejects the publish otherwise.
    assert len(manifest["description"]) <= 100


def test_pypi_readme_claims_the_registry_name():
    # The registry proves PyPI ownership by finding `mcp-name: <name>` in the package README.
    name = json.loads((REPO_ROOT / "server.json").read_text())["name"]
    assert f"mcp-name: {name}" in (PACKAGE / "README.md").read_text()


def test_smithery_launches_the_same_command():
    text = (REPO_ROOT / "smithery.yaml").read_text()
    assert re.search(r"command: 'uvx'", text)
    assert "'agami-core[model]', 'agami-core'" in text
