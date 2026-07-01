"""OCR-033 regression: `promote_credentials.py` and `setup_desktop_mcp.py` must run in a **marketplace**
layout — `scripts/` + `lib/` with NO `packages/` sibling and no `agami_paths.py` next to the script —
without a `ModuleNotFoundError` (they used to bare-import `agami_paths` off their own dir / a dev
`packages/src`, issue #1), and `setup_desktop_mcp.py` must NOT install from a hardcoded `packages/agami-core`
path (issue #8) — it delegates to `sm install`.

We reproduce the marketplace cache by copying just `scripts/` + `lib/` into a temp `agami-core/<version>/`
dir and running the scripts from there with the test interpreter.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "agami" / "scripts"
LIB = REPO / "plugins" / "agami" / "lib"
PLUGIN_VERSION = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())["metadata"]["version"]


def _marketplace_cache(tmp_path: Path) -> Path:
    """A scripts/ + lib/ cache with NO packages/ sibling — the marketplace install shape."""
    cache = tmp_path / "agami-core" / PLUGIN_VERSION
    shutil.copytree(SCRIPTS, cache / "scripts")
    shutil.copytree(LIB, cache / "lib")
    return cache


def test_promote_credentials_runs_in_marketplace_layout(tmp_path):
    cache = _marketplace_cache(tmp_path)
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art)}
    r = subprocess.run(
        [sys.executable, str(cache / "scripts" / "promote_credentials.py")],
        env=env, capture_output=True, text=True,
    )
    # The bug was a ModuleNotFoundError at import; now it resolves agami_paths via _agami_lib and runs.
    assert "ModuleNotFoundError" not in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    # No credentials.example present → the deterministic "nothing to promote" status, not a crash.
    assert r.stdout.startswith("NOTHING"), (r.stdout, r.stderr)


def test_setup_desktop_help_runs_in_marketplace_layout(tmp_path):
    cache = _marketplace_cache(tmp_path)
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art)}
    # `--help` still loads the module (the crash was at import, line 54) — the minimal #1 regression.
    r = subprocess.run(
        [sys.executable, str(cache / "scripts" / "setup_desktop_mcp.py"), "--help"],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "ModuleNotFoundError" not in r.stderr, r.stderr
    assert "usage:" in r.stdout.lower()


def test_setup_desktop_never_installs_from_packages_path():
    # #8: the install path must delegate to `sm install`, never a hardcoded `packages/agami-core[model]`.
    src = (SCRIPTS / "setup_desktop_mcp.py").read_text()
    assert "packages/agami-core[model]" not in src, "must not pip-install the dev-only packages/ path"
    assert '"sm"' in src and '"install"' in src, "install must delegate to `sm install`"
