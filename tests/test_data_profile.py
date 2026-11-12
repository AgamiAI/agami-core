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
yaml = pytest.importorskip("yaml")

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
    # The rate is of rows with a key (#448 review): 3 of the 4 find a customer.
    assert c.rate == pytest.approx(3 / 4) and not c.borne_out and not c.fans_out
    assert c.target_read == "whole"
    assert c.summary() == (
        "3 of 4 orders rows find their customer_id in customers.id (75%; whole table checked, 2 of 3 values); "
        "2 of 5 orders rows have no match (1 blank): an inner join drops them; "
        "1 of 3 customers keys have no orders row")


def test_every_value_matching_says_all_and_that_the_whole_table_was_read(db):
    c = PF.JoinCheck(ORDERS, ["id"], CUSTOMERS, ["id"])   # 1..5 against 1..3
    PF.run_checks(SQLITE, _runner(db), [c])
    assert c.summary().startswith("3 of 5 orders rows find their id in customers.id (60%; whole table checked")
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
    assert "3 of 3 sampled orders rows find their customer_id in customers.id (100%" in c.summary()
    assert "the first rows read, 4 rows" in c.summary()
    assert "inner join drops" not in c.summary()   # a sample says nothing about the other rows


def test_several_checks_share_one_statement_and_a_failing_one_costs_only_itself(db):
    seen: list[str] = []
    checks = [PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"]) for _ in range(3)]
    PF.run_checks(SQLITE, _runner(db, seen), checks)
    assert len(seen) == 1 and seen[0].count("AS probe") == 3
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
    assert c.summary().startswith("1 of 2 ships rows find their o, n in lines(o, n) (50%; whole table checked")


def test_a_join_to_a_table_too_large_to_read_is_not_checked():
    assert PF.checkable(SQLITE, ORDERS, CUSTOMERS, ["customer_id"]) is None
    huge = _t("huge", ["a", "b"], PF.JOIN_TARGET_MAX_ROWS + 1)
    assert PF.checkable(SQLITE, ORDERS, huge, ["customer_id"]) is None   # looked up, not skipped
    assert "several columns" in PF.checkable(SQLITE, ORDERS, huge, ["a", "b"])


# --- one spread-out sample clause per engine that has one -------------------------------------

_CLAUSES = {
    "postgres": "TABLESAMPLE SYSTEM (2)",
    "supabase": "TABLESAMPLE SYSTEM (2)",
    "duckdb": "TABLESAMPLE 2%",
    "snowflake": "SAMPLE SYSTEM (2)",
    "bigquery": "TABLESAMPLE SYSTEM (2 PERCENT)",
    "sqlserver": "TABLESAMPLE (2 PERCENT)",
    "trino": "TABLESAMPLE SYSTEM (2)",
    "oracle": "SAMPLE BLOCK (2)",
}


@pytest.mark.parametrize("key", sorted(_CLAUSES))
def test_an_engine_with_a_sample_clause_spreads_the_sample(key):
    d = D.get_dialect(key)
    sql, how = d.sampled_rows("SELECT a FROM t", 5_000, 500_000)
    assert how == "sample" and _CLAUSES[key] in sql and "5000" in sql


@pytest.mark.parametrize("key", ["redshift", "mysql", "sqlite", "databricks"])
def test_an_engine_without_one_reads_the_first_rows_bounded(key):
    sql, how = D.get_dialect(key).sampled_rows("SELECT a FROM t", 5_000, 500_000)
    assert how == "first_rows" and sql == "SELECT a FROM t LIMIT 5000"


def test_an_unknown_size_or_a_small_table_is_not_sampled_by_share():
    pg = D.get_dialect("postgres")
    assert pg.sampled_rows("SELECT a FROM t", 5_000, None) == ("SELECT a FROM t LIMIT 5000", "first_rows")
    assert pg.sampled_rows("SELECT a FROM t", 5_000, 4_000)[1] == "first_rows"
    assert "TABLESAMPLE SYSTEM (0.0001)" in pg.sampled_rows("SELECT a FROM t", 1, 10**12)[0]
    assert "SAMPLE BLOCK (99)" in D.get_dialect("oracle").sampled_rows("SELECT a FROM t", 9, 10)[0]


_SQLGLOT = {"postgres": "postgres", "duckdb": "duckdb", "snowflake": "snowflake", "bigquery": "bigquery",
            "databricks": "databricks", "sqlserver": "tsql", "trino": "trino", "oracle": "oracle",
            "mysql": "mysql", "redshift": "redshift", "sqlite": "sqlite"}


@pytest.mark.parametrize("key", sorted(_SQLGLOT))
def test_a_sampled_check_parses_in_its_engine(key):
    d = D.get_dialect(key)
    big = _t("orders", ["customer_id"], 10_000_000)
    sql, _, _ = PF.check_sql(d, PF.JoinCheck(big, ["customer_id"], CUSTOMERS, ["id"]), 0)
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
    assert ("## joins checked on the data (1 of 1: 1 on the whole table, 0 on a sample; 0 with no values to "
            "compare; 1 worth a look)") in text
    assert "! orders.customer_id -> customers.id: 1 of 3 orders rows find their customer_id" in text


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
    prof = PF.profile_table(SQLITE, _runner(db, seen), t, lambda t, c: c.sensitive, sample, today="2026-01-01")
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


def test_a_column_of_one_off_values_has_no_frequent_values_listed(tmp_path):
    """30 distinct references on 30 rows is under the 50-value line, but nothing repeats."""
    prof, _ = _profile(tmp_path)
    assert prof["columns"]["id"]["distinct"] == 30 and "top" not in prof["columns"]["id"]
    assert "top" in prof["columns"]["status"]


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
    prof = PF.profile_table(SQLITE, _runner(path), t, lambda t, c: False, _runner(path)("SELECT * FROM orders"))
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
    assert PF.profile_table(SQLITE, _runner(db, fail_on="COUNT(*) AS n"), t, lambda t, c: False) is None
    prof = PF.profile_table(SQLITE, _runner(db, fail_on="UNION ALL"), t, lambda t, c: False)
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


def test_text_holding_iso_dates_is_profiled_as_dates(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders(id INTEGER, placed_at TEXT, shipped_at TEXT)")
    for i in range(25):
        con.execute("INSERT INTO orders VALUES (?,?,?)", (i, f"2024-01-{i + 1:02d} 10:00:00", f"2024-02-{i + 1:02d}"))
    con.commit()
    con.close()
    t = _t("orders", ["id", "placed_at", "shipped_at"], 25)
    t.columns[1].date_format = t.columns[2].date_format = "iso8601"
    prof = PF.profile_table(SQLITE, _runner(path), t, lambda t, c: False)
    assert prof["columns"]["placed_at"]["max"] == "2024-01-25 10:00:00"
    assert prof["date_order"] == [{"earlier": "placed_at", "later": "shipped_at", "share": 1.0, "rows": 25}]


def _checked(**kw) -> PF.JoinCheck:
    c = PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"], read="whole")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_keys_left_blank_are_named_as_such_and_an_empty_table_says_so():
    c = _checked(rows_read=10, rows_keyed=4, rows_matched=4, values=2, values_matched=2, to_keys=2, to_rows=2)
    assert c.summary() == ("all 2 values of orders.customer_id are found in customers.id (whole table checked); "
                           "6 of 10 orders rows leave customer_id blank: an inner join drops them")
    assert _checked().summary() == "orders has no rows"
    assert _checked(read="sample").summary() == "no orders rows were read"


def test_the_report_lists_common_findings_one_line_per_table(tmp_path):
    rep = I.IntrospectReport(profile="p", db_type="sqlite", out_dir=str(tmp_path), dry_run=True)
    rep.profile_tables = {"main.t": {"findings": [
        {"column": "a", "kind": "empty", "detail": "a is empty on every row read"},
        {"column": "b", "kind": "empty", "detail": "b is empty on every row read"},
        {"column": "d", "kind": "date_range", "detail": "d has dates after today"}]}}
    text = rep.render()
    assert "## column profile (1 tables read; 3 finding(s)" in text
    assert "  - main.t: empty on every row read: a, b" in text and "  ! main.t: d has dates after today" in text


# --- review of #448: personal data, bounds, retries ---------------------------------------------

def _coded_db(tmp_path: Path, values: list[str]) -> Path:
    path = tmp_path / "log.db"
    con = sqlite3.connect(path)
    con.execute("DROP TABLE IF EXISTS visits")
    con.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY, ref TEXT)")
    con.executemany("INSERT INTO visits VALUES (?, ?)", [(i, values[i % len(values)]) for i in range(40)])
    con.commit()
    con.close()
    return path


def _profile_doc(out: Path) -> dict:
    import json
    return json.loads((out / ".introspect" / "profile.json").read_text())


def test_a_column_that_turns_personal_on_a_refresh_loses_its_values(tmp_path):
    """Item 1: a merged column keeps its stored flags, so this run's reading must count too."""
    out = tmp_path / "demo"
    path = _coded_db(tmp_path, ["a", "b", "c"])
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out)
    assert "top" in _profile_doc(out)["tables"]["main.visits"]["columns"]["ref"]
    _coded_db(tmp_path, ["+1 415 555 0101", "+1 415 555 0102", "+1 415 555 0103"])
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out, append=True)
    ref = _profile_doc(out)["tables"]["main.visits"]["columns"]["ref"]
    assert ref["personal"] and not {"top", "min", "max"} & set(ref)
    assert "555" not in (out / ".introspect" / "profile.json").read_text()


