"""A table's per-column reads, combined into a few statements (value lists, grain and key checks).

A build used to send one statement per column for its value lists, and one per candidate for the
grain and unenforced-key checks. Through `execute_sql.py` every statement is a new process and a new
connection, and on a view the view itself runs again each time: a 60-column view cost 60 of them. The
reads now go 25 columns to a statement, and the model they produce is the same one."""

from __future__ import annotations

import math
import os
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402

N_CODES = 35   # coded text columns on the wide table, each a value-list candidate
ROWS = 200


def _schema(con: sqlite3.Connection) -> None:
    codes = ", ".join(f"code_{i:02d} TEXT" for i in range(N_CODES))
    con.execute(f"CREATE TABLE wide (id INTEGER PRIMARY KEY, store_id INTEGER, region_id INTEGER, "
                f"agent_id INTEGER, batch_id INTEGER, {codes}, order_year INTEGER)")
    con.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, kind TEXT, tier TEXT)")
    # A view has no primary key, so its grain is probed on the data: four id-named columns that
    # repeat, then the one that doesn't.
    con.execute("CREATE VIEW wide_v AS SELECT store_id, region_id, agent_id, batch_id, id, "
                + ", ".join(f"code_{i:02d}" for i in range(N_CODES)) + ", order_year FROM wide")
    for r in range(ROWS):
        vals = []
        for i in range(N_CODES):
            if i % 7 == 0:
                vals.append(None)                       # a column that is always empty
            elif i % 5 == 0:
                vals.append(f"v{r % 40}")               # too many values for a list
            elif i % 3 == 0:
                vals.append(["Open", "Open ", "closed"][r % 3])   # spellings kept exactly
            else:
                vals.append(f"k{(r + i) % (2 + i % 4)}")
        year = 9999 if r % 50 == 0 else 2020 + r % 4      # a placeholder year among real ones
        con.execute(f"INSERT INTO wide VALUES ({', '.join('?' * (6 + N_CODES))})",
                    (r + 1, r % 9, r % 4, r % 50, r % 100, *vals, year))
    for s in range(9):
        con.execute("INSERT INTO stores VALUES (?, ?, ?)", (s, ["mall", "street"][s % 2], f"t{s % 3}"))


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "acme.db"
    con = sqlite3.connect(path)
    _schema(con)
    con.commit()
    con.close()
    return path


def _runner(db: Path, seen: list[str], refuse: str = ""):
    """Rows as `execute_sql.py` gives them (text, NULL as ""), every statement recorded."""
    def run(sql: str) -> list[dict]:
        seen.append(sql)
        if refuse and refuse in sql:
            raise RuntimeError("refused")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return [{k: "" if r[k] is None else str(r[k]) for k in r.keys()} for r in con.execute(sql)]
        finally:
            con.close()
    return run


def _build(db: Path, tmp: Path, seen: list[str], refuse: str = ""):
    return I.introspect("acme", "sqlite", runner=_runner(db, seen, refuse), artifacts_dir=tmp, dry_run=True)


def _value_reads(seen: list[str], table: str) -> list[str]:
    # not the join probe's overlap read, which has the same shape inside a count
    return [s for s in seen if "SELECT DISTINCT" in s and f'"{table}" WHERE' in s and "AS matched" not in s]


def _placeholder_reads(seen: list[str], table: str) -> list[str]:
    return [s for s in seen if s.startswith(("SELECT g.v AS v", "SELECT 0 AS c, b0.v AS v0, NULL AS v1, NULL AS v2"))
            and f'"{table}" WHERE' in s and "GROUP BY" in s]


def _grain_reads(seen: list[str], table: str) -> list[str]:
    return [s for s in seen if "AS distinct_count" in s and s.endswith(f'"{table}"')]


def _table(org, name: str):
    return next(t for sa in org.subject_areas if (t := sa.defined_table(name)) is not None)


def _same(a, b) -> None:
    (org_a, rep_a), (org_b, rep_b) = a, b
    assert org_a.model_dump(mode="json") == org_b.model_dump(mode="json")
    # the report too, but for the folder each build was pointed at
    assert rep_a.render().replace(rep_a.out_dir, "OUT") == rep_b.render().replace(rep_b.out_dir, "OUT")


