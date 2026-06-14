"""Tests for agami_paths — the single source of truth for on-disk paths after the
consolidation (everything self-contained under the artifacts dir; no separate ~/.agami)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import agami_paths as P  # noqa: E402


def test_artifacts_dir_resolution_order(monkeypatch, tmp_path):
    # 1. env var wins
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "from_env"))
    assert P.artifacts_dir() == (tmp_path / "from_env").resolve()
    # 2. pointer file, when no env
    monkeypatch.delenv("AGAMI_ARTIFACTS_DIR", raising=False)
    monkeypatch.setattr(P, "POINTER_PATH", tmp_path / ".config" / "agami" / "path")
    P.set_artifacts_dir(tmp_path / "chosen")
    assert P.artifacts_dir() == (tmp_path / "chosen").resolve()
    # 3. default, when neither
    P.POINTER_PATH.unlink()
    monkeypatch.setattr(P, "DEFAULT_ARTIFACTS_DIR", tmp_path / "default")
    assert P.artifacts_dir() == (tmp_path / "default").resolve()


def test_local_dir_holds_secrets_and_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    assert P.local_dir() == tmp_path / "local"
    assert P.credentials_path() == tmp_path / "local" / "credentials"
    assert P.config_path() == tmp_path / "local" / ".config"
    assert P.query_log_path() == tmp_path / "local" / "query_log.jsonl"
    assert P.dashboard_dir("model", "main") == tmp_path / "local" / "model" / "main"
    # the committable model sits next to local/, not inside it
    assert P.profile_dir("main") == tmp_path / "main"


def test_ensure_gitignore_excludes_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    P.ensure_gitignore()
    gi = (tmp_path / ".gitignore").read_text()
    assert "local/" in gi.split()
    # idempotent — a second call doesn't duplicate the entry
    P.ensure_gitignore()
    assert (tmp_path / ".gitignore").read_text().split().count("local/") == 1


def test_migrate_legacy_home_moves_everything_once(monkeypatch, tmp_path):
    legacy = tmp_path / "dot_agami"
    (legacy / "charts" / "main").mkdir(parents=True)
    (legacy / "credentials").write_text("[main]\ntype=postgres\n")
    (legacy / "query_log.jsonl").write_text("{}\n")
    monkeypatch.setattr(P, "LEGACY_HOME", legacy)
    monkeypatch.setattr(P, "POINTER_PATH", tmp_path / ".config" / "agami" / "path")
    art = tmp_path / "artifacts"

    assert P.migrate_legacy_home(art) is True
    # contents moved into <artifacts>/local/, intact
    assert (art / "local" / "credentials").read_text().startswith("[main]")
    assert (art / "local" / "charts" / "main").is_dir()
    # gitignore written, pointer set, tombstone left behind
    assert "local/" in (art / ".gitignore").read_text().split()
    assert P.POINTER_PATH.read_text().strip() == str(art.resolve())
    assert (legacy / "MOVED.txt").exists()
    # idempotent — second call is a no-op (local/ already exists)
    assert P.migrate_legacy_home(art) is False
