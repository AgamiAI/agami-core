"""#511: a join on a unique key is one-to-one, not many-to-one.

Introspection used to label a join from the primary keys alone, so a join onto a UNIQUE column came
out many-to-one and every metric over it carried a false fan-trap or chasm-trap finding. The keys are
now read from the catalog per dialect, kept on the table as `unique_keys`, and read on both sides of
a join, at introspection and at query time. Most of this runs against a real SQLite database.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import build  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402

_SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, credit NUMERIC);
CREATE TABLE passports(id INTEGER PRIMARY KEY, person_id INTEGER UNIQUE REFERENCES users(id), fee NUMERIC);
CREATE TABLE licenses(id INTEGER PRIMARY KEY, holder_id INTEGER UNIQUE REFERENCES users(id), fee NUMERIC);
CREATE TABLE accounts(id INTEGER PRIMARY KEY, owner_email TEXT UNIQUE, balance NUMERIC);
CREATE TABLE profiles(user_id INTEGER PRIMARY KEY REFERENCES users(id), bio TEXT);
CREATE TABLE customers(id INTEGER PRIMARY KEY, region TEXT, credit NUMERIC);
CREATE TABLE invoices(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), amount NUMERIC);
CREATE TABLE subscriptions(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), price NUMERIC);
CREATE TABLE seats(id INTEGER PRIMARY KEY, row_no INTEGER, seat_no INTEGER, UNIQUE(row_no, seat_no));
CREATE TABLE tickets(id INTEGER PRIMARY KEY, row_no INTEGER REFERENCES seats(row_no), price NUMERIC);
"""


def _fill(con: sqlite3.Connection) -> None:
    for i in range(1, 11):
        con.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"u{i}@example.com", i))
        con.execute("INSERT INTO passports VALUES (?, ?, ?)", (i, i, 10 * i))
        con.execute("INSERT INTO licenses VALUES (?, ?, ?)", (i, i, 5 * i))
        con.execute("INSERT INTO accounts VALUES (?, ?, ?)", (i, f"u{i}@example.com", 100 * i))
        con.execute("INSERT INTO profiles VALUES (?, 'bio')", (i,))
        con.execute("INSERT INTO customers VALUES (?, 'east', ?)", (i, i))
        # unique in the data, though nothing declares it
        con.execute("INSERT INTO subscriptions VALUES (?, ?, 9)", (i, i))
        con.execute("INSERT INTO seats VALUES (?, ?, ?)", (i, i % 3, i))
        con.execute("INSERT INTO tickets VALUES (?, ?, 20)", (i, i % 3))
    for i in range(1, 31):
        con.execute("INSERT INTO invoices VALUES (?, ?, ?)", (i, (i % 10) + 1, i))


def _runner(db: Path):
    def run(sql: str) -> list[dict]:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return [{k: "" if r[k] is None else str(r[k]) for k in r.keys()} for r in con.execute(sql)]
        finally:
            con.close()
    return run


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "acme.db"
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    _fill(con)
    con.commit()
    con.close()
    return path


def _build(db: Path, art: Path, **kw) -> tuple[m.Datasource, I.IntrospectReport]:
    return I.introspect("acme", "sqlite", runner=_runner(db), artifacts_dir=art, **kw)


def _rels(org: m.Datasource) -> dict[tuple[str, str], m.Relationship]:
    return {(r.from_table, r.to_table): r for sa in org.subject_areas for r in sa.relationships}


def _table(org: m.Datasource, name: str) -> m.Table:
    return next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == name)


# --- per-dialect catalog reads -------------------------------------------------------------------


