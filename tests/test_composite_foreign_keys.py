"""A foreign key over several columns is one join on all of them (#510).

Introspection used to write each column of a composite key as its own approved many-to-one join, and
either one used alone repeats rows. These cover the catalog queries (every dialect returns the
constraint name and column position, and none pairs a column with the wrong referenced column), the
grouping, the refresh that replaces the old split joins, the receipt's warning on a join written on
part of the key, and the places that read only `from_column`/`to_column`.
"""

from __future__ import annotations

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

from semantic_model import cli  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as R  # noqa: E402
from semantic_model import validator as V  # noqa: E402
from semantic_model.sql_dialect import sqlglot_dialect  # noqa: E402

_SCHEMA = (
    "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE order_lines(order_id INTEGER NOT NULL, line_no INTEGER NOT NULL, product TEXT, qty INTEGER,"
    " PRIMARY KEY(order_id, line_no));"
    "CREATE TABLE shipments(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),"
    " order_id INTEGER, line_no INTEGER, shipped_qty INTEGER,"
    " FOREIGN KEY(order_id, line_no) REFERENCES order_lines(order_id, line_no));"
    "INSERT INTO customers VALUES (1, 'a'), (2, 'b');"
    "INSERT INTO order_lines VALUES (1, 1, 'p', 2), (1, 2, 'q', 3), (2, 1, 'p', 4);"
    "INSERT INTO shipments VALUES (1, 1, 1, 1, 2), (2, 1, 1, 2, 3), (3, 2, 2, 1, 4);"
)
_COMPOSITE_ON = "shipments.order_id = order_lines.order_id AND shipments.line_no = order_lines.line_no"


def _sqlite_runner(db: Path):
    def run(sql: str) -> list[dict]:
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            con.close()
    return run


@pytest.fixture
def shop_db(tmp_path) -> Path:
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return db


def _rels(org: m.Datasource) -> list[m.Relationship]:
    return [r for sa in org.subject_areas for r in sa.relationships] + list(org.cross_subject_area_relationships)


def _between(org: m.Datasource, ft: str, tt: str) -> list[m.Relationship]:
    return [r for r in _rels(org) if r.from_table == ft and r.to_table == tt]


# --- the catalog queries ----------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["postgres", "supabase", "redshift", "mysql", "snowflake", "duckdb",
                                 "databricks", "trino", "sqlserver", "bigquery", "oracle"])
def test_every_fk_query_returns_the_key_name_and_column_position(key):
    sql = D.get_dialect(key).sql_foreign_keys("s")
    assert "AS constraint_name" in sql and "AS key_ordinal" in sql
    # the schema of both ends too: a key is grouped per (from_schema, from_table, constraint_name), so
    # same-named keys on same-named tables in two schemas would fuse without it (Trino lists none)
    if key != "trino":
        assert "AS from_schema" in sql and "AS to_schema" in sql
    if key == "bigquery":
        assert "kcu.table_schema AS from_schema" in sql and "ccu.table_schema AS to_schema" in sql
    if key == "oracle":
        assert "ac.owner AS from_schema" in sql and "rcc.owner AS to_schema" in sql


def _bigquery_catalog() -> sqlite3.Connection:
    """BigQuery's dataset `d1` INFORMATION_SCHEMA as canned rows: a composite key into its own
    `order_lines`, a single-column key into `d2.customers`, a composite key into `d2.parts`, and a
    local `d1.parts` whose own key must not lend `d2.parts` its positions."""
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE TABLE_CONSTRAINTS(constraint_name, table_schema, table_name, constraint_type);"
        "CREATE TABLE KEY_COLUMN_USAGE(constraint_name, table_schema, table_name, column_name,"
        " ordinal_position, position_in_unique_constraint);"
        "CREATE TABLE CONSTRAINT_COLUMN_USAGE(constraint_name, table_schema, table_name, column_name);")
    tc = [("pk_ol", "d1", "order_lines", "PRIMARY KEY"), ("pk_parts", "d1", "parts", "PRIMARY KEY"),
          ("fk_lines", "d1", "shipments", "FOREIGN KEY"), ("fk_cust", "d1", "shipments", "FOREIGN KEY"),
          ("fk_parts", "d1", "shipments", "FOREIGN KEY")]
    kcu = [("pk_ol", "d1", "order_lines", "order_id", 1, None), ("pk_ol", "d1", "order_lines", "line_no", 2, None),
           ("pk_parts", "d1", "parts", "b", 1, None), ("pk_parts", "d1", "parts", "a", 2, None),
           ("fk_lines", "d1", "shipments", "order_id", 1, 1), ("fk_lines", "d1", "shipments", "line_no", 2, 2),
           ("fk_cust", "d1", "shipments", "customer_id", 1, 1),
           ("fk_parts", "d1", "shipments", "part_a", 1, 1), ("fk_parts", "d1", "shipments", "part_b", 2, 2)]
    ccu = [("fk_lines", "d1", "order_lines", "order_id"), ("fk_lines", "d1", "order_lines", "line_no"),
           ("fk_cust", "d2", "customers", "id"),
           ("fk_parts", "d2", "parts", "a"), ("fk_parts", "d2", "parts", "b")]
    con.executemany("INSERT INTO TABLE_CONSTRAINTS VALUES (?,?,?,?)", tc)
    con.executemany("INSERT INTO KEY_COLUMN_USAGE VALUES (?,?,?,?,?,?)", kcu)
    con.executemany("INSERT INTO CONSTRAINT_COLUMN_USAGE VALUES (?,?,?,?)", ccu)
    return con


