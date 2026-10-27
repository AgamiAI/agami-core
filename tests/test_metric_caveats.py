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


def test_mentions_read_cross_area_metric_caveats(tmp_path):
    # a metric in the cross-cutting area is served like any other, so its prose is scanned too
    caveat = "No value when tickets.opened_at is missing."
    org = _org(_metric())
    org.cross_subject_area_metrics.append(_metric(name="tickets_reopened_share", caveats=[caveat]))
    lines = list(mentions._prose_lines(org, tmp_path, []))
    assert ("metric.caveat", "tickets_reopened_share", caveat, None) in lines
    assert ("metric.calculation", "tickets_reopened_share",
            "Average of resolved_at minus opened_at, in days.", None) in lines
    sql = "SELECT AVG(resolved_at - opened_at) FROM public.tickets"
    found = mentions.prose_mentions(org, tmp_path, sql, "postgres")["mentions"]
    assert {"about": "tickets.opened_at", "source": "metric.caveat",
            "where": "tickets_reopened_share", "text": caveat} in found


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


def _write_model(tmp_path) -> Path:
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
    return root


def test_curate_edits_metric_caveats_and_the_explorer_shows_them(tmp_path):
    root = _write_model(tmp_path)
    res = curate.apply(root, [{"op": "edit", "kind": "metric", "area": "support",
                               "name": "avg_resolution_days", "field": "caveats", "value": [CAVEAT]}])
    assert not res.errors, res.errors
    met = loader.load_datasource(root).subject_areas[0].metrics[0]
    assert met.caveats == [CAVEAT]

    from render_model_explorer import build_manifest
    manifest = build_manifest(root, "demo")
    assert json.loads(json.dumps(manifest))["metrics"][0]["caveats"] == [CAVEAT]


def _older_metric_model():
    # what a release before #524 reads a metric file with: the same fields minus `caveats`, unknown
    # keys forbidden
    from pydantic import ConfigDict, create_model
    fields = {k: (f.annotation, f) for k, f in m.Metric.model_fields.items() if k != "caveats"}
    return create_model("OlderMetric", __config__=ConfigDict(extra="forbid"), **fields)


def test_a_metric_without_caveats_is_written_without_the_key():
    from semantic_model.build import _model_dump
    doc = _model_dump(_metric())
    assert "caveats" not in doc
    assert "caveats" not in _metric().model_dump(mode="json", exclude_none=True)
    assert m.Metric(**doc).caveats == []
    _older_metric_model().model_validate(doc)
    with_caveats = _model_dump(_metric(caveats=[CAVEAT]))
    assert m.Metric(**with_caveats).caveats == [CAVEAT]


def test_write_items_leaves_caveats_out_of_the_file_when_empty(tmp_path):
    root = _write_model(tmp_path)
    res = curate.write_items(root, "support", "metric", [
        {"name": "tickets_opened", "calculation": "Count of tickets.",
         "source_tables": ["tickets"], "bindings": {"PostgreSQL": "COUNT(*)"}},
        {"name": "tickets_resolved", "calculation": "Count of resolved tickets.", "caveats": [CAVEAT],
         "source_tables": ["tickets"], "bindings": {"PostgreSQL": "COUNT(resolved_at)"}}])
    assert not res.errors and not res.skipped, (res.errors, res.skipped)
    metrics = root / "subject_areas" / "support" / "metrics"
    plain = yaml.safe_load((metrics / "tickets_opened.yaml").read_text())
    assert "caveats" not in plain
    _older_metric_model().model_validate(plain)
    assert yaml.safe_load((metrics / "tickets_resolved.yaml").read_text())["caveats"] == [CAVEAT]
    loaded = {mm.name: mm for mm in loader.load_datasource(root).subject_areas[0].metrics}
    assert loaded["tickets_opened"].caveats == [] and loaded["tickets_resolved"].caveats == [CAVEAT]


def test_clearing_a_metrics_caveats_removes_the_key(tmp_path):
    root = _write_model(tmp_path)
    path = root / "subject_areas" / "support" / "metrics" / "avg_resolution_days.yaml"
    for value in ([CAVEAT], []):
        res = curate.apply(root, [{"op": "edit", "kind": "metric", "area": "support",
                                   "name": "avg_resolution_days", "field": "caveats", "value": value}])
        assert not res.errors, res.errors
    assert "caveats" not in yaml.safe_load(path.read_text())
    assert loader.load_datasource(root).subject_areas[0].metrics[0].caveats == []


def test_clearing_a_cross_area_metrics_caveats_removes_the_key(tmp_path):
    root = _write_model(tmp_path)
    (root / "subject_areas" / "support" / "metrics" / "avg_resolution_days.yaml").unlink()
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_metrics"] = [_metric(subject_areas=["support"]).model_dump(exclude_none=True)]
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds))
    for value in ([CAVEAT], []):
        res = curate.apply(root, [{"op": "edit", "kind": "metric", "name": "avg_resolution_days",
                                   "field": "caveats", "value": value}])
        assert not res.errors, res.errors
    assert "caveats" not in yaml.safe_load((root / "datasource.yaml").read_text())["cross_subject_area_metrics"][0]
    assert loader.load_datasource(root).cross_subject_area_metrics[0].caveats == []