def test_a_wide_table_reads_its_value_lists_in_a_few_statements_and_builds_the_same_model(
        db, tmp_path, monkeypatch):
    size = I.COMBINED_READ_COLUMNS
    combined: list[str] = []
    new = _build(db, tmp_path / "a", combined)
    monkeypatch.setattr(I, "COMBINED_READ_COLUMNS", 1)   # one statement per column, as before
    alone: list[str] = []
    old = _build(db, tmp_path / "b", alone)

    _same(new, old)
    for table in ("wide", "wide_v"):
        before, after = len(_value_reads(alone, table)), len(_value_reads(combined, table))
        assert before == N_CODES          # one per coded column
        assert after == math.ceil(before / size)
        before, after = len(_placeholder_reads(alone, table)), len(_placeholder_reads(combined, table))
        assert before >= 30 and after == math.ceil(before / size)
    # the view's grain: the first of five id candidates alone, then the other four together
    assert len(_grain_reads(alone, "wide_v")) == 5 and len(_grain_reads(combined, "wide_v")) == 2
    assert len(combined) <= len(alone) - 2 * (N_CODES - 10)
    # what the reads found, so "the same" is not two empty models agreeing
    wide = _table(new[0], "wide")
    by = {c.name: c for c in wide.columns}
    assert set(by["code_03"].choice_field) == {"Open", "Open ", "closed"}
    assert by["code_05"].choice_field is None and by["code_07"].choice_field is None
    assert by["code_01"].choice_field == {"k0": "", "k1": "", "k2": ""}
    assert _table(new[0], "wide_v").grain == ["id"]
    assert [p.value for p in by["order_year"].placeholders] == ["9999"]


def test_a_refused_combined_read_falls_back_to_one_column_at_a_time(db, tmp_path):
    ok: list[str] = []
    expected = _build(db, tmp_path / "a", ok)
    seen: list[str] = []
    got = _build(db, tmp_path / "b", seen, refuse="b1.v AS v1")
    assert got[0].model_dump(mode="json") == expected[0].model_dump(mode="json")
    notes = [n for n in got[1].notes if "one column at a time" in n]
    assert len(notes) == 1 and notes[0].startswith("3 table(s)")
    assert len(_value_reads(seen, "wide")) > len(_value_reads(ok, "wide"))


def test_a_refused_grain_read_falls_back_to_one_candidate_at_a_time(db, tmp_path):
    seen: list[str] = []
    org, rep = _build(db, tmp_path, seen, refuse="AS distinct_count1")
    assert _table(org, "wide_v").grain == ["id"] and len(_grain_reads(seen, "wide_v")) == 1 + 5


def _shape_ok(rows, n: int) -> bool:
    return I._split_tagged(rows, n, ("v",)) is not None


def test_a_combined_result_that_is_not_one_is_not_trusted():
    # A runner that answered something else (no tag, a tag out of range) is read column by column.
    assert not _shape_ok([{"v": "a"}], 2)
    assert not _shape_ok([{"c": "7", "v0": "a"}], 2)
    assert not _shape_ok([{"c": "x", "v0": "a"}], 2)
    assert I._split_tagged([], 2, ("v",)) == [[], []]
    assert I._split_tagged([{"c": "1", "v0": "", "v1": "b"}, {"c": 0, "v0": "a", "v1": None}], 2, ("v",)) \
        == [[{"v": "a"}], [{"v": "b"}]]
    assert I._split_counts([{"total": "4"}], 2) is None
    assert I._split_counts([], 1) is None


# --- the SQL each engine is sent --------------------------------------------------------------

_ENGINES = {
    "postgres": "postgres", "redshift": "redshift", "snowflake": "snowflake", "bigquery": "bigquery",
    "sqlserver": "tsql", "oracle": "oracle", "mysql": "mysql", "duckdb": "duckdb", "sqlite": "sqlite",
    "databricks": "databricks", "trino": "trino",
}


@pytest.mark.parametrize("db_type", sorted(_ENGINES))
def test_the_combined_value_read_is_each_column_read_tagged_and_stacked(db_type):
    d = D.get_dialect(db_type)
    sql = d.distinct_many_sql("s", "t", ["status", "kind"], 26)
    arms = sql.split(" UNION ALL ")
    if d.union_types_pairwise:   # Postgres: the zero-row typing arm leads
        assert arms.pop(0) == (f"SELECT -1 AS c, t0.v AS v0, t1.v AS v1 FROM ({d.distinct_sql('s', 't', 'status', 26)}) t0, "
                               f"({d.distinct_sql('s', 't', 'kind', 26)}) t1 WHERE 1=0")
    assert len(arms) == 2
    for i, col in enumerate(["status", "kind"]):
        # each arm is the one-column read, unchanged, inside a derived table
        assert f"({d.distinct_sql('s', 't', col, 26)}) b{i}" in arms[i]
        assert arms[i].startswith(f"SELECT {i} AS c, ")
        assert f"b{i}.v AS v{i}" in arms[i] and f"NULL AS v{1 - i}" in arms[i]
    assert " AS b0" not in sql   # Oracle refuses AS before a table alias
    sqlglot.parse_one(sql, read=_ENGINES[db_type])