@pytest.mark.parametrize("db_type, needles", [
    ("postgres", ["pg_constraint", "contype = 'u'", "pg_index", "indisunique", "NOT i.indisprimary",
                  "indpred IS NULL", "i.indisvalid", "i.indisready", "'public'", "'users'"]),
    ("mysql", ["information_schema.statistics", "non_unique = 0", "index_name <> 'PRIMARY'", "sub_part"]),
    ("sqlite", ["pragma_index_list('users')", "pragma_index_info(il.name)", "origin <> 'pk'", "partial = 0"]),
    ("sqlserver", ["sys.indexes", "is_unique = 1", "is_primary_key = 0", "has_filter = 0",
                   "is_included_column = 0", "i.is_disabled = 0", "i.is_hypothetical = 0"]),
    ("oracle", ["all_constraints", "constraint_type = 'U'", "all_cons_columns",
                "ac.status = 'ENABLED'", "ac.validated = 'VALIDATED'"]),
    ("duckdb", ["duckdb_constraints()", "constraint_type = 'UNIQUE'"]),
    ("snowflake", ["information_schema.table_constraints", "constraint_type = 'UNIQUE'"]),
    ("redshift", ["information_schema.table_constraints", "constraint_type = 'UNIQUE'"]),
])
def test_each_dialect_reads_unique_keys_from_its_catalog(db_type, needles):
    sql = D.get_dialect(db_type).sql_unique_keys("public", "users")
    for needle in needles:
        assert needle in sql, (db_type, needle, sql)
    assert "constraint_name" in sql and "column_name" in sql


@pytest.mark.parametrize("db_type", ["bigquery", "databricks", "trino"])
def test_engines_without_unique_constraints_read_none(db_type):
    assert D.get_dialect(db_type).sql_unique_keys("public", "users") is None


def test_only_snowflake_and_redshift_declare_unique_keys_without_enforcing_them():
    unenforced = {t for t in ("postgres", "mysql", "sqlite", "sqlserver", "oracle", "duckdb",
                              "snowflake", "redshift") if not D.get_dialect(t).unique_enforced}
    assert unenforced == {"snowflake", "redshift"}


def test_the_sqlite_query_runs_and_leaves_out_the_primary_key_and_partial_indexes(tmp_path):
    con = sqlite3.connect(tmp_path / "k.db")
    con.executescript(
        "CREATE TABLE t(code TEXT PRIMARY KEY, a INT UNIQUE, b INT, c INT, d INT, UNIQUE(b, c));"
        "CREATE UNIQUE INDEX t_d ON t(d) WHERE d > 0;")
    rows = con.execute(D.SQLite().sql_unique_keys("main", "t")).fetchall()
    keys: dict[str, list[str]] = {}
    for name, column in rows:
        keys.setdefault(name, []).append(column)
    assert sorted(keys.values()) == [["a"], ["b", "c"]]


def test_the_duckdb_query_runs():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE t(id INT PRIMARY KEY, a INT UNIQUE, b INT, c INT, UNIQUE(b, c))")
    rows = con.execute(D.DuckDB().sql_unique_keys("main", "t")).fetchall()
    keys: dict = {}
    for name, column in rows:
        keys.setdefault(name, []).append(column)
    assert sorted(keys.values()) == [["a"], ["b", "c"]]


# --- the table field -----------------------------------------------------------------------------


def _cols(*names: str) -> list[m.Column]:
    return [m.Column(name=n, type="integer") for n in names]


def test_unique_keys_must_name_columns_the_table_has():
    with pytest.raises(ValueError, match="unknown column"):
        m.Table(name="users", columns=_cols("id"), unique_keys=[["email"]])
    with pytest.raises(ValueError, match="distinct columns"):
        m.Table(name="users", columns=_cols("id"), unique_keys=[["id", "id"]])


def test_a_table_without_unique_keys_dumps_as_before():
    # An older release forbids the key, so a table that has none must not write it.
    assert "unique_keys" not in m.Table(name="users", columns=_cols("id")).model_dump()
    t = m.Table(name="users", columns=_cols("id", "email"), unique_keys=[["email"]])
    assert t.model_dump()["unique_keys"] == [["email"]]


