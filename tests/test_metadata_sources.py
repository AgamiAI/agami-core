"""Tests for metadata_sources — enrich a model from in-DB metadata/lookup tables.

The pure transforms (rows -> curate ops / reference specs) test without a database. One
integration test runs the generated ops through `curate.apply` to confirm they land and that a
dictionary-sourced description is stamped with the authoritative `metadata` provenance.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate, loader  # noqa: E402
from semantic_model import metadata_sources as M  # noqa: E402


# --- canned ServiceNow-shaped source rows -----------------------------------

SYS_CHOICE = [
    {"name": "incident", "element": "severity", "value": "1", "label": "High"},
    {"name": "incident", "element": "severity", "value": "2", "label": "Medium"},
    {"name": "incident", "element": "severity", "value": "3", "label": "Low"},
    {"name": "incident", "element": "state", "value": "1", "label": "New"},
    {"name": "incident", "element": "state", "value": "", "label": "blank-value-skip"},
    {"name": "incident", "element": "note", "value": "x", "label": ""},   # blank label → skip
]

SYS_DICTIONARY = [
    {"name": "incident", "element": "severity", "column_label": "Severity",
     "comments": "Severity level of the incident impact.", "internal_type": "integer", "reference": ""},
    {"name": "incident", "element": "short_description", "column_label": "Short description",
     "comments": "", "internal_type": "string", "reference": ""},          # falls back to label
    {"name": "incident", "element": "caller_id", "column_label": "Caller",
     "comments": "Person who reported it.", "internal_type": "reference", "reference": "sys_user"},
    {"name": "incident", "element": "blankcol", "column_label": "", "comments": "",
     "internal_type": "string", "reference": ""},                          # no text → no op
]


# --- pure transforms --------------------------------------------------------

def test_choice_field_ops_groups_and_skips_blanks():
    ops = M.choice_field_ops(SYS_CHOICE, table_col="name", column_col="element",
                             value_col="value", label_col="label")
    by = {(o["name"], o["column"]): o["value"] for o in ops}
    assert by[("incident", "severity")] == {"1": "High", "2": "Medium", "3": "Low"}
    assert by[("incident", "state")] == {"1": "New"}          # blank-value row dropped
    assert ("incident", "note") not in by                     # blank-label column → no op
    assert all(o["op"] == "edit" and o["field"] == "choice_field" for o in ops)


def test_choice_field_ops_valid_filter_restricts_to_model_columns():
    ops = M.choice_field_ops(SYS_CHOICE, table_col="name", column_col="element",
                             value_col="value", label_col="label",
                             valid={("incident", "severity")})
    assert {(o["name"], o["column"]) for o in ops} == {("incident", "severity")}


def test_description_ops_prefers_comment_then_label_with_metadata_source():
    ops = M.description_ops(SYS_DICTIONARY, table_col="name", column_col="element",
                            label_col="column_label", comment_col="comments")
    by = {(o["name"], o["column"]): o for o in ops}
    assert by[("incident", "severity")]["value"] == "Severity level of the incident impact."
    assert by[("incident", "short_description")]["value"] == "Short description"  # label fallback
    assert ("incident", "blankcol") not in by                 # no text → no op
    assert all(o["source"] == "metadata" for o in ops)        # authoritative provenance


def test_reference_specs_extracts_only_reference_rows():
    specs = M.reference_specs(SYS_DICTIONARY, table_col="name", column_col="element",
                              type_col="internal_type", reference_col="reference")
    assert specs == [{"from_table": "incident", "from_column": "caller_id", "to_table": "sys_user"}]


def test_detect_preset_and_usable_sources():
    assert M.detect_preset(["incident", "sys_choice", "sys_user"]) == "servicenow"
    assert M.detect_preset(["orders", "customers"]) is None
    # only sys_choice present → only the choice role is usable
    usable = M.usable_sources("servicenow", ["incident", "sys_choice"])
    assert set(usable) == {"choice"}
    assert M.usable_sources("servicenow", ["incident", "sys_dictionary"]).keys() == {"dictionary"}


# --- integration: ops actually apply, metadata provenance persists ----------

def _incident_model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "itsm" / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "sn", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/itsm"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "Redshift"}))
    (root / "subject_areas" / "itsm" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "itsm",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "incident"}],
    }))
    (root / "subject_areas" / "itsm" / "tables" / "incident.yaml").write_text(yaml.safe_dump({
        "name": "incident", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "ServiceNow incident records",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "severity", "type": "integer"},
                    {"name": "short_description", "type": "string"}],
    }))


def test_generated_ops_apply_and_stamp_metadata(tmp_path):
    _incident_model(tmp_path)
    valid = {("incident", "severity"), ("incident", "short_description")}
    ops = (M.choice_field_ops(SYS_CHOICE, table_col="name", column_col="element",
                              value_col="value", label_col="label", valid=valid)
           + M.description_ops(SYS_DICTIONARY, table_col="name", column_col="element",
                               label_col="column_label", comment_col="comments", valid=valid))
    res = curate.apply(tmp_path, ops)
    assert res.validated and not res.errors, res.as_dict()
    assert len(res.applied) == 3

    org = loader.load_organization(tmp_path)
    incident = next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == "incident")
    cols = {c.name: c for c in incident.columns}
    assert cols["severity"].choice_field == {"1": "High", "2": "Medium", "3": "Low"}
    assert cols["severity"].description == "Severity level of the incident impact."
    assert cols["severity"].description_source == "metadata"          # authoritative, not a guess
    assert cols["short_description"].description == "Short description"


def test_route_references_intra_cross_and_skips_unmodelled():
    from semantic_model import cli, models as mm

    def tbl(name):
        return mm.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                        columns=[mm.Column(name="id", type="integer", primary_key=True)])
    itsm = mm.SubjectArea(name="itsm", description="d", tables_defined=[tbl("incident"), tbl("problem")])
    sysa = mm.SubjectArea(name="sys", description="d", tables_defined=[tbl("sys_user")])
    org = mm.Organization(organization="o", version=1, subject_areas=[itsm, sysa])
    specs = [
        {"from_table": "incident", "from_column": "problem_id", "to_table": "problem"},   # intra (itsm)
        {"from_table": "incident", "from_column": "caller_id", "to_table": "sys_user"},    # cross (itsm→sys)
        {"from_table": "incident", "from_column": "x", "to_table": "nope"},                # target unmodelled → skip
    ]
    intra, cross = cli._route_references(org, specs)
    assert [r["to_table"] for r in intra.get("itsm", [])] == ["problem"]
    assert "from_subject_area" not in intra["itsm"][0]                # plain intra-area edge
    assert intra["itsm"][0]["to_column"] == "id" and intra["itsm"][0]["relationship"] == "many_to_one"
    assert len(cross) == 1
    assert cross[0]["from_subject_area"] == "itsm" and cross[0]["to_subject_area"] == "sys"
    assert cross[0]["to_column"] == "id"


def _servicenow_model(root: Path) -> None:
    """`incident` (+ metadata tables) in the itsm area; `sys_user` in a SEPARATE sys area — so the
    dictionary's caller_id→sys_user reference is a CROSS-area edge, as it is in real ServiceNow."""
    (root / "datasources" / "c").mkdir(parents=True)
    for area in ("itsm", "sys"):
        (root / "subject_areas" / area / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "sn", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/itsm", "subject_areas/sys"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "Redshift"}))
    (root / "subject_areas" / "itsm" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "itsm",
        "tables": [{"storage_connection": "c", "schema": "public", "table": t}
                   for t in ("incident", "sys_choice", "sys_dictionary")],
    }))
    (root / "subject_areas" / "sys" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sys",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "sys_user"}],
    }))
    (root / "subject_areas" / "itsm" / "tables" / "incident.yaml").write_text(yaml.safe_dump({
        "name": "incident", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "incidents",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "severity", "type": "integer"},
                    {"name": "short_description", "type": "string"},
                    {"name": "caller_id", "type": "integer"}],
    }))
    (root / "subject_areas" / "sys" / "tables" / "sys_user.yaml").write_text(yaml.safe_dump({
        "name": "sys_user", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "users",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))
    for meta in ("sys_choice", "sys_dictionary"):
        (root / "subject_areas" / "itsm" / "tables" / f"{meta}.yaml").write_text(yaml.safe_dump({
            "name": meta, "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": meta,
            "columns": [{"name": "id", "type": "integer", "primary_key": True}],
        }))


def test_cmd_enrich_metadata_applies_from_canned_db(tmp_path, monkeypatch):
    _servicenow_model(tmp_path)
    from semantic_model import introspect as INTRO

    def fake_factory(profile, python=None):
        def run(sql):
            s = sql.lower()
            return SYS_CHOICE if "sys_choice" in s else SYS_DICTIONARY if "sys_dictionary" in s else []
        return run

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", fake_factory)
    from semantic_model import cli

    ns = cli.build_parser().parse_args(
        ["enrich-metadata", str(tmp_path), "--profile", "servicenow", "--db-type", "redshift"])
    assert ns.func(ns) == 0

    org = loader.load_organization(tmp_path)
    incident = next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == "incident")
    cols = {c.name: c for c in incident.columns}
    assert cols["severity"].choice_field == {"1": "High", "2": "Medium", "3": "Low"}
    assert cols["severity"].description_source == "metadata"
    # the caller_id→sys_user reference (cross-area, NOT <x>_id name-matchable) was written
    xrels = org.cross_subject_area_relationships
    edge = next((r for r in xrels if r.from_table == "incident" and r.to_table == "sys_user"), None)
    assert edge is not None and edge.from_column == "caller_id"
    assert edge.from_subject_area == "itsm" and edge.to_subject_area == "sys"
    assert edge.review_state == "unreviewed" and edge.confidence == "inferred"
