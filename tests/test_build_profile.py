"""Joins are checked on the data at build time, on the whole key or a spread-out sample (#448).

A join used to be confirmed on 50 distinct values with one match enough, and the card said
"100% of 38 sampled values match" without saying whether 38 was every value. These tests pin the
check: whole key under the size guard, a bounded sample above it, a match rate for declared keys,
both directions and a repeating key, several checks to a statement. Synthetic, generic names only.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import profile as PF  # noqa: E402
from semantic_model.models import Column, PerformanceHints, Table  # noqa: E402

# orders.customer_id: 5 rows, one blank, one pointing at a customer that isn't there (9).
# customers 3 has no order. visits.region_code points at regions.code, which repeats.
_SCHEMA = (
    "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);"
    "CREATE TABLE regions(code TEXT, loaded_on TEXT);"
    "CREATE TABLE visits(id INTEGER PRIMARY KEY, region_code TEXT);"
    "INSERT INTO customers VALUES (1, 'a'), (2, 'b'), (3, 'c');"
    "INSERT INTO orders VALUES (1, 1, 2.0), (2, 1, 3.0), (3, 2, 1.0), (4, NULL, 5.0), (5, 9, 1.0);"
    "INSERT INTO regions VALUES ('n', '2024-01-01'), ('n', '2024-02-01'), ('s', '2024-01-01');"
    "INSERT INTO visits VALUES (1, 'n'), (2, 's');"
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return path


def _runner(db: Path, seen: list[str] | None = None, fail_on: str | None = None):
    def run(sql: str) -> list[dict]:
        if seen is not None:
            seen.append(sql)
        if fail_on and fail_on in sql:
            raise RuntimeError("refused")
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            con.close()
    return run


def _t(name: str, cols: list[str], rows: int | None) -> Table:
    return Table(name=name, schema=None, storage_connection="c", grain=[], description="",
                 columns=[Column(name=c, type="string", description="") for c in cols],
                 performance_hints=PerformanceHints(estimated_row_count=rows) if rows is not None else None)


ORDERS = _t("orders", ["id", "customer_id"], 5)
CUSTOMERS = _t("customers", ["id"], 3)
SQLITE = D.get_dialect("sqlite")


def test_a_small_table_is_checked_on_the_whole_key_with_both_directions(db):
    c = PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"])
    PF.run_checks(SQLITE, _runner(db), [c])
    assert c.read == "whole"
    assert (c.rows_read, c.rows_keyed, c.rows_matched) == (5, 4, 3)
    assert (c.values, c.values_matched, c.to_keys, c.to_rows) == (3, 2, 3, 3)
    assert c.rate == pytest.approx(2 / 3) and not c.borne_out and not c.fans_out
    assert c.summary() == (
        "2 of 3 values of orders.customer_id are found in customers.id (67%) (whole table checked); "
        "2 of 5 orders rows have no match (1 blank): an inner join drops them; "
        "1 of 3 customers keys have no orders row")


def test_every_value_matching_says_all_and_that_the_whole_table_was_read(db):
    c = PF.JoinCheck(ORDERS, ["id"], CUSTOMERS, ["id"])   # 1..5 against 1..3
    PF.run_checks(SQLITE, _runner(db), [c])
    assert c.summary().startswith("3 of 5 values of orders.id")
    ok = PF.JoinCheck(_t("customers", ["id"], 3), ["id"], _t("orders", ["id"], 5), ["id"])
    PF.run_checks(SQLITE, _runner(db), [ok])
    assert ok.borne_out and ok.summary().startswith("all 3 values of customers.id are found in orders.id "
                                                    "(whole table checked)")


def test_a_key_that_repeats_on_the_side_it_points_at_is_called_a_fan_out(db):
    c = PF.JoinCheck(_t("visits", ["region_code"], 2), ["region_code"], _t("regions", ["code"], 3), ["code"])
    PF.run_checks(SQLITE, _runner(db), [c])
    assert c.borne_out and c.fans_out
    assert "regions.code repeats (3 rows for 2 values): each row joins to several" in c.summary()


def test_a_large_table_is_read_as_a_bounded_sample_and_says_so(db, monkeypatch):
    monkeypatch.setattr(PF, "JOIN_WHOLE_MAX_ROWS", 2)
    monkeypatch.setattr(PF, "JOIN_SAMPLE_ROWS", 4)
    seen: list[str] = []
    c = PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"])
    PF.run_checks(SQLITE, _runner(db, seen), [c])
    assert "LIMIT 4" in seen[0]
    assert c.read == "first_rows" and c.rows_read == 4
    assert "sampled orders rows are found in customers.id" in c.summary()
    assert "the first rows read, 4 rows" in c.summary()
    assert "inner join drops" not in c.summary()   # a sample says nothing about the other rows


def test_several_checks_share_one_statement_and_a_failing_one_costs_only_itself(db):
    seen: list[str] = []
    checks = [PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"]) for _ in range(3)]
    PF.run_checks(SQLITE, _runner(db, seen), checks)
    assert len(seen) == 1 and seen[0].count("UNION ALL") == 2
    assert all(c.rows_matched == 3 for c in checks)

    seen.clear()
    bad = PF.JoinCheck(ORDERS, ["customer_id"], _t("missing_table", ["id"], 1), ["id"])
    good = PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"])
    PF.run_checks(SQLITE, _runner(db, seen), [bad, good])
    assert len(seen) == 3   # the batch, then each alone
    assert bad.failed and bad.rate is None and bad.summary() == "the check could not compare the two columns"
    assert good.ran and good.rows_matched == 3


def test_many_checks_run_in_batches_of_the_statement_size(db):
    seen: list[str] = []
    checks = [PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"]) for _ in range(PF.CHECKS_PER_STATEMENT * 2 + 1)]
    PF.run_checks(SQLITE, _runner(db, seen), checks)
    assert len(seen) == 3 and all(c.ran for c in checks)


def test_a_composite_key_is_matched_on_all_its_columns(tmp_path):
    path = tmp_path / "c.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE lines(o INTEGER, n INTEGER); CREATE TABLE ships(o INTEGER, n INTEGER);"
                      "INSERT INTO lines VALUES (1,1),(1,2); INSERT INTO ships VALUES (1,1),(1,3);")
    con.close()
    c = PF.JoinCheck(_t("ships", ["o", "n"], 2), ["o", "n"], _t("lines", ["o", "n"], 2), ["o", "n"])
    PF.run_checks(SQLITE, _runner(path), [c])
    assert (c.values, c.values_matched) == (2, 1)
    assert c.summary().startswith("1 of 2 values of ships(o, n) are found in lines(o, n)")


def test_a_join_to_a_table_too_large_to_read_is_not_checked():
    assert PF.checkable(SQLITE, ORDERS, CUSTOMERS)
    assert not PF.checkable(SQLITE, ORDERS, _t("huge", ["id"], PF.JOIN_TARGET_MAX_ROWS + 1))


# --- one spread-out sample clause per engine that has one -------------------------------------

_CLAUSES = {
    "postgres": "TABLESAMPLE SYSTEM (2)",
    "supabase": "TABLESAMPLE SYSTEM (2)",
    "duckdb": "TABLESAMPLE 2%",
    "snowflake": "SAMPLE SYSTEM (2)",
    "bigquery": "TABLESAMPLE SYSTEM (2 PERCENT)",
    "databricks": "TABLESAMPLE (2 PERCENT)",
    "sqlserver": "TABLESAMPLE (2 PERCENT)",
    "trino": "TABLESAMPLE BERNOULLI (2)",
    "oracle": "SAMPLE (2)",
}


@pytest.mark.parametrize("key", sorted(_CLAUSES))
def test_an_engine_with_a_sample_clause_spreads_the_sample(key):
    d = D.get_dialect(key)
    sql, how = d.sampled_rows("SELECT a FROM t", 5_000, 500_000)
    assert how == "sample" and _CLAUSES[key] in sql and "5000" in sql


@pytest.mark.parametrize("key", ["redshift", "mysql", "sqlite"])
def test_an_engine_without_one_reads_the_first_rows_bounded(key):
    sql, how = D.get_dialect(key).sampled_rows("SELECT a FROM t", 5_000, 500_000)
    assert how == "first_rows" and sql == "SELECT a FROM t LIMIT 5000"


def test_an_unknown_size_or_a_small_table_is_not_sampled_by_share():
    pg = D.get_dialect("postgres")
    assert pg.sampled_rows("SELECT a FROM t", 5_000, None) == ("SELECT a FROM t LIMIT 5000", "first_rows")
    assert pg.sampled_rows("SELECT a FROM t", 5_000, 4_000)[1] == "first_rows"
    assert "TABLESAMPLE SYSTEM (0.0001)" in pg.sampled_rows("SELECT a FROM t", 1, 10**12)[0]
    assert "SAMPLE (99)" in D.get_dialect("oracle").sampled_rows("SELECT a FROM t", 9, 10)[0]


_SQLGLOT = {"postgres": "postgres", "duckdb": "duckdb", "snowflake": "snowflake", "bigquery": "bigquery",
            "databricks": "databricks", "sqlserver": "tsql", "trino": "trino", "oracle": "oracle",
            "mysql": "mysql", "redshift": "redshift", "sqlite": "sqlite"}


@pytest.mark.parametrize("key", sorted(_SQLGLOT))
def test_a_sampled_check_parses_in_its_engine(key):
    d = D.get_dialect(key)
    big = _t("orders", ["customer_id"], 10_000_000)
    sql, _ = PF.check_sql(d, PF.JoinCheck(big, ["customer_id"], CUSTOMERS, ["id"]), 0)
    sqlglot.parse_one(sql, read=_SQLGLOT[key])


def test_a_refused_sample_clause_falls_back_to_the_first_rows(db, monkeypatch):
    """A view, say, may not take the engine's sample clause: the check is read from the first rows."""
    monkeypatch.setattr(PF, "JOIN_WHOLE_MAX_ROWS", 2)
    monkeypatch.setattr(D.SQLite, "tablesample", lambda self, pct: f"TABLESAMPLE SYSTEM ({pct})", raising=False)
    c = PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"])
    PF.run_checks(SQLITE, _runner(db, fail_on="TABLESAMPLE"), [c])
    assert c.read == "first_rows" and c.ran


