"""#529: the plugin knows its own version, and says when the agami-core library it runs is older.

Covers the shared helper (`_plugin_version.py`), the warning `py` and `_agami_lib` print, and the
no-pip message from `sm`. The end-to-end cases build a real pip-less virtualenv holding a fake old
agami-core distribution (metadata only), which is exactly what the resolver met in the field.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "agami"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _plugin_version as PV  # noqa: E402

PLUGIN_VERSION = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout and shell shims")


def test_plugin_version_reads_the_manifest():
    assert PV.plugin_version() == PLUGIN_VERSION


def test_plugin_version_falls_back_to_a_versioned_folder_name(tmp_path):
    assert PV.plugin_version(tmp_path / "0.9.18") == "0.9.18"
    # a commit-hash cache folder starting with a digit is not a version
    assert PV.plugin_version(tmp_path / "3a99b893") is None
    assert PV.plugin_version(tmp_path / "agami") is None


def test_is_older_needs_both_versions():
    assert PV.is_older("0.9.11", "0.9.18")
    assert not PV.is_older("0.9.18", "0.9.18")
    assert not PV.is_older("0.9.18rc1", "0.9.18")
    assert not PV.is_older(None, "0.9.18") and not PV.is_older("0.9.11", None)


def test_warning_names_both_versions_the_interpreter_and_the_fix(monkeypatch):
    monkeypatch.setattr(PV, "installed_library", lambda: {"version": "0.9.11", "editable_from": None})
    msg = PV.library_warning("/x/python3")
    assert "0.9.11" in msg and PLUGIN_VERSION in msg and "/x/python3" in msg
    assert "sm\" install" in msg and "AGAMI_PYTHON" in msg


def test_no_warning_when_current_unknown_or_this_checkout(monkeypatch):
    for lib in ({"version": PLUGIN_VERSION, "editable_from": None},
                {"version": None, "editable_from": None},
                {"version": "0.0.1", "editable_from": str(REPO / "packages" / "agami-core")}):
        monkeypatch.setattr(PV, "installed_library", lambda lib=lib: lib)
        assert PV.library_warning("/x/python3") is None, lib


def _pipless_venv(root: Path, version: str | None) -> Path:
    """A real virtualenv with no pip; `version` adds a metadata-only agami-core of that version."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    py = root / "bin" / "python"
    if version:
        site = subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, check=True).stdout.strip()
        dist = Path(site) / f"agami_core-{version}.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: agami-core\nVersion: {version}\n")
    return py