def test_bigquery_keeps_cross_dataset_single_keys_and_pairs_local_composite_keys_by_position():
    sql = D.get_dialect("bigquery").sql_foreign_keys("d1").replace("`d1`.INFORMATION_SCHEMA.", "")
    cur = _bigquery_catalog().execute(sql)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]

    def of(to_table):
        return sorted((r["from_column"], r["to_schema"], r["to_column"], r["constraint_name"])
                      for r in rows if r["to_table"] == to_table)

    assert of("order_lines") == [("line_no", "d1", "line_no", "fk_lines"), ("order_id", "d1", "order_id", "fk_lines")]
    # a single-column key into another dataset is kept exactly as the name-only pairing gave it
    assert of("customers") == [("customer_id", "d2", "id", "fk_cust")]
    # a composite key into another dataset has no positions here: its rows come back unnamed, so
    # they are left ungrouped and unreviewed rather than paired by guesswork
    parts = [r for r in rows if r["to_table"] == "parts"]
    assert len(parts) == 4 and all(r["constraint_name"] is None for r in parts)
    assert all(r["from_schema"] == "d1" for r in rows)
    groups = I._fk_groups(rows)
    assert [grouped for g, grouped in groups if g[0]["to_table"] == "parts"] == [False] * 4
    assert [len(g) for g, grouped in groups if g[0]["to_table"] == "order_lines"] == [2]


def test_sqlite_fk_query_returns_the_key_name_and_column_position():
    sql = D.get_dialect("sqlite").sql_foreign_keys_for_table("t")
    assert "id AS constraint_name" in sql and "seq AS key_ordinal" in sql


@pytest.mark.parametrize("key", ["redshift", "duckdb", "databricks", "bigquery"])
def test_the_referenced_column_is_matched_by_position(key):
    # Joined on the constraint name alone, constraint_column_usage crossed a composite key's columns.
    assert "position_in_unique_constraint" in D.get_dialect(key).sql_foreign_keys("s")


def test_sql_server_pairs_columns_from_its_own_catalog():
    sql = D.get_dialect("sqlserver").sql_foreign_keys("dbo")
    assert "sys.foreign_key_columns" in sql and "referenced_column_id" in sql


def test_the_generic_query_pairs_a_composite_key_correctly_on_a_real_duckdb():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE order_lines(order_id INT, line_no INT, PRIMARY KEY(order_id, line_no))")
    con.execute("CREATE TABLE shipments(id INT PRIMARY KEY, order_id INT, line_no INT,"
                " FOREIGN KEY(order_id, line_no) REFERENCES order_lines(order_id, line_no))")
    cur = con.execute(D.get_dialect("duckdb").sql_foreign_keys("main"))
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    assert [(r["from_column"], r["to_table"], r["to_column"], r["key_ordinal"]) for r in rows] == [
        ("order_id", "order_lines", "order_id", 1), ("line_no", "order_lines", "line_no", 2)]
    assert len({r["constraint_name"] for r in rows}) == 1


# --- grouping ---------------------------------------------------------------------------------------


def _tables() -> list[m.Table]:
    def t(name: str, cols: list[str], grain: list[str]) -> m.Table:
        return m.Table(name=name, schema_name="s", storage_connection="c", grain=grain,
                       columns=[m.Column(name=c, type="integer") for c in cols])
    return [t("order_lines", ["order_id", "line_no", "qty"], ["order_id", "line_no"]),
            t("shipments", ["id", "order_id", "line_no", "customer_id", "shipped_qty"], ["id"]),
            t("customers", ["id"], ["id"])]


def _fk_rows(named: bool) -> list[dict]:
    def row(fc: str, tt: str, tc: str, name: str, pos: int) -> dict:
        r = {"from_table": "shipments", "from_column": fc, "from_schema": "s",
             "to_table": tt, "to_column": tc, "to_schema": "s"}
        return {**r, "constraint_name": name, "key_ordinal": pos} if named else r
    # The second column first: the key's order comes from key_ordinal, not from row order.
    return [row("line_no", "order_lines", "line_no", "fk_lines", 2),
            row("customer_id", "customers", "id", "fk_cust", 1),
            row("order_id", "order_lines", "order_id", "fk_lines", 1)]


def _build(key: str, rows: list[dict]) -> tuple[list[m.Relationship], I.IntrospectReport]:
    dialect = D.get_dialect(key)
    tables = _tables()
    fk_sql = {dialect.sql_foreign_keys_for_table("shipments") if key == "sqlite" else dialect.sql_foreign_keys("s")}

    def run(sql: str) -> list[dict]:
        return [dict(r) for r in rows] if sql in fk_sql else []

    report = I.IntrospectReport(profile="p", db_type=key, out_dir="", dry_run=True)
    pairs = [("shipments", "shipments")] if key == "sqlite" else [("s", t.name) for t in tables]
    return I._build_relationships(dialect, run, pairs, tables, report), report


@pytest.mark.parametrize("key", ["postgres", "redshift", "mysql", "snowflake", "duckdb", "databricks",
                                 "sqlserver", "bigquery", "oracle", "sqlite"])
def test_a_two_column_key_is_one_join_with_both_pairs(key):
    rels, _ = _build(key, _fk_rows(named=True))
    lines = [r for r in rels if r.to_table == "order_lines"]
    assert len(lines) == 1
    (rel,) = lines
    assert rel.from_column is None and rel.on
    assert R.join_key_pairs(rel, sqlglot_dialect(D.get_dialect(key).name)) == [
        ("order_id", "order_id"), ("line_no", "line_no")]
    if key not in ("snowflake", "oracle"):   # they fold an unquoted name to upper case, so it's quoted there
        assert rel.on == _COMPOSITE_ON
    assert rel.relationship == "many_to_one"     # the whole key is order_lines' grain
    (cust,) = [r for r in rels if r.to_table == "customers"]
    assert (cust.from_column, cust.to_column, cust.on) == ("customer_id", "id", None)
    if D.get_dialect(key).fk_enforced:
        assert rel.review_state == "approved" and rel.signed_off_by == "agami_introspect"


def test_an_unenforced_composite_key_is_confirmed_with_a_probe_on_every_column():
    dialect = D.get_dialect("redshift")
    tables = {t.name: t for t in _tables()}
    sql = I.overlap_sql(dialect, tables["shipments"], ["order_id", "line_no"], tables["order_lines"],
                        ["order_id", "line_no"])
    assert 't."order_id" = src.v0 AND t."line_no" = src.v1' in sql
    assert '"order_id" IS NOT NULL AND "line_no" IS NOT NULL' in sql
    # a single column keeps the probe it always had
    assert "AS v FROM" in I.overlap_sql(dialect, tables["shipments"], "order_id", tables["order_lines"], "order_id")