def test_unique_keys_round_trip_through_the_written_model(db, tmp_path):
    org, _ = _build(db, tmp_path / "art")
    back = L.load_datasource(tmp_path / "art" / "acme")
    assert _table(back, "users").unique_keys == [["email"]]
    assert _table(back, "seats").unique_keys == [["row_no", "seat_no"]]
    assert _table(back, "invoices").unique_keys == []
    text = (tmp_path / "art" / "acme" / "subject_areas" / "acme" / "tables" / "invoices.yaml").read_text()
    assert "unique_keys" not in text


def test_an_excluded_column_takes_its_unique_key_with_it(db, tmp_path):
    _build(db, tmp_path / "art")
    path = tmp_path / "art" / "acme" / "subject_areas" / "acme" / "tables" / "users.yaml"
    doc = yaml.safe_load(path.read_text())
    next(c for c in doc["columns"] if c["name"] == "email")["review_state"] = "rejected"
    path.write_text(yaml.safe_dump(doc))
    assert _table(L.load_datasource(tmp_path / "art" / "acme"), "users").unique_keys == []


# --- cardinality from the keys -------------------------------------------------------------------


def test_a_real_sqlite_build_labels_joins_from_primary_and_unique_keys(db, tmp_path):
    org, _ = _build(db, tmp_path / "art", dry_run=True)
    rels = _rels(org)
    assert rels[("profiles", "users")].relationship == "one_to_one"     # PK -> PK
    assert rels[("passports", "users")].relationship == "one_to_one"    # UNIQUE -> PK
    assert rels[("invoices", "customers")].relationship == "many_to_one"
    # A join on one column of a two-column unique key is not on a key.
    assert rels[("tickets", "seats")].relationship == "many_to_one"
    assert rels[("passports", "users")].review_state == "approved"


def test_catalog_mode_reads_unique_keys_on_both_sides():
    runner = make_catalog_runner(
        tables=["users", "passports", "accounts", "slots", "bookings"],
        columns={
            "users": [col("id", "integer", nullable=False), col("email", "varchar")],
            "passports": [col("id", "integer", nullable=False), col("person_id", "integer")],
            "accounts": [col("id", "integer", nullable=False), col("owner_email", "varchar")],
            "slots": [col("id", "integer", nullable=False), col("day", "integer"), col("hour", "integer")],
            "bookings": [col("id", "integer", nullable=False), col("day", "integer")],
        },
        fks=[
            {"from_table": "passports", "from_column": "person_id", "to_table": "users", "to_column": "id"},
            {"from_table": "accounts", "from_column": "owner_email", "to_table": "users", "to_column": "email"},
            {"from_table": "bookings", "from_column": "day", "to_table": "slots", "to_column": "day"},
        ],
        unique={"users": [["email"]], "passports": [["person_id"]], "accounts": [["owner_email"]],
                "slots": [["day", "hour"]]},
    )
    org, _ = I.introspect("acme", "postgres", runner=runner, artifacts_dir="/nonexistent", dry_run=True)
    rels = _rels(org)
    assert rels[("passports", "users")].relationship == "one_to_one"    # UNIQUE -> PK
    assert rels[("accounts", "users")].relationship == "one_to_one"     # UNIQUE <-> UNIQUE
    assert rels[("bookings", "slots")].relationship == "many_to_one"    # part of a composite key
    assert _table(org, "slots").unique_keys == [["day", "hour"]]


def test_a_declared_but_unenforced_unique_key_is_kept_only_when_the_data_bears_it_out():
    base = make_catalog_runner(
        tables=["users"], columns={"users": [col("id", "integer", nullable=False),
                                             col("email", "varchar"), col("handle", "varchar")]},
        unique={"users": [["email"], ["handle"]]})

    def runner(sql: str) -> list[dict]:
        if "COUNT(DISTINCT" in sql:
            distinct = "10" if '"email"' in sql else "7"
            return [{"total": "10", "distinct_count": distinct, "null_count": "0"}]
        return base(sql)

    org, _ = I.introspect("acme", "redshift", runner=runner, artifacts_dir="/nonexistent", dry_run=True)
    assert _table(org, "users").unique_keys == [["email"]]