# --- in a build ------------------------------------------------------------------------------

def _build(tmp_path: Path, db: Path, db_type: str = "sqlite", seen: list[str] | None = None):
    return I.introspect("demo", db_type, runner=_runner(db, seen), artifacts_dir=tmp_path,
                        out_dir=tmp_path / "demo")


_FK = (
    "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), amount REAL);"
    "INSERT INTO customers VALUES (1, 'a'), (2, 'b');"
)


def _fk_db(tmp_path: Path, orders: str) -> Path:
    path = tmp_path / "fk.db"
    con = sqlite3.connect(path)
    con.executescript(_FK + orders)
    con.close()
    return path


def test_every_join_of_a_build_is_checked_and_reported(tmp_path):
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 7, 1.0), (3, 8, 1.0);")
    _, report = _build(tmp_path, path)
    [chk] = [c for c in report.join_checks if c["join"].startswith("orders")]
    assert chk["read"] == "whole" and chk["declared"] and chk["values"] == 3 and chk["values_matched"] == 1
    text = report.render()
    assert "## joins checked on the data (1 of 1: 1 on the whole table, 0 on a sample; 1 worth a look)" in text
    assert "! orders.customer_id -> customers.id: 1 of 3 values of orders.customer_id" in text


def test_an_unenforced_declared_key_needs_a_match_rate_not_one_hit(tmp_path, monkeypatch):
    """One matching value used to confirm a declared key on an engine that doesn't enforce it."""
    monkeypatch.setattr(D.SQLite, "fk_enforced", False)
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 7, 1.0), (3, 8, 1.0);")
    org, _ = _build(tmp_path, path)
    [r] = [r for sa in org.subject_areas for r in sa.relationships if r.from_table == "orders"]
    assert r.confidence == "inferred" and r.review_state == "unreviewed"