@POSIX
def test_probe_on_a_real_pipless_venv(tmp_path):
    py = _pipless_venv(tmp_path / "venv", "0.9.11")
    out = subprocess.run([str(py), str(SCRIPTS / "_plugin_version.py"), "--probe"],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == {"has_pip": False, "agami_core": "0.9.11", "this_checkout": False}


@POSIX
def test_py_warns_when_the_library_is_older_than_the_plugin(tmp_path):
    py = _pipless_venv(tmp_path / "venv", "0.9.11")
    env = {**os.environ, "AGAMI_PYTHON": str(py), "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art")}
    r = subprocess.run(["bash", str(SCRIPTS / "py"), "-c", "print('ran')"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ran"
    assert f"is 0.9.11, older than this plugin ({PLUGIN_VERSION})" in r.stderr
    assert str(py) in r.stderr and "sm\" install" in r.stderr


@POSIX
def test_py_is_quiet_when_the_library_is_current(tmp_path):
    py = _pipless_venv(tmp_path / "venv", PLUGIN_VERSION)
    env = {**os.environ, "AGAMI_PYTHON": str(py), "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art")}
    r = subprocess.run(["bash", str(SCRIPTS / "py"), "-c", "pass"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == ""


@POSIX
def test_sm_says_the_python_has_no_pip(tmp_path):
    py = _pipless_venv(tmp_path / "venv", None)
    env = {**os.environ, "AGAMI_PYTHON": str(py), "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art")}
    r = subprocess.run(["bash", str(SCRIPTS / "sm"), "install"], env=env, capture_output=True, text=True)
    assert r.returncode == 3
    assert f"agami: {py} has no pip" in r.stderr
    assert "ensurepip" in r.stderr and "AGAMI_PYTHON" in r.stderr
    assert "couldn't install" not in r.stderr


# A stale library that imports but cannot be upgraded: no pip at all.
_SHIM_STALE_NO_PIP = """#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args[:2] == ["-m", "pip"]:
    sys.exit(1)
if args[:1] == ["-"]:
    sys.exit(1)        # below the version floor
sys.exit(0)            # imports work
"""


@POSIX
def test_sm_says_a_stale_library_cannot_be_upgraded_without_pip(tmp_path):
    cache = tmp_path / "agami-core" / PLUGIN_VERSION
    shutil.copytree(SCRIPTS, cache / "scripts")
    shim = tmp_path / "pyshim"
    shim.write_text(_SHIM_STALE_NO_PIP)
    shim.chmod(0o755)
    env = {**os.environ, "AGAMI_PYTHON": str(shim), "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art")}
    r = subprocess.run(["bash", str(cache / "scripts" / "sm"), "install"], env=env, capture_output=True, text=True)
    assert r.returncode == 3
    assert f"{shim} has no pip, so its agami-core can't be upgraded to this plugin's {PLUGIN_VERSION}" in r.stderr


def test_agami_lib_warns_once_for_an_installed_stale_library(monkeypatch, capsys, tmp_path):
    import _agami_lib

    monkeypatch.setattr(PV, "installed_library", lambda: {"version": "0.9.11", "editable_from": None})
    monkeypatch.delenv(PV.CHECKED_ENV, raising=False)
    _agami_lib._warn_if_older_than_plugin(tmp_path / "site-packages")
    assert "older than this plugin" in capsys.readouterr().err
    # the bundled copy is the plugin's own; and `py` already checked
    _agami_lib._warn_if_older_than_plugin(PLUGIN / "lib")
    monkeypatch.setenv(PV.CHECKED_ENV, "1")
    _agami_lib._warn_if_older_than_plugin(tmp_path / "site-packages")
    assert capsys.readouterr().err == ""


def _with_fake_pip(py: Path) -> Path:
    site = subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (Path(site) / "pip").mkdir()
    (Path(site) / "pip" / "__init__.py").write_text("")
    return py


@POSIX
def test_usable_exit_codes(tmp_path):
    helper = str(SCRIPTS / "_plugin_version.py")
    bare = _pipless_venv(tmp_path / "bare", None)
    stale = _pipless_venv(tmp_path / "stale", "0.9.11")
    current = _pipless_venv(tmp_path / "current", PLUGIN_VERSION)
    with_pip = _with_fake_pip(_pipless_venv(tmp_path / "pip", None))
    codes = {name: subprocess.run([str(p), helper, "--usable"]).returncode
             for name, p in (("bare", bare), ("stale", stale), ("current", current), ("pip", with_pip))}
    assert codes == {"bare": 3, "stale": 3, "current": 0, "pip": 0}


def _tools_without_python(tmp_path: Path) -> Path:
    """The shell tools from /usr/bin and /bin minus every Python, so the machine's own interpreter
    (which may or may not have pip) can't become a candidate and decide the test."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for d in (Path("/usr/bin"), Path("/bin")):
        for exe in d.iterdir():
            link = tools / exe.name
            # lexists: a dangling symlink in both folders reads as absent to exists().
            if not exe.name.startswith("python") and not os.path.lexists(link):
                link.symlink_to(exe)
    return tools


def _resolve_in_shell(tmp_path: Path, path_dirs: list[Path]) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("AGAMI_PYTHON", "VIRTUAL_ENV")}
    env.update(PATH=os.pathsep.join([*map(str, path_dirs), str(_tools_without_python(tmp_path))]),
               AGAMI_ARTIFACTS_DIR=str(tmp_path / "art"), HOME=str(tmp_path))
    r = subprocess.run(["bash", "-c", f'. "{SCRIPTS / "_python.sh"}"; resolve_python'], env=env,
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


@POSIX
def test_path_fallback_passes_over_a_pipless_venv_for_one_that_can_be_brought_current(tmp_path):
    # #529: an activated project venv with no pip and no agami-core shadowed a working Python.
    bad = _pipless_venv(tmp_path / "bad", None)
    good = _with_fake_pip(_pipless_venv(tmp_path / "good", None))
    assert _resolve_in_shell(tmp_path, [bad.parent, good.parent]) == str(good.parent / "python3")


@POSIX
def test_path_fallback_keeps_the_first_when_none_can_be_brought_current(tmp_path):
    bad = _pipless_venv(tmp_path / "bad", None)
    assert _resolve_in_shell(tmp_path, [bad.parent]) == str(bad.parent / "python3")


def _configure(tmp_path: Path, py: Path) -> None:
    cfg = tmp_path / "art" / "local" / ".config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"tool_paths": {"python3": str(py)}}), encoding="utf-8")


@POSIX
def test_a_configured_pipless_python_gives_way_to_a_path_one_that_can_be_brought_current(tmp_path):
    stale = _pipless_venv(tmp_path / "stale", "0.9.11")
    good = _with_fake_pip(_pipless_venv(tmp_path / "good", None))
    _configure(tmp_path, stale)
    assert _resolve_in_shell(tmp_path, [good.parent]) == str(good.parent / "python3")


@POSIX
def test_a_configured_pipless_python_is_kept_when_nothing_better_is_on_path(tmp_path):
    stale = _pipless_venv(tmp_path / "stale", "0.9.11")
    other = _pipless_venv(tmp_path / "other", None)
    _configure(tmp_path, stale)
    assert _resolve_in_shell(tmp_path, []) == str(stale)
    assert _resolve_in_shell(tmp_path, [other.parent]) == str(stale)


@POSIX
def test_a_configured_python_that_can_be_brought_current_is_kept(tmp_path):
    configured = _with_fake_pip(_pipless_venv(tmp_path / "configured", None))
    good = _with_fake_pip(_pipless_venv(tmp_path / "good", None))
    _configure(tmp_path, configured)
    assert _resolve_in_shell(tmp_path, [good.parent]) == str(configured)


@POSIX
@pytest.mark.parametrize("script", ["sm", "py"])
def test_no_python_anywhere_is_explained_not_a_silent_exit(tmp_path, script):
    env = {k: v for k, v in os.environ.items() if k not in ("AGAMI_PYTHON", "VIRTUAL_ENV")}
    env.update(PATH=str(_tools_without_python(tmp_path)), AGAMI_ARTIFACTS_DIR=str(tmp_path / "art"),
               HOME=str(tmp_path))
    r = subprocess.run(["bash", str(SCRIPTS / script), "validate"], env=env, capture_output=True, text=True)
    assert r.returncode == 3
    assert "agami: no Python interpreter found" in r.stderr


# #692: `sm` and connect_resolve.py must agree on the interpreter. The field case: $AGAMI_PYTHON set to
# a build venv holding the current agami-core without its [model] extra and with no pip, and a fully
# equipped Python with an OLDER agami-core on PATH. The resolver picked the older one; `sm` used the
# pinned one.
_SHIM_EQUIPPED_OLDER = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --probe) echo '{"has_pip": true, "agami_core": "0.9.1", "this_checkout": false}'; exit 0 ;;
    *sys.executable*) echo "$0"; exit 0 ;;
  esac
done
exit 0
"""


def _equipped_older(root: Path) -> Path:
    """A python3 on PATH that answers every import probe and has pip, carrying agami-core 0.9.1."""
    root.mkdir(parents=True)
    shim = root / "python3"
    shim.write_text(_SHIM_EQUIPPED_OLDER)
    shim.chmod(0o755)
    return shim


def _launcher_env(tmp_path: Path, path_dirs: list[Path], pinned: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("AGAMI_PYTHON", "VIRTUAL_ENV", "AGAMI_PROFILE")}
    env.update(PATH=os.pathsep.join([*map(str, path_dirs), str(_tools_without_python(tmp_path))]),
               AGAMI_ARTIFACTS_DIR=str(tmp_path / "art"), HOME=str(tmp_path))
    if pinned:
        env["AGAMI_PYTHON"] = str(pinned)
    return env


def _resolve_both(tmp_path: Path, path_dirs: list[Path], pinned: Path | None) -> tuple[str, dict]:
    env = _launcher_env(tmp_path, path_dirs, pinned)
    shell = subprocess.run(["bash", "-c", f'. "{SCRIPTS / "_python.sh"}"; resolve_python'], env=env,
                           capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run([sys.executable, str(SCRIPTS / "connect_resolve.py")], env=env,
                         capture_output=True, text=True, check=True).stdout
    return shell, json.loads(out)


def _py_launcher_python(tmp_path: Path, path_dirs: list[Path], pinned: Path | None) -> str:
    """The interpreter `py` runs: the deploy path's `py -m deploy_preflight` uses exactly this one."""
    env = _launcher_env(tmp_path, path_dirs, pinned)
    return subprocess.run(["bash", str(SCRIPTS / "py"), "-c", "import sys; print(sys.executable)"], env=env,
                          capture_output=True, text=True, check=True).stdout.strip()


@POSIX
def test_sm_and_connect_resolve_use_the_pinned_python_that_lacks_the_model_extra(tmp_path):
    pinned = _pipless_venv(tmp_path / "build", PLUGIN_VERSION)
    older = _equipped_older(tmp_path / "usr")
    shell, got = _resolve_both(tmp_path, [older.parent], pinned)
    assert shell == got["data"]["interpreter"]["python3"] == str(pinned)
    (lacks,) = [a for a in got["anomalies"] if a["kind"] == "agami_python_lacks"]
    assert str(pinned) in lacks["detail"]
    assert f"agami-core {PLUGIN_VERSION} without its [model] extra (pydantic, sqlglot, pyyaml)" in lacks["detail"]
    assert f'"{pinned}" -m pip install "agami-core[model]=={PLUGIN_VERSION}"' in lacks["detail"]


@POSIX
def test_sm_and_connect_resolve_agree_with_no_pin(tmp_path):
    bad = _pipless_venv(tmp_path / "bad", None)
    older = _equipped_older(tmp_path / "usr")
    shell, got = _resolve_both(tmp_path, [bad.parent, older.parent], None)
    assert shell == got["data"]["interpreter"]["python3"] == str(older)


@POSIX
def test_sm_names_the_missing_model_extra_and_the_missing_pip(tmp_path):
    py = _pipless_venv(tmp_path / "venv", PLUGIN_VERSION)
    env = {**os.environ, "AGAMI_PYTHON": str(py), "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art")}
    r = subprocess.run(["bash", str(SCRIPTS / "sm"), "install"], env=env, capture_output=True, text=True)
    assert r.returncode == 3
    assert (f"agami-core {PLUGIN_VERSION} is installed in {py} but without its model extra "
            "(pydantic, sqlglot, pyyaml)") in r.stderr
    assert "no pip" in r.stderr and "ensurepip" in r.stderr
    assert f'"{py}" -m pip install "agami-core[model]=={PLUGIN_VERSION}"' in r.stderr
    assert "agami-core can't be installed into it" not in r.stderr


# #698 review: without $AGAMI_PYTHON, preflight scored its own candidates and could pick a later Python
# with the deps while `sm`/`py` kept the first installable one, so setup checked one Python and the
# install went into another (exit 12); the deploy launcher's `py -m deploy_preflight` likewise missed
# the library. There is one selection now, `_python.sh`'s, and preflight reports it.


@POSIX
def test_preflight_sm_and_py_agree_when_the_first_python_is_installable_but_bare(tmp_path):
    bare = _with_fake_pip(_pipless_venv(tmp_path / "bare", None))
    older = _equipped_older(tmp_path / "usr")
    shell, got = _resolve_both(tmp_path, [bare.parent, older.parent], None)
    assert shell == str(bare.parent / "python3")
    assert got["data"]["interpreter"]["python3"] == shell
    assert got["data"]["interpreter"]["core_ready"] is False
    assert _py_launcher_python(tmp_path, [bare.parent, older.parent], None) == shell


@POSIX
def test_preflight_sm_and_py_agree_on_a_configured_python(tmp_path):
    configured = _with_fake_pip(_pipless_venv(tmp_path / "configured", None))
    older = _equipped_older(tmp_path / "usr")
    _configure(tmp_path, configured)
    shell, got = _resolve_both(tmp_path, [older.parent], None)
    assert shell == str(configured)
    assert got["data"]["interpreter"]["python3"] == shell
    assert _py_launcher_python(tmp_path, [older.parent], None) == shell


@POSIX
def test_the_deploy_launcher_uses_the_pinned_python_too(tmp_path):
    pinned = _pipless_venv(tmp_path / "build", PLUGIN_VERSION)
    older = _equipped_older(tmp_path / "usr")
    shell, got = _resolve_both(tmp_path, [older.parent], pinned)
    assert _py_launcher_python(tmp_path, [older.parent], pinned) == shell == got["data"]["interpreter"]["python3"]


def test_the_deploy_skill_runs_deploy_preflight_through_the_shared_launcher():
    text = (PLUGIN / "skills" / "agami-deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert 'scripts/py" -m deploy_preflight' in text
    assert "$PY -m deploy_preflight" not in text