def test_infer_cardinality_reads_each_tables_own_keys():
    # Two schemas, one table name: the key of one must not be lent to the other.
    crm = m.Table(name="users", schema="crm", columns=_cols("id", "email"), grain=["id"])
    legacy = m.Table(name="users", schema="legacy", columns=_cols("id", "email"), grain=["email"])
    cards = m.Table(name="cards", columns=_cols("id", "user_id"), grain=["id"], unique_keys=[["user_id"]])
    assert build.infer_cardinality(cards, crm, ["user_id"], ["id"]) == "one_to_one"
    assert build.infer_cardinality(cards, legacy, ["user_id"], ["id"]) == "one_to_many"
    assert build.infer_cardinality(cards, None, ["user_id"], ["id"]) == "one_to_many"
    assert build.infer_cardinality(None, crm, ["x"], ["ID"]) == "many_to_one"


# --- uniqueness only the data shows --------------------------------------------------------------


def test_a_join_unique_only_in_the_data_keeps_its_label_and_waits_for_review(db, tmp_path):
    org, _ = _build(db, tmp_path / "art", dry_run=True)
    rels = _rels(org)
    sub = rels[("subscriptions", "customers")]
    assert sub.relationship == "many_to_one"
    assert sub.review_state == "unreviewed" and sub.signed_off_by is None
    assert "unique on 10 rows" in sub.description and "looks one-to-one, confirm" in sub.description
    inv = rels[("invoices", "customers")]   # repeats in the data: left as it was
    assert inv.review_state == "approved" and "one-to-one" not in inv.description


def test_the_data_check_is_skipped_on_a_table_too_big_to_scan(db, tmp_path, monkeypatch):
    assert _rels(_build(db, tmp_path / "a", dry_run=True)[0])[("subscriptions", "customers")].review_state \
        == "unreviewed"
    monkeypatch.setattr(I, "_OVERLAP_PROBE_MAX_ROWS", 5)
    rels = _rels(_build(db, tmp_path / "b", dry_run=True)[0])
    assert rels[("subscriptions", "customers")].review_state == "approved"


# --- refresh -------------------------------------------------------------------------------------


def _as_an_older_release_wrote_it(root: Path) -> None:
    """The model as a release that read no unique keys left it: no `unique_keys`, the UNIQUE -> PK
    joins labelled many-to-one and approved by the engine, and the licenses join signed by a person."""
    for path in root.glob("subject_areas/*/tables/*.yaml"):
        doc = yaml.safe_load(path.read_text())
        doc.pop("unique_keys", None)
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
    path = root / "subject_areas" / "acme" / "relationships.yaml"
    doc = yaml.safe_load(path.read_text())
    for r in doc["relationships"]:
        if r["from_table"] in ("passports", "licenses"):
            r["relationship"] = "many_to_one"
        if r["from_table"] == "licenses":
            r.update(signed_off_by="you@example.com", signed_off_role="data_owner")
        if r["from_table"] == "subscriptions":
            r.update(review_state="approved", signed_off_by="agami_introspect",
                     signed_off_role="system", signed_off_at="2026-01-01T00:00:00Z", description="")
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_a_refresh_corrects_the_engines_own_join_and_leaves_a_persons_alone(db, tmp_path):
    art = tmp_path / "art"
    _build(db, art)
    root = art / "acme"
    _as_an_older_release_wrote_it(root)

    _, preview = _build(db, art, append=True, dry_run=True)
    corrected = preview.changes["main.passports"]["joins corrected from the database's constraints"]
    assert corrected == ["passports.person_id → users.id: many_to_one → one_to_one (primary key, unique constraint)"]
    assert "main.licenses" not in preview.changes or \
        "joins corrected from the database's constraints" not in preview.changes["main.licenses"]
    assert any("subscriptions.customer_id" in j and "looks one-to-one" in j
               for j in preview.changes["main.subscriptions"]["joins to review"])
    assert "joins corrected" in preview.render()

    org, _ = _build(db, art, append=True)
    rels = _rels(L.load_datasource(root))
    assert rels[("passports", "users")].relationship == "one_to_one"
    assert rels[("passports", "users")].review_state == "approved"
    assert rels[("licenses", "users")].relationship == "many_to_one"          # a person signed it
    assert rels[("licenses", "users")].signed_off_by == "you@example.com"
    sub = rels[("subscriptions", "customers")]
    assert sub.relationship == "many_to_one" and sub.review_state == "unreviewed"
    assert _table(L.load_datasource(root), "users").unique_keys == [["email"]]


