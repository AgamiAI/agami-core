"""Migration tests — run the onboard-then-migrate tool against the real local
profiles (finbud + main / Turning Pages) when present, plus a synthetic fixture
so the suite still exercises the decompositions where no profile is installed.

Profile-backed tests skip cleanly when ~/agami-artifacts/<profile> is absent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader as L  # noqa: E402
from semantic_model import migrate as M  # noqa: E402
from semantic_model import validator as V  # noqa: E402

ARTIFACTS = Path(os.environ.get("AGAMI_ARTIFACTS_DIR", Path.home() / "agami-artifacts"))


def _has(profile: str) -> bool:
    return (ARTIFACTS / profile / "index.yaml").exists()


# --- synthetic fixture: a tiny legacy profile written into tmp_path ---


def _write_legacy_fixture(tmp_path: Path) -> Path:
    import json
    import yaml

    prof = tmp_path / "demo"
    (prof / "PUBLIC").mkdir(parents=True)
    (prof / "index.yaml").write_text(yaml.safe_dump({
        "version": "0.1.1", "profile": "demo", "db_type": "postgres",
        "schemas": [{"name": "PUBLIC", "file": "PUBLIC/_schema.yaml", "table_count": 2,
                     "description": "Demo."}],
    }))
    trust = json.dumps({"agami": {"confidence": 0.65, "review_state": "approved",
                                  "signed_off_by": "x@y.com", "signed_off_at": "2026-01-01T00:00:00Z",
                                  "signed_off_role": "data_lead"}})
    (prof / "PUBLIC" / "_schema.yaml").write_text(yaml.safe_dump({
        "version": "0.1.1", "schema": "PUBLIC", "description": "demo",
        "tables": [
            {"name": "FACT", "file": "FACT.yaml", "description": "Fact rows. Do not use col_x for dates.",
             "primary_key": ["ID"], "estimated_row_count": 100},
            {"name": "DIM", "file": "DIM.yaml", "description": "Dimension.", "primary_key": ["ID"]},
        ],
        "relationships": [
            {"name": "fact_to_dim", "from": "FACT", "to": "DIM",
             "from_columns": ["ID"], "to_columns": ["ID"],
             "custom_extensions": [{"vendor_name": "COMMON", "data": trust}]},
        ],
    }))

    def _table(name, fields):
        coltrust = json.dumps({"agami": {"type": "integer", "review_state": "approved"}})
        return yaml.safe_dump({
            "version": "0.1.1",
            "semantic_model": [{"name": "demo", "datasets": [{
                "name": name, "source": f"PUBLIC.{name}", "primary_key": ["ID"],
                "description": f"{name} table.",
                "fields": [{"name": f, "description": "",
                            "custom_extensions": [{"vendor_name": "COMMON",
                                                   "data": json.dumps({"agami": {"type": "integer"}})}]}
                           for f in fields],
            }]}]})

    (prof / "PUBLIC" / "FACT.yaml").write_text(_table("FACT", ["ID", "col_x", "amount"]))
    (prof / "PUBLIC" / "DIM.yaml").write_text(_table("DIM", ["ID", "label"]))
    (prof / "examples.yaml").write_text(yaml.safe_dump({
        "examples": [{"question": "how many facts?", "sql": "SELECT COUNT(*) FROM FACT"}]}))
    return tmp_path


def test_migrate_fixture_produces_valid_model(tmp_path):
    art = _write_legacy_fixture(tmp_path)
    rep = M.migrate_profile("demo", art, dry_run=False)
    assert not rep.validator_errors
    org = L.load_organization(Path(rep.out_dir))
    assert V.validate(org).ok
    # relationship carried the trust block + inferred one_to_one (both PK=ID)
    rel = org.subject_areas[0].relationships[0]
    assert rel.signed_off_by == "x@y.com" and rel.relationship == "one_to_one"
    assert rel.confidence == "inferred"  # 0.65 -> inferred
    assert rel.migrated_from is not None


def test_migrate_fixture_decomposes_description(tmp_path):
    art = _write_legacy_fixture(tmp_path)
    rep = M.migrate_profile("demo", art, dry_run=False)
    org = L.load_organization(Path(rep.out_dir))
    fact = org.subject_areas[0].defined_table("FACT")
    # "Do not use col_x for dates." should land as a caveat, not the description
    assert any("col_x" in c for c in fact.caveats)


def test_migrate_fixture_examples_migrated_as_proposed(tmp_path):
    art = _write_legacy_fixture(tmp_path)
    rep = M.migrate_profile("demo", art, dry_run=False)
    assert rep.examples_migrated == 1
    exs = L.list_prompt_examples(Path(rep.out_dir), org_area_name(rep))
    assert exs and exs[0]["status"] == "proposed"


def org_area_name(rep):
    return rep.subject_areas[0]


def test_migrate_dry_run_writes_nothing(tmp_path):
    art = _write_legacy_fixture(tmp_path)
    rep = M.migrate_profile("demo", art, dry_run=True)
    assert rep.files_written  # would-write list is populated
    assert not (Path(rep.out_dir) / "org.yaml").exists()  # but nothing on disk


def test_migrate_idempotent(tmp_path):
    art = _write_legacy_fixture(tmp_path)
    rep1 = M.migrate_profile("demo", art, dry_run=False)
    out = Path(rep1.out_dir)
    first = {p: p.read_text() for p in out.rglob("*.yaml")}
    M.migrate_profile("demo", art, dry_run=False)
    second = {p: p.read_text() for p in out.rglob("*.yaml")}
    assert first == second  # byte-identical YAML on re-run


# --- real FinBud (deep tables + sensitive) ---


@pytest.mark.skipif(not _has("finbud"), reason="finbud profile not installed locally")
def test_finbud_migration(tmp_path):
    out = tmp_path / "finbud_v2"
    rep = M.migrate_profile("finbud", ARTIFACTS, out_dir=out, dry_run=False)
    assert not rep.validator_errors
    org = L.load_organization(out)
    assert V.validate(org).ok
    sa = org.subject_areas[0]
    # deep tables got column_groups (no orphans, validator-enforced)
    ld = sa.defined_table("LOAN_DETAILS")
    assert len(ld.columns) >= 100 and ld.column_groups
    # PII columns flagged sensitive
    pii = sa.defined_table("PII")
    assert any(c.sensitive for c in pii.columns)
    assert rep.sensitive_columns > 0


# --- real Turning Pages / main (subject-area split + cross-area edges) ---


@pytest.mark.skipif(not _has("main"), reason="main profile not installed locally")
def test_main_migration_splits_and_emits_cross_area(tmp_path):
    out = tmp_path / "main_v2"
    rep = M.migrate_profile("main", ARTIFACTS, out_dir=out, dry_run=False)
    assert not rep.validator_errors
    org = L.load_organization(out)
    assert V.validate(org).ok
    # 33 tables > ceiling -> must split into multiple areas
    assert len(org.subject_areas) > 1
    # cross-area relationships emitted for inter-area joins, carrying cardinality + executable
    assert org.cross_subject_area_relationships
    edge = org.cross_subject_area_relationships[0]
    assert edge.from_subject_area and edge.to_subject_area
    assert edge.relationship in ("many_to_one", "one_to_many", "one_to_one")
    assert edge.executable == "same_engine"
    # every table defined exactly once across the org
    names = [t.name for a in org.subject_areas for t in a.tables_defined]
    assert len(names) == len(set(names))
