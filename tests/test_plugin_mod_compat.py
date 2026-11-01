"""The pane's mod must not make an older Claude Code reject the whole plugin (and so hide its skills).

A build without mod support reads `hooks/hooks.json` as a classic hooks file, which needs a `hooks`
object, and refuses unknown keys in `plugin.json`. Measured on 2.1.109: a `hooks.json` holding only
`modules` fails with "hooks: expected record, received undefined", and a `"types"` key fails with
"Unrecognized key". A build with mod support (2.1.299+) reads `types/index.d.ts` by default, so the key
is not needed there.
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "agami"


def test_hooks_json_carries_an_empty_classic_hooks_object_beside_modules():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert hooks.get("hooks") == {}
    assert hooks.get("modules") == ["./register.tsx"]


def test_plugin_json_has_no_types_key_and_the_contract_sits_at_the_default_path():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert "types" not in manifest
    assert (PLUGIN / "types" / "index.d.ts").is_file()