def _small(tmp_path: Path, column: str, values: list, ctype: str = "string") -> dict:
    path = tmp_path / f"{column}.db"
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE parties(id INTEGER PRIMARY KEY, {column} TEXT)")
    con.executemany("INSERT INTO parties VALUES (?, ?)", [(i, values[i % len(values)]) for i in range(40)])
    con.commit()
    con.close()
    t = _t("parties", ["id", column], 40)
    t.columns[1].type = ctype
    return PF.profile_table(SQLITE, _runner(path), t, lambda t, c: False)["columns"][column]


@pytest.mark.parametrize("column", ["vendor_name", "party_name", "tax_id", "national_id", "contact_no",
                                    "license_no", "buyer_pan"])
def test_a_column_named_like_personal_data_gets_counts_only(tmp_path, column):
    """Item 2: a small table's name or number column repeats enough to look coded."""
    col = _small(tmp_path, column, ["X1", "X2", "X3"])
    assert col["personal"] and col["distinct"] == 3 and "top" not in col


def test_values_that_look_personal_or_like_guids_withhold_the_whole_column(tmp_path):
    assert "top" not in _small(tmp_path, "ref", ["you@example.com", "x", "y"])
    assert "top" not in _small(tmp_path, "kind", ["0f8fad5b-d9cb-469f-a165-70867728950e", "a", "b"])
    assert "top" in _small(tmp_path, "status", ["open", "paid", "void"])