def test_rows_without_a_key_name_are_kept_apart_and_left_for_review():
    rels, report = _build("postgres", _fk_rows(named=False))
    lines = [r for r in rels if r.to_table == "order_lines"]
    assert sorted(r.from_column for r in lines) == ["line_no", "order_id"]
    assert all(r.review_state == "unreviewed" and r.signed_off_by is None for r in lines)
    assert all("couldn't be grouped" in r.description for r in lines)
    # the only key between its two tables is single-column whatever the catalog says: as today
    (cust,) = [r for r in rels if r.to_table == "customers"]
    assert cust.review_state == "approved"
    assert any("belong to one key" in n for n in report.notes)


def test_a_real_sqlite_database_gets_one_join_for_its_composite_key(shop_db, tmp_path):
    org, _ = I.introspect("shop", "sqlite", runner=_sqlite_runner(shop_db), artifacts_dir=tmp_path)
    assert V.validate(org).ok
    (rel,) = _between(org, "shipments", "order_lines")
    assert rel.on == _COMPOSITE_ON and rel.review_state == "approved"
    (cust,) = _between(org, "shipments", "customers")
    assert (cust.from_column, cust.to_column) == ("customer_id", "id")
    # no single-column reference join is promoted for a column the composite key already covers
    assert not [r for r in _rels(org) if r.from_column in ("order_id", "line_no")]


# --- the shared pair reader -------------------------------------------------------------------------


def _on(text: str, ft: str = "shipments", tt: str = "order_lines") -> m.Relationship:
    return m.Relationship(from_table=ft, to_table=tt, on=text, relationship="many_to_one")


def test_join_key_pairs_reads_both_forms():
    pair = m.Relationship(from_table="a", to_table="b", from_column="b_id", to_column="id",
                          relationship="many_to_one")
    assert R.join_key_pairs(pair) == [("b_id", "id")]
    assert R.join_key_pairs(_on(_COMPOSITE_ON)) == [("order_id", "order_id"), ("line_no", "line_no")]
    # operand order and schema qualifiers don't matter; each pair is read from-side first
    assert R.join_key_pairs(_on("order_lines.order_id = s.shipments.oid AND shipments.ln = order_lines.line_no")) == [
        ("oid", "order_id"), ("ln", "line_no")]
    assert R.join_key_pairs(_on("`shipments`.`order_id` = `order_lines`.`order_id`"), "mysql") == [
        ("order_id", "order_id")]
    # a self-join keeps the written order
    assert R.join_key_pairs(_on("e.e.org = e.e.mgr_org AND e.mgr = e.id", "e", "e")) == [
        ("org", "mgr_org"), ("mgr", "id")]


@pytest.mark.parametrize("text", [
    "CAST(shipments.order_id AS TEXT) = order_lines.order_id",
    "shipments.order_id = order_lines.order_id AND order_lines.qty > 0",
    "shipments.order_id = customers.id",
    "shipments.order_id = :order",
    "not sql at all (",
    # a key equality plus a filter on the far table: not a key, as on main (see the parity tests below)
    "orders.customer_ntrl_key = customers_curr.customer_natural_key AND customers_curr.current_flag = 'Y'",
])
def test_join_key_pairs_is_none_for_anything_but_column_equalities(text):
    assert R.join_key_pairs(_on(text)) is None


# --- the receipt ------------------------------------------------------------------------------------


@pytest.fixture
def shop_org(shop_db, tmp_path) -> m.Datasource:
    org, _ = I.introspect("shop", "sqlite", runner=_sqlite_runner(shop_db), artifacts_dir=tmp_path)
    return org


def _aggregates(org: m.Datasource, sql: str) -> dict[str, R.AggregateReport]:
    return {a.aggregate: a for a in R.pre_flight_check(sql, org).aggregates}


@pytest.mark.parametrize("measure", ["s.shipped_qty", "ol.qty"])
def test_a_join_on_part_of_the_key_multiplies_either_side(shop_org, measure):
    sql = f"SELECT SUM({measure}) FROM shipments s JOIN order_lines ol ON s.order_id = ol.order_id"
    (agg,) = _aggregates(shop_org, sql).values()
    assert agg.status == R.MULTIPLIED
    (finding,) = agg.findings
    assert finding.risk == "fan_trap"
    assert "on order_id only; the declared join needs order_id AND line_no" in finding.reason


def test_a_max_over_a_partial_key_join_is_a_fan_out_not_a_trap(shop_org):
    sql = "SELECT MAX(s.shipped_qty) FROM shipments s JOIN order_lines ol ON ol.line_no = s.line_no"
    (agg,) = _aggregates(shop_org, sql).values()
    assert agg.status == R.MULTIPLIED and [f.risk for f in agg.findings] == ["fan_out_invariant"]
    assert "on line_no only" in agg.findings[0].reason


def test_a_join_on_the_whole_key_is_clean(shop_org):
    sql = ("SELECT SUM(s.shipped_qty) FROM shipments s JOIN order_lines ol "
           "ON ol.line_no = s.line_no AND s.order_id = ol.order_id")
    (agg,) = _aggregates(shop_org, sql).values()
    assert agg.status == R.NOT_MULTIPLIED and not agg.findings


def test_a_partial_key_join_still_runs_through_the_receipt(shop_org):
    sql = "SELECT SUM(s.shipped_qty) AS n FROM shipments s JOIN order_lines ol ON s.order_id = ol.order_id"
    receipt = R.assemble_receipt(shop_org, sql)
    (agg,) = receipt["aggregates"]["items"]
    assert agg["status"] == R.MULTIPLIED and agg["findings"][0]["risk"] == "fan_trap"


# --- the places that read from_column / to_column ---------------------------------------------------


def test_same_join_compares_the_column_pairs_of_on_joins():
    a = _on(_COMPOSITE_ON)
    assert I._same_join(a, _on("order_lines.line_no = shipments.line_no AND order_lines.order_id = shipments.order_id"))
    assert not I._same_join(a, _on("shipments.order_id = order_lines.order_id AND shipments.ship_no = order_lines.line_no"))
    assert not I._same_join(a, _on("shipments.order_id = order_lines.order_id"))
    one = m.Relationship(from_table="shipments", to_table="order_lines", from_column="order_id",
                         to_column="order_id", relationship="many_to_one")
    assert I._same_join(one, _on("shipments.order_id = order_lines.order_id"))
    assert not I._same_join(one, a)


