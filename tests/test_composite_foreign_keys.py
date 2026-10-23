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
    grain = {t.name: set(t.grain) for t in tables}
    pairs = [("shipments", "shipments")] if key == "sqlite" else [("s", t.name) for t in tables]
    return I._build_relationships(dialect, run, pairs, tables, grain, report), report


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