def test_the_frequent_value_line_is_the_value_list_line(tmp_path):
    assert "top" not in _small(tmp_path, "status", [f"s{i}" for i in range(PF.PROFILE_TOP_MAX_DISTINCT + 1)])
    assert PF.PROFILE_TOP_MAX_DISTINCT == I.ENUM_MAX_DISTINCT


def test_pay_to_a_person_keeps_its_counts_and_loses_its_range(tmp_path):
    col = _small(tmp_path, "pay_rate", ["10", "20", "30"], ctype="integer")
    assert col["distinct"] == 3 and "min" not in col and "max" not in col


def test_a_full_build_rewrites_the_file_and_drops_what_the_model_no_longer_serves(tmp_path):
    """Item 3: only a batched build merges; every write is cut to what the model serves now."""
    import json
    out = tmp_path / "demo"
    path = _coded_db(tmp_path, ["a", "b", "c"])
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out)
    f = out / ".introspect" / "profile.json"
    doc = json.loads(f.read_text())
    doc["tables"]["main.gone"] = {"columns": {"x": {"top": [{"value": "secret", "share": 1.0}]}}}
    f.write_text(json.dumps(doc))
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out)
    assert set(_profile_doc(out)["tables"]) == {"main.visits"}
    f.write_text(json.dumps(doc))
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out, append=True)
    assert "main.gone" not in _profile_doc(out)["tables"]   # merged, then cut to the model


