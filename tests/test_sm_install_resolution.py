"""OCR-031 regression: the `sm` launcher must install agami-core[model] in a **marketplace** layout —
`scripts/` + `lib/` with NO `packages/` sibling and no pip install — without crashing and without pointing
at the dev-only `packages/agami-core` path (the failure the real run-through hit).

We run `sm install` with a **fake `$PY` shim** (`AGAMI_PYTHON`) that logs every pip-install it's asked to
run and fails the bare index requirement `agami-core[model]` (simulating "not on PyPI yet") so we can
observe the git fallback — no real network install happens. Also asserts the dev checkout still uses the
editable install, and that the skill delegates to `sm install` (no hardcoded dev-path).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SM = REPO / "plugins" / "agami" / "scripts" / "sm"
SCRIPTS = REPO / "plugins" / "agami" / "scripts"
LIB = REPO / "plugins" / "agami" / "lib"
SKILL = REPO / "plugins" / "agami" / "skills" / "agami-connect" / "SKILL.md"
# The plugin version drives the cache-dir name + the pinned git ref sm derives — read it from the
# manifest so a version bump needs no edit here.
PLUGIN_VERSION = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())["metadata"]["version"]

# A fake interpreter: the import check fails until a successful install "lands" it (a marker file), each
# pip install is logged, and the bare index name fails so the git fallback is exercised — the `-e` (dev)
# and git requirements "succeed" and drop the marker so the post-install re-check passes.
_SHIM = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["SM_SHIM_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
marker = os.environ["SM_SHIM_INSTALLED"]
if args[:1] == ["-c"]:
    sys.exit(0 if os.path.exists(marker) else 1)  # `import semantic_model, …` works only once installed
if "pip" in args and "install" in args:
    joined = " ".join(args)
    if "agami-core[model]" in joined and "git+" not in joined and "-e" not in args:
        sys.exit(1)  # bare index name: pretend it's not on an index yet
    open(marker, "w").close()  # -e (dev) and git requirements install the package
    sys.exit(0)
sys.exit(0)
"""


def _run_install(sm_path: Path, tmp_path: Path):
    shim = tmp_path / "pyshim"
    shim.write_text(_SHIM)
    shim.chmod(0o755)
    log = tmp_path / "pip.log"
    env = {
        **os.environ,
        "AGAMI_PYTHON": str(shim),
        "SM_SHIM_LOG": str(log),
        "SM_SHIM_INSTALLED": str(tmp_path / "installed.marker"),
        "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art"),
    }
    r = subprocess.run(["bash", str(sm_path), "install"], env=env, capture_output=True, text=True)
    lines = log.read_text().splitlines() if log.exists() else []
    installs = [ln for ln in lines if "pip" in ln and "install" in ln]
    return r, installs


def test_marketplace_layout_installs_from_git_not_devpath(tmp_path):
    cache = tmp_path / "agami-core" / PLUGIN_VERSION  # the cache dir name is the version
    shutil.copytree(SCRIPTS, cache / "scripts")
    shutil.copytree(LIB, cache / "lib")  # NO packages/ sibling
    r, installs = _run_install(cache / "scripts" / "sm", tmp_path)

    assert r.returncode == 0, r.stderr  # no crash at the old PKG_DIR line
    assert installs, "sm attempted no install"
    # Never the dev-only editable path.
    assert not any("-e" in ln.split() for ln in installs), installs
    assert not any("/packages/agami-core" in ln for ln in installs), installs
    # Index tried first, git as the fallback, pinned to the cache's version.
    idx = next(i for i, ln in enumerate(installs) if "agami-core[model]" in ln and "git+" not in ln)
    git = next(i for i, ln in enumerate(installs) if "git+" in ln)
    assert idx < git, installs
    assert f"git+https://github.com/AgamiAI/agami-core@v{PLUGIN_VERSION}#subdirectory=packages/agami-core" in "\n".join(installs)


def test_dev_checkout_uses_editable(tmp_path):
    # The real repo has packages/agami-core → editable install wins; git never tried.
    r, installs = _run_install(SM, tmp_path)
    assert r.returncode == 0, r.stderr
    assert any("-e" in ln.split() and ln.rstrip().endswith("/packages/agami-core[model]") for ln in installs), installs


def test_skill_delegates_install_to_sm():
    txt = SKILL.read_text()
    assert "packages/agami-core[model]" not in txt  # no hardcoded dev-path install command
    assert 'scripts/sm" install' in txt
