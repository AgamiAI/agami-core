"""A curate op on a join, entity or metric that lives between subject areas resolves without an
`area`, and an op that can't be pinned to one entry fails the whole batch instead of being skipped
(#538)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import cli, curate, loader  # noqa: E402

_JOIN = {"from_table": "invoices", "from_column": "customer_id", "to_table": "customers",
         "to_column": "id", "from_subject_area": "billing", "to_subject_area": "crm",
         "from_schema": "billing", "to_schema": "crm", "relationship": "many_to_one",
         "confidence": "inferred", "review_state": "unreviewed"}
_SIGN = {"signer": "you@example.com", "role": "owner"}


def _model(root: Path, *, cross: list[dict], sidecar: bool = False, ds_extra: dict | None = None) -> None:
    """Two areas, `billing` (invoices) and `crm` (customers), and the cross-area joins given — on
    datasource.yaml, or in cross_subject_area_relationships.yaml when `sidecar`."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    for area, tbl in [("billing", "invoices"), ("crm", "customers")]:
        (root / "subject_areas" / area / "tables").mkdir(parents=True)
        (root / "subject_areas" / area / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": area, "tables": [{"storage_connection": "c", "schema": area, "table": tbl}]}))
        (root / "subject_areas" / area / "tables" / f"{tbl}.yaml").write_text(yaml.safe_dump({
            "name": tbl, "schema": area, "storage_connection": "c", "grain": ["id"], "description": tbl,
            "columns": [{"name": "id", "type": "integer", "primary_key": True},
                        {"name": "customer_id", "type": "integer"},
                        {"name": "payer_id", "type": "integer"}]}))
    ds = {"datasource": "acme", "version": 1,
          "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
          "subject_areas": ["subject_areas/billing", "subject_areas/crm"], **(ds_extra or {})}
    if sidecar:
        (root / "cross_subject_area_relationships.yaml").write_text(yaml.safe_dump({"edges": cross}))
    else:
        ds["cross_subject_area_relationships"] = cross
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))


def _cross_joins(root: Path) -> list:
    return loader.load_datasource(root, include_rejected=True).cross_subject_area_relationships


def _snapshot(root: Path) -> dict:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("sidecar", [False, True])
@pytest.mark.parametrize("action,expect", [
    ("reject", ("review_state", "rejected")),
    ("approve", ("review_state", "approved")),
    ("edit", ("description", "invoice to its account")),
])
def test_a_cross_area_join_without_an_area_applies(tmp_path, sidecar, action, expect):
    _model(tmp_path, cross=[_JOIN], sidecar=sidecar)
    op = {"op": action, "kind": "relationship", "name": "invoices->customers",
          "at": "2026-01-01T00:00:00Z"}
    if action == "edit":
        op.update(field="description", value="invoice to its account")
    res = curate.apply(tmp_path, [op], **_SIGN)
    assert res.validated and res.applied and not res.skipped and not res.errors, res.as_dict()
    fld, val = expect
    assert getattr(_cross_joins(tmp_path)[0], fld) == val
    logged = [json.loads(ln) for ln in (tmp_path / "curation_log.jsonl").read_text().splitlines()]
    assert logged[-1]["op"] == action and logged[-1]["name"] == "invoices->customers"


def test_a_rejected_cross_area_join_is_no_longer_served(tmp_path):
    _model(tmp_path, cross=[_JOIN])
    res = curate.apply(tmp_path, [{"op": "reject", "kind": "relationship", "name": "invoices->customers"}])
    assert res.applied, res.as_dict()
    assert loader.load_datasource(tmp_path).cross_subject_area_relationships == []


def test_two_matching_joins_fail_the_batch_and_write_nothing(tmp_path):
    second = {**_JOIN, "from_column": "payer_id"}
    _model(tmp_path, cross=[_JOIN, second])
    before = _snapshot(tmp_path)
    ops = [{"op": "edit", "kind": "table", "area": "billing", "name": "invoices",
            "field": "description", "value": "changed"},
           {"op": "reject", "kind": "relationship", "name": "invoices->customers"}]
    res = curate.apply(tmp_path, ops)
    assert not res.applied and not res.validated
    assert len(res.errors) == 1, res.errors
    msg = res.errors[0]
    assert "reject relationship" in msg and "2 joins match invoices->customers" in msg
    assert "invoices.customer_id -> customers.id" in msg and "invoices.payer_id -> customers.id" in msg
    assert "cross-area billing->crm" in msg
    assert _snapshot(tmp_path) == before   # the table edit earlier in the batch was rolled back too


def test_naming_the_columns_settles_two_matching_joins(tmp_path):
    second = {**_JOIN, "from_column": "payer_id"}
    _model(tmp_path, cross=[_JOIN, second])
    res = curate.apply(tmp_path, [{"op": "reject", "kind": "relationship", "name": "invoices->customers",
                                   "from_column": "payer_id", "to_column": "id"}])
    assert res.applied and not res.errors, res.as_dict()
    states = {r.from_column: r.review_state for r in _cross_joins(tmp_path)}
    assert states == {"customer_id": "unreviewed", "payer_id": "rejected"}


def test_no_matching_join_fails_the_batch_clearly(tmp_path):
    _model(tmp_path, cross=[_JOIN])
    before = _snapshot(tmp_path)
    res = curate.apply(tmp_path, [{"op": "reject", "kind": "relationship", "name": "customers->invoices"}])
    assert not res.applied and not res.skipped
    assert "no join customers->invoices in any area or the cross-area joins" in res.errors[0]
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "curation_log.jsonl").exists()


