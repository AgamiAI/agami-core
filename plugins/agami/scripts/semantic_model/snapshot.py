"""Model snapshots — the source of an answer's `model_version` pin.

An answer's trust receipt reports `model_version` = the newest directory name
under `<profile>/.snapshots/`, where the directory name IS a content hash of the
model (read by `mcp_server._model_version` and the agami-query skill's
`ls -t .snapshots/ | head -1`). Nothing used to WRITE that directory, so
`model_version` was `null` for every profile. This module is the writer: it is
called from the deterministic model-write chokepoints (introspect's
`build.write_tree` and every curation commit via `curate._git_commit`) so a
snapshot is stamped whenever the model changes — independent of git, identical
across the skill / MCP server / cron.

Design:
  * The directory NAME is a 12-char SHA-256 over the model's content (every file
    under the profile root except machine-state: .snapshots/, .introspect/,
    .legacy_backup/, .git/, curation_log.jsonl). So the hash changes iff the
    model changes, and writing is idempotent — an unchanged model re-uses its
    existing directory (we just refresh its mtime so it stays "newest").
  * Each snapshot dir holds a deterministic `manifest.json` (model hash + per-file
    hashes) — no timestamp in the content, so a committed snapshot never churns;
    recency comes from the directory mtime, which is what the readers use.
  * Best-effort: never raises into the write path; a snapshot failure must not
    break introspection or curation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

SNAPSHOT_DIR = ".snapshots"
KEEP = 20  # cap retained snapshots; prune oldest beyond this

# Machine-state that is NOT part of the model's identity.
_EXCLUDE_TOP = {SNAPSHOT_DIR, ".introspect", ".legacy_backup", ".git"}
_EXCLUDE_NAMES = {"curation_log.jsonl"}


def _model_files(root: Path) -> list[Path]:
    """Every model file under root (sorted), excluding machine-state."""
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in _EXCLUDE_TOP:
            continue
        if p.name in _EXCLUDE_NAMES:
            continue
        out.append(p)
    return out


def _file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def compute_model_hash(root: str | os.PathLike) -> str:
    """12-char content hash over the model tree (path + bytes of each file)."""
    root = Path(root)
    h = hashlib.sha256()
    for p in _model_files(root):
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


def _prune(snaps: Path, keep: int) -> None:
    dirs = sorted((p for p in snaps.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def write_snapshot(root: str | os.PathLike) -> str | None:
    """Stamp a snapshot of the current model state. Returns the hash (the
    `model_version`), or None if there's no model / on any error (best-effort)."""
    try:
        root = Path(root)
        if not (root / "org.yaml").exists():
            return None  # not a model root — nothing to snapshot
        digest = compute_model_hash(root)
        snaps = root / SNAPSHOT_DIR
        snaps.mkdir(exist_ok=True)
        d = snaps / digest
        if d.exists():
            os.utime(d, None)  # unchanged model → keep its dir, mark it newest
        else:
            d.mkdir()
            manifest = {
                "schema_version": 1,
                "model_hash": digest,
                "files": {str(p.relative_to(root)).replace(os.sep, "/"): _file_sha(p)
                          for p in _model_files(root)},
            }
            (d / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        _prune(snaps, KEEP)
        return digest
    except Exception:
        return None