def _with_composite(org: m.Datasource) -> m.Datasource:
    """`org` with its shipments -> order_lines joins as the one composite `on:` join, however they were built."""
    org = org.model_copy(deep=True)
    sa = org.subject_areas[0]
    old = [r for r in sa.relationships if r.to_table == "order_lines"]
    sa.relationships = [r for r in sa.relationships if r.to_table != "order_lines"] + [
        old[0].model_copy(update={"from_column": None, "to_column": None, "on": _COMPOSITE_ON})]
    return org


def test_route_references_reads_an_on_join(shop_org):
    shop_org = _with_composite(shop_org)
    specs = [{"from_table": "shipments", "from_column": "order_id", "to_table": "order_lines"},
             {"from_table": "shipments", "from_column": "line_no", "to_table": "order_lines"}]
    intra, cross, skipped = cli._route_references(shop_org, specs)   # raised AttributeError before
    assert intra == {} and cross == [] and skipped == 0               # the composite join covers both


def test_composite_key_columns_go_in_the_references_group():
    cols = [m.Column(name="id", type="integer", primary_key=True), m.Column(name="order_id", type="integer"),
            m.Column(name="line_no", type="integer")] + [m.Column(name=f"f_{i}", type="decimal") for i in range(35)]
    t = m.Table(name="shipments", schema_name="s", storage_connection="c", grain=["id"], columns=cols,
                column_groups={"misc": [c.name for c in cols]})
    rel = _on(_COMPOSITE_ON).model_copy(update={"from_schema": "s"})
    I._regroup_columns_with_references([t], [rel])
    assert sorted(t.column_groups["references"]) == ["line_no", "order_id"]


