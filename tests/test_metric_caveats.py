"""A metric carries `caveats`, like a table or a column, and they are served with it (#524).

A caveat that belongs to a metric ("gives no value when the start date is missing") used to go
into its description or calculation, where it reads as part of the definition. These pin that the
field validates and that every surface serving the metric carries it.
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

import admin  # noqa: E402
import tools  # noqa: E402
from semantic_model import curate, loader, mentions  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402
from semantic_model import validator as V  # noqa: E402

CAVEAT = "Gives no value for a ticket with no opened_at."


def _metric(**kw) -> m.Metric:
    fields = {"name": "avg_resolution_days", "description": "Days from opening to resolution",
              "calculation": "Average of resolved_at minus opened_at, in days.",
              "source_tables": ["tickets"], "bindings": {"PostgreSQL": "AVG(resolved_at - opened_at)"}}
    return m.Metric(**{**fields, **kw})


def _org(metric: m.Metric) -> m.Datasource:
    t = m.Table(name="tickets", schema="public", storage_connection="c", grain=["id"],
                description="One row per support ticket.", columns=[
                    m.Column(name="id", type="integer", primary_key=True),
                    m.Column(name="opened_at", type="timestamp", description="When it was opened."),
                    m.Column(name="resolved_at", type="timestamp", description="When it was resolved.")])
    return m.Datasource(datasource="demo", subject_areas=[
        m.SubjectArea(name="support", tables_defined=[t], metrics=[metric])])


def test_a_metric_takes_caveats_and_defaults_to_none():
    assert _metric().caveats == []
    assert _metric(caveats=[CAVEAT]).caveats == [CAVEAT]


def test_an_empty_caveat_is_refused():
    with pytest.raises(ValueError, match="non-empty"):
        _metric(caveats=["  "])


def test_the_schema_tool_serves_caveats_only_when_there_are_some():
    assert tools._metric_full(_metric(caveats=[CAVEAT]), "support", "PostgreSQL")["caveats"] == [CAVEAT]
    assert "caveats" not in tools._metric_full(_metric(), "support", "PostgreSQL")


def test_metric_search_serves_caveats():
    hits = RT.resolve_metrics("avg_resolution_days by month", _org(_metric(caveats=[CAVEAT])))
    assert hits and hits[0]["caveats"] == [CAVEAT]


def test_the_admin_card_and_the_review_queue_show_caveats():
    met = _metric(caveats=[CAVEAT])
    assert "opened_at" in admin._metric_card_html(met)
    item = curate._metric_item("support", met)
    assert {"label": "Caveat", "text": CAVEAT} in item["extra_lines"]


def test_validator_and_mentions_read_metric_caveats(tmp_path):
    org = _org(_metric(caveats=[CAVEAT]))
    assert (f"metric {'avg_resolution_days'!r} caveat", CAVEAT) in list(V._model_texts(org))
    lines = list(mentions._prose_lines(org, tmp_path, []))
    assert ("metric.caveat", "avg_resolution_days", CAVEAT, None) in lines


def test_a_metric_caveat_saying_missing_is_zero_silences_the_null_fill_warning():
    def warnings(caveats):
        met = _metric(bindings={"PostgreSQL": "AVG(COALESCE(resolved_at - opened_at, 0))"},
                      caveats=caveats)
        org = _org(met)
        org.subject_areas[0].tables_defined[0].columns[2].description = (
            "When it was resolved. NULL means it was not recorded.")
        res = V.ValidationResult()
        V._check_metric_null_fill(org, res)
        return [f for f in res.findings if f.code == "metric_null_filled"]

    assert warnings([])
    assert not warnings(["Following the support KB formula, a missing duration counts as 0."])


def test_curate_edits_metric_caveats_and_the_explorer_shows_them(tmp_path):
    root = tmp_path / "demo"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "support" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "support" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/support"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "support" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "support", "tables": [{"storage_connection": "c", "schema": "public", "table": "tickets"}]}))
    (root / "subject_areas" / "support" / "tables" / "tickets.yaml").write_text(yaml.safe_dump(
        _org(_metric()).subject_areas[0].tables_defined[0].model_dump(by_alias=True, exclude_none=True)))
    (root / "subject_areas" / "support" / "metrics" / "avg_resolution_days.yaml").write_text(
        yaml.safe_dump(_metric().model_dump(exclude_none=True)))

    res = curate.apply(root, [{"op": "edit", "kind": "metric", "area": "support",
                               "name": "avg_resolution_days", "field": "caveats", "value": [CAVEAT]}])
    assert not res.errors, res.errors
    met = loader.load_datasource(root).subject_areas[0].metrics[0]
    assert met.caveats == [CAVEAT]

    from render_model_explorer import build_manifest
    manifest = build_manifest(root, "demo")
    assert json.loads(json.dumps(manifest))["metrics"][0]["caveats"] == [CAVEAT]