def test_an_unenforced_declared_key_the_data_bears_out_is_confirmed_and_read_once(tmp_path, monkeypatch):
    monkeypatch.setattr(D.SQLite, "fk_enforced", False)
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 1, 1.0), (3, NULL, 1.0);")
    seen: list[str] = []
    org, report = _build(tmp_path, path, seen=seen)
    [r] = [r for sa in org.subject_areas for r in sa.relationships if r.from_table == "orders"]
    assert r.confidence == "confirmed" and r.review_state == "approved"
    assert r.description == "declared FK, confirmed on the data"   # no figure in the model
    checks = [s for s in seen if "AS rows_read" in s and '"orders"' in s.split("LEFT JOIN")[0]
              and '"customers"' in s]
    assert len(checks) == 1   # the later check of every join reuses it
    assert report.join_checks[0]["match_rate"] == 1.0


def test_a_sample_that_comes_back_empty_is_read_again_from_the_first_rows(db, monkeypatch):
    """A share of a table taken from a stale estimate can return no rows at all."""
    monkeypatch.setattr(PF, "JOIN_WHOLE_MAX_ROWS", 2)
    monkeypatch.setattr(D.SQLite, "tablesample", lambda self, pct: "WHERE 1 = 0", raising=False)
    stale = _t("orders", ["customer_id"], 10**9)
    checks = [PF.JoinCheck(stale, ["customer_id"], CUSTOMERS, ["id"]) for _ in range(2)]
    PF.run_checks(SQLITE, _runner(db), checks)
    assert all(c.read == "first_rows" and c.rows_read == 5 for c in checks)
    empty = PF.JoinCheck(stale, ["customer_id"], CUSTOMERS, ["id"])
    empty.read, empty.rows_read = "sample", 0
    assert empty.summary() == "no orders rows were read"