# --- query time ----------------------------------------------------------------------------------


@pytest.fixture
def built(db, tmp_path) -> m.Datasource:
    _build(db, tmp_path / "art")
    return L.load_datasource(tmp_path / "art" / "acme")


def _statuses(org: m.Datasource, sql: str) -> list[tuple[str, list[str]]]:
    items = rt.assemble_receipt(org, sql)["aggregates"]["items"]
    return [(it["status"], sorted(f["risk"] for f in it["findings"])) for it in items]


def test_a_metric_over_a_one_to_one_join_has_no_fan_or_chasm_finding(built):
    sql = ("SELECT SUM(u.credit) AS c, SUM(p.fee) AS f, SUM(l.fee) AS l FROM users u "
           "JOIN passports p ON p.person_id = u.id JOIN licenses l ON l.holder_id = u.id")
    assert _statuses(built, sql) == [(rt.NOT_MULTIPLIED, [])] * 3
    assert rt.pre_flight_check(sql, built).findings == []
    # A genuine many-to-one still fans.
    sql = "SELECT SUM(c.credit) AS c FROM customers c JOIN invoices i ON i.customer_id = c.id"
    assert _statuses(built, sql) == [(rt.MULTIPLIED, ["fan_trap"])]


def test_a_join_the_query_writes_on_unique_columns_is_one_to_one(built):
    sql = "SELECT SUM(a.balance) AS b FROM accounts a JOIN users u ON a.owner_email = u.email"
    receipt = rt.assemble_receipt(built, sql)
    (join,) = receipt["joins"]["items"]
    assert join["status"] == rt.UNDECLARED
    assert _statuses(built, sql) == [(rt.NOT_MULTIPLIED, [])]
    assert [r.status for r in rt.pre_flight_check(sql, built).aggregates] == [rt.NOT_MULTIPLIED]
    # Not a key on one side: whether it repeats rows is still not established.
    sql = "SELECT SUM(a.balance) AS b FROM accounts a JOIN users u ON a.owner_email = u.credit"
    assert _statuses(built, sql) == [(rt.UNDETERMINED, [])]


def test_a_cte_joined_on_the_far_tables_unique_key_does_not_fan(built):
    sql = ("WITH s AS (SELECT owner_email, SUM(balance) AS b FROM accounts GROUP BY owner_email) "
           "SELECT SUM(s.b) AS b FROM users u JOIN s ON s.owner_email = u.email")
    # One row per email on both sides. The CTE's own measure stays `undetermined` (it reads a
    # relation the statement computed), but the join no longer reads as a fan.
    assert _statuses(built, sql) == [(rt.UNDETERMINED, [])]



def test_reconcile_reads_a_unique_key_as_unique_and_plans_no_cardinality_probe_for_it(built):
    from semantic_model import probes as P
    plan = P.join_probes(built, "SELECT a.balance FROM accounts a JOIN users u ON a.owner_email = u.email")
    assert plan["unique_by_model"] == {"accounts.owner_email": True, "users.email": True}
    assert plan["cardinality"] == {"accounts.owner_email": None, "users.email": None}


# --- review fixes --------------------------------------------------------------------------------


def _sqlite_runner(con: sqlite3.Connection):
    con.row_factory = sqlite3.Row

    def run(sql: str) -> list[dict]:
        return [dict(r) for r in con.execute(sql)]
    return run