@pytest.mark.parametrize("area", ["billing", "crm"])
def test_an_op_naming_an_area_still_applies(tmp_path, area):
    # Unchanged behaviour, kept as a guard: `billing` is how the explorer and approve-queue name it
    # (the from side).
    _model(tmp_path, cross=[_JOIN])
    res = curate.apply(tmp_path, [{"op": "reject", "kind": "relationship", "area": area,
                                   "name": "invoices->customers", "from_column": "customer_id",
                                   "to_column": "id"}])
    assert res.applied and not res.errors, res.as_dict()
    assert _cross_joins(tmp_path)[0].review_state == "rejected"


_CROSS_METRIC = {"name": "billed customers", "calculation": "customers with an invoice",
                 "bindings": {"PostgreSQL": "COUNT(DISTINCT invoices.customer_id)"},
                 "source_tables": ["invoices"], "confidence": "proposed", "review_state": "unreviewed"}
_CROSS_ENTITY = {"name": "account", "maps_to": [{"table": "customers", "column": "id"}]}


def test_a_cross_area_metric_and_entity_resolve_without_an_area(tmp_path):
    _model(tmp_path, cross=[_JOIN], ds_extra={"cross_subject_area_metrics": [_CROSS_METRIC]})
    (tmp_path / "cross_subject_area_entities.yaml").write_text(yaml.safe_dump({"entities": [_CROSS_ENTITY]}))
    res = curate.apply(tmp_path, [
        {"op": "approve", "kind": "metric", "name": "billed customers", "at": "2026-01-01T00:00:00Z"},
        {"op": "edit", "kind": "entity", "name": "account", "field": "description", "value": "a payer"},
    ], **_SIGN)
    assert res.validated and len(res.applied) == 2 and not res.errors, res.as_dict()
    org = loader.load_datasource(tmp_path, include_rejected=True)
    assert org.cross_subject_area_metrics[0].review_state == "approved"
    assert org.cross_subject_area_metrics[0].signed_off_by == "you@example.com"
    assert org.cross_subject_area_entities[0].description == "a payer"


def test_a_missing_metric_fails_the_batch(tmp_path):
    _model(tmp_path, cross=[_JOIN])
    res = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "name": "no such metric"}], **_SIGN)
    assert not res.applied and "no metric named 'no such metric'" in res.errors[0]


def test_a_metric_in_two_places_fails_the_batch(tmp_path):
    _model(tmp_path, cross=[_JOIN], ds_extra={"cross_subject_area_metrics": [_CROSS_METRIC]})
    (tmp_path / "subject_areas" / "billing" / "metrics").mkdir()
    (tmp_path / "subject_areas" / "billing" / "metrics" / "billed_customers.yaml").write_text(
        yaml.safe_dump(_CROSS_METRIC))
    before = _snapshot(tmp_path)
    res = curate.apply(tmp_path, [{"op": "reject", "kind": "metric", "name": "billed customers"}])
    assert not res.applied and "is in 2 places (billing, cross-area)" in res.errors[0]
    assert _snapshot(tmp_path) == before
    # naming the area picks its copy, as before
    res2 = curate.apply(tmp_path, [{"op": "reject", "kind": "metric", "area": "billing",
                                    "name": "billed customers"}])
    assert res2.applied and not res2.errors, res2.as_dict()


def test_approve_queue_signs_off_a_cross_area_join_and_metric(tmp_path):
    _model(tmp_path, cross=[_JOIN], ds_extra={"cross_subject_area_metrics": [_CROSS_METRIC]})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["approve-queue", str(tmp_path), "--signer", "you@example.com", "--role", "owner"])
    out = json.loads(buf.getvalue())
    assert rc == 0 and out["validated"] and len(out["applied"]) == 2 and not out["skipped"], out
    org = loader.load_datasource(tmp_path)
    assert org.cross_subject_area_relationships[0].review_state == "approved"
    assert org.cross_subject_area_metrics[0].review_state == "approved"
    assert curate.review_queue(org)["counts"]["total"] == 0


def test_a_named_area_that_lacks_the_join_finds_it_in_another_area(tmp_path):
    _model(tmp_path, cross=[])
    (tmp_path / "subject_areas" / "billing" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "invoices", "from_column": "payer_id", "to_table": "invoices",
                           "to_column": "id", "relationship": "many_to_one",
                           "confidence": "inferred", "review_state": "unreviewed"}]}))
    res = curate.apply(tmp_path, [{"op": "reject", "kind": "relationship", "area": "crm",
                                   "name": "invoices->invoices"}])
    assert res.applied and not res.errors, res.as_dict()
    rel = loader.load_datasource(tmp_path, include_rejected=True).subject_area("billing").relationships[0]
    assert rel.review_state == "rejected"


def test_a_named_area_op_with_a_missing_target_is_still_a_skip(tmp_path):
    # As on main: naming an area keeps the per-op skip, and the rest of the batch applies.
    _model(tmp_path, cross=[_JOIN])
    res = curate.apply(tmp_path, [
        {"op": "approve", "kind": "metric", "area": "billing", "name": "no such metric"},
        {"op": "reject", "kind": "relationship", "area": "billing", "name": "customers->invoices"},
        {"op": "reject", "kind": "relationship", "name": "invoices->customers"},
    ])
    assert res.validated and not res.errors, res.as_dict()
    assert res.applied == ["reject relationship /invoices->customers"]
    assert [s["reason"] for s in res.skipped] == [
        "no metric named 'no such metric' in area 'billing'",
        "relationship customers->invoices not found in the area or the cross-area relationships"]
