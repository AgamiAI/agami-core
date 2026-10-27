"""Where a column's list of allowed values comes from (#517).

A list the catalog declares (an enum type, a simple CHECK ... IN) is read first, on a table of any
size, and kept exactly. Otherwise, on a small table only, the list is one bounded DISTINCT over the
whole column: complete, or none at all. Keys, measures and years get no list read from the data, and
stored values are listed exactly as stored.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate as C  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import validator as V  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402


def _orders(org):
    return org.subject_areas[0].defined_table("orders")


def _recording(runner, seen):
    def run(sql):
        seen.append(" ".join(sql.split()))
        return runner(sql)
    return run


# --- declared values: Postgres -----------------------------------------------------------------


def test_postgres_enum_values_are_read_from_pg_enum():
    sql = " ".join(D.get_dialect("postgres").sql_enum_values("public", "orders").split())
    assert "JOIN pg_enum e ON e.enumtypid = a.atttypid" in sql
    assert "n.nspname = 'public'" in sql and "c.relname = 'orders'" in sql
    assert sql.endswith("ORDER BY a.attnum, e.enumsortorder")
    assert "information_schema" not in sql  # readable by a SELECT-only role


def test_a_postgres_enum_column_gets_its_declared_values_in_order(tmp_path):
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col("status", "USER-DEFINED")]},
        enums=[{"column_name": "status", "enum_value": v} for v in ("placed", "shipped", "returned")],
        estimate="50000",
    )
    org, _ = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    status = _orders(org).get_column("status")
    assert list(status.choice_field) == ["placed", "shipped", "returned"]
    assert V.validate(org).ok


def test_a_postgres_check_constraint_declares_the_values(tmp_path):
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col("priority", "character varying"),
                            col("level", "integer"), col("qty", "integer")]},
        checks=["CHECK (((priority)::text = ANY ((ARRAY['low'::character varying, 'high'::character varying])::text[])))",
                "CHECK ((level = ANY (ARRAY[1, 2, 3])))",
                "CHECK ((qty > 0))"],
        estimate="50000",
    )
    org, _ = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    t = _orders(org)
    assert t.get_column("priority").choice_field == {"low": "", "high": ""}
    assert t.get_column("level").choice_field == {"1": "", "2": "", "3": ""}  # an integer column too
    assert t.get_column("qty").choice_field is None
    assert V.validate(org).ok


def test_a_declared_list_on_a_big_table_is_kept_and_nothing_is_scanned(tmp_path):
    seen: list[str] = []
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col("priority", "text"), col("note", "text")]},
        checks=["CHECK ((priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text, 'urgent'::text])))"],
        estimate="50000",
        sample=lambda sql: [{"id": i, "priority": "low", "note": "n"} for i in range(500)],
    )
    org, _ = I.introspect("shop", "postgres", runner=_recording(runner, seen), artifacts_dir=tmp_path, dry_run=True)
    assert list(_orders(org).get_column("priority").choice_field) == ["low", "medium", "high", "urgent"]
    assert _orders(org).get_column("note").choice_field is None
    assert not any("SELECT DISTINCT" in s for s in seen)


def test_a_declared_list_is_kept_past_the_cap_on_a_list_read_from_the_data(tmp_path):
    def build(n):
        runner = make_catalog_runner(
            tables=["orders"],
            columns={"orders": [col("id", "integer", nullable=False), col("region", "USER-DEFINED")]},
            enums=[{"column_name": "region", "enum_value": f"r{i:03d}"} for i in range(n)],
        )
        org, _ = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
        return _orders(org).get_column("region").choice_field

    assert len(build(40)) == 40 > I.ENUM_MAX_DISTINCT
    assert len(build(I.DECLARED_MAX_VALUES)) == I.DECLARED_MAX_VALUES
    assert build(I.DECLARED_MAX_VALUES + 1) is None


def test_two_checks_that_disagree_on_a_column_declare_nothing():
    runner = make_catalog_runner(
        tables=["orders"], columns={"orders": [col("status", "text")]},
        checks=["CHECK ((status = ANY (ARRAY['a'::text, 'b'::text])))",
                "CHECK ((status = ANY (ARRAY['a'::text, 'c'::text])))"],
    )
    got = I._declared_values(D.get_dialect("postgres"), runner, "public", "orders")
    assert got.values == {} and got.rejected == {"status"}


# --- declared values: the other engines ---------------------------------------------------------


def test_mysql_enum_column_type_is_parsed():
    assert I._enum_type_values("enum('low','it''s','Open ','a\\\\b')") == ["low", "it's", "Open ", "a\\b"]
    assert I._enum_type_values("varchar(20)") == []

    def runner(sql):
        if "data_type = 'enum'" in sql:
            return [{"COLUMN_NAME": "size", "COLUMN_TYPE": "enum('S','M','L')"}]
        return []

    sql = D.get_dialect("mysql").sql_enum_values("shop", "orders")
    assert "information_schema.columns" in sql and "table_name = 'orders'" in sql
    rows = lambda s: [I._Row(r) for r in runner(s)]  # noqa: E731 — MySQL answers in upper case
    assert I._declared_values(D.get_dialect("mysql"), rows, "shop", "orders").values == {"size": ["S", "M", "L"]}


@pytest.mark.parametrize("db_type, clause, expected", [
    ("postgres", "CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text]))) NOT VALID",
     [("status", ["open", "closed"])]),
    ("mysql", "(`status` in (_utf8mb4'open',_utf8mb4'closed'))", [("status", ["open", "closed"])]),
    ("sqlserver", "([status]=N'open' OR [status]='closed')", [("status", ["open", "closed"])]),
    ("oracle", '"PRIORITY" IN (1,2,-3)', [("PRIORITY", ["1", "2", "-3"])]),
    ("duckdb", "(status IN ('open', 'closed'))", [("status", ["open", "closed"])]),
    ("sqlite", "CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT CHECK (status IN ('open','closed')), "
               "kind TEXT, CHECK (kind = 'a' OR kind = 'b'), CHECK (length(kind) < 4))",
     [("kind", ["a", "b"]), ("status", ["open", "closed"])]),
    # anything but one column's literal list declares nothing
    ("oracle", '"PRIORITY" IS NOT NULL', []),
    ("postgres", "CHECK (((qty >= 1) AND (qty <= 3)))", []),
    ("postgres", "CHECK (((a = 'x'::text) OR (b = 'y'::text)))", []),
    ("postgres", "CHECK ((status <> ALL (ARRAY['x'::text])))", []),
    ("postgres", "CHECK ((NOT (status IN ('x', 'y'))))", []),
    ("postgres", "CHECK ((status IN ('x', upper(note))))", []),
    ("postgres", "not sql at all ((", []),
])
def test_check_constraint_shapes(db_type, clause, expected):
    assert sorted(I._check_lists(clause, D.get_dialect(db_type))) == sorted(expected)


@pytest.mark.parametrize("db_type, needle", [
    ("postgres", "JOIN information_schema.check_constraints cc"),
    ("mysql", "information_schema.check_constraints"),
    ("sqlserver", "sys.check_constraints"),
    ("oracle", "constraint_type = 'C'"),
    ("sqlite", "FROM sqlite_master"),
    ("duckdb", "duckdb_constraints()"),
])
def test_engines_with_check_constraints_read_them(db_type, needle):
    assert needle in D.get_dialect(db_type).sql_check_constraints("s", "orders")


@pytest.mark.parametrize("db_type", ["postgres", "mysql", "sqlserver", "oracle", "sqlite", "duckdb"])
def test_every_value_list_read_passes_the_always_on_gate(db_type):
    # Introspection reads skip only the model pass; the read-only and recon gates still run, and a
    # read they refuse would silently declare nothing.
    import sql_guard

    d = D.get_dialect(db_type)
    for q in (d.sql_enum_values("s", "t"), d.sql_check_constraints("s", "t"), d.distinct_sql("s", "t", "c", 26)):
        if q:
            assert sql_guard.check_read_only(q) is None and sql_guard.check_no_recon(q) is None, q


@pytest.mark.parametrize("db_type", ["bigquery", "snowflake", "databricks", "trino", "redshift"])
def test_engines_without_declared_values_read_nothing(db_type):
    d = D.get_dialect(db_type)
    assert d.sql_check_constraints("s", "t") is None and d.sql_enum_values("s", "t") is None


# --- the data step: a bounded DISTINCT -----------------------------------------------------------


@pytest.mark.parametrize("db_type, sql", [
    ("postgres", 'SELECT DISTINCT "status" AS v FROM "s"."t" WHERE "status" IS NOT NULL LIMIT 26'),
    ("mysql", "SELECT DISTINCT `status` AS v FROM `s`.`t` WHERE `status` IS NOT NULL LIMIT 26"),
    ("sqlserver", "SELECT DISTINCT TOP 26 [status] AS v FROM [s].[t] WHERE [status] IS NOT NULL"),
    ("oracle", 'SELECT DISTINCT "status" AS v FROM "s"."t" WHERE "status" IS NOT NULL FETCH FIRST 26 ROWS ONLY'),
])
def test_the_distinct_read_per_dialect(db_type, sql):
    assert D.get_dialect(db_type).distinct_sql("s", "t", "status", I.ENUM_MAX_DISTINCT + 1) == sql


def _status_table(statuses_in_sample, distinct, *, estimate="5000", seen=None, extra=()):
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col("status", "text"), *extra]},
        estimate=estimate,
        sample=lambda sql: [{"id": i, "status": statuses_in_sample[i % len(statuses_in_sample)]} for i in range(500)],
    )

    def run(sql):
        s = " ".join(sql.split())
        if seen is not None:
            seen.append(s)
        if s.startswith('SELECT DISTINCT "status"'):
            return [{"v": v} for v in distinct[:I.ENUM_MAX_DISTINCT + 1]]
        return runner(sql)
    return run


def test_a_column_with_more_values_than_the_sample_lists_all_of_them(tmp_path):
    first_rows = ["placed", "paid", "packed", "shipped", "delivered", "cancelled"]
    every = first_rows + ["returned", "refunded", "lost", "held", "disputed", "split", "merged", "draft"]
    org, _ = I.introspect("shop", "postgres", runner=_status_table(first_rows, every),
                          artifacts_dir=tmp_path, dry_run=True)
    assert set(_orders(org).get_column("status").choice_field) == set(every)


def test_more_values_than_a_list_holds_gives_no_list(tmp_path):
    every = [f"s{i}" for i in range(40)]
    org, _ = I.introspect("shop", "postgres", runner=_status_table(every[:6], every),
                          artifacts_dir=tmp_path, dry_run=True)
    assert _orders(org).get_column("status").choice_field is None


def test_a_big_table_with_nothing_declared_reads_no_values(tmp_path):
    seen: list[str] = []
    org, _ = I.introspect("shop", "postgres", runner=_status_table(["a", "b"], ["a", "b"], estimate="50000", seen=seen),
                          artifacts_dir=tmp_path, dry_run=True)
    assert _orders(org).get_column("status").choice_field is None
    assert not any("SELECT DISTINCT" in s for s in seen)


def test_values_are_listed_as_stored_and_lookalikes_get_a_caveat(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_status_table(["Open", "Open ", "closed"], ["Open", "Open ", "closed", "Closed"]),
                          artifacts_dir=tmp_path, dry_run=True)
    status = _orders(org).get_column("status")
    assert set(status.choice_field) == {"Open", "Open ", "closed", "Closed"}
    assert status.caveats == [
        "values differ only by spacing or case: 'Closed', 'closed'; 'Open', 'Open ' — match them exactly"]


def test_a_failed_distinct_read_gives_no_list():
    status = I.Column(name="status", type="string", description="")

    def run(sql):
        if "DISTINCT" in sql:
            raise RuntimeError("permission denied")
        return [{"status": "a" if i % 2 else "b"} for i in range(40)]

    I._enrich_from_sample(D.get_dialect("postgres"), run, "s", "t", [status], est_rows=None)
    assert status.choice_field is None


def test_the_distinct_reads_are_capped_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "DISTINCT_READ_CAP", 1)
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col("status", "text"), col("channel", "text")]},
        sample=lambda sql: [{"id": i, "status": "ab"[i % 2], "channel": "xy"[i % 2]} for i in range(40)],
    )
    org, rep = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    t = _orders(org)
    assert [c.name for c in t.columns if c.choice_field] == ["status"]
    assert any("1 column(s) got no value list" in n for n in rep.notes)


@pytest.mark.parametrize("name, ctype", [
    ("store_key", "integer"), ("product_sk", "integer"), ("dept_fk", "integer"),
    ("year", "integer"), ("year", "text"), ("fiscal_year", "text"), ("order_year", "integer"),
    ("revenue", "integer"),
])
def test_keys_measures_and_years_get_no_list_from_the_data(tmp_path, name, ctype):
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col(name, ctype)]},
        sample=lambda sql: [{"id": i, name: str(2020 + i % 3)} for i in range(60)],
    )
    org, _ = I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path, dry_run=True)
    assert _orders(org).get_column(name).choice_field is None


# --- a real SQLite database --------------------------------------------------------------------


def _sqlite_runner(db: Path):
    """Rows as the CSV executor returns them: every value a string, NULL as ''."""
    def run(sql):
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [I._Row({n: "" if v is None else str(v) for n, v in zip(names, row)}) for row in cur.fetchall()]
        finally:
            con.close()
    return run


@pytest.fixture
def shop_db(tmp_path):
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT, store_key INTEGER, state TEXT,"
        " priority TEXT CHECK (priority IN ('low','medium','high','urgent')));"
    )
    rows = [(i, ["placed", "paid"][i % 2] if i < 500 else ["returned", "refunded", "lost"][i % 3],
             i % 3, "Open " if i % 4 == 0 else "Open" if i % 4 == 1 else "Closed", "low") for i in range(900)]
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def test_a_real_sqlite_table(tmp_path, shop_db):
    I.introspect("shop", "sqlite", runner=_sqlite_runner(shop_db), artifacts_dir=tmp_path)
    org = load_datasource(tmp_path / "shop")  # as written and read back: the YAML keeps 'Open '
    t = _orders(org)
    assert set(t.get_column("status").choice_field) == {"placed", "paid", "returned", "refunded", "lost"}
    assert list(t.get_column("priority").choice_field) == ["low", "medium", "high", "urgent"]
    assert t.get_column("store_key").choice_field is None
    assert set(t.get_column("state").choice_field) == {"Open", "Open ", "Closed"}
    assert t.get_column("state").caveats == [
        "values differ only by spacing or case: 'Open', 'Open ' — match them exactly"]
    assert V.validate(org).ok


def test_a_refresh_leaves_an_existing_list_alone_and_lists_a_new_column(tmp_path, shop_db):
    run = _sqlite_runner(shop_db)
    org, _ = I.introspect("shop", "sqlite", runner=run, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    area = org.subject_areas[0].name
    labels = {"placed": "Placed", "paid": "Paid"}  # a person's curation, short of the real values
    res = C.apply(root, [{"op": "edit", "kind": "table", "area": area, "name": "orders",
                          "column": "status", "field": "choice_field", "value": labels}])
    assert not res.errors, res.errors
    con = sqlite3.connect(shop_db)
    con.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
    con.execute("UPDATE orders SET channel = CASE id % 2 WHEN 0 THEN 'web' ELSE 'store' END")
    con.commit()
    con.close()

    I.introspect("shop", "sqlite", runner=run, artifacts_dir=tmp_path, tables=["main.orders"], append=True)
    t = load_datasource(root).subject_areas[0].defined_table("orders")
    assert t.get_column("status").choice_field == labels
    assert t.get_column("channel").choice_field == {"store": "", "web": ""}


# --- review fixes: only enforced, unambiguous CHECKs; rejected lists; exact parsing -------------


def _sql(db_type):
    return " ".join(D.get_dialect(db_type).sql_check_constraints("s", "orders").split())


def test_postgres_reads_only_validated_checks_with_a_name_no_other_table_uses():
    sql = _sql("postgres")
    assert "con.convalidated" in sql
    assert ("NOT EXISTS (SELECT 1 FROM pg_constraint o WHERE o.connamespace = con.connamespace "
            "AND o.conname = con.conname AND o.oid <> con.oid)") in sql


def test_mysql_reads_only_enforced_checks_with_a_name_unique_in_the_schema():
    sql = _sql("mysql")
    assert "tc.enforced = 'YES'" in sql
    assert "SELECT COUNT(*) FROM information_schema.table_constraints t2" in sql and sql.endswith(") = 1")
    fallback = D.get_dialect("mariadb").sql_check_constraints_fallback("s", "orders")
    assert "cc.table_name = 'orders'" in fallback and "cc.constraint_schema = 's'" in fallback


def test_sqlserver_and_oracle_read_only_enabled_trusted_checks():
    assert "cc.is_disabled = 0 AND cc.is_not_trusted = 0" in _sql("sqlserver")
    assert "status = 'ENABLED' AND validated = 'VALIDATED'" in _sql("oracle")


@pytest.mark.parametrize("db_type", ["postgres", "mysql", "mariadb", "sqlserver", "oracle"])
def test_the_filtered_check_reads_pass_the_always_on_gate(db_type):
    import sql_guard

    d = D.get_dialect(db_type)
    for q in (d.sql_check_constraints("s", "t"), d.sql_check_constraints_fallback("s", "t")):
        if q:
            assert sql_guard.check_read_only(q) is None and sql_guard.check_no_recon(q) is None, q


def _mysql_runner(*, mysql_form_fails: bool, mariadb_form_fails: bool = False):
    def run(sql):
        if "check_constraints" in sql:
            if "tc.enforced" in sql:
                if mysql_form_fails:
                    raise RuntimeError("Unknown column 'tc.enforced'")
                return [I._Row({"CHECK_CLAUSE": "(`status` in (_utf8mb4'open',_utf8mb4'closed'))"})]
            if mariadb_form_fails:
                raise RuntimeError("Unknown table 'check_constraints'")
            return [I._Row({"CHECK_CLAUSE": "`status` in ('open','closed')"})]
        return []
    return run


@pytest.mark.parametrize("mysql_fails, mariadb_fails, expected", [
    (False, False, {"status": ["open", "closed"]}),   # MySQL 8.0.16+
    (True, False, {"status": ["open", "closed"]}),    # MariaDB: the fallback form
    (True, True, {}),                                 # older MySQL: no view, nothing declared
])
def test_mariadb_falls_back_to_its_own_check_read(mysql_fails, mariadb_fails, expected):
    run = _mysql_runner(mysql_form_fails=mysql_fails, mariadb_form_fails=mariadb_fails)
    assert I._declared_values(D.get_dialect("mariadb"), run, "shop", "orders").values == expected


def test_a_declared_value_the_column_type_cant_hold_gives_no_list(tmp_path):
    db = tmp_path / "lv.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE orders(id INTEGER PRIMARY KEY, level INTEGER CHECK (level IN (1.0, 2.0)));")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(i, 1 + i % 2) for i in range(40)])
    con.commit()
    con.close()
    I.introspect("shop", "sqlite", runner=_sqlite_runner(db), artifacts_dir=tmp_path)
    org = load_datasource(tmp_path / "shop")
    assert _orders(org).get_column("level").choice_field is None
    assert V.validate(org).ok


def test_a_rejected_declaration_gets_no_list_from_the_data_either(tmp_path):
    seen: list[str] = []
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col("region", "USER-DEFINED"),
                            col("status", "text")]},
        enums=[{"column_name": "region", "enum_value": f"r{i:03d}"} for i in range(I.DECLARED_MAX_VALUES + 1)],
        sample=lambda sql: [{"id": i, "region": ["r000", "r001"][i % 2], "status": "ab"[i % 2]} for i in range(60)],
    )
    org, _ = I.introspect("shop", "postgres", runner=_recording(runner, seen), artifacts_dir=tmp_path, dry_run=True)
    t = _orders(org)
    assert t.get_column("region").choice_field is None
    assert not any('SELECT DISTINCT "region"' in s for s in seen)
    assert t.get_column("status").choice_field == {"a": "", "b": ""}  # an undeclared column still is


def test_mysql_enum_escapes_are_decoded():
    assert I._enum_type_values(r"enum('a\nb','c\td','e\\f','it\'s','g\rh','z\0','q\Zr','\%')") == [
        "a\nb", "c\td", "e\\f", "it's", "g\rh", "z\0", "q\x1ar", "%"]


@pytest.mark.parametrize("db_type, clause, expected", [
    # a cast to a string type on the column is Postgres's own spelling and changes nothing
    ("postgres", "CHECK (((status)::text = ANY ((ARRAY['a'::character varying, 'b'::character varying])::text[])))",
     [("status", ["a", "b"])]),
    ("postgres", "CHECK ((level = ANY (ARRAY[(1)::bigint, (2)::bigint])))", [("level", ["1", "2"])]),
    # any other cast changes which stored values pass: no list
    ("sqlite", "CREATE TABLE t (status TEXT CHECK (CAST(status AS INTEGER) IN (1, 2)))", []),
    ("postgres", "CHECK (((status)::character varying(1) = ANY (ARRAY['a'::text])))", []),
    ("postgres", "CHECK ((level = ANY (ARRAY['01'::integer, '2'::integer])))", []),
    # a number cast to a number type is unwrapped only when it keeps its value
    ("sqlite", "CREATE TABLE t (level DECIMAL CHECK (level IN (CAST(1.9 AS INTEGER), CAST(2.9 AS INTEGER))))", []),
    ("sqlite", "CREATE TABLE t (level DECIMAL CHECK (level IN (CAST(1.25 AS DECIMAL(3,1)), 2)))", []),
    ("postgres", "CHECK ((level = ANY ((ARRAY[1.9, 2])::integer[])))", []),
    ("sqlite", "CREATE TABLE t (level DECIMAL CHECK (level IN (CAST(-2 AS DECIMAL(3,1)), CAST(1.5 AS REAL))))",
     [("level", ["-2", "1.5"])]),
    ("postgres", "CHECK ((level = ANY (ARRAY[(1.5)::numeric, (2.25)::double precision])))", [("level", ["1.5", "2.25"])]),
    # MySQL's and SQL Server's DECIMAL with no scale rounds to a whole number
    ("mysql", "(`level` in (cast(1.5 as decimal),2))", []),
    ("sqlserver", "([level]=CAST(1.5 AS DECIMAL) OR [level]=(2))", []),
])
def test_only_casts_that_keep_the_values_are_unwrapped(db_type, clause, expected):
    assert I._check_lists(clause, D.get_dialect(db_type)) == expected


_CI = "this database compares values case-insensitively: 'open' also matches other capitalisations"
_CI_AI = ("this database compares values case- and accent-insensitively: 'open' also matches other "
          "capitalisations and accented spellings")


def _ci(caveats):
    return any(c.startswith(("this database compares values case-insensitively: '",
                             "this database compares values case- and accent-insensitively: '"))
               for c in caveats)


def _declared_runner(enum_type=None, check=None):
    def run(sql):
        if "data_type = 'enum'" in sql:
            return [I._Row({"COLUMN_NAME": "status", "COLUMN_TYPE": enum_type})] if enum_type else []
        if "check_constraints" in sql:
            return [I._Row({"check_clause": check})] if check else []
        return []
    return run


@pytest.mark.parametrize("db_type, run, caveat", [
    # MySQL/MariaDB's default *_ci collations ignore accents as well as case
    ("mysql", _declared_runner(check="(`status` in (_utf8mb4'open',_utf8mb4'closed'))"), _CI_AI),
    ("mariadb", _declared_runner(check="(`status` in (_utf8mb4'open',_utf8mb4'closed'))"), _CI_AI),
    ("mysql", _declared_runner(check="(`status` in (_utf8mb4\\'open\\',_utf8mb4\\'closed\\'))"), _CI_AI),
    ("sqlserver", _declared_runner(check="([status]='open' OR [status]='closed')"), _CI),
    ("mysql", _declared_runner(enum_type="enum('open','closed')"), None),   # an ENUM stores exactly
    ("postgres", _declared_runner(check="CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text])))"), None),
])
def test_a_check_list_on_a_case_insensitive_engine_gets_a_caveat(db_type, run, caveat):
    status = I.Column(name="status", type="string", description="")
    I._apply_declared_values(D.get_dialect(db_type), run, "s", "orders", [status])
    assert list(status.choice_field) == ["open", "closed"]
    assert status.caveats == ([caveat] if caveat else [])


@pytest.mark.parametrize("db_type, caveat", [("mysql", True), ("sqlserver", True), ("postgres", False)])
def test_a_data_list_on_a_case_insensitive_engine_gets_a_caveat(db_type, caveat):
    status = I.Column(name="status", type="string", description="")
    level = I.Column(name="level", type="integer", description="")

    def run(sql):
        if "DISTINCT" in sql:
            return [{"v": "open"}, {"v": "closed"}] if "status" in sql else [{"v": "1"}, {"v": "2"}]
        return [{"status": ["open", "closed"][i % 2], "level": str(1 + i % 2)} for i in range(40)]

    I._enrich_from_sample(D.get_dialect(db_type), run, "s", "t", [status, level], est_rows=None)
    assert set(status.choice_field) == {"open", "closed"} and _ci(status.caveats) is caveat
    assert level.choice_field == {"1": "", "2": ""} and level.caveats == []  # numbers have no case


def test_a_sqlite_nocase_column_gets_the_caveat(tmp_path):
    db = tmp_path / "nc.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE orders(id INTEGER PRIMARY KEY,"
        " status TEXT COLLATE NOCASE CHECK (status IN ('open','closed')),"
        " channel TEXT COLLATE NOCASE, kind TEXT CHECK (kind IN ('open','x')));")
    con.executemany("INSERT INTO orders VALUES (?,?,?,?)",
                    [(i, "open", ["open", "store"][i % 2], "x") for i in range(40)])
    con.commit()
    con.close()
    I.introspect("shop", "sqlite", runner=_sqlite_runner(db), artifacts_dir=tmp_path)
    t = _orders(load_datasource(tmp_path / "shop"))
    assert _CI in t.get_column("status").caveats            # a CHECK list on a NOCASE column
    assert _ci(t.get_column("channel").caveats)             # a data list on a NOCASE column
    assert not _ci(t.get_column("kind").caveats)          # SQLite compares exactly by default


def test_postgres_skips_no_inherit_checks():
    # a parent's NO INHERIT CHECK says nothing of its children's rows, which reading the parent returns
    assert "AND NOT con.connoinherit" in _sql("postgres")


def test_columns_whose_names_differ_only_by_case_get_no_declared_list(tmp_path):
    seen: list[str] = []
    runner = make_catalog_runner(
        tables=["orders"],
        columns={"orders": [col("id", "integer", nullable=False), col("status", "text"), col("STATUS", "text"),
                            col("kind", "text")]},
        checks=["CHECK ((status = ANY (ARRAY['b'::text, 'a'::text])))",
                "CHECK ((kind = ANY (ARRAY['x'::text, 'y'::text])))"],
        sample=lambda sql: [{"id": i, "status": "ab"[i % 2], "STATUS": "pq"[i % 2], "kind": "x"} for i in range(60)],
    )
    org, _ = I.introspect("shop", "postgres", runner=_recording(runner, seen), artifacts_dir=tmp_path, dry_run=True)
    t = _orders(org)
    assert list(t.get_column("kind").choice_field) == ["x", "y"]
    # neither gets the CHECK's list (declared order b, a); each reads its own values from the data
    assert list(t.get_column("status").choice_field) == ["a", "b"]
    assert list(t.get_column("STATUS").choice_field) == ["p", "q"]
    assert any('SELECT DISTINCT "status"' in s for s in seen) and any('SELECT DISTINCT "STATUS"' in s for s in seen)


@pytest.mark.parametrize("clause, expected", [
    ("CHECK (((status = 'a'::text) OR (\"STATUS\" = 'b'::text)))", []),     # two columns, not one
    ("CHECK (((\"Status\" = 'a'::text) OR (\"Status\" = 'b'::text)))", [("Status", ["a", "b"])]),
    ("CHECK (((status = 'a'::text) OR (STATUS = 'b'::text)))", [("status", ["a", "b"])]),  # both fold
])
def test_a_quoted_column_name_in_a_check_is_exact(clause, expected):
    assert I._check_lists(clause, D.get_dialect("postgres")) == expected


@pytest.mark.parametrize("raw, expected", [
    # MySQL 8's information_schema text, as the engine returns it: every quote and backslash escaped
    ("(`status` in (_utf8mb4\\'open\\',_utf8mb4\\'closed\\'))", [("status", ["open", "closed"])]),
    ("(`s` in (_utf8mb4\\'it\\\\\\'s\\',_utf8mb4\\'a\\\\\\\\b\\'))", [("s", ["it's", "a\\b"])]),
    # MariaDB's plain text is read as it is
    ("`s` in ('a\\'b','c')", [("s", ["a'b", "c"])]),
])
def test_mysql_check_text_is_unescaped(raw, expected):
    assert I._check_lists(I._mysql_check_text(raw), D.get_dialect("mysql")) == expected
