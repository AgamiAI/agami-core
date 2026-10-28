"""One rule for a metric binding that nests aggregates, everywhere it is judged (#519).

`sm add` accepted `AVG(SUM(a))`, the database then refused every query using it, and the
derived-metric check refused a legal aggregate over a subquery while missing a SUM inside a
percentile's WITHIN GROUP. `derived.binding_problems` is now the one rule: `sm add` and `curate`
refuse a new or edited binding it flags, the validator WARNS on one already in a model (so no model
starts failing validation), and query time says why instead of falling back silently.
"""

from __future__ import annotations

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
from semantic_model import derived as D  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402
from semantic_model import validator as V  # noqa: E402

AREA = "sales"
CODE = "metric_binding_nested_aggregate"


def _write_model(root: Path, metrics: tuple[dict, ...] = ()) -> None:
    """A one-table PostgreSQL model on disk, with `metrics` written straight to YAML (as an existing
    model would hold them, bypassing `sm add`)."""
    sa = root / "subject_areas" / AREA
    (root / "datasources" / "c").mkdir(parents=True)
    (sa / "tables").mkdir(parents=True)
    (sa / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "acme", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": [f"subject_areas/{AREA}"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (sa / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": AREA,
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}],
    }))
    (sa / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "One row per order.",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "order_date", "type": "date"},
                    {"name": "amount", "type": "decimal"}],
    }))
    for met in metrics:
        (sa / "metrics" / f"{met['name']}.yaml").write_text(yaml.safe_dump(met))


def _metric(name: str, binding: str, **extra) -> dict:
    return {"name": name, "description": "A metric.", "calculation": "x",
            "bindings": {"PostgreSQL": binding}, "source_tables": ["orders"], **extra}


NESTED = "AVG(SUM(amount))"
PERCENTILE_OF_SUM = "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY SUM(amount))"

# binding -> whether the shared rule flags it. Every other column of the agreement table below is
# derived from this one verdict, which is the point: they must not disagree.
CASES = [
    pytest.param(NESTED, True, id="avg-of-sum"),
    pytest.param(PERCENTILE_OF_SUM, True, id="percentile-of-sum"),
    pytest.param("MEDIAN(SUM(amount))", True, id="median-of-sum"),
    pytest.param("COUNT(*) FILTER (WHERE SUM(amount) > 0)", True, id="sum-in-filter"),
    pytest.param("(SELECT AVG(t) FROM (SELECT customer_id, SUM(amount) AS t FROM orders "
                 "GROUP BY customer_id) s)", False, id="avg-over-derived-table"),
    pytest.param("AVG((SELECT SUM(amount) FROM orders))", False, id="scalar-subquery-in-avg"),
    pytest.param("SUM(amount) FILTER (WHERE amount > 0)", False, id="filter-aggregate"),
    pytest.param("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount)", False, id="plain-percentile"),
    pytest.param("SUM(SUM(amount)) OVER ()", False, id="window-over-aggregate"),
    pytest.param("SUM(COALESCE(amount, 0)) / NULLIF(COUNT(amount), 0)", False, id="present-values"),
]


@pytest.mark.parametrize("binding,nested", CASES)
def test_the_rule_sm_add_the_validator_and_query_time_agree(binding, nested, tmp_path):
    assert bool(D.binding_problems(binding, "PostgreSQL")) is nested

    # sm add: refused (skipped with the reason, nothing written) exactly when the rule flags it
    _write_model(tmp_path / "add")
    res = curate.write_items(tmp_path / "add", AREA, "metric", [_metric("m1", binding)])
    path = tmp_path / "add" / "subject_areas" / AREA / "metrics" / "m1.yaml"
    assert path.exists() is (not nested)
    assert bool(res.skipped) is nested
    if nested:
        assert "nests an aggregate inside another" in res.skipped[0]["reason"]

    # an existing model holding it: a warning (never an error) exactly when the rule flags it
    _write_model(tmp_path / "existing", (_metric("m1", binding),))
    vres = V.validate(loader.load_datasource(tmp_path / "existing", include_rejected=True))
    assert vres.ok
    assert any(f.code == CODE for f in vres.findings) is nested

    # query time: the matched metric says why it won't run, exactly when the rule flags it
    org = loader.load_datasource(tmp_path / "existing")
    hit = RT.resolve_metrics("m1", org)[0]
    assert ("binding_problem" in hit) is nested