def test_a_column_flagged_after_the_build_is_scrubbed_when_the_profile_is_read(tmp_path, capsys):
    import json

    from semantic_model import cli
    out = tmp_path / "demo"
    path = _coded_db(tmp_path, ["a", "b", "c"])
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out)
    [table_file] = list(out.rglob("tables/visits.yaml"))
    doc = yaml.safe_load(table_file.read_text())
    for c in doc["columns"]:
        if c["name"] == "ref":
            c["sensitive"] = True
    table_file.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert cli.main(["profile", str(out)]) == 0
    ref = json.loads(capsys.readouterr().out)["tables"]["main.visits"]["columns"]["ref"]
    assert ref["personal"] and "top" not in ref


def test_an_unsized_object_is_not_read_where_a_bounded_read_can_still_scan_it(db):
    """Item 4: MySQL views and BigQuery tables whose size the catalog doesn't give."""
    mysql, bq = D.get_dialect("mysql"), D.get_dialect("bigquery")
    unsized = _t("v_orders", ["customer_id"], None)
    for d in (mysql, bq):
        assert not d.scans_unsized and "unknown size" in PF.checkable(d, unsized, CUSTOMERS, ["customer_id"])
        assert "unknown size" in PF.checkable(d, ORDERS, _t("v_c", ["id"], None), ["customer_id"])
        assert PF.profile_table(d, _runner(db), unsized, lambda t, c: False) is None
    assert PF.checkable(D.get_dialect("postgres"), unsized, CUSTOMERS, ["customer_id"]) is None


def test_a_key_into_a_table_too_large_to_read_is_looked_up_not_skipped(db, tmp_path, monkeypatch):
    """Item 5: only the keys the starting rows hold are read from the big table (an IN list)."""
    big = _t("customers", ["id"], PF.JOIN_TARGET_MAX_ROWS + 1)
    seen: list[str] = []
    c = PF.JoinCheck(ORDERS, ["customer_id"], big, ["id"])
    PF.run_checks(SQLITE, _runner(db, seen), [c])
    assert c.target_read == "lookup" and (c.rows_keyed, c.rows_matched) == (4, 3)
    assert '"id" IN (SELECT s2.v0 FROM' in seen[0]
    assert "keys have no" not in c.summary()   # the other side wasn't read whole
    unsized = PF.JoinCheck(ORDERS, ["customer_id"], _t("customers", ["id"], None), ["id"])
    assert "IN (SELECT" in PF.check_sql(SQLITE, unsized, 0)[0]   # an unknown size is not read whole
    # a declared key into a big table on an unenforcing engine is still confirmed
    monkeypatch.setattr(D.SQLite, "fk_enforced", False)
    monkeypatch.setattr(PF, "JOIN_TARGET_MAX_ROWS", 1)
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 1, 1.0);")
    org, _ = _build(tmp_path, path)
    [r] = [r for sa in org.subject_areas for r in sa.relationships if r.from_table == "orders"]
    assert r.confidence == "confirmed" and r.review_state == "approved"


@pytest.mark.parametrize("key,clause", [("trino", "TABLESAMPLE SYSTEM ("), ("oracle", "SAMPLE BLOCK (")])
def test_engines_that_sample_rows_sample_blocks_instead(key, clause):
    """Item 6: a row-level sample reads every row; a block sample reads only its share."""
    assert clause in D.get_dialect(key).sampled_rows("SELECT a FROM t", 5_000, 10**8)[0]
    assert D.get_dialect("databricks").sampled_rows("SELECT a FROM t", 5_000, 10**8)[1] == "first_rows"


