"""A table's identity in the served model is its catalog, schema and name, not its bare name (#258).

`model_table` was keyed (org, datasource, area, name), so two same-named tables in one subject area
landed on one key and the deploy failed, and `dataset_names` stripped every qualifier, so asking for
`public.orders` could return `archive.orders`. Migration 028 adds the schema and catalog to the key,
the lookups resolve by them, and a reply keys an ambiguous name by its qualified spelling.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import model_store  # noqa: E402
import tools  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402
from store import MIGRATIONS_DIR, Store  # noqa: E402


def _table(name: str, schema: str | None, *cols: str) -> m.Table:
    return m.Table(name=name, schema=schema, storage_connection="c", grain=["id"],
                   description=f"{schema}.{name}",
                   columns=[m.Column(name=c, type="integer") for c in ("id", *cols)])


def _two_schemas_org() -> m.Datasource:
    """`orders` under `public` and `archive`, in ONE subject area — what used to collide."""
    return m.Datasource(
        datasource="acme",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=[m.SubjectArea(name="sales", tables_defined=[
            _table("orders", "public", "amount"),
            _table("orders", "archive", "archived_at"),
            _table("customers", "public"),
        ])],
    )


def _store() -> Store:
    s = Store.connect("sqlite://")
    s.run_migrations()
    return s


# ---------------------------------------------------------------- storage


def test_the_key_carries_catalog_and_schema():
    s = _store()
    pk = sorted((r["pk"], r["name"]) for r in s.query("PRAGMA table_info(model_table)") if r["pk"])
    assert [n for _, n in pk] == ["org_id", "datasource", "area", "catalog_name", "schema_name",
                                  "name"]


def test_same_named_tables_in_one_area_round_trip():
    s = _store()
    model_store.write_datasource(s, "acme", _two_schemas_org())
    loaded = model_store.load_datasource(s, "acme")
    got = [(t.schema_name, t.name) for t in loaded.subject_areas[0].tables_defined]
    assert got == [("public", "customers"), ("archive", "orders"), ("public", "orders")]
    rows = s.query("SELECT schema_name, name FROM model_table ORDER BY name, schema_name")
    assert [(r["schema_name"], r["name"]) for r in rows] == [
        ("public", "customers"), ("archive", "orders"), ("public", "orders")]


def test_a_schema_embedded_in_the_name_is_stored_as_the_schema():
    s = _store()
    org = _two_schemas_org()
    org.subject_areas[0].tables_defined[2] = _table("public.customers", None)
    model_store.write_datasource(s, "acme", org)
    row = s.query("SELECT schema_name FROM model_table WHERE name = 'public.customers'")[0]
    assert row["schema_name"] == "public"


def test_the_migration_keeps_rows_written_under_the_old_key(tmp_path):
    """A deployment migrates with its model already stored: every row copies over, with '' for the
    parts the old key did not have, and still loads."""
    old = tmp_path / "old"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name < "028":
            shutil.copy(f, old / f.name)
    s = Store.connect("sqlite://")
    s.run_migrations(migrations_dir=old)
    doc = _table("orders", "public").model_dump(mode="json")
    s.execute("INSERT INTO datasource_model (org_id, datasource, description, doc) "
              "VALUES ('local', 'acme', NULL, ?)", (json.dumps({"datasource": "acme"}),))
    s.execute("INSERT INTO subject_area (org_id, datasource, name, description, default_time_window,"
              " table_count, doc) VALUES ('local', 'acme', 'sales', NULL, NULL, 1, ?)",
              (json.dumps({"name": "sales"}),))
    s.execute("INSERT INTO model_table (org_id, datasource, area, name, est_row_count, doc) "
              "VALUES ('local', 'acme', 'sales', 'orders', NULL, ?)", (json.dumps(doc),))
    s.commit()

    shutil.copy(MIGRATIONS_DIR / "028_model_table_identity.sql", old)
    assert s.run_migrations(migrations_dir=old) == ["028_model_table_identity.sql"]
    row = s.query("SELECT catalog_name, schema_name, name FROM model_table")[0]
    assert (row["catalog_name"], row["schema_name"], row["name"]) == ("", "", "orders")
    loaded = model_store.load_datasource(s, "acme")
    assert loaded.subject_areas[0].tables_defined[0].schema_name == "public"
    assert s.run_migrations(migrations_dir=old) == []


def test_declaring_datasources_match_the_schema_named():
    s = _store()
    model_store.write_datasource(s, "acme", _two_schemas_org())
    found = model_store.datasources_declaring(
        s, ["orders", "archive.orders", "staging.orders", "missing"])
    assert found == {"orders": ["acme"], "archive.orders": ["acme"]}


def test_a_row_stored_without_a_schema_matches_any_schema():
    """Rows from before 028 carry '' and cannot be told apart, so they still answer the hint."""
    s = _store()
    model_store.write_datasource(s, "acme", _two_schemas_org())
    s.execute("UPDATE model_table SET schema_name = '' WHERE name = 'customers'")
    assert model_store.datasources_declaring(s, ["staging.customers"]) == {
        "staging.customers": ["acme"]}


# ---------------------------------------------------------------- validator


def test_one_area_defining_a_table_twice_is_an_error():
    org = _two_schemas_org()
    org.subject_areas[0].tables_defined.append(_table("public.orders", None))
    res = V.validate(org)
    assert [f.code for f in res.findings if f.severity == "error"] == ["duplicate_table_identity"]


def test_two_schemas_of_one_name_in_one_area_validate():
    res = V.validate(_two_schemas_org())
    assert res.ok
    assert "table_name_in_multiple_schemas" in {f.code for f in res.findings}


def test_a_table_ref_with_a_schema_resolves_that_table():
    """`expose_column_groups` is checked against the table the ref names, not the last same-named
    one defined."""
    org = _two_schemas_org()
    pub, arc = org.subject_areas[0].tables_defined[:2]
    pub.column_groups = {"money": ["id", "amount"]}
    arc.column_groups = {"history": ["id", "archived_at"]}
    org.subject_areas.append(m.SubjectArea(name="finance", tables=[
        m.TableRef(storage_connection="c", schema="public", table="orders",
                   expose_column_groups=["money"])]))
    assert "unknown_column_group" not in {f.code for f in V.validate(org).findings}


# ---------------------------------------------------------------- dataset_names


def test_a_qualified_name_finds_that_schemas_table_and_never_the_other():
    org = _two_schemas_org()
    idx = L.build_table_index(org)
    for name, schema in (("public.orders", "public"), ("archive.orders", "archive")):
        assert [t.schema_name for t in L._find_tables(org, name)] == [schema]
        assert [t.schema_name for t in L._find_tables(org, name, index=idx)] == [schema]
    assert L._find_tables(org, "staging.orders") == []


def test_an_unqualified_name_under_two_schemas_returns_both():
    org = _two_schemas_org()
    org.subject_areas[0].tables_defined[1].caveats = ["rows before 2020 only"]
    ctx = L.get_table_context(org, ["orders"], include=["default_filters", "caveats"])
    assert sorted(ctx["tables"]) == ["archive.orders", "public.orders"]
    assert ctx["tables"]["archive.orders"]["schema"] == "archive"
    assert ctx["tables"]["archive.orders"]["caveats"] == ["rows before 2020 only"]
    assert "caveats" not in ctx["tables"]["public.orders"]


def test_an_unambiguous_name_is_keyed_as_before():
    ctx = L.get_table_context(_two_schemas_org(), ["customers", "public.customers"])
    assert list(ctx["tables"]) == ["customers"]


def test_an_area_prefix_still_resolves_a_lone_table():
    """`bare_name` has always stripped an area prefix as well as a schema one."""
    org = _two_schemas_org()
    assert [t.name for t in L._find_tables(org, "sales.customers")] == ["customers"]
    assert L._find_tables(org, "elsewhere.customers") == []


@pytest.fixture
def served(tmp_path, monkeypatch):
    """The two-schemas model served through the tool, from a store."""
    db = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(db)
    s.run_migrations()
    model_store.write_datasource(s, "acme", _two_schemas_org())
    s.close()
    monkeypatch.setenv("AGAMI_DB_URL", db)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "none"))
    tools._ORG_CACHE.clear()
    return "acme"


def _schema_reply(profile: str, **args) -> dict:
    out = tools.tool_get_datasource_schema({"datasource": profile, **args})
    return json.JSONDecoder().raw_decode(out)[0]


def test_the_tool_returns_the_table_the_qualified_name_names(served):
    reply = _schema_reply(served, dataset_names=["archive.orders"])
    assert list(reply["tables"]) == ["archive.orders"]
    assert [c["name"] for c in reply["tables"]["archive.orders"]["columns"]] == ["id", "archived_at"]


def test_a_wrong_schema_is_told_the_spellings_that_resolve(served):
    reply = _schema_reply(served, dataset_names=["staging.orders", "customers"])
    entry = reply["tables"]["staging.orders"]
    assert entry["error"] == "not found in scope"
    assert "'public.orders'" in entry["hint"] and "'archive.orders'" in entry["hint"]
