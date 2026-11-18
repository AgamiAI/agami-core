"""`<artifacts_dir>/local/` is 0700 whoever creates it, and an existing one is tightened.

The skill used to run `mkdir -m 700 -p <artifacts_dir>/local` and then say `.config` needed no chmod
because `local/` was "already private". It wasn't, always: `sm` and `py` create `local/tmp` before
that step on every call, with a plain `mkdir -p` under the usual umask, so `local/` came out 0755.
Every writer into `local/` now makes it 0700, and `.config` is written 0600.
"""

from __future__ import annotations

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