class _Slow(RuntimeError):
    kind = "resource_limit"


def test_a_batch_that_runs_out_of_time_is_not_retried(db):
    """Item 7: retried check by check, each would run to the same limit."""
    seen: list[str] = []

    def slow(sql):
        seen.append(sql)
        raise _Slow("refused: resource_limit")
    checks = [PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"]) for _ in range(3)]
    PF.run_checks(SQLITE, slow, checks)
    assert len(seen) == 1 and all(c.failed for c in checks)


def test_a_lone_sample_that_runs_out_of_time_stops_and_a_refused_one_reads_the_first_rows(db, monkeypatch):
    monkeypatch.setattr(PF, "JOIN_WHOLE_MAX_ROWS", 2)
    monkeypatch.setattr(D.SQLite, "tablesample", lambda self, pct: "TABLESAMPLE SYSTEM (1)", raising=False)
    stale = _t("orders", ["customer_id"], 10**9)
    seen: list[str] = []

    def slow_sample(sql):
        seen.append(sql)
        if "TABLESAMPLE" in sql:
            raise _Slow("timed out")
        return _runner(db)(sql)
    c = PF.JoinCheck(stale, ["customer_id"], CUSTOMERS, ["id"])
    PF._one(SQLITE, slow_sample, c)
    assert c.failed and len(seen) == 1
    c = PF.JoinCheck(stale, ["customer_id"], CUSTOMERS, ["id"])
    PF._one(SQLITE, _runner(db, fail_on="TABLESAMPLE"), c)
    assert c.read == "first_rows" and c.rows_read == 5


def test_a_lone_check_whose_sample_comes_back_empty_reads_the_first_rows(db, monkeypatch):
    monkeypatch.setattr(PF, "JOIN_WHOLE_MAX_ROWS", 2)
    monkeypatch.setattr(D.SQLite, "tablesample", lambda self, pct: "WHERE 1 = 0", raising=False)
    c = PF.JoinCheck(_t("orders", ["customer_id"], 10**9), ["customer_id"], CUSTOMERS, ["id"])
    PF._one(SQLITE, _runner(db), c)
    assert c.read == "first_rows" and c.rows_read == 5


def test_one_column_of_an_odd_type_costs_its_detail_not_the_table(tmp_path):
    """Item 8: an enum or interval refusing TRIM or COUNT(DISTINCT) falls back to fill counts."""
    db = _claims_db(tmp_path)
    prof = PF.profile_table(SQLITE, _runner(db, fail_on="TRIM("), _claims_table(), lambda t, c: False)
    assert prof["rows_read"] == 30 and prof["columns"]["unused"]["filled"] == 0.0
    assert "distinct" not in prof["columns"]["status"]
    assert "CAST(" in PF.stats_sql(SQLITE, "SELECT 1", [PF._ColStat("s", "string", False)], [])


def test_a_repeating_key_is_a_fan_out_only_where_the_join_says_one_partner(db):
    """Item 9: on a one-to-many join the far side is meant to repeat."""
    regions = _t("regions", ["code"], 3)
    visits = _t("visits", ["region_code"], 2)
    one = PF.JoinCheck(visits, ["region_code"], regions, ["code"])
    many = PF.JoinCheck(visits, ["region_code"], regions, ["code"], target_is_one=False)
    PF.run_checks(SQLITE, _runner(db), [one, many])
    assert one.fans_out and not many.fans_out and "repeats" not in many.summary()


def test_a_shown_percent_never_rounds_up_past_the_rule():
    """Item 10: 94.96% must not show as the 95% a declared key needs."""
    assert (PF.percent(0.9496), PF.percent(0.9958), PF.percent(1.0), PF.percent(0.95)) == ("94%", "99%", "100%", "95%")
    c = _checked(rows_read=10_000, rows_keyed=10_000, rows_matched=9_496, values=10, values_matched=9,
                 to_keys=10, to_rows=10)
    assert not c.borne_out and "(94%;" in c.summary()


def test_the_match_rate_is_of_rows_on_a_whole_read_and_a_sample_alike():
    """Item 11: one value that holds most rows decides an inner join, not the count of values."""
    whole = _checked(rows_read=100, rows_keyed=100, rows_matched=99, values=2, values_matched=1, to_keys=1, to_rows=1)
    assert whole.rate == 0.99 and whole.borne_out
    sample = _checked(read="sample", rows_read=100, rows_keyed=100, rows_matched=99, values=2, values_matched=1)
    assert sample.rate == whole.rate


def test_past_the_cap_a_declared_key_stays_for_review_and_the_note_says_so(tmp_path, monkeypatch):
    """Item 12: one cap; declared keys are confirmed from the run's check of every join."""
    monkeypatch.setattr(D.SQLite, "fk_enforced", False)
    monkeypatch.setattr(PF, "JOIN_CHECK_CAP", 0)
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 1, 1.0);")
    org, report = _build(tmp_path, path)
    [r] = [r for sa in org.subject_areas for r in sa.relationships if r.from_table == "orders"]
    assert r.review_state == "unreviewed"
    assert any("capped at 0 checks per run" in n for n in report.notes)


