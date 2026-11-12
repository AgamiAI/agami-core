"""#616: an approved metric or entity always carries a person's sign-off.

An approve after an include in the same batch, with no signer, left a metric or entity approved
with no stamp, and `sm add` wrote whatever trust block the caller's file carried. Every approve now
needs a signer and role, `sm add` ignores the file's sign-off, and the validator warns on an
approved metric or entity with no complete stamp.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_model import cli, curate  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import validator as V  # noqa: E402

from test_curate_cross_area_resolution import _CROSS_METRIC, _JOIN, _model  # noqa: E402
from test_curate_edit_goes_stale import _write_model  # noqa: E402

AT = "2026-10-27T10:00:00Z"
SIGN = {"signer": "you@example.com", "role": "owner"}
SAMPLE = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "samples" / "store" / "model"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "demo"
    _write_model(r)
    return r


def _ops(kind: str, name: str, first: str = "include") -> list[dict]:
    return [{"op": first, "kind": kind, "area": "sales", "name": name},
            {"op": "approve", "kind": kind, "area": "sales", "name": name, "at": AT}]


def _item(root: Path, kind: str, name: str) -> dict:
    return yaml.safe_load(_path(root, kind, name).read_text())


def _path(root: Path, kind: str, name: str) -> Path:
    return root / "subject_areas" / "sales" / {"metric": "metrics", "entity": "entities"}[kind] / f"{name}.yaml"


@pytest.mark.parametrize("kind, name", [("metric", "revenue"), ("entity", "customer"),
                                        ("relationship", "orders->customers")])
@pytest.mark.parametrize("first", ["include", "reject"])
def test_an_unsigned_approve_after_include_or_reject_fails_the_batch(root, kind, name, first):
    res = curate.apply(root, _ops(kind, name, first))
    assert not res.validated and not res.applied
    assert any("signer" in e for e in res.errors), res.errors


@pytest.mark.parametrize("signer, role", [("you@example.com", None), (None, "owner"),
                                          ("   ", "owner"), ("you@example.com", " "), ("", "")])
def test_an_approve_needs_both_signer_and_role(root, signer, role):
    res = curate.apply(root, _ops("metric", "revenue"), signer=signer, role=role)
    assert not res.applied and "signer" in res.errors[0]


@pytest.mark.parametrize("kind, name", [("metric", "revenue"), ("entity", "customer")])
def test_a_signed_approve_after_include_stamps_the_signer(root, kind, name):
    res = curate.apply(root, _ops(kind, name), **SIGN)
    assert res.validated, res.errors
    it = _item(root, kind, name)
    assert (it["review_state"], it["signed_off_by"], it["signed_off_role"], it["signed_off_at"]) == \
        ("approved", "you@example.com", "owner", AT)


def test_an_approve_without_a_time_is_stamped_now(root):
    ops = [{"op": "include", "kind": "metric", "area": "sales", "name": "revenue"},
           {"op": "approve", "kind": "metric", "area": "sales", "name": "revenue"}]
    res = curate.apply(root, ops, **SIGN)
    assert res.validated, res.errors
    assert _item(root, "metric", "revenue")["signed_off_at"]


def test_sm_curate_without_signer_refuses_the_batch(root, tmp_path, capsys):
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps(_ops("metric", "revenue")))
    assert cli.main(["curate", str(root), str(ops)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["validated"] is False and out["applied"] == []
    assert _item(root, "metric", "revenue")["review_state"] == "approved"   # untouched


def _metric(**extra) -> dict:
    return {"name": "order count", "calculation": "number of orders",
            "bindings": {"SQLite": "COUNT(orders.id)"}, "source_tables": ["orders"], **extra}


CLAIMED = {"review_state": "approved", "signed_off_by": "someone@example.com",
           "signed_off_role": "owner", "signed_off_at": "2026-01-01T00:00:00Z"}


def test_sm_add_writes_a_new_item_unreviewed_and_says_the_sign_off_was_ignored(root):
    res = curate.write_items(root, "sales", "metric", [_metric(**CLAIMED)])
    assert res.validated, res.errors
    it = _item(root, "metric", "order_count")
    assert it.get("review_state", "unreviewed") == "unreviewed"
    assert not any(k in it for k in ("signed_off_by", "signed_off_at", "signed_off_role"))
    assert len(res.notes) == 1 and "ignored review_state" in res.notes[0]


def test_sm_add_replacing_an_approved_item_leaves_it_stale(root):
    changed = {"name": "revenue", "calculation": "net order amount",
               "bindings": {"SQLite": "SUM(orders.amount) - SUM(orders.discount)"},
               "source_tables": ["orders"], **CLAIMED}
    res = curate.write_items(root, "sales", "metric", [changed])
    assert res.validated, res.errors
    it = _item(root, "metric", "revenue")
    # the stamp is the approval the change took away, as an edit leaves it
    assert (it["review_state"], it["signed_off_by"]) == ("stale", "you@example.com")
    assert res.stale == ["metric sales/revenue"]
    assert "stale" in res.notes[0]


def test_sm_add_of_an_unchanged_approved_item_keeps_its_approval(root):
    same = {"name": "revenue", "calculation": "total order amount",
            "bindings": {"SQLite": "SUM(orders.amount)"}, "source_tables": ["orders"]}
    res = curate.write_items(root, "sales", "metric", [same])
    assert res.validated, res.errors
    it = _item(root, "metric", "revenue")
    assert (it["review_state"], it["signed_off_by"]) == ("approved", "you@example.com")
    assert not res.stale and not res.notes


def test_sm_add_cross_area_ignores_the_files_sign_off(tmp_path):
    _model(tmp_path, cross=[_JOIN], ds_extra={"cross_subject_area_metrics": [
        {**_CROSS_METRIC, **CLAIMED, "signed_off_by": "you@example.com"}]})
    changed = {**_CROSS_METRIC, "calculation": "customers billed at least once",
               "bindings": {"PostgreSQL": "COUNT(DISTINCT invoices.id)"}, **CLAIMED}
    added = {**_CROSS_METRIC, "name": "invoice count", **CLAIMED}
    res = curate.write_items(tmp_path, curate.CROSS_AREA, "metric", [changed, added])
    assert res.validated, res.errors
    got = {m.name: m for m in L.load_datasource(tmp_path).cross_subject_area_metrics}
    assert (got["billed customers"].review_state, got["billed customers"].signed_off_by) == \
        ("stale", "you@example.com")
    assert (got["invoice count"].review_state, got["invoice count"].signed_off_by) == ("unreviewed", None)
    assert len(res.notes) == 2


def test_the_validator_warns_on_an_approved_metric_or_entity_with_no_sign_off(root):
    for kind, name in (("metric", "revenue"), ("entity", "customer")):
        path = _path(root, kind, name)
        doc = yaml.safe_load(path.read_text())
        doc.pop("signed_off_by")
        path.write_text(yaml.safe_dump(doc))
    res = V.validate(L.load_datasource(root, include_rejected=True))
    assert res.ok   # a warning, so a model written before #616 still loads and takes edits
    warned = [f for f in res.findings if f.code == "trust_block_incomplete"]
    assert len(warned) == 1 and "'revenue'" in warned[0].message and "'customer'" in warned[0].message


def test_the_shipped_sample_has_no_unsigned_approval():
    res = V.validate(L.load_datasource(SAMPLE, include_rejected=True))
    assert res.ok and not [f for f in res.findings if f.code == "trust_block_incomplete"]