def test_sm_add_refuses_through_the_cli_with_the_reason(tmp_path, capsys):
    _write_model(tmp_path)
    items = tmp_path / "items.json"
    items.write_text(json.dumps([_metric("avg_order_total", NESTED)]))
    rc = cli.main(["add", str(tmp_path), "--kind", "metric", "--area", AREA, "--file", str(items)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["applied"] == []
    assert "SUM(...) is nested inside AVG(...)" in out["skipped"][0]["reason"]
    assert not (tmp_path / "subject_areas" / AREA / "metrics" / "avg_order_total.yaml").exists()


def test_one_refused_item_does_not_stop_the_rest_of_the_batch(tmp_path):
    _write_model(tmp_path)
    res = curate.write_items(tmp_path, AREA, "metric", [
        _metric("bad", PERCENTILE_OF_SUM), _metric("revenue", "SUM(amount)")])
    assert res.validated and res.applied == [f"metric {AREA}/revenue"]
    assert [s["item"] for s in res.skipped] == ["bad"]


def test_an_existing_nested_metric_still_lets_curate_write_other_edits(tmp_path):
    _write_model(tmp_path, (_metric("avg_order_total", NESTED),))
    res = curate.apply(tmp_path, [
        {"op": "edit", "kind": "table", "area": AREA, "name": "orders",
         "field": "description", "value": "Every order placed."},
        {"op": "approve", "kind": "metric", "area": AREA, "name": "avg_order_total"},
    ])
    assert res.validated and len(res.applied) == 2 and not res.errors
    org = loader.load_datasource(tmp_path)
    assert org.subject_areas[0].defined_table("orders").description == "Every order placed."


def test_curate_refuses_an_edit_that_nests_and_accepts_one_that_fixes_it(tmp_path):
    _write_model(tmp_path, (_metric("revenue", "SUM(amount)"), _metric("avg_order_total", NESTED)))
    path = tmp_path / "subject_areas" / AREA / "metrics" / "revenue.yaml"
    before = path.read_text()
    res = curate.apply(tmp_path, [{"op": "edit", "kind": "metric", "area": AREA, "name": "revenue",
                                   "field": "bindings", "value": {"PostgreSQL": NESTED}}])
    assert not res.applied
    assert "nests an aggregate inside another" in res.skipped[0]["reason"]
    assert path.read_text() == before

    res = curate.apply(tmp_path, [{"op": "edit", "kind": "metric", "area": AREA,
                                   "name": "avg_order_total", "field": "bindings",
                                   "value": {"PostgreSQL": "AVG(amount)"}}])
    assert res.validated and res.applied


def test_second_order_synthesis_is_still_accepted_and_unchanged(tmp_path):
    _write_model(tmp_path)
    res = curate.write_items(tmp_path, AREA, "metric", [
        _metric("daily_revenue", "SUM(amount)"),
        _metric("avg_daily_revenue", "AVG({daily_revenue})", base_metrics=["daily_revenue"],
                inner_grain=["order_date"]),
    ])
    assert res.validated and len(res.applied) == 2 and not res.skipped
    org = loader.load_datasource(tmp_path)
    vres = V.validate(org)
    assert vres.ok and not [f for f in vres.findings if f.code in (CODE, "derived_metric")]
    met = next(x for x in org.subject_areas[0].metrics if x.name == "avg_daily_revenue")
    assert D.resolve_metric_sql(met, "PostgreSQL", D.metric_index(org), org) == (
        "(SELECT AVG(daily_revenue) FROM (SELECT order_date, SUM(amount) AS daily_revenue "
        "FROM orders GROUP BY order_date) _inner)")


def _derived_org(binding: str, base: str = "SUM(amount)") -> m.Datasource:
    rev = m.Metric(name="revenue", calculation="x", bindings={"PostgreSQL": base},
                   source_tables=["orders"])
    der = m.Metric(name="d", calculation="x", bindings={"PostgreSQL": binding},
                   base_metrics=["revenue"], source_tables=["orders"])
    return m.Datasource(datasource="acme", subject_areas=[
        m.SubjectArea(name=AREA, metrics=[rev, der])])


def _derived_findings(binding: str, base: str = "SUM(amount)") -> list[tuple[str, str]]:
    res = V.ValidationResult()
    V._check_derived_metrics(_derived_org(binding, base), res)
    return [(f.severity, f.code) for f in res.findings]


def test_a_derived_nesting_in_the_argument_is_still_an_error():
    assert _derived_findings("AVG({revenue})") == [("error", "derived_metric")]


def test_a_derived_nesting_only_the_new_rule_sees_is_a_warning():
    # The old check never looked inside WITHIN GROUP, so a model holding this validated: it must
    # keep validating.
    assert _derived_findings("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {revenue})") == [
        ("warning", CODE)]


def test_the_inner_aggregates_own_filter_leaves_it_in_the_argument():
    # The SUM's FILTER is its own, so AVG still nests it in the argument: still the error it was.
    assert _derived_findings("AVG({revenue})", base="SUM(amount) FILTER (WHERE amount > 0)") == [
        ("error", "derived_metric")]
    assert [p.clause for p in D.binding_problems(
        "AVG(SUM(amount) FILTER (WHERE amount > 0))", "PostgreSQL")] == ["argument"]
    assert [p.clause for p in D.binding_problems(
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY SUM(amount) FILTER (WHERE amount > 0))",
        "PostgreSQL")] == ["within_group"]
    assert [p.clause for p in D.binding_problems(
        "SUM(amount) FILTER (WHERE PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) > 0)",
        "PostgreSQL")] == ["filter"]


@pytest.mark.parametrize("derived_first", [True, False])
def test_a_batch_judges_a_derived_metric_with_its_sibling_base_known(tmp_path, derived_first):
    _write_model(tmp_path)
    items = [_metric("median_revenue", "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {revenue})",
                     base_metrics=["revenue"]),
             _metric("revenue", "SUM(amount)")]
    res = curate.write_items(tmp_path, AREA, "metric", items if derived_first else items[::-1])
    assert res.applied == [f"metric {AREA}/revenue"]
    assert [s["item"] for s in res.skipped] == ["median_revenue"]
    assert "nested inside PERCENTILE_CONT(...) WITHIN GROUP" in res.skipped[0]["reason"]
    assert not (tmp_path / "subject_areas" / AREA / "metrics" / "median_revenue.yaml").exists()


def test_a_derived_aggregate_over_a_subquery_is_no_longer_refused():
    # The old check descended into subqueries and refused this legal shape.
    assert _derived_findings("{revenue} / (SELECT COUNT(*) FROM orders)") == []


def test_query_time_names_why_a_derived_metric_fell_back():
    hit = RT.resolve_metrics("d", _derived_org("AVG({revenue})"))[0]
    assert hit["bindings"] == {"PostgreSQL": "AVG({revenue})"}
    assert "aggregate of an aggregate" in hit["binding_problem"]


def test_a_metric_the_rule_accepts_carries_no_new_key():
    hit = RT.resolve_metrics("d", _derived_org("{revenue} * 2"))[0]
    assert set(hit) == {"metric", "subject_area", "score", "calculation", "bindings", "confidence"}


def test_a_binding_that_does_not_parse_is_never_flagged():
    assert D.binding_problems("AVG({revenue})", "PostgreSQL") == []
    assert D.binding_problems("SUM(amount", "PostgreSQL") == []
    assert D.binding_problems("AVG(SUM(amount))", "NoSuchEngine") == []
