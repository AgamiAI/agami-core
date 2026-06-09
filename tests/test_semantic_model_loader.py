"""Unit tests for semantic_model/loader.py — context assembly + disk round-trip."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _wide_org():
    cols = [m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="soc", type="decimal", description="state of charge"),
            m.Column(name="zone_name", type="string"),
            m.Column(name="alert", type="string")]
    t = m.Table(name="wide", schema="powerbi", storage_connection="c", grain=["id"],
                description="wide snapshot", columns=cols,
                column_groups={"telemetry": ["id", "soc"], "location": ["zone_name"],
                               "alerts": ["alert"]},
                default_filters=["{alias}.report_date = (SELECT MAX(report_date) FROM powerbi.wide)",
                                 "{alias}.tenant_id = :tenant_id"])
    sa = m.SubjectArea(name="battery_state",
                       tables=[m.TableRef(storage_connection="c", schema="powerbi", table="wide",
                                          expose_column_groups=["telemetry", "location"])],
                       tables_defined=[t])
    return m.Organization(organization="Sun",
                          storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
                          subject_areas=[sa])


def test_collect_default_filters_param_substitution():
    org = _wide_org()
    fs = L.collect_default_filters(org, ["wide"], area="battery_state", params={"tenant_id": "42"})
    assert any("MAX(report_date)" in f for f in fs)
    assert any("tenant_id = 42" in f for f in fs)


def test_collect_default_filters_dedup():
    org = _wide_org()
    fs = L.collect_default_filters(org, ["wide", "wide"], area="battery_state")
    # same table twice -> filters not duplicated
    assert len(fs) == len(set(fs))


def test_get_table_index_honors_expose_groups():
    org = _wide_org()
    t = org.subject_areas[0].defined_table("wide")
    idx = L.get_table_index(t, ["telemetry", "location"])
    names = {c["name"] for c in idx["columns"]}
    assert names == {"id", "soc", "zone_name"}
    assert idx["column_count_total"] == 4 and idx["column_count_visible"] == 3


def test_get_table_context_scopes_columns_by_area():
    org = _wide_org()
    ctx = L.get_table_context(org, ["wide"], area="battery_state")
    cols = {c["name"] for c in ctx["tables"]["wide"]["columns"]}
    assert "alert" not in cols  # alerts group not exposed in this area


def test_get_table_context_specific_columns():
    org = _wide_org()
    ctx = L.get_table_context(org, ["wide"], area="battery_state", columns=["soc"])
    cols = [c["name"] for c in ctx["tables"]["wide"]["columns"]]
    assert cols == ["soc"]


def test_get_subject_area_bundle():
    org = _wide_org()
    bundle = L.get_subject_area_bundle(org, "battery_state")
    assert bundle["subject_area"]["name"] == "battery_state"
    assert "wide" in bundle["tables"]


def test_disk_round_trip(tmp_path):
    """Write a minimal v2 tree by hand, load it, and assert it parses back."""
    import yaml

    root = tmp_path / ".semantic_v2"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "area" / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "O", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/area"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL", "storage_config": {}}))
    (root / "subject_areas" / "area" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "area", "description": "d",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "t"}],
    }))
    (root / "subject_areas" / "area" / "tables" / "t.yaml").write_text(yaml.safe_dump({
        "name": "t", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "one line",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))
    (root / "subject_areas" / "area" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [],
    }))
    org = L.load_organization(root)
    assert org.organization == "O"
    sa = org.subject_area("area")
    assert sa is not None and sa.defined_table("t").grain == ["id"]
    assert org.storage_connection("c").storage_type == "PostgreSQL"
