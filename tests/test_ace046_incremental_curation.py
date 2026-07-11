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


# ------------------------------------------------------------------ incremental validation (cache)


def _area(name: str, *, ftype: str = "integer", extra_rels: int = 0) -> m.SubjectArea:
    """An area with two uniquely-named tables (so table names don't collide across areas) and a
    baseline FK relationship; `ftype` drives the FK type-compat verdict, `extra_rels` grows the
    area's content without touching its tables."""
    ta, tb = f"{name}_a", f"{name}_b"
    a = m.Table(name=ta, schema="s", storage_connection="c", grain=["x"], description="d",
                columns=[m.Column(name="x", type=ftype)])
    b = m.Table(name=tb, schema="s", storage_connection="c", grain=["y"], description="d",
                columns=[m.Column(name="y", type="integer")])
    rels = [m.Relationship(from_table=ta, to_table=tb, from_column="x", to_column="y",
                           relationship="many_to_one", confidence="inferred")
            for _ in range(1 + extra_rels)]
    refs = [m.TableRef(storage_connection="c", schema="s", table=ta),
            m.TableRef(storage_connection="c", schema="s", table=tb)]
    return m.SubjectArea(name=name, tables=refs, tables_defined=[a, b], relationships=rels)


def _findings_tuple(res):
    return [(f.severity, f.code, f.message) for f in res.findings]


def test_cached_validate_is_identical_to_full_validate():
    # A 3-area model, one area carrying an FK type mismatch → a real finding. The cached path must
    # produce the exact same findings (same order, severity, code, message) as a full validate.
    org = _org(_area("alpha"), _area("beta", ftype="string"), _area("gamma"))
    full = v.validate(org)
    cached = v.validate(org, cache={})
    assert _findings_tuple(cached) == _findings_tuple(full)
    assert "fk_type_mismatch" in {f.code for f in cached.findings}


def test_only_the_changed_area_revalidates(monkeypatch):
    calls: list[str] = []
    real = v._validate_area

    def spy(sa, org, org_tables):
        calls.append(sa.name)
        return real(sa, org, org_tables)

    monkeypatch.setattr(v, "_validate_area", spy)

    cache: dict = {}
    org1 = _org(_area("alpha"), _area("beta"), _area("gamma"))
    v.validate(org1, cache=cache)
    assert calls == ["alpha", "beta", "gamma"]  # cold cache → all three run

    # Same alpha + gamma content, beta grows by one relationship (no table change).
    calls.clear()
    org2 = _org(_area("alpha"), _area("beta", extra_rels=1), _area("gamma"))
    res2 = v.validate(org2, cache=cache)
    assert calls == ["beta"]  # only the edited area re-runs; alpha + gamma are cache hits
    assert _findings_tuple(res2) == _findings_tuple(v.validate(org2))  # still correct


def test_table_change_forces_full_revalidate(monkeypatch):
    calls: list[str] = []
    real = v._validate_area
    monkeypatch.setattr(v, "_validate_area",
                        lambda sa, org, ot: (calls.append(sa.name), real(sa, org, ot))[1])

    cache: dict = {}
    org1 = _org(_area("alpha"), _area("beta"), _area("gamma"))
    v.validate(org1, cache=cache)
    calls.clear()

    # Add a column to alpha's table → the org-wide table registry changes → every area's key misses,
    # so all three re-run even though beta/gamma are untouched (conservative, verdict-preserving).
    org2 = _org(_area("alpha"), _area("beta"), _area("gamma"))
    org2.subject_areas[0].tables_defined[0].columns.append(m.Column(name="z", type="integer"))
    res2 = v.validate(org2, cache=cache)
    assert sorted(calls) == ["alpha", "beta", "gamma"]
    assert _findings_tuple(res2) == _findings_tuple(v.validate(org2))