def test_a_hidden_column_of_a_composite_key_hides_its_join(shop_db, tmp_path):
    org, _ = I.introspect("shop", "sqlite", runner=_sqlite_runner(shop_db), artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    path = next(root.glob("subject_areas/*/tables/shipments.yaml"))
    doc = yaml.safe_load(path.read_text())
    next(c for c in doc["columns"] if c["name"] == "line_no")["review_state"] = "rejected"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    served = L.load_datasource(root)
    assert not _between(served, "shipments", "order_lines")
    assert _between(served, "shipments", "customers")


def test_the_validator_warns_on_an_on_join_naming_a_missing_column(shop_org):
    shop_org = _with_composite(shop_org)
    org = shop_org.model_copy(deep=True)
    rel = next(r for r in _rels(org) if r.on)
    rel.on = "shipments.order_id = order_lines.order_id AND shipments.line_number = order_lines.line_no"
    res = V.validate(org)
    assert res.ok
    assert any(f.code == "relationship_on_unknown_column" and "line_number" in f.message for f in res.findings)
    assert not any(f.code == "relationship_on_unknown_column" for f in V.validate(shop_org).findings)


# --- refreshing a model that holds the old split joins ----------------------------------------------


@pytest.fixture
def shop(shop_db, tmp_path, monkeypatch):
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    creds = art / "local" / "credentials"
    creds.write_text(f"[shop]\ntype = sqlite\npath = {shop_db}\n", encoding="utf-8")
    creds.chmod(0o600)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return {"art": art, "root": art / "shop"}


def _introspect(shop, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "semantic_model.cli", "introspect", "--profile", "shop",
         "--db-type", "sqlite", "--artifacts", str(shop["art"]), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(PKG_SRC),
        env={**os.environ, "PYTHONPATH": str(PKG_SRC)},
    )


def _split_the_key(shop, signer: str = "agami_introspect", role: str = "system") -> Path:
    """Rewrite the model's composite join as the two approved single-column joins an older build wrote,
    the `line_no` one signed off by `signer`."""
    path = next(shop["root"].glob("subject_areas/*/relationships.yaml"))
    doc = yaml.safe_load(path.read_text())
    composite = next(r for r in doc["relationships"] if r.get("on"))
    doc["relationships"].remove(composite)
    for col, who, as_ in (("order_id", "agami_introspect", "system"), ("line_no", signer, role)):
        split = {k: v for k, v in composite.items() if k != "on"}
        doc["relationships"].append({**split, "from_column": col, "to_column": col,
                                     "signed_off_by": who, "signed_off_role": as_})
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_a_refresh_replaces_the_engines_split_joins_with_one_composite_join(shop):
    assert _introspect(shop).returncode == 0
    path = _split_the_key(shop)
    preview = _introspect(shop, "--append", "--dry-run")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "joins merged into one composite join" in preview.stdout
    assert "shipments → order_lines: (order_id) + (line_no) → order_id AND line_no" in preview.stdout
    assert len(_between(L.load_datasource(shop["root"]), "shipments", "order_lines")) == 2   # a preview writes nothing

    done = _introspect(shop, "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "joins merged into one composite join" in done.stdout
    (rel,) = _between(L.load_datasource(shop["root"]), "shipments", "order_lines")
    assert rel.on == _COMPOSITE_ON
    (cust,) = _between(L.load_datasource(shop["root"]), "shipments", "customers")
    assert cust.from_column == "customer_id"

    written = path.read_text()
    again = _introspect(shop, "--append")
    assert again.returncode == 0 and "composite" not in again.stdout
    assert path.read_text() == written


def test_a_refresh_leaves_a_split_key_a_person_signed_off(shop):
    assert _introspect(shop).returncode == 0
    _split_the_key(shop, signer="you@example.com", role="owner")
    before = _between(L.load_datasource(shop["root"]), "shipments", "order_lines")
    done = _introspect(shop, "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "composite foreign key not applied" in done.stdout
    assert "not changed: signed off by you@example.com" in done.stdout
    assert _between(L.load_datasource(shop["root"]), "shipments", "order_lines") == before


def _relabel(path: Path, card: str, **match) -> None:
    doc = yaml.safe_load(path.read_text())
    for r in doc["relationships"]:
        if all(r.get(k) == v for k, v in match.items()):
            r["relationship"] = card
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_a_refresh_relabels_the_engines_composite_join_from_all_its_columns(shop):
    assert _introspect(shop).returncode == 0
    _relabel(next(shop["root"].glob("subject_areas/*/relationships.yaml")), "one_to_one", on=_COMPOSITE_ON)
    done = _introspect(shop, "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    assert ("shipments.(order_id, line_no) -> order_lines.(order_id, line_no): one_to_one → many_to_one"
            in done.stdout)
    (rel,) = _between(L.load_datasource(shop["root"]), "shipments", "order_lines")
    assert rel.on == _COMPOSITE_ON and rel.relationship == "many_to_one"


def test_split_joins_the_merge_replaces_are_not_also_listed_as_relabelled(shop):
    assert _introspect(shop).returncode == 0
    path = _split_the_key(shop)
    _relabel(path, "one_to_one", to_table="order_lines")
    done = _introspect(shop, "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "joins merged into one composite join" in done.stdout
    assert "joins corrected" not in done.stdout


# --- review fixes -----------------------------------------------------------------------------------


@pytest.mark.parametrize("key, storage", [("mysql", "MySQL"), ("sqlserver", "SQLServer")])
def test_a_generated_quoted_composite_key_validates_in_its_engines_grammar(key, storage):
    on = I._composite_on(D.get_dialect(key), "Shipments", ["OrderId", "LineNo"], "OrderLines", ["OrderId", "LineNo"])
    assert "OrderId" in on and on != on.replace("`", "").replace("[", "")   # quoted the engine's way
    assert V._sqlparse_error(on, as_condition=True) is not None             # the generic grammar refuses it

    def t(name, cols, grain):
        return m.Table(name=name, schema="s", storage_connection="c", grain=grain, description=name,
                       columns=[m.Column(name=c, type="integer") for c in cols])
    rel = m.Relationship(from_table="Shipments", to_table="OrderLines", on=on, relationship="many_to_one")
    org = m.Datasource(datasource="Shop", storage_connections=[m.StorageConnection(name="c", storage_type=storage)],
                       subject_areas=[m.SubjectArea(name="sales", relationships=[rel], tables_defined=[
                           t("Shipments", ["Id", "OrderId", "LineNo"], ["Id"]),
                           t("OrderLines", ["OrderId", "LineNo"], ["OrderId", "LineNo"])])])
    res = V.validate(org)
    assert res.ok, V.format_result(res)
    assert not any(f.code in ("relationship_on_unparseable", "relationship_on_unknown_column") for f in res.findings)


def _partial_findings(org: m.Datasource, sql: str) -> list:
    return [f for a in R.pre_flight_check(sql, org).aggregates for f in a.findings
            if any("(partial key)" in j for j in f.triggering_joins)]


@pytest.mark.parametrize("sql", [
    # two aliases, each on half the key: together they name all of it, yet each join repeats rows
    "SELECT SUM(s.shipped_qty) FROM shipments s JOIN order_lines a ON s.order_id = a.order_id "
    "JOIN order_lines b ON s.line_no = b.line_no",
    # a whole-key join does not cover a second, partial one
    "SELECT SUM(s.shipped_qty) FROM shipments s JOIN order_lines a ON s.order_id = a.order_id "
    "AND s.line_no = a.line_no JOIN order_lines b ON s.order_id = b.order_id",
])
def test_each_join_is_checked_against_the_key_on_its_own(shop_org, sql):
    (finding,) = _partial_findings(shop_org, sql)
    assert finding.risk == "fan_trap"


def test_a_semi_or_anti_join_on_part_of_the_key_repeats_nothing(shop_org):
    for kind in ("SEMI", "ANTI"):
        sql = f"SELECT SUM(s.shipped_qty) FROM shipments s LEFT {kind} JOIN order_lines ol ON s.order_id = ol.order_id"
        assert _partial_findings(shop_org, sql) == [], kind
    # an ordinary join between the same tables is still checked
    sql = ("SELECT SUM(s.shipped_qty) FROM shipments s LEFT SEMI JOIN order_lines a ON s.order_id = a.order_id "
           "JOIN order_lines b ON s.order_id = b.order_id")
    assert len(_partial_findings(shop_org, sql)) == 1


def test_a_cross_area_on_join_checks_its_target_against_the_target_area():
    def t(name, cols):
        return m.Table(name=name, schema="s", storage_connection="c", grain=[cols[0]], description=name,
                       columns=[m.Column(name=c, type="integer") for c in cols])

    def org(on):
        rel = m.CrossSubjectAreaRelationship(from_subject_area="ship", to_subject_area="sales",
                                             from_table="shipments", to_table="order_lines", on=on,
                                             relationship="many_to_one")
        return m.Datasource(
            datasource="Shop", storage_connections=[m.StorageConnection(name="c", storage_type="SQLite")],
            subject_areas=[m.SubjectArea(name="ship", tables_defined=[t("shipments", ["id", "order_id", "line_no"])]),
                           m.SubjectArea(name="sales", tables_defined=[t("order_lines", ["order_id", "line_no"])])],
            cross_subject_area_relationships=[rel])

    def warned(o):
        return [f.message for f in V.validate(o).findings if f.code == "relationship_on_unknown_column"]

    assert warned(org(_COMPOSITE_ON)) == []
    (msg,) = warned(org("shipments.order_id = order_lines.order_id AND shipments.line_no = order_lines.line_number"))
    assert "line_number" in msg and "order_lines" in msg


def test_the_golden_eval_keeps_the_same_key_in_two_schemas_apart():
    sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
    import run_golden_eval

    rel = {"from_table": "shipments", "to_table": "order_lines", "on": _COMPOSITE_ON, "relationship": "many_to_one"}
    text = run_golden_eval._relationships_text([{"relationships": [
        {**rel, "from_schema": "billing", "to_schema": "billing"},
        {**rel, "from_schema": "crm", "to_schema": "crm"},
        {**rel, "from_schema": "crm", "to_schema": "crm"}]}])
    assert [line.split(" -- ")[0] for line in text.splitlines()] == [
        "billing.shipments -> billing.order_lines", "crm.shipments -> crm.order_lines"]


# --- a key equality plus a filter on the far table (ashwin's case) ---------------------------------
#
# `on: orders.customer_ntrl_key = customers_curr.customer_natural_key AND customers_curr.current_flag = 'Y'`
# is not a composite key. Wherever this PR reads join keys it must behave as main does, except the
# loader, which now hides it with its key column (D12).

_FILTERED_ON = ("orders.customer_ntrl_key = customers_curr.customer_natural_key "
                "AND customers_curr.current_flag = 'Y'")
_PERSON = {"confidence": "confirmed", "review_state": "approved", "signed_off_by": "you@example.com",
           "signed_off_role": "owner", "signed_off_at": "2026-01-01T00:00:00Z"}


def _filtered_org(on: str = _FILTERED_ON, extra_cols: tuple = ()) -> m.Datasource:
    orders = m.Table(name="orders", schema="public", storage_connection="c", grain=["order_id"], description="o",
                     columns=[m.Column(name="order_id", type="integer"),
                              m.Column(name="customer_ntrl_key", type="string"),
                              m.Column(name="amount", type="decimal", aggregation="additive"),
                              *[m.Column(name=c, type="string") for c in extra_cols]])
    cust = m.Table(name="customers_curr", schema="public", storage_connection="c", grain=["customer_sk"],
                   description="c",
                   columns=[m.Column(name="customer_sk", type="integer"),
                            m.Column(name="customer_natural_key", type="string"),
                            m.Column(name="current_flag", type="string"), m.Column(name="segment", type="string"),
                            *[m.Column(name=c, type="string") for c in extra_cols]])
    rel = m.Relationship(from_table="orders", to_table="customers_curr", on=on, relationship="many_to_one",
                         join_type="LEFT", **_PERSON)
    return m.Datasource(datasource="Shop", storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
                        subject_areas=[m.SubjectArea(name="sales", tables_defined=[orders, cust], relationships=[rel])])


_SUM_BY_SEGMENT = ("SELECT c.segment, SUM(o.amount) AS total FROM orders o JOIN customers_curr c "
                   "ON o.customer_ntrl_key = c.customer_natural_key{flag} GROUP BY c.segment")


def _verdict(org: m.Datasource, sql: str) -> dict:
    receipt = R.assemble_receipt(org, sql)
    return {
        "aggregates": [(a.aggregate, a.status, sorted((f.risk, tuple(f.triggering_joins)) for f in a.findings))
                       for a in R.pre_flight_check(sql, org).aggregates],
        "joins": [(j.get("status"), j.get("review_state")) for j in receipt["joins"]["items"]],
        "receipt_aggregates": [(a["status"], sorted(f["risk"] for f in a["findings"]))
                               for a in receipt["aggregates"]["items"]],
    }


@pytest.mark.parametrize("flag, on_main", [
    # what origin/main (63e64317) gives for the same model and statement, read off a checkout of it
    (" AND c.current_flag = 'Y'", {"aggregates": [("SUM(o.amount)", "not_multiplied", [])],
                                   "joins": [("declared", "approved")],
                                   "receipt_aggregates": [("not_multiplied", [])]}),
    ("", {"aggregates": [("SUM(o.amount)", "undetermined", [])],
          "joins": [("undetermined", None)],
          "receipt_aggregates": [("undetermined", [])]}),
])
def test_a_filtered_join_gets_the_receipt_main_gives(flag, on_main):
    assert _verdict(_filtered_org(), _SUM_BY_SEGMENT.format(flag=flag)) == on_main


def test_a_composite_key_with_a_filter_written_on_one_key_column_is_a_partial_join():
    on = ("orders.customer_ntrl_key = customers_curr.customer_natural_key AND orders.region = customers_curr.region "
          "AND customers_curr.current_flag = 'Y'")
    sql = ("SELECT SUM(o.amount) FROM orders o JOIN customers_curr c "
           "ON o.customer_ntrl_key = c.customer_natural_key AND c.current_flag = 'Y'")
    (finding,) = _partial_findings(_filtered_org(on, extra_cols=("region",)), sql)
    assert finding.risk == "fan_trap" and "the declared join needs customer_ntrl_key AND region" in finding.reason


def test_the_loose_reader_keeps_the_key_of_a_filtered_join():
    rel = _filtered_org().subject_areas[0].relationships[0]
    assert R.join_key_pairs(rel) is None
    assert R.join_column_pairs(rel) == [("customer_ntrl_key", "customer_natural_key")]


def test_the_validator_accepts_a_filtered_join():
    res = V.validate(_filtered_org())
    assert res.ok, V.format_result(res)
    assert not any(f.code in ("relationship_on_unknown_column", "relationship_on_unparseable") for f in res.findings)


def test_route_references_reads_a_filtered_join_without_proposing_it_again():
    specs = [{"from_table": "orders", "from_column": "customer_ntrl_key", "to_table": "customers_curr"}]
    intra, cross, skipped = cli._route_references(_filtered_org(), specs)   # main raised AttributeError here
    assert intra == {} and cross == [] and skipped == 0


_CUSTOMERS_SCHEMA = (
    "CREATE TABLE customers_curr(customer_sk INTEGER PRIMARY KEY, customer_natural_key {key_type},"
    " region TEXT, current_flag TEXT, UNIQUE(customer_natural_key), UNIQUE(customer_natural_key, region));"
    "CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_ntrl_key {key_type}, region TEXT, amount REAL,"
    " FOREIGN KEY(customer_ntrl_key) REFERENCES customers_curr(customer_natural_key),"
    " FOREIGN KEY(customer_ntrl_key, region) REFERENCES customers_curr(customer_natural_key, region));"
    "INSERT INTO customers_curr VALUES (1, 'k1', 'eu', 'Y'), (2, 'k2', 'us', 'Y');"
    "INSERT INTO orders VALUES (1, 'k1', 'eu', 5), (2, 'k2', 'us', 7);"
)


@pytest.fixture
def customers(tmp_path, monkeypatch):
    db = tmp_path / "cust.db"

    def build(key_type: str) -> None:
        db.unlink(missing_ok=True)
        con = sqlite3.connect(db)
        con.executescript(_CUSTOMERS_SCHEMA.format(key_type=key_type))
        con.commit()
        con.close()

    build("TEXT")
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    creds = art / "local" / "credentials"
    creds.write_text(f"[shop]\ntype = sqlite\npath = {db}\n", encoding="utf-8")
    creds.chmod(0o600)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return {"art": art, "root": art / "shop", "rebuild": build}


def _filtered_entry(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text())
    (entry,) = [r for r in doc["relationships"] if r.get("on") == _FILTERED_ON]
    return entry


def _entry_text(path: Path) -> str:
    """The filtered join's lines as written, to compare byte for byte."""
    text = path.read_text()
    start = text.index("- ", text.rfind("\n- ", 0, text.index("customer_natural_key AND")) + 1)
    end = text.find("\n- ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _sign_a_filtered_join(shop) -> Path:
    """The model's orders -> customers_curr joins replaced by the one filtered join a person signed."""
    path = next(shop["root"].glob("subject_areas/*/relationships.yaml"))
    doc = yaml.safe_load(path.read_text())
    keep = [r for r in doc["relationships"] if r["to_table"] != "customers_curr"]
    first = next(r for r in doc["relationships"] if r["to_table"] == "customers_curr")
    filtered = {k: v for k, v in first.items() if k not in ("from_column", "to_column", "on", "description")}
    doc["relationships"] = keep + [{**filtered, "on": _FILTERED_ON, "relationship": "many_to_one",
                                   "description": "the current row of each customer", **_PERSON}]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_a_refresh_leaves_a_person_signed_filtered_join_alone(customers):
    assert _introspect(customers).returncode == 0
    path = _sign_a_filtered_join(customers)
    before = _filtered_entry(path)

    done = _introspect(customers, "--append")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _filtered_entry(path) == before          # every field as the person left it
    assert "joins merged into one composite join" not in done.stdout
    assert "composite foreign key not applied" not in done.stdout
    written = _entry_text(path)
    # the single-column FK the catalog also declares is its own join, not a split of the composite
    # one: a second refresh merges nothing and keeps both
    again = _introspect(customers, "--append")
    assert again.returncode == 0 and _entry_text(path) == written
    assert "composite" not in again.stdout, again.stdout
    pairs = sorted(r.from_column or r.on for r in _between(L.load_datasource(customers["root"]), "orders", "customers_curr"))
    assert pairs == sorted(["customer_ntrl_key", _FILTERED_ON,
                            "orders.customer_ntrl_key = customers_curr.customer_natural_key AND orders.region = customers_curr.region"])

    # a retype of its key column leaves its sign-off as it is, as on main
    customers["rebuild"]("INTEGER")
    retyped = _introspect(customers, "--append")
    assert retyped.returncode == 0, retyped.stdout + retyped.stderr
    assert _entry_text(path) == written and _filtered_entry(path)["confidence"] == "confirmed"
    assert "composite foreign key not applied" not in retyped.stdout


def test_hiding_the_key_column_hides_a_filtered_join(customers):
    # D12, and a change from main: main never hid a filtered join, whatever its columns
    assert _introspect(customers).returncode == 0
    _sign_a_filtered_join(customers)
    tpath = next(customers["root"].glob("subject_areas/*/tables/orders.yaml"))
    doc = yaml.safe_load(tpath.read_text())
    next(c for c in doc["columns"] if c["name"] == "customer_ntrl_key")["review_state"] = "rejected"
    tpath.write_text(yaml.safe_dump(doc, sort_keys=False))
    served = L.load_datasource(customers["root"])
    assert not [r for r in _between(served, "orders", "customers_curr") if r.on == _FILTERED_ON]


# --- second review round ----------------------------------------------------------------------------


def test_a_split_join_in_another_schema_is_not_merged_into_this_schemas_key():
    # the stored join embeds its schema in the table name and leaves the schema fields empty
    split = m.Relationship(from_table="crm.shipments", to_table="crm.order_lines", from_column="order_id",
                           to_column="order_id", relationship="many_to_one", review_state="approved",
                           confidence="confirmed", signed_off_by="agami_introspect", signed_off_role="system",
                           signed_off_at="2026-01-01T00:00:00Z")
    fresh = m.Relationship(from_table="shipments", to_table="order_lines", from_schema="billing",
                           to_schema="billing", on=_COMPOSITE_ON, relationship="many_to_one")
    assert not I._same_tables(fresh, split)
    assert I._same_tables(fresh, split.model_copy(update={"from_table": "billing.shipments",
                                                           "to_table": "billing.order_lines"}))
    report = I.IntrospectReport(profile="p", db_type="sqlite", out_dir="", dry_run=True)
    dropped, kept = I._merge_split_joins([fresh], [split], report, "sqlite")
    assert dropped == set() and kept == [fresh] and not report.changes


def test_a_join_naming_its_schema_in_the_table_name_stays_in_that_schemas_area():
    from semantic_model import build

    split = m.Relationship(from_table="crm.shipments", to_table="crm.order_lines", from_column="order_id",
                           to_column="order_id", relationship="many_to_one")
    names = {"shipments", "order_lines"}
    assert build._rel_in_area(split, {("crm", n) for n in names}, names)
    assert not build._rel_in_area(split, {("billing", n) for n in names}, names)
    assert build._rel_in_area(split, {(None, n) for n in names}, names)   # a schemaless model keeps it


@pytest.mark.parametrize("key", ["sqlite", "postgres", "mysql", "snowflake", "sqlserver", "oracle", "bigquery"])
def test_a_composite_key_on_reserved_words_is_quoted_and_reads_back(key):
    dialect = D.get_dialect(key)
    on = I._composite_on(dialect, "shipments", ["order", "group"], "order", ["order", "group"])
    assert dialect.quote_ident("order") + "." + dialect.quote_ident("group") in on
    rel = m.Relationship(from_table="shipments", to_table="order", on=on, relationship="many_to_one")
    assert R.join_key_pairs(rel, sqlglot_dialect(dialect.name)) == [("order", "order"), ("group", "group")]
    # a plain name stays as it was
    if key not in ("snowflake", "oracle"):
        assert I._composite_on(dialect, "shipments", ["order_id", "line_no"], "order_lines",
                               ["order_id", "line_no"]) == _COMPOSITE_ON


def test_a_composite_key_on_reserved_words_runs_on_a_real_sqlite(tmp_path):
    db = tmp_path / "r.db"
    con = sqlite3.connect(db)
    con.executescript(
        'CREATE TABLE "order"("order" INTEGER, "group" INTEGER, PRIMARY KEY("order", "group"));'
        'CREATE TABLE shipments(id INTEGER PRIMARY KEY, "order" INTEGER, "group" INTEGER,'
        ' FOREIGN KEY("order", "group") REFERENCES "order"("order", "group"));'
        'INSERT INTO "order" VALUES (1, 1), (1, 2); INSERT INTO shipments VALUES (1, 1, 1), (2, 1, 2);')
    con.close()
    org, _ = I.introspect("r", "sqlite", runner=_sqlite_runner(db), artifacts_dir=tmp_path)
    (rel,) = _between(org, "shipments", "order")
    assert rel.on == 'shipments."order" = "order"."order" AND shipments."group" = "order"."group"'
    con = sqlite3.connect(db)
    assert con.execute(f'SELECT COUNT(*) FROM shipments JOIN "order" ON {rel.on}').fetchone() == (2,)
    con.close()
    assert V.validate(org).ok


def _two_joins_org(extra: m.Relationship) -> m.Datasource:
    def t(name, cols, grain):
        return m.Table(name=name, schema="s", storage_connection="c", grain=grain, description=name,
                       columns=[m.Column(name=c, type="integer") for c in cols])
    composite = m.Relationship(from_table="shipments", to_table="order_lines", on=_COMPOSITE_ON,
                               relationship="many_to_one")
    return m.Datasource(
        datasource="Shop", storage_connections=[m.StorageConnection(name="c", storage_type="SQLite")],
        subject_areas=[m.SubjectArea(name="sales", relationships=[composite, extra], tables_defined=[
            t("shipments", ["id", "order_id", "line_no", "alt_order_id", "shipped_qty"], ["id"]),
            t("order_lines", ["order_id", "line_no", "qty"], ["order_id", "line_no"])])])


def test_a_join_matching_another_declaration_does_not_hide_a_partial_one():
    alt = m.Relationship(from_table="shipments", to_table="order_lines", from_column="alt_order_id",
                         to_column="order_id", relationship="many_to_one")
    sql = ("SELECT SUM(s.shipped_qty) FROM shipments s JOIN order_lines a ON s.alt_order_id = a.order_id "
           "JOIN order_lines b ON s.order_id = b.order_id")
    (finding,) = _partial_findings(_two_joins_org(alt), sql)
    assert "on order_id only" in finding.reason


def test_a_join_a_declaration_names_whole_is_not_a_partial_key():
    one = m.Relationship(from_table="shipments", to_table="order_lines", from_column="order_id",
                         to_column="order_id", relationship="many_to_one")
    sql = ("SELECT SUM(s.shipped_qty) FROM shipments s JOIN order_lines a ON s.order_id = a.order_id "
           "AND s.line_no = a.line_no JOIN order_lines b ON s.order_id = b.order_id")
    assert _partial_findings(_two_joins_org(one), sql) == []


def _nodes_org() -> m.Datasource:
    nodes = m.Table(name="nodes", schema="s", storage_connection="c", grain=["tenant_id", "id"], description="n",
                    columns=[m.Column(name="tenant_id", type="integer"), m.Column(name="id", type="integer"),
                             m.Column(name="parent_id", type="integer"),
                             m.Column(name="amount", type="decimal", aggregation="additive")])
    rel = m.Relationship(from_table="nodes", to_table="nodes", relationship="many_to_one",
                         on=I._composite_on(D.get_dialect("sqlite"), "nodes", ["tenant_id", "parent_id"],
                                            "nodes", ["tenant_id", "id"]))
    return m.Datasource(datasource="Tree", storage_connections=[m.StorageConnection(name="c", storage_type="SQLite")],
                        subject_areas=[m.SubjectArea(name="t", relationships=[rel], tables_defined=[nodes])])


def test_an_aliased_self_join_on_part_of_the_key_is_partial():
    org = _nodes_org()
    sql = "SELECT SUM(n.amount) FROM nodes n JOIN nodes p ON n.parent_id = p.id"
    (finding,) = _partial_findings(org, sql)
    assert finding.risk == "fan_trap" and "on parent_id only" in finding.reason
    whole = "SELECT SUM(n.amount) FROM nodes n JOIN nodes p ON n.tenant_id = p.tenant_id AND n.parent_id = p.id"
    assert _partial_findings(org, whole) == []


@pytest.mark.parametrize("sql, partial", [
    ("SELECT SUM(s.shipped_qty) FROM shipments s JOIN order_lines ol USING (order_id)", True),
    ("SELECT SUM(s.shipped_qty) FROM shipments s JOIN order_lines ol USING (order_id, line_no)", False),
    # two relations before the join: only shipments has order_id
    ("SELECT SUM(s.shipped_qty) FROM customers c JOIN shipments s ON s.customer_id = c.id "
     "JOIN order_lines ol USING (order_id)", True),
])
def test_using_columns_are_the_joins_key(shop_org, sql, partial):
    (agg,) = _aggregates(shop_org, sql).values()
    assert bool(_partial_findings(shop_org, sql)) is partial
    assert (agg.status == R.MULTIPLIED) is partial


def test_a_condition_within_one_table_is_not_a_key_column():
    on = "orders.customer_ntrl_key = customers_curr.customer_natural_key AND customers_curr.segment = customers_curr.region"
    sql = "SELECT SUM(o.amount) FROM orders o JOIN customers_curr c ON o.customer_ntrl_key = c.customer_natural_key"
    assert _partial_findings(_filtered_org(on, extra_cols=("region",)), sql) == []
