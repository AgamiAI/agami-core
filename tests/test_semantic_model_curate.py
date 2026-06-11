"""Unit tests for semantic_model/curate.py — review queue, model tree, and the
apply (exclude/include/approve/reject) write path with validation gating."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate, loader  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _write_model(root: Path, *, git: bool = True) -> None:
    """A tiny two-table model with a metric + a relationship, written to disk."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "metrics").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "shop", "version": 1,
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
                    {"name": "customer_id", "type": "integer"},
                    {"name": "ssn", "type": "string", "sensitive": True}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "customers",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))
    (root / "subject_areas" / "sales" / "metrics" / "order_count.yaml").write_text(yaml.safe_dump({
        "name": "order_count", "calculation": "count of orders",
        "bindings": {"PostgreSQL": "COUNT(*)"}, "source_tables": ["orders"],
        "confidence": "proposed", "review_state": "unreviewed",
    }))
    (root / "subject_areas" / "sales" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "orders", "from_column": "customer_id",
                           "to_table": "customers", "to_column": "id",
                           "relationship": "many_to_one", "confidence": "inferred",
                           "review_state": "unreviewed"}],
    }))
    if git:
        subprocess.run(["git", "-C", str(root), "init", "-q"])
        subprocess.run(["git", "-C", str(root), "add", "-A"])
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "-m", "init"])


def test_review_queue_partitions_rule1_rule2(tmp_path):
    _write_model(tmp_path, git=False)
    org = loader.load_organization(tmp_path)
    q = curate.review_queue(org)
    assert q["counts"]["rule_1"] == 1  # the proposed metric
    assert q["counts"]["rule_2"] == 1  # the inferred relationship
    assert q["rule_1"][0]["kind"] == "metric"
    assert q["rule_2"][0]["kind"] == "relationship"


def test_model_tree_shows_columns_and_state(tmp_path):
    _write_model(tmp_path, git=False)
    org = loader.load_organization(tmp_path, include_rejected=True)
    tree = curate.model_tree(org)
    orders = next(t for t in tree["subject_areas"][0]["tables"] if t["table"] == "orders")
    assert orders["review_state"] == "approved"
    ssn = next(c for c in orders["columns"] if c["name"] == "ssn")
    assert ssn["sensitive"] is True


def test_exclude_table_hides_from_runtime(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table",
                                   "area": "sales", "name": "orders"}])
    assert res.validated and res.applied
    runtime = loader.load_organization(tmp_path)  # drops rejected
    assert not any(t.name == "orders" for sa in runtime.subject_areas for t in sa.tables_defined)
    # still visible with include_rejected (so the explorer can toggle it back)
    full = loader.load_organization(tmp_path, include_rejected=True)
    assert any(t.name == "orders" for sa in full.subject_areas for t in sa.tables_defined)


def test_include_restores(tmp_path):
    _write_model(tmp_path)
    curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "area": "sales", "name": "orders"}])
    curate.apply(tmp_path, [{"op": "include", "kind": "table", "area": "sales", "name": "orders"}])
    runtime = loader.load_organization(tmp_path)
    assert any(t.name == "orders" for sa in runtime.subject_areas for t in sa.tables_defined)


def test_exclude_column(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "area": "sales",
                                   "name": "orders", "column": "ssn"}])
    assert res.validated
    orders = loader.load_organization(tmp_path).subject_areas[0].defined_table("orders")
    assert not any(c.name == "ssn" for c in orders.columns)


def test_approve_metric_records_signoff(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "area": "sales",
                                   "name": "order_count", "at": "2026-06-09T00:00:00Z"}],
                       signer="reviewer@example.com", role="cto")
    assert res.validated
    org = loader.load_organization(tmp_path)
    mm = org.subject_areas[0].metrics[0]
    assert mm.review_state == "approved" and mm.signed_off_by == "reviewer@example.com"
    # no longer in the Rule 1 queue
    assert curate.review_queue(org)["counts"]["rule_1"] == 0


def test_approve_relationship(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "approve", "kind": "relationship", "area": "sales",
                                   "name": "orders->customers", "at": "2026-06-09T00:00:00Z"}],
                       signer="reviewer@example.com", role="cto")
    assert res.validated
    rel = loader.load_organization(tmp_path).subject_areas[0].relationships[0]
    assert rel.review_state == "approved" and rel.signed_off_by == "reviewer@example.com"


def test_edit_relationship_on_clause(tmp_path):
    _write_model(tmp_path)
    # the "approve with fix" flow: set on: to a CAST, drop the simple columns
    res = curate.apply(tmp_path, [
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "on", "value": "CAST(orders.customer_id AS TEXT) = customers.id"},
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "from_column", "value": None},
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "to_column", "value": None},
    ])
    assert res.validated, res.errors
    rel = loader.load_organization(tmp_path).subject_areas[0].relationships[0]
    assert rel.on and "CAST" in rel.on