def test_a_sqlite_unique_index_over_an_expression_is_not_kept_as_its_plain_columns(tmp_path):
    con = sqlite3.connect(tmp_path / "x.db")
    con.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, a INT, b TEXT, c INT UNIQUE);"
                      "CREATE UNIQUE INDEX t_ab ON t(a, lower(b));")
    keys = I._unique_keys(D.SQLite(), _sqlite_runner(con), "main", "t", _cols("id", "a", "b", "c"), ["id"])
    assert keys == [["c"]]


def _two_composite_keyed_tables(tmp_path) -> m.Datasource:
    path = tmp_path / "ck.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE a(id INTEGER PRIMARY KEY, x INT, y INT, UNIQUE(x, y));"
        "CREATE TABLE b(id INTEGER PRIMARY KEY, x INT, y INT, amount NUMERIC, UNIQUE(x, y));")
    con.commit()
    con.close()
    I.introspect("acme", "sqlite", runner=_runner(path), artifacts_dir=tmp_path / "art")
    return L.load_datasource(tmp_path / "art" / "acme")


def test_a_join_that_compares_each_table_with_itself_is_not_keyed_one_to_one(tmp_path):
    org = _two_composite_keyed_tables(tmp_path)
    keyed = "SELECT SUM(b.amount) AS s FROM a JOIN b ON a.x = b.x AND a.y = b.y"
    assert _statuses(org, keyed) == [(rt.NOT_MULTIPLIED, [])]
    cartesian = "SELECT SUM(b.amount) AS s FROM a JOIN b ON a.x = a.y AND b.x = b.y"
    assert _statuses(org, cartesian)[0][0] != rt.NOT_MULTIPLIED