def test_a_check_reads_the_table_it_points_at_once(db):
    """Item 13: one grouping of its keys gives the distinct count and the row count."""
    sql, _, _ = PF.check_sql(SQLITE, PF.JoinCheck(ORDERS, ["customer_id"], CUSTOMERS, ["id"]), 0)
    assert sql.count('FROM "customers"') == 1


def test_one_value_and_a_composite_key_left_blank_read_naturally():
    one = _checked(rows_read=3, rows_keyed=3, rows_matched=3, values=1, values_matched=1, to_keys=1, to_rows=1)
    assert one.summary().startswith("the one value of orders.customer_id is found")
    two = PF.JoinCheck(ORDERS, ["a", "b"], CUSTOMERS, ["a", "b"], read="whole", rows_read=4, rows_keyed=1,
                       rows_matched=1, values=1, values_matched=1, to_keys=1, to_rows=1)
    assert "3 of 4 orders rows leave a, b blank" in two.summary()


def test_a_browser_sign_in_is_detected_without_reading_a_secret(tmp_path, monkeypatch):
    """A build holds up to `WORKERS` connections; a browser sign-in would open that many windows."""
    from semantic_model import cli
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    assert not cli._signs_in_with_a_browser("demo")   # no credentials file
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "credentials").write_text(
        "[demo]\ntype = snowflake\nauthenticator = externalbrowser\n[acme]\ntype = postgres\npassword = %x\n")
    assert cli._signs_in_with_a_browser("demo") and not cli._signs_in_with_a_browser("acme")


# --- review of PR #650 ----------------------------------------------------------------------------

@pytest.mark.parametrize("column", ["vendorName", "partyName", "taxId"])
def test_a_camel_case_name_is_read_word_by_word_for_personal_data(tmp_path, column):
    """`vendorName` lower-cased whole is `vendorname`, which no word rule matched."""
    col = _small(tmp_path, column, ["X1", "X2", "X3"])
    assert col["personal"] and "top" not in col


def test_a_camel_case_pay_name_loses_its_range(tmp_path):
    col = _small(tmp_path, "bonusAmount", ["10", "20", "30"], ctype="integer")
    assert "min" not in col and "max" not in col


def _copy(area_cols: dict, **flags) -> Table:
    t = _t("visits", list(area_cols), 40)
    for c in t.columns:
        for k, v in area_cols[c.name].items():
            setattr(c, k, v)
    return t


