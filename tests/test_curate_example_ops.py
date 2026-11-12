"""#608: `sm curate` takes example rejections in the same ops file as model edits.

`{"op": "reject", "kind": "example", "area", "question"}` rejects that example exactly as `sm
remove-example` does, and is one batch with the model ops: a batch that fails reverts it too.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate  # noqa: E402
from semantic_model import loader as L  # noqa: E402

Q1, Q2 = "how many orders?", "total billed?"


def _write_model(root: Path) -> None:
    """Area `sales`: one table, one unreviewed metric and two examples."""
    sa = root / "subject_areas" / "sales"
    (root / "datasources" / "c").mkdir(parents=True)
    for d in ("tables", "metrics"):
        (sa / d).mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "SQLite"}))
    (sa / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales", "tables": [{"storage_connection": "c", "table": "orders"}]}))
    (sa / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "storage_connection": "c", "grain": ["id"], "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal"}]}))
    (sa / "metrics" / "revenue.yaml").write_text(yaml.safe_dump({
        "name": "revenue", "calculation": "total order amount",
        "bindings": {"SQLite": "SUM(orders.amount)"}, "source_tables": ["orders"],
        "confidence": "proposed", "review_state": "unreviewed"}))
    curate.add_examples(root, "sales", [
        {"question": Q1, "sql": "SELECT COUNT(*) FROM orders", "status": "confirmed"},
        {"question": Q2, "sql": "SELECT SUM(amount) FROM orders", "status": "confirmed"},
    ])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "demo"
    _write_model(r)
    return r


def _status(root: Path, question: str) -> str | None:
    return next(e.get("status") for e in L.list_prompt_examples(root, "sales", include_rejected=True)
                if e["question"] == question)


def _log(root: Path) -> list[dict]:
    path = root / curate.CURATION_LOG
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


REJECT_Q2 = {"op": "reject", "kind": "example", "area": "sales", "question": f"  {Q2} "}
APPROVE = {"op": "approve", "kind": "metric", "area": "sales", "name": "revenue",
           "at": "2026-01-01T00:00:00Z"}


def test_reject_example_alone_marks_it_rejected_and_keeps_it(root: Path):
    res = curate.apply(root, [REJECT_Q2], signer="you@example.com", role="data_lead")
    assert res.validated and res.applied == [f"reject example sales/{Q2}"], res.as_dict()
    assert _status(root, Q2) == "rejected" and _status(root, Q1) == "confirmed"
    assert [e["question"] for e in L.list_prompt_examples(root, "sales")] == [Q1]
    entry = [e for e in _log(root) if e.get("op") == "reject"]
    assert len(entry) == 1 and entry[0]["name"] == Q2 and entry[0]["signer"] == "you@example.com"
    assert entry[0]["kind"] == "example" and entry[0]["area"] == "sales"
    assert entry[0]["before"] == "confirmed"   # the status it replaced


def test_example_op_writes_the_file_as_remove_examples_does(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write_model(a)
    _write_model(b)
    curate.apply(a, [REJECT_Q2])
    curate.remove_examples(b, "sales", [Q2])
    path = Path("prompt_examples") / "sales" / "examples.yaml"
    assert (a / path).read_text() == (b / path).read_text()


def test_mixed_batch_applies_the_metric_approve_and_the_example_reject(root: Path):
    res = curate.apply(root, [APPROVE, REJECT_Q2], signer="you@example.com", role="data_lead")
    assert res.validated and len(res.applied) == 2, res.as_dict()
    assert L.load_datasource(root).subject_areas[0].metrics[0].review_state == "approved"
    assert _status(root, Q2) == "rejected"


def test_a_batch_failing_validation_reverts_the_example_too(root: Path, monkeypatch):
    before = (root / "prompt_examples" / "sales" / "examples.yaml").read_text()
    monkeypatch.setattr(curate.V, "validate", lambda *a, **k: types.SimpleNamespace(ok=False, errors=["forced"]))
    res = curate.apply(root, [REJECT_Q2, APPROVE], signer="you@example.com", role="data_lead")
    assert not res.validated and res.errors == ["forced"] and not res.applied, res.as_dict()
    assert (root / "prompt_examples" / "sales" / "examples.yaml").read_text() == before
    assert _status(root, Q2) == "confirmed"
    assert not [e for e in _log(root) if e.get("op") == "reject"]


def test_a_batch_error_reverts_the_example_too(root: Path):
    before = (root / "prompt_examples" / "sales" / "examples.yaml").read_text()
    res = curate.apply(root, [REJECT_Q2, {"op": "reject", "kind": "metric", "name": "no_such_metric"}])
    assert res.errors and not res.applied, res.as_dict()
    assert (root / "prompt_examples" / "sales" / "examples.yaml").read_text() == before


@pytest.mark.parametrize("op, reason", [
    ({"area": "nowhere", "question": Q2}, "no examples for area 'nowhere'"),
    ({"area": "sales", "question": "no such q?"}, "no example with that question"),
    ({"area": "sales"}, "an example op needs area + question"),
])
def test_unknown_area_or_question_is_skipped_with_a_reason(root: Path, op: dict, reason: str):
    res = curate.apply(root, [{"op": "reject", "kind": "example", **op}, APPROVE],
                       signer="you@example.com", role="data_lead")
    assert res.validated and res.applied == ["approve metric sales/revenue"], res.as_dict()
    assert [s["reason"] for s in res.skipped] == [reason]


def test_already_rejected_example_is_skipped(root: Path):
    curate.remove_examples(root, "sales", [Q2])
    res = curate.apply(root, [REJECT_Q2])
    assert not res.applied and res.skipped[0]["reason"] == "already rejected"


@pytest.mark.parametrize("verb", ["include", "approve", "exclude", "edit"])
def test_other_ops_on_an_example_are_refused(root: Path, verb: str):
    res = curate.apply(root, [{**REJECT_Q2, "op": verb}])
    assert not res.applied and res.skipped[0]["reason"] == f"an example takes only op 'reject', not {verb!r}"
    assert _status(root, Q2) == "confirmed"
