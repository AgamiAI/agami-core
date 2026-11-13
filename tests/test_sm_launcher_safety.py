"""The launchers never run code from the caller's folder, and the verbs the permission hook approves
never install.

The hook (`scripts/approve_own_calls.py`) lets a few read-only `sm` verbs run with no prompt. Two
things behind it must hold for that to be safe: a missing or stale library must not turn such a call
into a promptless pip install (a network fetch that runs build code), and no Python the launchers
start may import a `pip.py` (or anything) planted in the session's working folder, which is a cloned
repository as often as not.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "agami" / "scripts"
SM = SCRIPTS / "sm"
PY = SCRIPTS / "py"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher tests")

# Logs every call; the import probe (`-c`) fails, so the library reads as missing; `-m pip` and the
# `-c` probe each run the REAL interpreter in the same folder, so a planted `pip.py` would run.
_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$SM_SHIM_LOG"
case "$1" in
  -c) exec "$REAL_PY" -c 'import pip, sys; sys.exit(1)' ;;
  -m) [ "$2" = pip ] && exec "$REAL_PY" -m pip --version >/dev/null; exit 0 ;;
  *) exit 0 ;;
esac
"""


def _hostile_cwd(tmp_path: Path) -> tuple[Path, Path]:
    cwd = tmp_path / "cloned-repo"
    cwd.mkdir()
    marker = tmp_path / "pwned"
    (cwd / "pip.py").write_text(f"open({str(marker)!r}, 'w').close()\nraise SystemExit(0)\n")
    return cwd, marker


def _run(tmp_path: Path, launcher: Path, *args: str, shim: bool = True) -> tuple:
    cwd, marker = _hostile_cwd(tmp_path)
    log = tmp_path / "calls.log"
    env = {**os.environ, "SM_SHIM_LOG": str(log), "REAL_PY": sys.executable,
           "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art")}
    env.pop("PYTHONSAFEPATH", None)
    if shim:
        py = tmp_path / "pyshim"
        py.write_text(_SHIM)
        py.chmod(0o755)
        env["AGAMI_PYTHON"] = str(py)
    else:
        env["AGAMI_PYTHON"] = sys.executable
    proc = subprocess.run(["bash", str(launcher), *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls, marker


def _hook_verbs() -> set[str]:
    spec = importlib.util.spec_from_file_location("hook_for_launcher_test",
                                                  SCRIPTS / "approve_own_calls.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.VERBS)


def _launcher_verbs() -> set[str]:
    m = re.search(r'^NO_INSTALL_VERBS="([^"]*)"', SM.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, "sm lost its NO_INSTALL_VERBS list"
    return set(m.group(1).split())


def test_the_launchers_no_install_list_is_the_hooks_table():
    assert _launcher_verbs() == _hook_verbs()


@pytest.mark.parametrize("verb", sorted(_hook_verbs()))
def test_an_approved_verb_with_no_library_stops_without_installing(tmp_path, verb):
    proc, calls, marker = _run(tmp_path, SM, verb, str(tmp_path / "art" / "acme"))
    assert proc.returncode == 3
    assert proc.stderr.strip().splitlines()[-1] == (
        "agami's library needs installing or updating: run  "
        f'bash "{SM.parent.parent.resolve()}/scripts/sm" install')
    assert not [c for c in calls if "pip" in c]
    assert not marker.exists()


@pytest.mark.parametrize("verb", ["introspect", "install", "curate"])
def test_other_verbs_still_install_and_never_from_the_callers_folder(tmp_path, verb):
    proc, calls, marker = _run(tmp_path, SM, verb, "--profile", "acme")
    assert [c for c in calls if c.startswith("-m pip install")], calls  # the install was attempted
    assert not marker.exists(), "a pip.py in the working folder ran"


def test_the_import_probe_does_not_import_from_the_callers_folder(tmp_path):
    # the shim's `-c` imports `pip` from wherever sys.path says, in the folder it was started in
    _run(tmp_path, SM, "steps", str(tmp_path / "art" / "acme"))
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("args", [["-c", "import pip"], ["-m", "pip", "--version"]])
def test_py_does_not_import_from_the_callers_folder(tmp_path, args):
    if sys.version_info < (3, 11):
        pytest.skip("PYTHONSAFEPATH needs Python 3.11")
    proc, _, marker = _run(tmp_path, PY, *args, shim=False)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()


def test_py_still_runs_a_plugin_script_that_imports_its_neighbours(tmp_path):
    # PYTHONSAFEPATH would drop a script's own folder from sys.path; py must not set it for scripts
    assert "from _agami_lib import" in (SCRIPTS / "csv_to_sections.py").read_text(encoding="utf-8")
    proc, _, _ = _run(tmp_path, PY, "csv_to_sections.py", "--help", shim=False)
    assert proc.returncode == 0, proc.stderr


def test_every_pip_call_in_sm_runs_from_root():
    text = SM.read_text(encoding="utf-8")
    body = text[text.index("_pip_try() {"):text.index("\n}\n", text.index("_pip_try() {"))]
    assert "( cd / && {" in body
    assert text.index("export PYTHONSAFEPATH=1") < text.index('PY="$(resolve_python)"')