def test_scrub_merges_every_area_copy_of_a_table(tmp_path):
    """A column personal in ANY copy loses its values; one served by ANY copy stays."""
    saved = {"visits": {"columns": {"ref": {"top": [{"value": "a", "share": 1.0}], "distinct": 1},
                                    "note": {"top": [{"value": "x", "share": 1.0}], "distinct": 1}}}}
    flagged = _copy({"ref": {"sensitive": True}, "note": {}})
    plain = _copy({"ref": {}, "note": {"review_state": "rejected"}})
    out = PF.scrub(saved, [flagged, plain], lambda t, c: c.sensitive)["visits"]["columns"]
    assert out["ref"]["personal"] and "top" not in out["ref"]
    assert "top" in out["note"]   # rejected in one copy, still served by the other
    out = PF.scrub(saved, [plain, flagged], lambda t, c: c.sensitive)["visits"]["columns"]
    assert out["ref"]["personal"] and "top" not in out["ref"]


def _set_join_state(out: Path, state: str) -> None:
    [rel_file] = list(out.rglob("relationships.yaml"))
    doc = yaml.safe_load(rel_file.read_text())
    for r in doc["relationships"]:
        r["review_state"] = state
    rel_file.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_sm_profile_drops_join_evidence_for_a_join_the_model_no_longer_serves(tmp_path, capsys):
    import json

    from semantic_model import cli
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0);")
    _build(tmp_path, path)
    out = tmp_path / "demo"
    assert cli.main(["profile", str(out)]) == 0
    assert len(json.loads(capsys.readouterr().out)["joins"]) == 1
    _set_join_state(out, "rejected")
    assert cli.main(["profile", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["joins"] == []


def test_a_one_to_many_join_whose_far_side_repeats_is_not_worth_a_look(tmp_path):
    rep = I.IntrospectReport(profile="p", db_type="sqlite", out_dir=str(tmp_path), dry_run=True)
    many = PF.JoinCheck(_t("visits", ["region_code"], 2), ["region_code"], _t("regions", ["code"], 3), ["code"],
                        target_is_one=False, read="whole", target_read="whole", rows_read=2, rows_keyed=2,
                        rows_matched=2, values=2, values_matched=2, to_keys=2, to_rows=3, to_rows_matched=3)
    rep.join_checks = [{"join": "visits.region_code -> regions.code", **many.evidence()}]
    assert "0 worth a look" in rep.render()


def test_fan_out_counts_only_the_keys_the_starting_rows_match(tmp_path):
    """Customer 2 repeating says nothing about orders that only ever point at customer 1."""
    path = tmp_path / "f.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE customers(id INTEGER); CREATE TABLE orders(id INTEGER, customer_id INTEGER);"
                      "INSERT INTO customers VALUES (1), (2), (2); INSERT INTO orders VALUES (1, 1), (2, 1);")
    con.close()
    cust = _t("customers", ["id"], 3)
    c = PF.JoinCheck(_t("orders", ["customer_id"], 2), ["customer_id"], cust, ["id"])
    PF.run_checks(SQLITE, _runner(path), [c])
    assert not c.fans_out and "repeats" not in c.summary() and not c.evidence()["fans_out"]
    con = sqlite3.connect(path)
    con.execute("INSERT INTO customers VALUES (1)")
    con.commit()
    con.close()
    c = PF.JoinCheck(_t("orders", ["customer_id"], 2), ["customer_id"], cust, ["id"])
    PF.run_checks(SQLITE, _runner(path), [c])
    assert c.fans_out and "customers.id repeats (2 rows for 1 values" in c.summary()


def test_a_refresh_confirms_the_declared_key_it_keeps_not_the_copy_it_drops(tmp_path, monkeypatch):
    """A declared key left for review on the first build is approved once the data bears it out."""
    monkeypatch.setattr(D.SQLite, "fk_enforced", False)
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 7, 1.0), (3, 7, 1.0);")
    out = tmp_path / "demo"
    org, _ = _build(tmp_path, path)
    assert _orders_join(org).review_state == "unreviewed"
    con = sqlite3.connect(path)
    con.execute("UPDATE orders SET customer_id = 1")
    con.commit()
    con.close()
    org, _ = I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out, append=True)
    assert _orders_join(org).review_state == "approved"


