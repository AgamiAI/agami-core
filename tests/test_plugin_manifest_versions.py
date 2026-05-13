"""Enforces the three-version-strings-match invariant from CONTRIBUTING.md.

The plugin marketplace caches by version, so the three version fields must
stay in sync on every user-visible release:
    - .claude-plugin/marketplace.json#metadata.version
    - .claude-plugin/marketplace.json#plugins[0].version
    - plugins/agami/.claude-plugin/plugin.json#version
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "agami" / ".claude-plugin" / "plugin.json"


def test_three_version_strings_match() -> None:
    market = json.loads(MARKETPLACE.read_text())
    plugin = json.loads(PLUGIN_MANIFEST.read_text())

    market_meta_version = market["metadata"]["version"]
    market_plugin_version = next(
        p["version"] for p in market["plugins"] if p["name"] == "agami"
    )
    plugin_version = plugin["version"]

    versions = {
        "marketplace.json#metadata.version": market_meta_version,
        "marketplace.json#plugins[agami].version": market_plugin_version,
        "plugins/agami/.claude-plugin/plugin.json#version": plugin_version,
    }
    distinct = set(versions.values())
    assert len(distinct) == 1, (
        "Version strings out of sync (CONTRIBUTING.md §version-bump discipline):\n  "
        + "\n  ".join(f"{k} = {v!r}" for k, v in versions.items())
    )
