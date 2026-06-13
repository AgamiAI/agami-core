"""Tests for semantic_model/org_draft.py — the factual ORGANIZATION.md draft, and the
render_model_explorer fallback that uses it so the file is never blank."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _model(root):
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (root / "subject_areas" / "s" / "entities").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "acme", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales", "description": "orders & customers",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "one row per order",
        "performance_hints": {"estimated_row_count": 1234567},
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "unit": "INR"}]}))
    (root / "subject_areas" / "s" / "metrics" / "total_revenue.yaml").write_text(yaml.safe_dump({
        "name": "total_revenue", "calculation": "sum of order amounts", "unit": "INR",
        "other_names": ["revenue"], "confidence": "inferred", "review_state": "unreviewed"}))
    (root / "subject_areas" / "s" / "entities" / "customer.yaml").write_text(yaml.safe_dump({
        "name": "customer", "plural": "customers",
        "maps_to": [{"table": "orders", "column": "id", "primary": True}],
        "other_names": ["client"], "confidence": "inferred", "review_state": "unreviewed"}))


def test_draft_is_a_summary_not_a_full_listing(tmp_path):
    from semantic_model.loader import load_organization
    from semantic_model import org_draft
    _model(tmp_path)
    md = org_draft.draft_organization_md(load_organization(tmp_path))
    # SUMMARY shape: org + counts + subject areas + conventions + glossary prompt
    assert "# About this database" in md
    assert "acme" in md and "1 table across 1 subject area" in md
    assert "## Subject areas" in md and "sales" in md and "orders & customers" in md
    assert "1 metric and 1 entity are defined" in md and "model explorer" in md
    assert "INR" in md                      # conventions summary (distinct units)
    assert "## Key terminology" in md
    # it must NOT rehash the structured model
    assert "## What the data contains" not in md
    assert "## Metrics" not in md and "## Entities" not in md
    assert "total_revenue" not in md        # metric is counted, not enumerated
    assert "one row per order" not in md     # per-table description not dumped
    assert "1,234,567" not in md             # per-table row count not dumped


def test_key_terminology_seeded_from_glossary_and_enums(tmp_path):
    # the section is no longer a bare prompt: curated glossary terms + auto-derived enum
    # legends from choice_field columns both render.
    from semantic_model.loader import load_organization
    from semantic_model import org_draft, curate
    _model(tmp_path)
    p = tmp_path / "subject_areas" / "s" / "tables" / "orders.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["columns"].append({"name": "status", "type": "string",
                           "choice_field": {"P": "pending", "S": "shipped"}})
    p.write_text(yaml.safe_dump(doc))
    res = curate.set_key_terminology(tmp_path, {"TIU": "Telematics Interface Unit", "bp": "battery pack"})
    assert res.validated and res.applied
    md = org_draft.draft_organization_md(load_organization(tmp_path))
    assert "**TIU** — Telematics Interface Unit" in md
    assert "**bp** — battery pack" in md
    assert "orders.status" in md and "`P` = pending" in md   # auto enum legend
    assert "only you can fill this in" not in md             # bare placeholder is gone


def test_set_key_terminology_merges_then_replaces(tmp_path):
    from semantic_model.loader import load_organization
    from semantic_model import curate
    _model(tmp_path)
    assert curate.set_key_terminology(tmp_path, {"TIU": "Telematics Interface Unit"}).validated
    curate.set_key_terminology(tmp_path, {"ESV": "Energy Storage Vehicle"})            # merge (default)
    assert load_organization(tmp_path).key_terminology == {
        "TIU": "Telematics Interface Unit", "ESV": "Energy Storage Vehicle"}
    curate.set_key_terminology(tmp_path, {"SoC": "State of Charge"}, merge=False)      # replace
    assert load_organization(tmp_path).key_terminology == {"SoC": "State of Charge"}


def test_explorer_falls_back_to_draft_when_org_md_blank(tmp_path):
    from render_model_explorer import build_manifest
    _model(tmp_path)
    # no ORGANIZATION.md at all
    m = build_manifest(tmp_path, "acme")
    assert "About this database" in m["organization_md"] and "Subject areas" in m["organization_md"]
    # a comments-only file is still "blank" → draft
    (tmp_path / "ORGANIZATION.md").write_text("<!-- nothing here yet -->\n")
    m2 = build_manifest(tmp_path, "acme")
    assert "Subject areas" in m2["organization_md"]
    # a real file is left as-is
    (tmp_path / "ORGANIZATION.md").write_text("# About\nWe are a lending startup.")
    m3 = build_manifest(tmp_path, "acme")
    assert m3["organization_md"] == "# About\nWe are a lending startup."
