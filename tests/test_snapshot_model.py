"""
Tests for plugins/agami/scripts/snapshot_model.py.

The script replaced a bash chain (find | sha256sum | rsync | chmod -R) that
silently failed on Windows because rsync wasn't on PATH. These tests pin the
contract so the regression can't sneak back:

  - Hash is deterministic across runs (content-addressed)
  - Hash changes when any model file changes
  - Hash is stable across Linux / Windows path separators (forward-slash
    normalization in the rollup)
  - .snapshots/ and .git/ are excluded from both the hash and the copy
  - Snapshot directory contains every model file with the right content
  - Snapshot files are read-only after creation
  - Re-snapshotting the same content is idempotent (no error on existing dir)
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from snapshot_model import (  # noqa: E402
    compute_model_version,
    snapshot_to,
    _iter_model_files,
)


def _build_minimal_profile(root: Path) -> Path:
    """Make a small artifacts_dir/<profile>/ tree on disk so we don't have to
    invent every test fixture inline."""
    pdir = root / "test_profile"
    pdir.mkdir()
    (pdir / "index.yaml").write_text(
        "version: 0.1.1\nprofile: test\ndb_type: postgres\nschemas: []\n"
    )
    (pdir / "ORGANIZATION.md").write_text("# About this database\n")
    (pdir / "PUBLIC").mkdir()
    (pdir / "PUBLIC" / "_schema.yaml").write_text(
        "version: 0.1.1\nschema: PUBLIC\ntables: []\n"
    )
    (pdir / "PUBLIC" / "orders.yaml").write_text(
        "version: 0.1.1\nsemantic_model:\n  - name: t\n    datasets: []\n"
    )
    return pdir


def test_hash_is_deterministic(tmp_path):
    pdir = _build_minimal_profile(tmp_path)
    h1 = compute_model_version(pdir)
    h2 = compute_model_version(pdir)
    assert h1 == h2
    assert len(h1) == 12, f"expected 12-char hash, got {h1!r}"


def test_hash_changes_when_a_file_changes(tmp_path):
    pdir = _build_minimal_profile(tmp_path)
    h1 = compute_model_version(pdir)

    # Edit one yaml — hash should change.
    (pdir / "PUBLIC" / "orders.yaml").write_text(
        "version: 0.1.1\nsemantic_model:\n  - name: t2\n    datasets: []\n"
    )
    h2 = compute_model_version(pdir)
    assert h1 != h2


def test_hash_changes_when_a_file_is_added(tmp_path):
    pdir = _build_minimal_profile(tmp_path)
    h1 = compute_model_version(pdir)

    # Add another table yaml.
    (pdir / "PUBLIC" / "customers.yaml").write_text(
        "version: 0.1.1\nsemantic_model:\n  - name: c\n    datasets: []\n"
    )
    h2 = compute_model_version(pdir)
    assert h1 != h2


def test_excluded_dirs_dont_affect_hash(tmp_path):
    """.snapshots/ and .git/ contents are excluded from the model-content
    hash. Adding files in either dir must not change the version."""
    pdir = _build_minimal_profile(tmp_path)
    h1 = compute_model_version(pdir)

    # Add files inside excluded dirs.
    (pdir / ".snapshots").mkdir()
    (pdir / ".snapshots" / "stale.yaml").write_text("# old snapshot\n")
    (pdir / ".git").mkdir()
    (pdir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    h2 = compute_model_version(pdir)
    assert h1 == h2, "excluded dirs should not affect the hash"


def test_only_yaml_yml_md_files_are_hashed(tmp_path):
    """Audit logs (curation_log.jsonl, corrections.jsonl) are NOT model
    content. They must be excluded from the hash so adding a curation event
    doesn't break snapshot idempotency."""
    pdir = _build_minimal_profile(tmp_path)
    h1 = compute_model_version(pdir)

    (pdir / "curation_log.jsonl").write_text('{"action":"approve"}\n')
    (pdir / "corrections.jsonl").write_text('{"who":"x"}\n')
    (pdir / "random.txt").write_text("ignored")

    h2 = compute_model_version(pdir)
    assert h1 == h2


