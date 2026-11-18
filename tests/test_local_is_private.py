"""`<artifacts_dir>/local/` is 0700 whoever creates it, and an existing one is tightened.

The skill used to run `mkdir -m 700 -p <artifacts_dir>/local` and then say `.config` needed no chmod
because `local/` was "already private". It wasn't, always: `sm` and `py` create `local/tmp` before
that step on every call, with a plain `mkdir -p` under the usual umask, so `local/` came out 0755.
Every writer into `local/` now makes it 0700, and `.config` is written 0600.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "packages" / "agami-core" / "src"))

import agami_paths as P  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX modes")


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_ensure_local_dir_creates_it_private(tmp_path, loose_umask):
    local = P.ensure_local_dir(tmp_path / "art")
    assert local == tmp_path / "art" / "local"
    assert _mode(local) == 0o700


def test_ensure_local_dir_tightens_an_existing_one(tmp_path, loose_umask):
    (tmp_path / "local").mkdir(mode=0o755)
    os.chmod(tmp_path / "local", 0o755)
    P.ensure_local_dir(tmp_path)
    assert _mode(tmp_path / "local") == 0o700


def test_ensure_local_dir_leaves_a_linked_local_alone(tmp_path, loose_umask):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o755)
    art = tmp_path / "art"
    art.mkdir()
    (art / "local").symlink_to(elsewhere)
    P.ensure_local_dir(art)
    assert _mode(elsewhere) == 0o755


def test_bootstrap_tightens_but_never_creates_local(tmp_path, monkeypatch, loose_umask):
    monkeypatch.setattr(P, "LEGACY_HOME", tmp_path / "no-legacy")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "fresh"))
    P.bootstrap()
    assert not (tmp_path / "fresh" / "local").exists()
    art = tmp_path / "old"
    (art / "local").mkdir(parents=True)
    os.chmod(art / "local", 0o755)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    P.bootstrap()
    assert _mode(art / "local") == 0o700


def _scratch_dir(art: Path) -> str:
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art)}
    out = subprocess.run(
        ["bash", "-c", f'umask 022; . "{SCRIPTS / "_python.sh"}"; agami_scratch_dir'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_the_launchers_scratch_folder_is_private(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    assert _scratch_dir(art) == str(art / "local" / "tmp")
    assert _mode(art / "local") == 0o700
    assert _mode(art / "local" / "tmp") == 0o700


def test_the_launchers_tighten_an_existing_local(tmp_path):
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    os.chmod(art / "local", 0o755)
    _scratch_dir(art)
    assert _mode(art / "local") == 0o700


def test_the_launchers_make_nothing_before_a_folder_is_chosen(tmp_path):
    _scratch_dir(tmp_path / "not-yet")
    assert not (tmp_path / "not-yet").exists()


def test_build_settings_writes_config_0600_in_a_private_local(tmp_path, loose_umask):
    from semantic_model import build_settings as BS

    path = BS.set_personal_data(tmp_path, BS.PERSONAL_DATA_ENFORCED)
    assert _mode(path) == 0o600
    assert _mode(tmp_path / "local") == 0o700
    os.chmod(path, 0o644)  # an older file keeps its mode through O_CREAT unless re-set
    BS.set_personal_data(tmp_path, BS.PERSONAL_DATA_DEFAULT)
    assert _mode(path) == 0o600


def test_the_sample_builder_makes_local_private(tmp_path, loose_umask):
    sys.path.insert(0, str(REPO / "plugins" / "agami" / "samples" / "store"))
    import build_sample

    out = tmp_path / "art" / "local" / "samples" / "store.db"
    (tmp_path / "art").mkdir()
    build_sample.build(out, prefer_cli=False)
    assert out.is_file()
    assert _mode(tmp_path / "art" / "local") == 0o700


# #698 review: a `local/` another user owns, or one chmod cannot tighten, was used anyway, so
# credentials and `.config` went into a folder someone else can change. On POSIX it is refused now,
# in both copies of agami_paths and in the launchers, before anything is written there.


def _plugin_copy():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "plugin_agami_paths", REPO / "plugins" / "agami" / "lib" / "agami_paths.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["package", "plugin"])
def paths_mod(request):
    return P if request.param == "package" else _plugin_copy()


def _chmod_fails(monkeypatch):
    def refuse(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", refuse)


def test_ensure_local_dir_refuses_a_local_it_cannot_make_private(tmp_path, monkeypatch, paths_mod):
    (tmp_path / "local").mkdir(mode=0o755)
    _chmod_fails(monkeypatch)
    with pytest.raises(paths_mod.NotPrivate, match="0700"):
        paths_mod.ensure_local_dir(tmp_path)


def test_ensure_local_dir_refuses_a_local_another_user_owns(tmp_path, monkeypatch, paths_mod):
    (tmp_path / "local").mkdir(mode=0o700)
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    with pytest.raises(paths_mod.NotPrivate, match="another user"):
        paths_mod.ensure_local_dir(tmp_path)


def test_bootstrap_does_not_stop_on_a_local_it_cannot_make_private(tmp_path, monkeypatch):
    # bootstrap only tightens and writes nothing; the writers below refuse for themselves
    monkeypatch.setattr(P, "LEGACY_HOME", tmp_path / "no-legacy")
    (tmp_path / "art" / "local").mkdir(parents=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))
    _chmod_fails(monkeypatch)
    assert P.bootstrap() == (tmp_path / "art").resolve()


def test_no_config_is_written_into_a_local_it_cannot_make_private(tmp_path, monkeypatch):
    from semantic_model import build_settings as BS

    (tmp_path / "local").mkdir(mode=0o755)
    _chmod_fails(monkeypatch)
    with pytest.raises(P.NotPrivate):
        BS.set_personal_data(tmp_path, BS.PERSONAL_DATA_ENFORCED)
    assert not (tmp_path / "local" / ".config").exists()


def test_init_local_says_why_it_refused_a_foreign_local(tmp_path, monkeypatch, capsys):
    from semantic_model import cli

    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    assert cli.main(["init-local", str(art)]) == 2
    assert "another user" in json.loads(capsys.readouterr().out)["error"]
    assert not (art / "local" / "tmp").exists()


def _scratch_dir_with(art: Path, override: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art)}
    script = f'umask 022; . "{SCRIPTS / "_python.sh"}"; {override}; agami_scratch_dir'
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def test_the_launchers_refuse_a_local_they_cannot_make_private(tmp_path):
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    r = _scratch_dir_with(art, "chmod() { return 1; }")
    assert r.returncode != 0 and r.stdout == ""
    assert "0700" in r.stderr
    assert not (art / "local" / "tmp").exists()


def test_the_launchers_refuse_a_local_another_user_owns(tmp_path):
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    r = _scratch_dir_with(art, "_local_is_mine() { return 1; }")
    assert r.returncode != 0 and "another user" in r.stderr
    assert not (art / "local" / "tmp").exists()


def test_sm_stops_before_running_anything_in_a_local_it_cannot_make_private(tmp_path):
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "chmod").write_text("#!/bin/sh\nexit 1\n")
    os.chmod(shims / "chmod", 0o755)
    env = {
        **os.environ,
        "AGAMI_ARTIFACTS_DIR": str(art),
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
    }
    for script in ("sm", "py"):
        r = subprocess.run(
            ["bash", str(SCRIPTS / script), "validate"], env=env, capture_output=True, text=True
        )
        assert r.returncode == 2, (script, r.stderr)
        assert "0700" in r.stderr, script