# --- the column profile ----------------------------------------------------------------------

def _claims_db(tmp_path: Path) -> Path:
    """30 claims: payment_date before submitted_date on every row (a misnamed date), created_on in
    the future on one, a note that's almost always blank, a constant source, an empty column, a
    status list, a contact email, and a text date column written two ways."""
    path = tmp_path / "claims.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE claims(id INTEGER PRIMARY KEY, status TEXT, submitted_date DATE, payment_date DATE,"
                " created_on DATE, note TEXT, source TEXT, unused TEXT, contact_email TEXT, logged TEXT)")
    for i in range(30):
        con.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (i, "open" if i < 20 else "paid", f"2024-02-{i % 28 + 1:02d}", f"2024-01-{i % 28 + 1:02d}",
                     "2999-01-01" if i == 0 else "2024-01-01", "x" if i == 0 else "", "web", None,
                     f"p{i}@example.com", f"2024-01-{i % 28 + 1:02d}" if i % 2 else f"{i % 28 + 1:02d}/01/2024"))
    con.commit()
    con.close()
    return path


def _claims_table(rows: int | None = 30) -> Table:
    types = {"id": "integer", "status": "string", "submitted_date": "date", "payment_date": "date",
             "created_on": "date", "note": "string", "source": "string", "unused": "string",
             "contact_email": "string", "logged": "string"}
    t = _t("claims", list(types), rows)
    for c in t.columns:
        c.type = types[c.name]
        c.sensitive = c.name == "contact_email"
    return t