def test_iter_model_files_is_sorted_by_relative_path(tmp_path):
    """The rollup hash relies on files being sorted by relative path bytes
    (LC_ALL=C-equivalent) so it's reproducible across platforms."""
    pdir = _build_minimal_profile(tmp_path)
    (pdir / "PUBLIC" / "z_last.yaml").write_text("a:\n")
    (pdir / "PUBLIC" / "a_first.yaml").write_text("b:\n")
    files = _iter_model_files(pdir)
    rel_paths = [p.relative_to(pdir).as_posix() for p in files]
    assert rel_paths == sorted(rel_paths), \
        f"files should be sorted by relative path: {rel_paths}"


def test_snapshot_creates_immutable_copy(tmp_path):
    pdir = _build_minimal_profile(tmp_path)
    version = compute_model_version(pdir)
    snap_dir = snapshot_to(pdir, version)

    assert snap_dir.is_dir()
    assert snap_dir == pdir / ".snapshots" / version

    # Every model file must be in the snapshot with matching content.
    src_index = (pdir / "index.yaml").read_text()
    dst_index = (snap_dir / "index.yaml").read_text()
    assert src_index == dst_index

    src_orders = (pdir / "PUBLIC" / "orders.yaml").read_text()
    dst_orders = (snap_dir / "PUBLIC" / "orders.yaml").read_text()
    assert src_orders == dst_orders


def test_snapshot_files_are_read_only(tmp_path):
    pdir = _build_minimal_profile(tmp_path)
    version = compute_model_version(pdir)
    snap_dir = snapshot_to(pdir, version)

    for fpath in (
        snap_dir / "index.yaml",
        snap_dir / "PUBLIC" / "orders.yaml",
    ):
        mode = stat.S_IMODE(os.stat(fpath).st_mode)
        # Owner-write bit must be off.
        assert not (mode & stat.S_IWUSR), \
            f"{fpath} should be read-only, got mode {oct(mode)}"


def test_snapshot_is_idempotent(tmp_path):
    """Running snapshot_to twice with the same version should not fail and
    should not re-copy."""
    pdir = _build_minimal_profile(tmp_path)
    version = compute_model_version(pdir)
    snap_dir1 = snapshot_to(pdir, version)
    snap_dir2 = snapshot_to(pdir, version)
    assert snap_dir1 == snap_dir2
    assert snap_dir1.is_dir()


def test_snapshot_excludes_snapshots_and_git_dirs(tmp_path):
    """When snapshotting, the .snapshots/ and .git/ dirs in the source must
    not be recursively copied into the new snapshot — otherwise each
    re-snapshot doubles the disk usage."""
    pdir = _build_minimal_profile(tmp_path)
    (pdir / ".snapshots").mkdir()
    (pdir / ".snapshots" / "old_hash").mkdir()
    (pdir / ".snapshots" / "old_hash" / "index.yaml").write_text("old\n")
    (pdir / ".git").mkdir()
    (pdir / ".git" / "config").write_text("[core]\n")

    version = compute_model_version(pdir)
    snap_dir = snapshot_to(pdir, version)

    # The snapshot should NOT have its own .snapshots or .git inside.
    assert not (snap_dir / ".snapshots").exists()
    assert not (snap_dir / ".git").exists()


# --- CLI behavior --------------------------------------------------------

SCRIPT_PATH = REPO_ROOT / "plugins" / "agami" / "scripts" / "snapshot_model.py"


def _run_cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
    )


def test_cli_prints_only_the_hash_on_stdout(tmp_path):
    pdir = _build_minimal_profile(tmp_path)
    result = _run_cli("--profile-dir", str(pdir))
    assert result.returncode == 0, result.stderr
    # stdout should be ONLY the 12-char hash + newline (no banner, no info)
    out = result.stdout.strip()
    assert len(out) == 12, f"expected 12-char hash, got {out!r}"
    assert all(c in "0123456789abcdef" for c in out)


def test_cli_fails_if_profile_dir_missing(tmp_path):
    result = _run_cli("--profile-dir", str(tmp_path / "nonexistent"))
    assert result.returncode != 0
    assert "not found" in result.stderr.lower()


def test_cli_fails_if_index_yaml_missing(tmp_path):
    # A directory that exists but has no index.yaml is not a model.
    empty = tmp_path / "empty_profile"
    empty.mkdir()
    result = _run_cli("--profile-dir", str(empty))
    assert result.returncode != 0
    assert "index.yaml" in result.stderr
