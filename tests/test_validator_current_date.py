"""`metric_today_not_current_date`: a metric about today measures to today (#482).

A binding that stops at a fixed date or at the latest date in the data gives a stable figure, but
when the data is not loaded up to today it silently changes the answer and which rows qualify.
These pin what the warning catches across engines and what it deliberately leaves alone.
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

TODAY = "Days from each project's start to today, averaged over open projects."


def _org(binding: str, *, calculation: str = TODAY, storage_type: str = "PostgreSQL") -> m.Datasource:
    t = m.Table(name="projects", schema="s", storage_connection="c", grain=["project_id"],
                description="One row per project.", columns=[
                    m.Column(name="project_id", type="integer", primary_key=True),
                    m.Column(name="start_date", type="date")])
    met = m.Metric(name="days_running", description="Average days a project has been running",
                   calculation=calculation, source_tables=["projects"],
                   bindings={storage_type: binding})
    sa = m.SubjectArea(name="s", tables_defined=[t], metrics=[met])
    return m.Datasource(datasource="demo", subject_areas=[sa])


def _warnings(org: m.Datasource) -> list[str]:
    res = V.ValidationResult()
    V._check_metric_current_date(org, res)
    return [f.message for f in res.findings if f.code == "metric_today_not_current_date"]


@pytest.mark.parametrize("storage_type,binding", [
    ("PostgreSQL", "AVG(CURRENT_DATE - start_date)"),
    ("PostgreSQL", "AVG(EXTRACT(DAY FROM NOW() - start_date))"),
    ("SQLServer", "AVG(DATEDIFF(day, start_date, GETDATE()))"),
    ("Oracle", "AVG(SYSDATE - start_date)"),
    ("MySQL", "AVG(DATEDIFF(CURDATE(), start_date))"),
    ("BigQuery", "AVG(DATE_DIFF(CURRENT_DATE(), start_date, DAY))"),
    ("SQLite", "AVG(julianday('now') - julianday(start_date))"),
    # a date literal beside today is a filter (a placeholder end date), not the reference point
    ("PostgreSQL", "AVG(CASE WHEN start_date < DATE '9999-12-31' THEN CURRENT_DATE - start_date END)"),
])
def test_a_binding_that_measures_to_today_is_left_alone(storage_type, binding):
    assert _warnings(_org(binding, storage_type=storage_type)) == []


@pytest.mark.parametrize("binding,reason", [
    ("AVG(DATE '2025-02-19' - start_date)", "fixed date '2025-02-19'"),
    ("AVG('2025-02-19'::date - start_date)", "fixed date '2025-02-19'"),
    ("AVG((SELECT MAX(start_date) FROM projects) - start_date)", "latest date in the data"),
    ("AVG(start_date - start_date)", "has no current date"),
])
def test_a_binding_that_does_not_measure_to_today_warns(binding, reason):
    msgs = _warnings(_org(binding))
    assert len(msgs) == 1
    assert "days_running" in msgs[0] and reason in msgs[0] and "CURRENT_DATE" in msgs[0]


@pytest.mark.parametrize("calculation", [
    "Days between each project's start and the current date.",
    "Age of each project as of the run date.",
])
def test_other_ways_of_saying_today_are_recognised(calculation):
    assert len(_warnings(_org("AVG(DATE '2025-02-19' - start_date)", calculation=calculation))) == 1


def test_a_metric_that_does_not_say_today_is_left_alone():
    # "current" alone is a latest snapshot ("current balance"), not a date calculation
    calc = "Current backlog: days between start and the planned end, averaged."
    assert _warnings(_org("AVG(DATE '2025-02-19' - start_date)", calculation=calc)) == []


def test_unparseable_binding_never_warns():
    assert _warnings(_org("AVG(DATE '2025-02-19' - ")) == []


def test_validate_runs_the_check():
    res = V.validate(_org("AVG(DATE '2025-02-19' - start_date)"))
    assert any(f.code == "metric_today_not_current_date" for f in res.findings)
