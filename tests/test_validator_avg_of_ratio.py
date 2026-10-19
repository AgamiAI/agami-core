"""`metric_avg_of_ratio`: an average of per-row ratios of two additive amounts (#480).

For a group, AVG(a / b) weighs every row the same whatever its size, so a store with 100 sales
counts as much as one with 1,000 and the result disagrees with the totals beside it. The group's
share is SUM(a) / SUM(b). These pin what the warning catches and what it deliberately leaves alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402


def _org(binding: str, *, calculation: str = "Share of sales made online.",
         online: str = "additive", total: str = "additive") -> m.Datasource:
    t = m.Table(name="store_sales", schema="s", storage_connection="c", grain=["store_id"],
                description="Sales per store.", columns=[
                    m.Column(name="store_id", type="integer", primary_key=True),
                    m.Column(name="online_sales", type="decimal", aggregation=online),
                    m.Column(name="total_sales", type="decimal", aggregation=total),
                    m.Column(name="score", type="decimal", aggregation="averageable")])
    met = m.Metric(name="online_share", description="Share of sales made online",
                   calculation=calculation, source_tables=["store_sales"],
                   bindings={"PostgreSQL": binding})
    sa = m.SubjectArea(name="s", tables_defined=[t], metrics=[met])
    return m.Datasource(datasource="demo", subject_areas=[sa])


def _codes(org: m.Datasource) -> list[str]:
    res = V.ValidationResult()
    V._check_metric_avg_of_ratio(org, res)
    return [f.code for f in res.findings]


@pytest.mark.parametrize("binding", [
    "AVG(online_sales / total_sales)",
    "AVG(online_sales / NULLIF(total_sales, 0))",
    "AVG(CAST(online_sales AS NUMERIC) / total_sales)",
    "AVG(100.0 * store_sales.online_sales / store_sales.total_sales)",
    "ROUND(AVG(online_sales::numeric / total_sales), 2)",
    "AVG((online_sales / total_sales) * 100)",  # the ratio wrapped inside the average
])
def test_average_of_per_row_ratio_of_additive_columns_warns(binding):
    assert _codes(_org(binding)) == ["metric_avg_of_ratio"]


def test_the_warning_names_the_metric_and_the_fix():
    res = V.ValidationResult()
    V._check_metric_avg_of_ratio(_org("AVG(online_sales / total_sales)"), res)
    msg = res.warnings[0]
    assert "online_share" in msg and "SUM(numerator) / NULLIF(SUM(denominator), 0)" in msg


@pytest.mark.parametrize("binding", [
    "SUM(online_sales) / NULLIF(SUM(total_sales), 0)",  # the ratio of totals: what it should be
    "AVG(online_sales)",  # no ratio
    "AVG(score / total_sales)",  # averageable numerator: not a share of totals
    "AVG((SELECT SUM(online_sales) / SUM(total_sales) FROM store_sales))",  # per group first
    "AVG(SUM(online_sales) / SUM(total_sales))",  # a ratio of aggregates, not per row
    "AVG(online_sales / 100)",  # a unit factor, no second column
])
def test_what_is_not_an_average_of_additive_ratios_is_left_alone(binding):
    assert _codes(_org(binding)) == []


def test_an_unknown_aggregation_class_never_warns():
    # `unknown` is never enforced against elsewhere either: no evidence, no warning
    org = _org("AVG(online_sales / total_sales)", total="unknown")
    assert _codes(org) == []


@pytest.mark.parametrize("calculation", [
    "The average of each row's ratio of online to total sales.",
    "Per-row online share, then the average across stores.",
    "Row-level share of online sales, averaged.",
])
def test_a_calculation_that_declares_a_per_row_average_is_left_alone(calculation):
    assert _codes(_org("AVG(online_sales / total_sales)", calculation=calculation)) == []


def test_unparseable_binding_never_warns():
    assert _codes(_org("AVG(online_sales / ")) == []


def test_validate_runs_the_check():
    res = V.validate(_org("AVG(online_sales / total_sales)"))
    assert any(f.code == "metric_avg_of_ratio" for f in res.findings)


def test_a_binding_for_an_engine_with_no_dialect_never_warns():
    # parsed only in its own engine's grammar; an engine this build cannot parse is no evidence
    org = _org("AVG(online_sales / total_sales)")
    met = org.subject_areas[0].metrics[0]
    met.bindings = {"NoSuchEngine": met.bindings["PostgreSQL"]}
    assert _codes(org) == []