def _profile(tmp_path, **kw):
    db = _claims_db(tmp_path)
    seen: list[str] = []
    t = _claims_table(kw.pop("rows", 30))
    sample = _runner(db)("SELECT * FROM claims")
    prof = PF.profile_table(SQLITE, _runner(db, seen), t, lambda c: c.sensitive, sample, today="2026-01-01")
    return prof, seen


def test_a_table_is_profiled_in_two_statements_with_rates_counts_ranges_and_top_values(tmp_path):
    prof, seen = _profile(tmp_path)
    assert len(seen) == 2
    assert prof["read"] == "whole" and prof["rows_read"] == 30
    cols = prof["columns"]
    assert cols["status"]["distinct"] == 2
    assert cols["status"]["top"] == [{"value": "open", "share": 0.667}, {"value": "paid", "share": 0.333}]
    assert (cols["payment_date"]["min"], cols["payment_date"]["max"]) == ("2024-01-01", "2024-01-28")
    assert cols["note"]["blank_text"] == pytest.approx(0.967) and cols["unused"]["filled"] == 0.0


def test_a_personal_column_gets_counts_and_never_a_value(tmp_path):
    prof, seen = _profile(tmp_path)
    email = prof["columns"]["contact_email"]
    assert email["personal"] and email["distinct"] == 30
    assert "top" not in email and "min" not in email
    assert not any('"contact_email"' in s for s in seen[1:])   # not in the values read


def test_the_profile_finds_the_quirks_that_give_a_plausible_wrong_number(tmp_path):
    prof, _ = _profile(tmp_path)
    kinds = {(f["column"], f["kind"]) for f in prof["findings"]}
    assert kinds == {("unused", "empty"), ("note", "mostly_blank"), ("source", "constant"),
                     ("created_on", "date_range"), ("payment_date", "date_order"), ("logged", "mixed_formats")}
    assert {"earlier": "payment_date", "later": "submitted_date", "share": 1.0, "rows": 30} in prof["date_order"]
    detail = {f["kind"]: f["detail"] for f in prof["findings"]}
    assert detail["date_order"].startswith("payment_date falls before submitted_date on 100% of rows")
    assert detail["mixed_formats"] == "logged mixes ISO dates (2024-01-31) and slash dates (31/01/2024)"


def test_a_clean_table_has_no_findings(tmp_path):
    path = tmp_path / "clean.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders(id INTEGER, placed DATE, closed DATE, status TEXT)")
    for i in range(30):
        con.execute("INSERT INTO orders VALUES (?,?,?,?)",
                    (i, f"2024-01-{i % 28 + 1:02d}", f"2024-02-{i % 28 + 1:02d}", "open" if i % 2 else "done"))
    con.commit()
    con.close()
    t = _t("orders", ["id", "placed", "closed", "status"], 30)
    for c, ty in zip(t.columns, ["integer", "date", "date", "string"]):
        c.type = ty
    prof = PF.profile_table(SQLITE, _runner(path), t, lambda c: False, _runner(path)("SELECT * FROM orders"))
    assert "findings" not in prof
    assert prof["date_order"] == [{"earlier": "placed", "later": "closed", "share": 1.0, "rows": 30}]


def test_a_large_table_is_profiled_on_a_bounded_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(PF, "PROFILE_WHOLE_MAX_ROWS", 10)
    monkeypatch.setattr(PF, "PROFILE_SAMPLE_ROWS", 12)
    prof, seen = _profile(tmp_path, rows=10**6)
    assert prof["read"] == "first_rows" and prof["rows_read"] == 12 and prof["rows_estimated"] == 10**6
    assert all("LIMIT 12" in s for s in seen)


def test_a_table_that_cannot_be_read_has_no_profile_and_a_failed_value_read_keeps_the_counts(tmp_path):
    db = _claims_db(tmp_path)
    t = _claims_table()
    assert PF.profile_table(SQLITE, _runner(db, fail_on="COUNT(*) AS n"), t, lambda c: False) is None
    prof = PF.profile_table(SQLITE, _runner(db, fail_on="UNION ALL"), t, lambda c: False)
    assert prof["columns"]["status"]["distinct"] == 2 and "top" not in prof["columns"]["status"]