def test_a_refresh_leaves_a_declared_key_a_person_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(D.SQLite, "fk_enforced", False)
    path = _fk_db(tmp_path, "INSERT INTO orders VALUES (1, 1, 1.0), (2, 1, 1.0);")
    out = tmp_path / "demo"
    _build(tmp_path, path)
    _set_join_state(out, "rejected")
    I.introspect("demo", "sqlite", runner=_runner(path), artifacts_dir=tmp_path, out_dir=out, append=True)
    from semantic_model.loader import load_datasource
    assert _orders_join(load_datasource(out, include_rejected=True)).review_state == "rejected"


def _orders_join(org):
    [r] = [r for sa in org.subject_areas for r in sa.relationships if r.from_table == "orders"]
    return r


def test_a_join_with_a_condition_beyond_its_keys_is_reported_unchecked(db, tmp_path):
    from semantic_model.models import Relationship
    rel = Relationship(from_table="visits", to_table="regions", relationship="many_to_one",
                       on="visits.region_code = regions.code AND regions.loaded_on = '2024-01-01'")
    rep = I.IntrospectReport(profile="p", db_type="sqlite", out_dir=str(tmp_path), dry_run=True)
    tables = [_t("visits", ["id", "region_code"], 2), _t("regions", ["code", "loaded_on"], 3)]
    seen: list[str] = []
    todo = I._check_joins(SQLITE, _runner(db, seen), [rel], tables, rep, "sqlite", {})
    assert todo == [] and seen == []
    assert any("a condition beyond its key columns" in n for n in rep.notes)


def test_a_profile_whose_sample_clause_is_refused_reads_the_first_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(PF, "PROFILE_WHOLE_MAX_ROWS", 10)
    monkeypatch.setattr(D.SQLite, "tablesample", lambda self, pct: "TABLESAMPLE SYSTEM (1)", raising=False)
    db = _claims_db(tmp_path)
    t = _claims_table(10**6)
    prof = PF.profile_table(SQLITE, _runner(db, fail_on="TABLESAMPLE"), t, lambda t, c: False)
    assert prof["read"] == "first_rows" and prof["rows_read"] == 30 and "top" in prof["columns"]["status"]


def test_a_profile_whose_sample_comes_back_empty_reads_the_first_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(PF, "PROFILE_WHOLE_MAX_ROWS", 10)
    monkeypatch.setattr(D.SQLite, "tablesample", lambda self, pct: "WHERE 1 = 0", raising=False)
    prof = PF.profile_table(SQLITE, _runner(_claims_db(tmp_path)), _claims_table(10**6), lambda t, c: False)
    assert prof["read"] == "first_rows" and prof["rows_read"] == 30


def test_a_profile_that_runs_out_of_time_is_not_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr(PF, "PROFILE_WHOLE_MAX_ROWS", 10)
    seen: list[str] = []

    def slow(sql):
        seen.append(sql)
        raise _Slow("timed out")
    assert PF.profile_table(SQLITE, slow, _claims_table(10**6), lambda t, c: False) is None
    assert len(seen) == 1


def test_the_reverse_date_share_counts_rows_where_both_dates_are_equal(tmp_path):
    """25 same-day pairs and 2 backwards ones: not 'the reverse on 100% of rows'."""
    path = tmp_path / "eq.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE claims(id INTEGER, submitted_date DATE, payment_date DATE)")
    rows = [(i, "2024-01-05", "2024-01-05") for i in range(25)] + [(90, "2024-01-05", "2024-01-01"),
                                                                    (91, "2024-01-06", "2024-01-02")]
    con.executemany("INSERT INTO claims VALUES (?,?,?)", rows)
    con.commit()
    con.close()
    t = _t("claims", ["id", "submitted_date", "payment_date"], 27)
    for c, ty in zip(t.columns, ["integer", "date", "date"]):
        c.type = ty
    prof = PF.profile_table(SQLITE, _runner(path), t, lambda t, c: False)
    assert "findings" not in prof
    [order] = prof["date_order"]
    assert order["rows"] == 27 and order["share"] == pytest.approx(round(2 / 27, 3))