@pytest.mark.parametrize("db_type, bounded", [
    ("postgres", "LIMIT 26) b0"), ("redshift", "LIMIT 26) b0"), ("snowflake", "LIMIT 26) b0"),
    ("bigquery", "LIMIT 26) b0"), ("mysql", "LIMIT 26) b0"), ("duckdb", "LIMIT 26) b0"),
    ("sqlite", "LIMIT 26) b0"), ("sqlserver", "(SELECT DISTINCT TOP 26 [status] AS v"),
    ("oracle", "FETCH FIRST 26 ROWS ONLY) b0"),
])
def test_each_engine_bounds_a_column_inside_its_own_arm(db_type, bounded):
    sql = D.get_dialect(db_type).distinct_many_sql("s", "t", ["status", "kind"], 26)
    assert bounded in sql
    if db_type == "bigquery":
        assert "`status`" in sql
    if db_type == "mysql":
        assert "`status`" in sql


@pytest.mark.parametrize("db_type", sorted(_ENGINES))
def test_the_combined_count_is_one_aggregate_over_every_column(db_type):
    d = D.get_dialect(db_type)
    sql = d.count_distinct_many_sql("s", "t", ["id", "order_id"])
    q = d.quote_ident("order_id")
    assert sql.startswith("SELECT COUNT(*) AS total, ")
    assert f"COUNT(DISTINCT {q}) AS distinct_count1" in sql and f"COUNT({q}) AS filled1" in sql
    assert sql.endswith(f"FROM {d.qualified('s', 't')}")
    sqlglot.parse_one(sql, read=_ENGINES[db_type])


