"""A metric with no description is counted beside `metric_index`, not listed in it (#406).

`description` is the one-line label the index shows and search matches. Every generated metric used
to arrive without one, so the index listed each by its own name — characters on every schema call
that told the agent nothing the key did not. These tests pin what replaced that: the predicate that
decides "described", the index and its count, the search that still finds an undescribed metric,
and the validator warning that tells a curator which ones to fill in.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import model_store  # noqa: E402
import tools  # noqa: E402
from semantic_model import validator as V  # noqa: E402
from semantic_model.models import Datasource, Metric  # noqa: E402
from store import Store  # noqa: E402


@pytest.mark.parametrize(
    "name,description,described",
    [
        ("revenue", "", False),
        ("revenue", "   ", False),
        ("revenue", "Revenue", False),  # the name, capitalised
        ("incident_count", "Incident count.", False),  # the name in words
        ("incident_count", "Incidents opened in the period", True),
        ("revenue", "Revenue net of refunds", True),  # starts with the name, says more
        ("sales_total", "売上合計", True),  # no Latin letters at all, and still a description
        ("incident_count", "Incident-count!", False),  # punctuation does not make it say more
        ("revenue", "The revenue", False),  # an article does not either
        ("incident_count", "Count of incident", False),  # nor the same words reordered
        ("order count", "Count of orders.", False),  # nor a plural
        ("gross_revenue", "Revenue", False),  # nor part of the name
        ("the", "The", False),  # a name made only of filler is still the name restated
    ],
)
def test_described(name, description, described):
    assert Metric(name=name, calculation="c", description=description).described is described


def _seed(tmp_path, monkeypatch, metrics: list[dict]) -> None:
    db_url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(db_url)
    s.run_migrations()
    org = {
        "datasource": "acme",
        "version": 1,
        "storage_connections": [{"name": "w", "storage_type": "PostgreSQL"}],
        "subject_areas": [
            {
                "name": "ops",
                "tables_defined": [
                    {
                        "name": "changes",
                        "schema": "public",
                        "storage_connection": "w",
                        "grain": ["id"],
                        "description": "One row per change request",
                        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
                    }
                ],
                "metrics": metrics,
            }
        ],
    }
    model_store.write_datasource(s, "main", Datasource.model_validate(org))
    s.close()
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "none"))


def _schema(**args) -> dict:
    out = tools.tool_get_datasource_schema({"datasource": "main", **args})
    return json.JSONDecoder().raw_decode(out)[0]


DURATION = {
    "name": "changes_avg_duration_days",
    "calculation": "Average days from change start to change closure",
    "source_tables": ["changes"],
}
BACKLOG = {
    "name": "change_backlog",
    "description": "Changes approved but not yet scheduled",
    "calculation": "Count of change requests in the approved state with no planned start",
    "source_tables": ["changes"],
}


def test_the_index_lists_described_metrics_and_counts_the_rest(tmp_path, monkeypatch):
    named_only = {"name": "changes_count", "description": "Changes count", "calculation": "rows",
                  "source_tables": ["changes"]}
    _seed(tmp_path, monkeypatch, [DURATION, BACKLOG, named_only])
    head = _schema(mode="index")
    assert head["metric_index"] == {"change_backlog": "Changes approved but not yet scheduled"}
    assert head["metrics_without_description"] == 2


def test_no_count_when_every_metric_is_described(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [BACKLOG])
    assert "metrics_without_description" not in _schema(mode="index")


def test_opening_the_table_still_returns_an_undescribed_metric_in_full(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [DURATION, BACKLOG])
    head = _schema(dataset_names=["changes"])
    full = {m["name"]: m for m in head["metrics"]}
    assert full["changes_avg_duration_days"]["calculation"] == DURATION["calculation"]
    assert "changes_avg_duration_days" not in head["metric_index"]
    assert head["metrics_without_description"] == 1


def test_query_finds_an_undescribed_metric_by_its_calculation(tmp_path, monkeypatch):
    """The question's words are in the calculation and nowhere else: the name abbreviates them."""
    _seed(tmp_path, monkeypatch, [DURATION, BACKLOG])
    head = _schema(mode="index", query="average time between change start and change closure")
    assert [m["name"] for m in head["metrics"]] == ["changes_avg_duration_days"]


def test_a_described_metric_is_not_matched_on_its_calculation(tmp_path, monkeypatch):
    """Its description is what it is meant to be found by; matching every metric's sentences too
    would widen every query."""
    _seed(tmp_path, monkeypatch, [BACKLOG])
    head = _schema(mode="index", query="planned start approved state requests")
    assert head["metrics"] == []


def test_one_word_of_a_calculation_is_not_a_strong_match(tmp_path, monkeypatch):
    """A substring hit is a strong match and is never capped, so the calculation must not feed that
    path: a query of one common word would otherwise pull every undescribed metric in full."""
    many = [
        {**DURATION, "name": f"m_{i}", "calculation": f"Average days for change {i}"}
        for i in range(15)
    ]
    _seed(tmp_path, monkeypatch, many)
    assert len(_schema(mode="index", query="change")["metrics"]) <= tools._METRIC_MATCH_TOP_K


def test_the_validator_warns_once_for_the_whole_model():
    org = Datasource.model_validate(
        {
            "datasource": "acme",
            "version": 1,
            "subject_areas": [
                {
                    "name": "ops",
                    "metrics": [
                        {"name": f"m{i}", "calculation": "c"} for i in range(7)
                    ] + [{"name": "ok", "calculation": "c", "description": "Something useful"}],
                }
            ],
        }
    )
    found = [f for f in V.validate(org).findings if f.code == "metric_undescribed"]
    assert len(found) == 1 and found[0].severity == "warning"
    assert found[0].message.startswith("7 metric(s)") and "and 2 more" in found[0].message
    assert "'ok'" not in found[0].message


def test_a_description_in_another_script_is_searchable(tmp_path, monkeypatch):
    """It counts as a description, so it is listed — and a question in the same script finds it."""
    _seed(tmp_path, monkeypatch, [{**BACKLOG, "name": "sales_total", "description": "売上合計"}])
    head = _schema(mode="index", query="売上合計")
    assert head["metric_index"] == {"sales_total": "売上合計"}
    assert [m["name"] for m in head["metrics"]] == ["sales_total"]


def test_a_shared_name_carries_the_selector_that_picks_it(tmp_path, monkeypatch):
    """Two areas share `backlog`; neither is described, so neither is in `metric_index`. The table
    reply is the only place the agent reads them from, and the bare name would select the first."""
    db_url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(db_url)
    s.run_migrations()
    area = lambda name: {  # noqa: E731
        "name": name,
        "tables_defined": [{"name": f"{name}_items", "schema": "public", "storage_connection": "w",
                            "grain": ["id"], "description": "d",
                            "columns": [{"name": "id", "type": "integer", "primary_key": True}]}],
        "metrics": [{"name": "backlog", "calculation": f"open {name} items",
                     "source_tables": [f"{name}_items"]}],
    }
    org = {"datasource": "acme", "version": 1,
           "storage_connections": [{"name": "w", "storage_type": "PostgreSQL"}],
           "subject_areas": [area("ops"), area("finance")]}
    model_store.write_datasource(s, "main", Datasource.model_validate(org))
    s.close()
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "none"))

    entries = _schema(mode="full")["metrics"]
    selectors = {e["calculation"]: e.get("selector", e["name"]) for e in entries}
    assert len(set(selectors.values())) == 2, entries
    for calc, sel in selectors.items():
        picked = _schema(mode="index", metric_names=[sel])["metrics"]
        assert [p["calculation"] for p in picked] == [calc]
