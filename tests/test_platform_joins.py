"""Reference joins on a platform exported as renamed views (ACE-181).

End to end where the defect lived: a SQLite warehouse whose platform tables reach it as renamed views
(`acme_task_v`, `person_v`, ...) with link columns `<field>_sys_id`, and whose dictionary, choice
and class tables sit beside them under a raw-layer suffix, not in the model. The real `sm` CLI runs
`introspect` and then `enrich-metadata` through the real `execute_sql.py` child, so the model-scope
gate that refused the dictionary is in the path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import introspect as I  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import metadata_sources as M  # noqa: E402

VIEWS = ["person_v", "team_v", "config_item_v", "acme_cmdb_ci_service_v", "acme_task_v",
         "acme_incident_v", "audit_v", "widget_v"]


def sid(kind: str, n: int) -> str:
    """A platform record id: 32 hex characters, unique across every class like the real ones."""
    return hashlib.md5(f"{kind}{n}".encode()).hexdigest()


U = [sid("user", i) for i in range(20)]
G = [sid("group", i) for i in range(5)]
C = [sid("ci", i) for i in range(12)]          # C[8:] are services too (a child class)
T = [sid("task", i) for i in range(30)]        # T[20:] are incidents too (a child class)
JUNK = [sid("junk", i) for i in range(30)]

DICTIONARY = [  # (class, field, type, reference)
    ("task", "number", "string", ""), ("task", "short_description", "string", ""),
    ("task", "assigned_to", "reference", "sys_user"), ("task", "assignment_group", "reference", "sys_user_group"),
    ("task", "parent", "reference", "task"), ("task", "cmdb_ci", "reference", "cmdb_ci"),
    ("task", "requested_by", "reference", "sys_user"), ("task", "opened_by", "reference", "sys_user"),
    ("incident", "severity", "integer", ""), ("incident", "state", "string", ""),
    ("incident", "business_service", "reference", "cmdb_ci_service"),
    ("sys_user", "user_name", "string", ""), ("sys_user", "title", "string", ""),
    ("sys_user", "manager", "reference", "sys_user"), ("sys_user", "active", "boolean", ""),
    ("sys_user_group", "name", "string", ""), ("sys_user_group", "description", "string", ""),
    ("sys_user_group", "manager", "reference", "sys_user"),
    # `parent` points at a different class here than on task: keyed by field alone it was dropped
    ("sys_user_group", "parent", "reference", "sys_user_group"),
    ("cmdb_ci", "name", "string", ""), ("cmdb_ci", "serial_number", "string", ""),
    ("cmdb_ci", "owned_by", "reference", "sys_user"),
    ("cmdb_ci_service", "busines_criticality", "string", ""),
]
# bookkeeping fields every class declares, and a tiny class that has nothing else
AUDIT = ("sys_created_on", "sys_created_by", "sys_updated_by")
DICTIONARY += [(cls, f, "string", "") for cls in ("task", "incident", "sys_user", "sys_user_group", "cmdb_ci",
                                                   "cmdb_ci_service", "sys_tag") for f in AUDIT]
CLASSES = [("task", "c1", ""), ("incident", "c2", "c1"), ("sys_user", "c3", ""), ("sys_user_group", "c4", ""),
           ("cmdb_ci", "c5", ""), ("cmdb_ci_service", "c6", "c5"), ("sys_tag", "c7", "")]
CHOICES = [("incident", "state", "new", "New"), ("incident", "state", "closed", "Closed"),
           ("incident", "severity", "1", "High"), ("incident", "severity", "2", "Low")]


def _warehouse(db: Path, spelling: str = "plain") -> None:
    """`spelling="value"` writes the metadata the way some exports write every reference field: a
    `<field>_value` / `<field>_link` pair, with a reference's target held as the class's record id."""
    con = sqlite3.connect(db)
    ex = con.execute
    ex("CREATE TABLE person_v(sys_id TEXT PRIMARY KEY, user_name TEXT, title TEXT, manager_sys_id TEXT, "
       "team_sys_id TEXT, active INTEGER)")
    for i, u in enumerate(U):
        ex("INSERT INTO person_v VALUES (?,?,?,?,?,1)", (u, f"user{i}", "Analyst", U[i // 5 * 5], G[i % 5]))
    ex("CREATE TABLE team_v(sys_id TEXT PRIMARY KEY, name TEXT, description TEXT, manager_sys_id TEXT, "
       "parent_sys_id TEXT)")
    for i, g in enumerate(G):
        ex("INSERT INTO team_v VALUES (?,?,?,?,?)", (g, f"team{i}", "A team", U[i], G[0] if i else None))
    ex("CREATE TABLE config_item_v(sys_id TEXT PRIMARY KEY, name TEXT, serial_number TEXT, owned_by_sys_id TEXT)")
    for i, c in enumerate(C):
        ex("INSERT INTO config_item_v VALUES (?,?,?,?)", (c, f"ci{i}", f"SN{i}", U[i]))
    ex("CREATE TABLE acme_cmdb_ci_service_v(sys_id TEXT PRIMARY KEY, name TEXT, busines_criticality TEXT)")
    for i, c in enumerate(C[8:]):
        ex("INSERT INTO acme_cmdb_ci_service_v VALUES (?,?,?)", (c, f"service{i}", "high"))
    ex("CREATE TABLE acme_task_v(sys_id TEXT PRIMARY KEY, number TEXT, short_description TEXT, "
       "assigned_to_sys_id TEXT, assignment_group_sys_id TEXT, parent_sys_id TEXT, cmdb_ci_sys_id TEXT, "
       "approver_sys_id TEXT, requested_by_sys_id TEXT)")
    for i, t in enumerate(T):
        ex("INSERT INTO acme_task_v VALUES (?,?,?,?,?,?,?,?,?)",
           (t, f"TASK{i}", "Do it", U[i % 20], G[i % 5], T[i - 1] if i and i % 3 == 0 else None, C[i % 12],
            U[(i + 3) % 20], U[i % 20] if i < 18 else JUNK[i]))          # requested_by: 60% match
    ex("CREATE TABLE acme_incident_v(sys_id TEXT PRIMARY KEY, number TEXT, assigned_to_sys_id TEXT, "
       "assignment_group_sys_id TEXT, opened_by_sys_id TEXT, business_service_sys_id TEXT, "
       "affected_item_sys_id TEXT, severity TEXT, state TEXT)")
    for i, t in enumerate(T[20:]):
        ex("INSERT INTO acme_incident_v VALUES (?,?,?,?,?,?,?,?,?)",
           (t, f"INC{i}", U[i], G[i % 5], U[i] if i < 8 else JUNK[i],    # opened_by: 80% match
            C[8 + i % 4], C[8 + i % 4], "1" if i % 2 else "2", "NEW" if i % 2 else "Closed"))
    # a decoy: its own ids coincide with people's, but it is not a class of person
    ex("CREATE TABLE audit_v(sys_id TEXT PRIMARY KEY, note TEXT)")
    for u in U:
        ex("INSERT INTO audit_v VALUES (?, 'seen')", (u,))
    # a custom table: it shares only the bookkeeping columns with any class
    ex("CREATE TABLE widget_v(sys_id TEXT PRIMARY KEY, widget_name TEXT, sys_created_on TEXT, "
       "sys_created_by TEXT, sys_updated_by TEXT)")
    ex("INSERT INTO widget_v VALUES ('w1', 'dial', '2026-01-01', 'admin', 'admin')")
    # decoys a name match alone would take for the choice table: a second `_set` layer
    ex("CREATE TABLE sys_choice_set(name TEXT, sys_id TEXT)")
    ex("CREATE TABLE audit_set(id TEXT)")
    # the platform's own metadata, under the raw layer's suffix and never modelled
    if spelling == "value":
        class_id = {name: i for name, i, _ in CLASSES}
        ex("CREATE TABLE sys_dictionary_raw(name TEXT, element TEXT, column_label TEXT, comments TEXT, "
           "internal_type_value TEXT, internal_type_link TEXT, reference_value TEXT, reference_link TEXT, "
           "_fivetran_synced TEXT)")
        for cls, field, typ, ref in DICTIONARY:
            ex("INSERT INTO sys_dictionary_raw VALUES (?,?,?,?,?,?,?,?,?)",
               (cls, field, field.replace("_", " ").title(), f"The {field.replace('_', ' ')}.", typ,
                f"https://your-instance.example.com/api/{typ}", class_id.get(ref, ref),
                f"https://your-instance.example.com/api/{ref}", "2026-01-01"))
        ex("CREATE TABLE sys_db_object_raw(name TEXT, sys_id TEXT, super_class_value TEXT, super_class_link TEXT)")
        con.executemany("INSERT INTO sys_db_object_raw VALUES (?,?,?,'')", CLASSES)
    else:
        ex("CREATE TABLE sys_dictionary_raw(name TEXT, element TEXT, column_label TEXT, comments TEXT, "
           "internal_type TEXT, reference TEXT)")
        for cls, field, typ, ref in DICTIONARY:
            ex("INSERT INTO sys_dictionary_raw VALUES (?,?,?,?,?,?)",
               (cls, field, field.replace("_", " ").title(), f"The {field.replace('_', ' ')}.", typ, ref))
        ex("CREATE TABLE sys_db_object_raw(name TEXT, sys_id TEXT, super_class TEXT)")
        con.executemany("INSERT INTO sys_db_object_raw VALUES (?,?,?)", CLASSES)
    ex("CREATE TABLE sys_choice_raw(name TEXT, element TEXT, value TEXT, label TEXT)")
    con.executemany("INSERT INTO sys_choice_raw VALUES (?,?,?,?)", CHOICES)
    con.commit()
    con.close()


def _sm(art: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "semantic_model.cli", *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(PKG_SRC),
        env={**os.environ, "PYTHONPATH": str(PKG_SRC), "AGAMI_ARTIFACTS_DIR": str(art)},
    )


def _platform(tmp: Path, spelling: str = "plain") -> dict:
    db = tmp / "wh.db"
    _warehouse(db, spelling)
    art = tmp / "art"
    (art / "local").mkdir(parents=True)
    creds = art / "local" / "credentials"
    creds.write_text(f"[wh]\ntype = sqlite\npath = {db}\n", encoding="utf-8")
    creds.chmod(0o600)
    return {"db": db, "art": art, "root": art / "wh"}


@pytest.fixture(scope="module", params=["plain", "value"])
def built(request, tmp_path_factory) -> dict:
    """Introspect the views alone, then enrich from the platform's metadata: once per spelling of the
    metadata, and every claim below holds for both."""
    wh = _platform(tmp_path_factory.mktemp("platform"), request.param)
    intro = _sm(wh["art"], "introspect", "--profile", "wh", "--db-type", "sqlite",
                "--artifacts", str(wh["art"]), "--tables", *VIEWS)
    assert intro.returncode == 0, intro.stdout + intro.stderr
    enrich = _sm(wh["art"], "enrich-metadata", str(wh["root"]), "--profile", "wh", "--db-type", "sqlite")
    assert enrich.returncode == 0, enrich.stdout + enrich.stderr
    org = L.load_datasource(wh["root"])
    doc = json.loads((wh["root"] / ".introspect" / "join_candidates.json").read_text(encoding="utf-8"))
    return {**wh, "out": json.loads(enrich.stdout), "org": org,
            "rels": {(r.from_table, r.from_column, r.to_table): r
                     for sa in org.subject_areas for r in sa.relationships},
            "review": {c["id"]: c for c in doc["candidates"] if "review_reason" in c},
            "candidates": {c["id"] for c in doc["candidates"]}}


PLANTED = {
    ("acme_task_v", "assigned_to_sys_id", "person_v"), ("acme_task_v", "assignment_group_sys_id", "team_v"),
    ("acme_task_v", "cmdb_ci_sys_id", "config_item_v"), ("acme_task_v", "parent_sys_id", "acme_task_v"),
    # declared on the parent class only: the child's view inherits them
    ("acme_incident_v", "assigned_to_sys_id", "person_v"), ("acme_incident_v", "assignment_group_sys_id", "team_v"),
    ("acme_incident_v", "business_service_sys_id", "acme_cmdb_ci_service_v"),
    # these three tables map by their fields, not their names
    ("person_v", "manager_sys_id", "person_v"), ("team_v", "manager_sys_id", "person_v"),
    ("team_v", "parent_sys_id", "team_v"), ("config_item_v", "owned_by_sys_id", "person_v"),
}


# borne out but not declared: written usable and unreviewed, approved by use
INFERRED = {
    ("acme_incident_v", "sys_id", "acme_task_v"),                  # a child record is its parent class's
    ("acme_cmdb_ci_service_v", "sys_id", "config_item_v"),
    ("person_v", "team_sys_id", "team_v"),                         # by name
}


def test_every_borne_out_reference_is_written_and_only_declared_ones_approved(built):
    assert set(built["rels"]) == PLANTED | INFERRED
    for k, r in built["rels"].items():
        assert r.to_column == "sys_id"
        if k in PLANTED:
            assert (r.confidence, r.review_state, r.signed_off_role) == ("confirmed", "approved", "system")
        else:
            assert (r.confidence, r.review_state, r.signed_off_by) == ("inferred", "unreviewed", None)
    assert built["out"]["relationships_added"] == len(PLANTED | INFERRED)
    assert built["out"]["relationships_approved"] == len(PLANTED)


def test_a_field_two_classes_point_at_different_targets_is_kept_for_each(built):
    # `parent` is task -> task and group -> group. Keyed by field name alone the two declarations
    # conflicted and both were dropped: the self and parent references a build lost.
    assert ("team_v", "parent_sys_id", "team_v") in built["rels"]
    assert ("acme_task_v", "parent_sys_id", "acme_task_v") in built["rels"]


def test_only_what_a_person_must_settle_goes_to_review_with_its_reason(built):
    reasons = {i: c["review_reason"] for i, c in built["review"].items()}
    assert set(reasons) == {
        "acme_incident_v.opened_by_sys_id->person_v.sys_id",                  # declared, but 80%
        "acme_incident_v.affected_item_sys_id->acme_cmdb_ci_service_v.sys_id",  # a second link, same tables
        "acme_task_v.approver_sys_id->person_v.sys_id",                       # by values, two unrelated tables
        "acme_task_v.approver_sys_id->audit_v.sys_id",
    }, reasons
    assert reasons["acme_incident_v.opened_by_sys_id->person_v.sys_id"].startswith("partial match (80%")
    assert reasons["acme_incident_v.affected_item_sys_id->acme_cmdb_ci_service_v.sys_id"] == (
        "found by values; another link joins the same two tables")
    assert reasons["acme_task_v.approver_sys_id->audit_v.sys_id"].startswith("points at several tables")
    # the item it also matches is the service's parent class: the most specific wins, alone
    assert "acme_incident_v.affected_item_sys_id->config_item_v.sys_id" not in built["candidates"]
    assert built["out"]["relationships_for_review"] == len(reasons)


def test_decoys_are_neither_written_nor_offered(built):
    everything = set(built["rels"]) | {tuple(i.replace(".sys_id", "").split("->")) for i in built["candidates"]}
    # two tables' own ids coincide, but neither is a class of the other
    assert not [i for i in built["candidates"] if i.startswith(("audit_v.sys_id", "person_v.sys_id"))]
    # declared by the dictionary, but only 60% of rows find a partner
    assert not [k for k in everything if "requested_by" in str(k)]


def test_a_table_sharing_only_bookkeeping_fields_maps_to_no_class(built):
    assert built["out"]["tables_mapped_to_platform"] == {
        "person_v": "sys_user", "team_v": "sys_user_group", "config_item_v": "cmdb_ci",
        "acme_cmdb_ci_service_v": "cmdb_ci_service", "acme_task_v": "task", "acme_incident_v": "incident"}


def test_the_metadata_is_read_where_it_lies_and_never_modelled(built):
    tables = {t.name for sa in built["org"].subject_areas for t in sa.tables_defined}
    assert tables == set(VIEWS)
    assert built["out"]["fetched"] == {"choice": len(CHOICES), "dictionary": len(DICTIONARY), "hierarchy": len(CLASSES)}


def test_reading_the_unmodelled_dictionary_needs_build_mode(built, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(built["art"]))
    with pytest.raises(I.ExecuteSqlFailure) as refused:
        I.make_execute_sql_runner("wh")("SELECT name FROM sys_dictionary_raw")
    assert refused.value.refusal_rule
    assert I.make_execute_sql_runner("wh", building_the_model=True)("SELECT name FROM sys_dictionary_raw")


def test_renamed_columns_get_the_dictionary_description_and_labels_ignoring_case(built):
    inc = next(t for sa in built["org"].subject_areas for t in sa.tables_defined if t.name == "acme_incident_v")
    cols = {c.name: c for c in inc.columns}
    assert cols["assigned_to_sys_id"].description == "The assigned to."
    assert cols["state"].choice_field == {"NEW": "New", "Closed": "Closed"}
    assert cols["severity"].choice_field == {"1": "High", "2": "Low"}


# --- the pure pieces -----------------------------------------------------------------------------


def test_a_layer_s_words_are_read_off_the_names():
    pre, suf = M.common_affixes(["acme_task_v", "acme_user_v", "group_v", "orders"], min_share=0.25)
    assert (pre, suf) == ({"acme"}, {"v"})
    assert M.stems("acme_task_v", pre, suf) == ["acme_task_v", "acme_task", "task_v", "task"]


def test_a_source_is_found_under_a_renamed_layer_in_any_schema():
    catalog = [("raw", "sys_dictionary_raw"), ("raw", "sys_choice_raw"), ("app", "task_v")]
    assert M.locate("sys_dictionary", catalog) == ("raw", "sys_dictionary_raw")
    assert M.locate("sys_db_object", catalog) is None
    # its own name beats a renamed copy; two copies under one name go to the schema the model reads
    assert M.locate("sys_choice", [*catalog, ("app", "sys_choice")]) == ("app", "sys_choice")
    two = [("a", "sys_choice"), ("b", "sys_choice")]
    assert M.locate("sys_choice", two) is None
    assert M.locate("sys_choice", two, ["b"]) == ("b", "sys_choice")
    # a table that has the name but not the columns the role reads is not the source
    layered = [("raw", "sys_choice_raw"), ("raw", "sys_choice_set"), ("raw", "x_raw"), ("raw", "y_set")]
    assert M.locate("sys_choice", layered) is None
    assert M.locate("sys_choice", layered, fits=lambda s, t: t != "sys_choice_set") == ("raw", "sys_choice_raw")


def test_a_table_maps_by_its_fields_only_with_a_clear_lead():
    fields = {"user": {"user_name", "title", "manager"}, "group": {"name", "manager", "parent"}}
    cols = {"person_v": {"sys_id", "user_name", "title", "manager_sys_id"},
            "unit_v": {"sys_id", "name", "manager_sys_id"}}
    out = M.map_tables(cols, fields, {}, key="sys_id", link_suffix="_sys_id")
    assert out["person_v"]["class"] == "user" and out["person_v"]["by"] == "fields"
    assert "unit_v" not in out                                  # two shared fields is not enough
    # fields most classes declare (bookkeeping) say nothing about which class a table is
    audit = {"created_on", "created_by", "updated_by"}
    classes = {"user": {"user_name", *audit}, "group": {"name", *audit}, "tag": set(audit)}
    assert M.map_tables({"widget_v": {"sys_id", "widget_name", *audit}}, classes, {},
                        key="sys_id", link_suffix="_sys_id") == {}
    tie = {"a": {"x", "y", "z"}, "b": {"x", "y", "z"}}
    assert M.map_tables({"t_v": {"x", "y", "z"}}, tie, {}, key="sys_id", link_suffix="_sys_id") == {}


def test_a_role_column_is_found_under_the_export_s_value_spelling():
    cfg = {"source": "sys_dictionary", "table_col": "name", "column_col": "element", "reference_col": "reference"}
    sfx = M.PRESETS["servicenow"]["column_suffixes"]
    assert M.resolve_columns(cfg, ["NAME", "element", "reference_value", "reference_link"], sfx) == {
        "name": "NAME", "element": "element", "reference": "reference_value"}
    # the exact name wins over the value half, and a column missing under every spelling is no fit
    assert M.resolve_columns(cfg, ["name", "element", "reference", "reference_value"], sfx)["reference"] == "reference"
    assert M.resolve_columns(cfg, ["name", "element", "reference_link"], sfx) is None
    assert M.resolve_columns(cfg, ["name", "element", "reference_value"]) is None    # no suffixes, no alias
    # the choice table's decoy has neither spelling of the choice role's columns
    choice = M.PRESETS["servicenow"]["choice"]
    assert M.resolve_columns(choice, ["name", "sys_id", "name_value"], sfx) is None


def test_a_reference_held_as_a_record_id_reads_as_that_record_s_name():
    names = M.record_names([{"name": "sys_user", "sys_id": "c3"}, {"name": "", "sys_id": "c9"}],
                           M.PRESETS["servicenow"]["hierarchy"])
    assert names == {"c3": "sys_user"}
    rows = [{"reference": "c3"}, {"reference": "cmn_location"}, {"reference": "c9"}, {"reference": None}]
    assert M.resolve_record_ids(rows, "reference", names) == [
        {"reference": "sys_user"}, {"reference": "cmn_location"}, {"reference": "c9"}, {"reference": None}]
    assert M.resolve_record_ids(rows, "reference", {}) is rows
    assert M.record_names([{"name": "reference", "sys_id": "t1"}], M.PRESETS["servicenow"]["field_type"]) == {
        "t1": "reference"}


def test_class_parents_resolve_ids_and_stop_on_a_loop():
    rows = [{"name": "task", "sys_id": "1", "super_class": "3"}, {"name": "incident", "sys_id": "2", "super_class": "1"},
            {"name": "base", "sys_id": "3", "super_class": "2"}]
    parents = M.class_parents(rows, table_col="name", id_col="sys_id", parent_col="super_class")
    assert parents["incident"] == "task"
    assert M.ancestors("incident", parents) == ["task", "base"]
