#!/usr/bin/env python3
"""
Snapshot the per-profile semantic model under `.snapshots/<model_version>/`.

Used by agami-connect Phase 3d after the validator promotes the staging YAMLs
to the canonical `<artifacts_dir>/<profile>/`. The snapshot is an immutable
copy (chmod 0o444 on every file) pinned by content hash; agami-query
reads the hash off the directory name at query time so old answers reproduce
against the exact model that produced them.

**Why this is Python instead of a bash chain.**

The original Phase 3d used:

    find ... | LC_ALL=C sort | xargs sha256sum | sha256sum | cut | head
    rsync -a --exclude '.snapshots' --exclude '.git' src/ dst/
    chmod -R a-w dst/

On Windows, `rsync` isn't on PATH by default. The rsync call failed silently
and the snapshot directory was empty or missing — a real bug reported by an
early adopter testing on Windows. Plus `find`, `xargs`, `sha256sum`, `chmod`
are all Unix utilities (Git Bash on Windows has them, plain cmd / PowerShell
doesn't), so the whole block was fragile cross-platform.

This script does the same work using stdlib only (`hashlib`, `pathlib`,
`shutil`, `os`). Works identically on macOS, Linux, and Windows.

Usage:

    python3 snapshot_model.py --profile-dir ~/agami-artifacts/<profile>

Exit: 0 on success, non-zero on error. Prints the 12-char model_version hash
on stdout (only the hash, nothing else) so the calling SKILL can capture it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path


# Filename / directory globs that aren't part of the model content and thus
# shouldn't be hashed or copied into the snapshot.
EXCLUDED_DIRS = {".snapshots", ".git"}

# Only these file extensions count as "model content" for hashing / copying.
# (curation_log.jsonl and corrections.jsonl are audit logs, not model
# definition — they don't go into the snapshot.)
INCLUDED_EXTS = {".yaml", ".yml", ".md"}


def _iter_model_files(profile_dir: Path) -> list[Path]:
    """Walk profile_dir, yielding files that are part of the model content.
    Sorted by relative path (LC_ALL=C-equivalent — sorted bytes-wise) so the
    hash is reproducible across platforms."""
    out: list[Path] = []
    for root, dirs, files in os.walk(profile_dir):
        # Filter excluded subdirs in place (os.walk respects mutation).
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() in INCLUDED_EXTS:
                out.append(p)
    # Sort by relative path bytes — matches `LC_ALL=C sort` for stable hashing.
    out.sort(key=lambda p: bytes(p.relative_to(profile_dir)))
    return out


def compute_model_version(profile_dir: Path) -> str:
    """SHA-256 of the concatenated SHA-256s of every model file, truncated to
    12 hex chars. Identical model content produces identical hashes (idempotent
    snapshots); any change to any included file produces a new hash."""
    rollup = hashlib.sha256()
    for p in _iter_model_files(profile_dir):
        # Each line: "<file sha256>  <relative path>\n" — same shape as
        # `sha256sum` so the rollup matches what the old bash chain produced
        # byte-for-byte (modulo path separators on Windows; see normalize below).
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        rel = p.relative_to(profile_dir).as_posix()  # forward slashes on Windows
        rollup.update(f"{h.hexdigest()}  {rel}\n".encode("utf-8"))
    return rollup.hexdigest()[:12]


def snapshot_to(profile_dir: Path, model_version: str) -> Path:
    """Copy the model into .snapshots/<model_version>/ and make every file
    read-only. Returns the snapshot directory path."""
    dst = profile_dir / ".snapshots" / model_version
    if dst.exists():
        # Idempotent — same content hash means same snapshot. Don't re-copy,
        # don't fail. The dir already has the right content.
        return dst

    # shutil.copytree respects ignore patterns so .snapshots/ and .git/ don't
    # recurse into themselves.
    def _ignore(src: str, names: list[str]) -> list[str]:
        return [n for n in names if n in EXCLUDED_DIRS]

    shutil.copytree(profile_dir, dst, ignore=_ignore)

    # Make every file read-only. Directories stay writable so the next
    # introspect's snapshot can land alongside (under .snapshots/<new-hash>/).
    _set_readonly(dst)
    return dst


def _set_readonly(root: Path) -> None:
    """Recursively chmod every file under `root` to 0o444 (read-only for
    owner/group/other). Skips directories so future snapshots can be added
    alongside. Works on Windows (Python's os.chmod sets the read-only attribute
    via the file mode on the NTFS file)."""
    for dirpath, _, files in os.walk(root):
        for fname in files:
            p = Path(dirpath) / fname
            try:
                os.chmod(p, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            except OSError as e:
                # Best-effort — Windows may refuse on some files. Don't fail
                # the whole snapshot for a chmod hiccup; the content is still
                # there, and the directory is the source of truth.
                sys.stderr.write(
                    f"warning: couldn't set read-only on {p}: {e}\n"
                )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--profile-dir",
        required=True,
        help="Absolute path to <artifacts_dir>/<profile>/ — the directory "
             "that contains index.yaml and the per-schema subdirs.",
    )
    args = p.parse_args()

    profile_dir = Path(os.path.expanduser(args.profile_dir)).resolve()
    if not profile_dir.is_dir():
        sys.stderr.write(f"error: profile dir not found: {profile_dir}\n")
        return 1
    if not (profile_dir / "index.yaml").exists():
        sys.stderr.write(
            f"error: {profile_dir}/index.yaml is missing — nothing to snapshot. "
            f"Run agami-connect to introspect first.\n"
        )
        return 1

    model_version = compute_model_version(profile_dir)
    if not model_version:
        sys.stderr.write("error: no model files found to hash\n")
        return 1

    snapshot_to(profile_dir, model_version)
    # Print only the hash so the SKILL can capture it directly:
    #     model_version=$(python3 snapshot_model.py --profile-dir ...)
    print(model_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
