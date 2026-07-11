"""ACE-046 — incremental curation validation + snapshot caching, all behaviour-preserving.

Slice 1 here: `_all_tables(sa)` is built once per area (not once per relationship), and a snapshot
write reads each model file once (not twice), with a byte-identical hash + manifest.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import snapshot as S  # noqa: E402
from semantic_model import validator as v  # noqa: E402


# --------------------------------------------------------------------------- validator memoize


def _col(name, type="integer"):
    return m.Column(name=name, type=type)


def _area_with_n_rels(name: str, n: int) -> m.SubjectArea:
    """One area, two tables, and `n` identical compatible relationships between them — so the
    per-relationship FK-type check runs n times but must consult a single shared table index."""
    a = m.Table(name="a", schema="s", storage_connection="c", grain=["x"], description="d",
                columns=[_col("x")])
    b = m.Table(name="b", schema="s", storage_connection="c", grain=["y"], description="d",
                columns=[_col("y")])
    rels = [m.Relationship(from_table="a", to_table="b", from_column="x", to_column="y",
                           relationship="many_to_one", confidence="inferred") for _ in range(n)]
    refs = [m.TableRef(storage_connection="c", schema="s", table="a"),
            m.TableRef(storage_connection="c", schema="s", table="b")]
    return m.SubjectArea(name=name, tables=refs, tables_defined=[a, b], relationships=rels)


def _org(*areas) -> m.Organization:
    return m.Organization(organization="O",
                          storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
                          subject_areas=list(areas))


def test_all_tables_built_once_per_area_not_per_relationship(monkeypatch):
    # 5 relationships in one area would rebuild the table index 5× before ACE-046; now it's built
    # once per area and shared. Count calls to _all_tables and assert it tracks area count, not R.
    calls = {"n": 0}
    real = v._all_tables

    def counting(sa):
        calls["n"] += 1
        return real(sa)

    monkeypatch.setattr(v, "_all_tables", counting)

    v.validate(_org(_area_with_n_rels("one", 5)))
    assert calls["n"] == 1  # one area → one build, regardless of the 5 relationships

    calls["n"] = 0
    v.validate(_org(_area_with_n_rels("one", 5), _area_with_n_rels("two", 3)))
    assert calls["n"] == 2  # two areas → exactly two builds


def test_memoized_validation_verdict_is_unchanged():
    # Compatible FK types on every relationship → no fk_type findings; incompatible → a finding.
    ok = v.validate(_org(_area_with_n_rels("one", 4)))
    assert "fk_type_mismatch" not in {f.code for f in ok.findings}

    bad_area = _area_with_n_rels("one", 1)
    bad_area.tables_defined[1].columns = [_col("y", "string")]  # to_column now a type mismatch
    bad = v.validate(_org(bad_area))
    assert "fk_type_mismatch" in {f.code for f in bad.findings}


# --------------------------------------------------------------------------- single-pass snapshot


def _mini_model(root: Path) -> None:
    (root / "subject_areas" / "a").mkdir(parents=True, exist_ok=True)
    (root / "org.yaml").write_text("organization: t\nversion: 1\n", encoding="utf-8")
    (root / "subject_areas" / "a" / "subject_area.yaml").write_text("name: a\n", encoding="utf-8")
    (root / "subject_areas" / "a" / "note.yaml").write_text("k: v\n", encoding="utf-8")


def _expected_hash_and_manifest(root: Path) -> tuple[str, dict[str, str]]:
    """Independently recompute the rolling model hash + per-file shas, to prove the folded
    single-pass computation is byte-identical to computing them separately."""
    h = hashlib.sha256()
    manifest: dict[str, str] = {}
    for p in S._model_files(root):
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        data = p.read_bytes()
        h.update(rel.encode("utf-8")); h.update(b"\0"); h.update(data); h.update(b"\0")
        manifest[rel] = hashlib.sha256(data).hexdigest()
    return h.hexdigest()[:12], manifest


def test_snapshot_reads_each_file_once(tmp_path, monkeypatch):
    r = tmp_path / "m"
    _mini_model(r)
    model_files = {str(p.resolve()) for p in S._model_files(r)}

    reads: dict[str, int] = {}
    real_read = Path.read_bytes

    def counting_read(self):
        key = str(self.resolve())
        if key in model_files:
            reads[key] = reads.get(key, 0) + 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    S.write_snapshot(r)  # a NEW snapshot — the case that used to read the whole tree twice

    assert reads and all(n == 1 for n in reads.values())  # each model file read exactly once
    assert set(reads) == model_files  # and every model file was read


def test_snapshot_hash_and_manifest_are_byte_identical(tmp_path):
    import json

    r = tmp_path / "m"
    _mini_model(r)
    exp_hash, exp_manifest = _expected_hash_and_manifest(r)

    digest = S.write_snapshot(r)
    assert digest == exp_hash  # dir name == independently-computed rolling hash

    manifest = json.loads((r / ".snapshots" / digest / "manifest.json").read_text())
    assert manifest["model_hash"] == exp_hash
    assert manifest["files"] == exp_manifest  # per-file shas byte-identical