def _write_two_schema_model(root: Path) -> m.Datasource:
    """`accounts` in two schemas: only the crm one has a unique owner_email."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/crm", "subject_areas/legacy"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    for area, unique in (("crm", [["owner_email"]]), ("legacy", [])):
        tables = root / "subject_areas" / area / "tables"
        tables.mkdir(parents=True)
        refs = [{"storage_connection": "c", "schema": area, "table": "accounts"}]
        if area == "crm":
            refs.append({"storage_connection": "c", "schema": "public", "table": "users"})
            (tables / "users.yaml").write_text(yaml.safe_dump({
                "name": "users", "schema": "public", "storage_connection": "c", "grain": ["id"],
                "description": "u", "unique_keys": [["email"]],
                "columns": [{"name": "id", "type": "integer", "primary_key": True},
                            {"name": "email", "type": "string"}]}))
        (root / "subject_areas" / area / "subject_area.yaml").write_text(
            yaml.safe_dump({"name": area, "tables": refs}))
        doc = {"name": "accounts", "schema": area, "storage_connection": "c", "grain": ["id"],
               "description": "a", "columns": [{"name": "id", "type": "integer", "primary_key": True},
                                               {"name": "owner_email", "type": "string"},
                                               {"name": "balance", "type": "decimal"}]}
        if unique:
            doc["unique_keys"] = unique
        (tables / "accounts.yaml").write_text(yaml.safe_dump(doc))
    return L.load_datasource(root)


def test_a_bare_name_two_schemas_define_does_not_borrow_a_key_at_query_time(tmp_path):
    org = _write_two_schema_model(tmp_path / "p")
    sql = ("SELECT SUM(a.balance) AS b FROM legacy.accounts a "
           "JOIN public.users u ON a.owner_email = u.email")
    assert _statuses(org, sql)[0][0] != rt.NOT_MULTIPLIED


def test_a_join_end_reads_only_its_own_schemas_table():
    crm = m.Table(name="users", schema="crm", columns=_cols("id"), grain=["id"])
    legacy = m.Table(name="users", schema="legacy", columns=_cols("id"), grain=["id"])
    plain = m.Table(name="cards", columns=_cols("id"), grain=["id"])
    tables = [crm, legacy, plain]
    assert I._endpoint(tables, "legacy.users", None) is legacy
    assert I._endpoint(tables, "users", "legacy") is legacy
    assert I._endpoint(tables, "users", "archive") is None     # no fallback to another schema
    assert I._endpoint(tables, "users", None) is None          # two schemas, none named
    assert I._endpoint(tables, "cards", "crm") is plain        # a schema-less table still matches
    assert I._endpoint([crm], "users", None) is crm


def _reset_subscriptions(rels: list[m.Relationship]) -> list[m.Relationship]:
    out = []
    for r in rels:
        if r.from_table == "subscriptions":
            out.append(r.model_copy(update={"review_state": "approved", "description": "",
                                            "signed_off_by": "agami_introspect",
                                            "signed_off_role": "system"}))
    return out


def test_the_uniqueness_probe_cap_is_shared_across_passes(db, tmp_path):
    org, _ = _build(db, tmp_path / "art", dry_run=True)
    tables = [t for sa in org.subject_areas for t in sa.tables_defined]
    rels = [r for sa in org.subject_areas for r in sa.relationships]
    probes = []
    base = _runner(db)

    def counting(sql: str) -> list[dict]:
        if "COUNT(DISTINCT" in sql.upper():
            probes.append(sql)
        return base(sql)

    report = I.IntrospectReport(profile="acme", db_type="sqlite", out_dir="", dry_run=True)
    budget = I._ProbeBudget(left=1)
    I._suggest_data_unique(D.SQLite(), counting, _reset_subscriptions(rels), tables, report, budget)
    I._suggest_data_unique(D.SQLite(), counting, _reset_subscriptions(rels), tables, report, budget)
    assert len(probes) == 1
    assert sum("stopped checking joins' values" in n for n in report.notes) == 1


def test_a_refresh_spends_one_budget_on_both_passes(db, tmp_path, monkeypatch):
    art = tmp_path / "art"
    _build(db, art)
    _as_an_older_release_wrote_it(art / "acme")
    made = []
    real = I._ProbeBudget

    def tracking(*a, **kw):
        made.append(real(*a, **kw))
        return made[-1]
    monkeypatch.setattr(I, "_ProbeBudget", tracking)
    _build(db, art, append=True, dry_run=True)
    assert len(made) == 1 and made[0].left < I._UNIQUE_PROBE_CAP


def _edit_join(root: Path, value: str, **kw):
    from semantic_model import curate
    op = {"op": "edit", "kind": "relationship", "area": "acme", "name": "passports->users",
          "field": "relationship", "value": value, "at": "2026-10-26T00:00:00Z"}
    return curate.apply(root, [op], **kw)


@pytest.mark.parametrize("signer", ["you@example.com", None], ids=["signed", "unsigned"])
def test_a_persons_edit_of_an_engine_join_survives_a_refresh(db, tmp_path, signer):
    art = tmp_path / "art"
    _build(db, art)
    root = art / "acme"
    _as_an_older_release_wrote_it(root)
    res = _edit_join(root, "many_to_one", signer=signer, role="data_owner" if signer else None)
    assert res.validated and res.applied, res
    edited = _rels(L.load_datasource(root))[("passports", "users")]
    if signer:
        assert (edited.review_state, edited.signed_off_by, edited.signed_off_role) == \
            ("approved", "you@example.com", "data_owner")
    else:
        assert edited.review_state == "unreviewed" and edited.signed_off_by is None

    _build(db, art, append=True)
    after = _rels(L.load_datasource(root))[("passports", "users")]
    assert after.relationship == "many_to_one"


def test_a_description_edit_keeps_the_engine_sign_off(db, tmp_path):
    from semantic_model import curate
    art = tmp_path / "art"
    _build(db, art)
    root = art / "acme"
    op = {"op": "edit", "kind": "relationship", "area": "acme", "name": "passports->users",
          "field": "description", "value": "each person has one passport"}
    assert curate.apply(root, [op]).validated
    assert _rels(L.load_datasource(root))[("passports", "users")].signed_off_by == "agami_introspect"