def test_a_wide_table_reads_a_bounded_number_of_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(PF, "PROFILE_MAX_COLUMNS", 3)
    prof, _ = _profile(tmp_path)
    assert list(prof["columns"]) == ["id", "status", "submitted_date"] and prof["columns_not_read"] == 7


@pytest.mark.parametrize("key", sorted(_SQLGLOT))
def test_the_profile_statements_parse_in_each_engine(key):
    d = D.get_dialect(key)
    t = _claims_table(10**7)
    src, _ = PF._table_rows(d, t, [c.name for c in t.columns])
    stats = [PF._ColStat(c.name, c.type, False) for c in t.columns]
    sqlglot.parse_one(PF.stats_sql(d, src, stats, [(2, 3)]), read=_SQLGLOT[key])
    sqlglot.parse_one(PF.top_sql(d, src, [(1, stats[1]), (0, stats[0])]), read=_SQLGLOT[key])


@pytest.mark.parametrize("key,cast", [("postgres", "CAST(x AS VARCHAR(255))"), ("mysql", "CAST(x AS CHAR)"),
                                      ("bigquery", "CAST(x AS STRING)"), ("databricks", "CAST(x AS STRING)")])
def test_values_are_read_as_text_in_each_engines_spelling(key, cast):
    assert D.get_dialect(key).text_cast("x") == cast


def test_a_build_writes_the_profile_and_the_join_checks_beside_the_model(tmp_path):
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 1, 1.0), (3, NULL, 1.0);")
    _, report = _build(tmp_path, path)
    import json
    doc = json.loads((tmp_path / "demo" / ".introspect" / "profile.json").read_text())
    assert doc["version"] == 1 and set(doc["tables"]) == {"main.customers", "main.orders"}
    assert doc["tables"]["main.orders"]["columns"]["customer_id"]["filled"] == pytest.approx(0.667)
    assert [j["join"] for j in doc["joins"]] == ["orders.customer_id -> customers.id"]
    assert "## column profile (2 tables read" in report.render()


def test_a_batched_build_keeps_the_tables_an_earlier_batch_profiled(tmp_path):
    import json
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0);")
    out = tmp_path / "demo"
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out, tables=["main.customers"])
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out, tables=["main.orders"],
                 append=True)
    doc = json.loads((out / ".introspect" / "profile.json").read_text())
    assert set(doc["tables"]) == {"main.customers", "main.orders"}


def test_sm_profile_prints_the_tables_asked_for_and_the_joins_that_touch_them(tmp_path, capsys):
    import json

    from semantic_model import cli
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0);")
    _build(tmp_path, path)
    root = str(tmp_path / "demo")
    assert cli.main(["profile", root, "--table", "customers"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert list(doc["tables"]) == ["main.customers"] and len(doc["joins"]) == 1
    assert cli.main(["profile", root, "--table", "main.orders"]) == 0
    assert list(json.loads(capsys.readouterr().out)["tables"]) == ["main.orders"]
    assert cli.main(["profile", str(tmp_path / "nowhere")]) == 1
    assert "run introspect first" in capsys.readouterr().out


def test_a_join_checked_on_the_whole_key_settles_uniqueness_with_no_extra_probe(tmp_path):
    """The many side of a join whose values turn out unique is flagged one-to-one for review (#511);
    the join check already counted its rows and distinct values, so no COUNT(DISTINCT) runs."""
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 2, 1.0);")
    seen: list[str] = []
    org, _ = _build(tmp_path, path, seen=seen)
    [r] = [r for sa in org.subject_areas for r in sa.relationships if r.from_table == "orders"]
    assert "unique on 2 rows" in r.description and r.review_state == "unreviewed"
    assert not any("AS distinct_count" in s for s in seen)