def test_the_combined_reads_run_and_agree_with_one_column_at_a_time(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    d = D.SQLite()
    cols = ["code_01", "code_03", "code_07", "store_id"]

    def rows(sql):
        return [dict(r) for r in con.execute(sql)]

    split = I._split_tagged(rows(d.distinct_many_sql("main", "wide", cols, 26)), len(cols), ("v",))
    for c, got in zip(cols, split):
        alone = rows(d.distinct_sql("main", "wide", c, 26))
        assert sorted(map(str, (r["v"] for r in got))) == sorted(map(str, (r["v"] for r in alone)))
    scans = I._split_tagged(rows(d.placeholder_scan_many_sql("main", "wide", [(c, "string") for c in cols], 10)),
                            len(cols), ("v", "lo", "hi"), ("n", "total"))
    for c, got in zip(cols, scans):   # in the column's own order: most rows first, then by value
        assert got == rows(d.placeholder_scan_sql("main", "wide", c, "string", 10))
    counts = I._split_counts(rows(d.count_distinct_many_sql("main", "wide", cols)), len(cols))
    for c, got in zip(cols, counts):
        alone = rows(d.count_distinct_sql("main", "wide", c))[0]
        assert {k: int(v) for k, v in got.items()} == alone


def test_on_duckdb_columns_of_different_types_share_a_statement_and_keep_their_values():
    # Each column keeps its own output column, so an integer, a decimal and a text column are never
    # cast to a common type: 1.50 stays 1.50 and 'A  ' keeps its spaces.
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE t (n INTEGER, amt DECIMAL(10,2), s VARCHAR, d DATE)")
    con.execute("INSERT INTO t VALUES (1, 1.50, 'A  ', DATE '2024-01-02'), (2, 2.00, 'b', NULL), "
                "(1, NULL, NULL, DATE '2024-01-02')")
    d = D.DuckDB()
    cols = ["n", "amt", "s", "d"]

    def rows(sql):
        cur = con.execute(sql)
        names = [x[0] for x in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    split = I._split_tagged(rows(d.distinct_many_sql("main", "t", cols, 26)), len(cols))
    for c, got in zip(cols, split):
        assert sorted(map(repr, (r["v"] for r in got))) == \
            sorted(map(repr, (r["v"] for r in rows(d.distinct_sql("main", "t", c, 26)))))
    assert {r["v"] for r in split[2]} == {"A  ", "b"}


@pytest.mark.parametrize("db_type", sorted(_ENGINES))
def test_the_combined_placeholder_scan_keeps_each_column_read_and_its_order(db_type):
    d = D.get_dialect(db_type)
    sql = d.placeholder_scan_many_sql("s", "t", [("status", "string"), ("opened", "date")], 10)
    for i, (col, ctype) in enumerate([("status", "string"), ("opened", "date")]):
        assert f"({d.placeholder_scan_sql('s', 't', col, ctype, 10)}) b{i}" in sql
    assert "b0.n AS n, b0.total AS total" in sql and "NULL AS lo1" in sql
    assert sql.endswith(" ORDER BY c, n DESC, v0, v1")
    sqlglot.parse_one(sql, read=_ENGINES[db_type])


@pytest.mark.parametrize("db_type, typed", [("postgres", True), ("supabase", True), ("redshift", False),
                                            ("snowflake", False), ("sqlserver", False), ("oracle", False)])
def test_only_an_engine_that_types_a_union_pairwise_gets_the_typing_arm(db_type, typed):
    d = D.get_dialect(db_type)
    sql = d.distinct_many_sql("s", "t", ["a", "b", "n"], 26)
    assert sql.startswith("SELECT -1 AS c, ") is typed and ("WHERE 1=0" in sql) is typed
    scan = d.placeholder_scan_many_sql("s", "t", [("a", "string"), ("n", "integer")], 10)
    assert scan.startswith("SELECT -1 AS c, t0.v AS v0, t1.v AS v1, t0.lo AS lo0") is typed
    if typed:
        assert "t0.n AS n, t0.total AS total FROM (" in scan
    sqlglot.parse_one(scan, read=_ENGINES.get(db_type, "postgres"))


def _pg():
    """A live Postgres, opt-in as the other integration tests are: AGAMI_IT_PG_* (password required)."""
    psycopg2 = pytest.importorskip("psycopg2")
    if not os.environ.get("AGAMI_IT_PG_PASSWORD"):
        pytest.skip("set AGAMI_IT_PG_PASSWORD to run against a live Postgres")
    try:
        return psycopg2.connect(host=os.environ.get("AGAMI_IT_PG_HOST", "127.0.0.1"),
                                port=int(os.environ.get("AGAMI_IT_PG_PORT", "55432")),
                                user=os.environ.get("AGAMI_IT_PG_USER", "agami_test"),
                                password=os.environ["AGAMI_IT_PG_PASSWORD"],
                                dbname=os.environ.get("AGAMI_IT_PG_DB", "shop"), connect_timeout=3)
    except Exception as exc:
        pytest.skip(f"no reachable Postgres ({exc})")


def test_on_postgres_an_integer_and_a_date_after_two_text_columns_read_in_one_statement():
    # Postgres types a UNION two arms at a time: without the typing arm, the NULLs arms 0 and 1 put in
    # v2 are text, and arm 2's integer fails the whole statement ("UNION types text and integer").
    con = _pg()
    con.autocommit = True
    cur = con.cursor()
    cur.execute("DROP SCHEMA IF EXISTS agami_it_combined CASCADE; CREATE SCHEMA agami_it_combined")
    try:
        cur.execute("CREATE TABLE agami_it_combined.t (a text, b varchar(5), n int, d date, s char(3))")
        cur.execute("INSERT INTO agami_it_combined.t VALUES ('x', 'p', 1, '2024-01-02', 'A'), "
                    "('y', 'q', 2, NULL, 'B '), ('x', NULL, 1, '2024-01-03', NULL)")
        d = D.PostgreSQL()
        cols = ["a", "b", "n", "d", "s"]

        def rows(sql):
            cur.execute(sql)
            names = [x[0] for x in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

        split = I._split_tagged(rows(d.distinct_many_sql("agami_it_combined", "t", cols, 26)), len(cols))
        for c, got in zip(cols, split):
            alone = rows(d.distinct_sql("agami_it_combined", "t", c, 26))
            assert sorted(map(repr, (r["v"] for r in got))) == sorted(map(repr, (r["v"] for r in alone)))
        scan = [(c, t) for c, t in zip(cols, ["string", "string", "integer", "date", "string"])]
        split = I._split_tagged(rows(d.placeholder_scan_many_sql("agami_it_combined", "t", scan, 10)),
                                len(cols), ("v", "lo", "hi"), ("n", "total"))
        for (c, t), got in zip(scan, split):
            assert got == rows(d.placeholder_scan_sql("agami_it_combined", "t", c, t, 10))
    finally:
        cur.execute("DROP SCHEMA agami_it_combined CASCADE")
        con.close()


def test_a_view_whose_first_id_is_unique_reads_its_grain_in_one_statement(tmp_path):
    db = tmp_path / "g.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, store_id INTEGER, region_id INTEGER)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, i % 3, i % 2) for i in range(20)])
    con.execute("CREATE VIEW v AS SELECT id, store_id, region_id FROM t")
    con.commit()
    con.close()
    seen: list[str] = []
    org, _ = I.introspect("acme", "sqlite", runner=_runner(db, seen), artifacts_dir=tmp_path, dry_run=True)
    assert _table(org, "v").grain == ["id"]
    assert _grain_reads(seen, "v") == ['SELECT COUNT(*) AS total, COUNT(DISTINCT "id") AS distinct_count, '
                                       'COUNT(*) - COUNT("id") AS null_count FROM "main"."v"']


def test_a_lower_row_cap_takes_fewer_columns_to_a_statement(db, tmp_path, monkeypatch):
    expected = _build(db, tmp_path / "a", [])
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "100")   # three value lists of 26 rows, ten scans of 10
    assert I._combined_size(I.ENUM_MAX_DISTINCT + 1) == 3 and I._combined_size(10) == 10
    assert I._combined_size(1) == I.COMBINED_READ_COLUMNS
    seen: list[str] = []
    got = _build(db, tmp_path / "b", seen)
    assert got[0].model_dump(mode="json") == expected[0].model_dump(mode="json")
    assert len(_value_reads(seen, "wide")) == math.ceil(N_CODES / 3)
    for raw, size in (("", 25), ("0", 25), ("x", 25), ("20", 1)):
        monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", raw)
        assert I._combined_size(26) == size


def test_after_a_combined_read_times_out_the_table_is_read_a_column_at_a_time(db, tmp_path):
    expected = _build(db, tmp_path / "a", [])
    seen: list[str] = []
    run = _runner(db, seen)

    def slow(sql):
        if "UNION ALL" in sql and '"main"."wide" WHERE' in sql and " AS c, b0.v AS v0" in sql:
            seen.append(sql)
            raise RuntimeError("canceling statement due to statement timeout")
        return run(sql)

    org, rep = I.introspect("acme", "sqlite", runner=slow, artifacts_dir=tmp_path / "b", dry_run=True)
    assert org.model_dump(mode="json") == expected[0].model_dump(mode="json")
    # the first combined read of `wide` timed out; neither its next chunk nor its placeholder scan tried again
    assert sum(" AS c, b0.v AS v0" in q and '"main"."wide" WHERE' in q for q in seen) == 1
    assert ("main", "wide") in rep.combined_too_slow
    # another table is not held to it
    assert sum(" AS c, b0.v AS v0" in q and '"main"."wide_v" WHERE' in q for q in seen) == 4


@pytest.mark.parametrize("db_type, raw, ok", [
    ("oracle", "CLOB", False), ("oracle", "NCLOB", False), ("oracle", "VARCHAR2(20)", True),
    ("oracle", "LONG RAW", False), ("bigquery", "ARRAY<STRING>", False), ("bigquery", "STRUCT<a INT64>", False),
    ("bigquery", "JSON", False), ("bigquery", "STRING", True), ("sqlserver", "ntext", False),
    ("sqlserver", "text", False), ("sqlserver", "image", False), ("sqlserver", "nvarchar", True),
    ("postgres", "json", False), ("postgres", "jsonb", True), ("postgres", "text", True),
    ("redshift", "super", False), ("snowflake", "VARIANT", True), ("sqlite", None, True),
])
def test_a_type_the_engine_cannot_compare_is_known_by_its_catalog_name(db_type, raw, ok):
    assert D.get_dialect(db_type).comparable(raw) is ok


def test_a_column_of_such_a_type_is_read_alone_and_the_rest_together(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    seen: list[str] = []

    def run(sql):
        seen.append(sql)
        return [dict(r) for r in con.execute(sql)]

    rep = I.IntrospectReport(profile="acme", db_type="sqlite", out_dir="", dry_run=True)
    rep.uncomparable[("main", "wide")] = ["code_02"]
    cols = ["code_01", "code_02", "code_03"]
    got = I._value_reads(D.SQLite(), run, "main", "wide", cols, 26, rep)
    assert [s.startswith("SELECT 0 AS c") for s in seen] == [False, True]
    assert '"code_02"' in seen[0] and '"code_02"' not in seen[1]
    assert [sorted(r["v"] for r in g) for g in got] == \
        [sorted(r["v"] for r in run(D.SQLite().distinct_sql("main", "wide", c, 26))) for c in cols]


def test_the_catalog_names_the_columns_no_combined_read_may_hold():
    d = D.get_dialect("oracle")
    out: list[str] = []
    rows = [{"column_name": "NOTES", "data_type": "CLOB"}, {"column_name": "STATUS", "data_type": "VARCHAR2"}]
    cols, mode = I._columns(d, lambda sql: rows, "S", "T", uncomparable=out)
    assert mode == "catalog" and [c.name for c in cols] == ["NOTES", "STATUS"] and out == ["NOTES"]
