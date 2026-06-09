"""Tests for plugins/agami/scripts/render_model_explorer.py.

build_manifest now reads the semantic model (subject areas → the explorer's
top-level groups, tables → tables, columns → fields), with include_rejected=True
so excluded entries show (toggleable in the UI). These tests build a small model
on disk and assert the manifest + render output.

(The OSI-era apply_model_exclusions.py tests were retired — the model explorer now
applies exclude/include via semantic_model.curate; see test_semantic_model_curate.py.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_model_explorer import build_manifest, render  # noqa: E402


@pytest.fixture
def profile_dir(tmp_path):
    """A small model: area 'sales' with two tables; customers excluded, and
    orders.customer_id excluded."""
    root = tmp_path / "test"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "metrics").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "test", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"},
                   {"storage_connection": "c", "schema": "public", "table": "customers"}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer", "review_state": "rejected"},
                    {"name": "total", "type": "decimal"}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "customers", "review_state": "rejected",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "name", "type": "string"}],
    }))
    (root / "subject_areas" / "sales" / "metrics" / "order_count.yaml").write_text(yaml.safe_dump({
        "name": "order_count", "calculation": "count of orders",
        "bindings": {"PostgreSQL": "COUNT(*)"}, "source_tables": ["orders"]}))
    return root


def test_manifest_captures_areas_tables_fields(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["schemas"] == 1
    assert m["totals"]["tables"] == 2
    assert m["totals"]["fields"] == 5  # 3 in orders + 2 in customers


def test_manifest_excluded_table_flagged(profile_dir):
    m = build_manifest(profile_dir, "test")
    customers = next(t for t in m["schemas"][0]["tables"] if t["name"] == "customers")
    assert customers["excluded"] is True and customers["review_state"] == "rejected"


def test_manifest_excluded_column_flagged(profile_dir):
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    cid = next(f for f in orders["fields"] if f["name"] == "customer_id")
    assert cid["excluded"] is True and cid["review_state"] == "rejected"
    for f in orders["fields"]:
        if f["name"] != "customer_id":
            assert f["excluded"] is False


def test_manifest_totals_excluded_counts(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["excluded_tables"] == 1
    assert m["totals"]["excluded_fields"] == 1


def test_manifest_qnames_use_area(profile_dir):
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    assert orders["qname"] == "sales.orders"
    cid = next(f for f in orders["fields"] if f["name"] == "customer_id")
    assert cid["qname"] == "sales.orders.customer_id"


def test_manifest_collects_metrics(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["metrics"] == 1
    assert m["metrics"][0]["name"] == "order_count"


def test_render_embeds_manifest_and_substitutes(profile_dir):
    import re
    m = build_manifest(profile_dir, "test")
    html = render(title="Model explorer · test", profile="test", manifest=m)
    assert not re.search(r"\{\{[A-Z_]+\}\}", html)  # all placeholders substituted
    assert "orders" in html and "order_count" in html


def test_sensitive_column_surfaced(profile_dir):
    p = profile_dir / "subject_areas" / "sales" / "tables" / "orders.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["columns"].append({"name": "ssn", "type": "string", "sensitive": True})
    p.write_text(yaml.safe_dump(doc))
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    ssn = next(f for f in orders["fields"] if f["name"] == "ssn")
    assert ssn["sensitive"] is True
