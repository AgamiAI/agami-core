"""#655 — a metric written in an equivalent spelling is matched, not reported as a different number.

The `columns` section compares an output column with each declared binding by parsed structure. It
used to treat only qualifiers, unquoted-name case and a `ROUND` wrapper as presentation, so
`SUM(CASE WHEN p THEN x END)` read `unmatched` beside a binding `SUM(x) FILTER (WHERE p)` — "this
number is not the organisation's agreed definition" — when it is the same number on every input.

Every rule the matcher now applies is pinned here TWICE:

  * the receipt matches the two spellings, through `assemble_receipt` on a model declaring one of
    them, so the test is about the path a caller reaches and not a helper;
  * the two spellings RETURN THE SAME ROWS when executed — on SQLite and DuckDB always, and on
    Postgres when `AGAMI_IT_PG_PASSWORD` is set (the `test_postgres_named_cursor_integration`
    convention) — over data built to break them: NULL values among the matching rows, a group with
    no matching row, a group whose matching rows are all NULL, a NULL predicate, a table with rows
    but no match, and an empty table, each as a global aggregate and grouped.

And every rule that was CONSIDERED AND REJECTED is pinned the other way: the receipt does not match
it, and a witness on real data shows the two spellings returning different rows. A rule whose two
forms can differ on some input is not an equivalence, however close.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

from receipt_trick_queries import SPELLINGS_DIFFERENT  # noqa: E402

# --- the data the two spellings are run on -----------------------------------

_ROWS = [
    (1, "paid", 10),
    (1, "paid", None),
    (1, "open", 5),  # a match with a NULL value beside it
    (2, "open", 7),
    (2, "open", None),  # a group with no matching row
    (3, "paid", None),  # a group whose only match is NULL
    (4, None, 3),  # a NULL predicate
    (5, "paid", 4),
    (5, "paid", 6),
]
DATASETS = {
    "full": _ROWS,
    "none_paid": [r for r in _ROWS if r[1] != "paid"],  # rows, but no row matches
    "empty": [],
}


def _sqlite(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (g INTEGER, p TEXT, x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    return conn


def _duckdb(rows):
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect()
    conn.execute("CREATE TABLE t (g INTEGER, p TEXT, x INTEGER)")
    if rows:
        conn.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    return conn


def _postgres(rows):
    psycopg2 = pytest.importorskip("psycopg2")
    if not os.environ.get("AGAMI_IT_PG_PASSWORD"):
        pytest.skip("set AGAMI_IT_PG_PASSWORD to run the Postgres half")
    try:
        conn = psycopg2.connect(
            host=os.environ.get("AGAMI_IT_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("AGAMI_IT_PG_PORT", "55432")),
            user=os.environ.get("AGAMI_IT_PG_USER", "agami_test"),
            password=os.environ["AGAMI_IT_PG_PASSWORD"],
            dbname=os.environ.get("AGAMI_IT_PG_DB", "postgres"),
            connect_timeout=3,
        )
    except Exception as exc:  # no reachable Postgres here: skip, never fail
        pytest.skip(f"no reachable Postgres ({exc})")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE t (g INTEGER, p TEXT, x INTEGER)")
    if rows:
        cur.executemany("INSERT INTO t VALUES (%s, %s, %s)", rows)
    return conn


ENGINES = {"sqlite": _sqlite, "duckdb": _duckdb, "postgres": _postgres}


def _sqlite_nocase(rows):
    """SQLite with a text `x` declared COLLATE NOCASE beside a plain (BINARY) `p`."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (g INTEGER, p TEXT, x TEXT COLLATE NOCASE)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    return conn


# Engines and data a rejected rule's witness may use; the SAME battery never runs on these, because
# its numeric aggregates assume the integer `x` of `_ROWS`.
WITNESS_ENGINES = {**ENGINES, "sqlite_nocase": _sqlite_nocase}
WITNESS_DATASETS = {
    **DATASETS,
    "nocase": [(1, "paid", "a"), (1, "paid", "A"), (1, "paid", "B"), (3, "A", "a")],
}


def _run(conn, sql: str):
    """Rows, or the error's class name: a statement that fails returns no number to compare."""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]
    except Exception as exc:
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception:
                pass
        return type(exc).__name__


def _statement(expr: str, grouped: bool) -> str:
    if grouped:
        return f"SELECT g, {expr} AS v FROM t GROUP BY g ORDER BY g"
    return f"SELECT {expr} AS v FROM t"


# --- the receipt's view of the two spellings ----------------------------------


def _org(tmp_path: Path, binding: str, extra: dict[str, str] | None = None):
    """A one-table model declaring one metric, `m`, whose binding is `binding`, plus any `extra`
    metrics by name."""
    root = Path(tempfile.mkdtemp(dir=tmp_path)) / "p"  # one test may build several models
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "p",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
                "subject_areas": ["subject_areas/s"],
            }
        )
    )
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {"name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "t"}]}
        )
    )
    (root / "subject_areas" / "s" / "tables" / "t.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "t",
                "schema": "public",
                "storage_connection": "c",
                "grain": ["g"],
                "description": "t",
                "columns": [
                    {"name": "g", "type": "integer"},
                    {"name": "p", "type": "string"},
                    {"name": "x", "type": "integer"},
                ],
            }
        )
    )
    for name, text in {"m": binding, **(extra or {})}.items():
        (root / "subject_areas" / "s" / "metrics" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "calculation": name,
                    "description": name,
                    "source_tables": ["t"],
                    "bindings": {"PostgreSQL": text},
                    "confidence": "proposed",
                    "review_state": "unreviewed",
                }
            )
        )
    return L.load_datasource(root)


def _status(tmp_path: Path, binding: str, written: str, grouped: bool) -> tuple[str, str | None]:
    """The receipt's status for output column `v`, and the metric it names, if any."""
    section = rt.assemble_receipt(_org(tmp_path, binding), _statement(written, grouped))["columns"]
    item = next(i for i in section["items"] if i["kind"] == "output" and i["column"] == "v")
    return item["status"], item["name"]


# --- the rules ---------------------------------------------------------------

# (binding, written, grouped_only). `grouped_only` marks a rule that holds only where every group
# has a row, i.e. under a plain GROUP BY; both modes are still checked for it, and the global one
# must NOT match (see `test_a_grouped_only_rule_does_not_match_a_global_aggregate`).
SAME = [
    # FILTER (WHERE p) <-> CASE WHEN p THEN x END, for the aggregates that ignore NULLs.
    ("SUM(x) FILTER (WHERE p = 'paid')", "SUM(CASE WHEN p = 'paid' THEN x END)", False),
    ("AVG(x) FILTER (WHERE p = 'paid')", "AVG(CASE WHEN p = 'paid' THEN x END)", False),
    ("COUNT(x) FILTER (WHERE p = 'paid')", "COUNT(CASE WHEN p = 'paid' THEN x END)", False),
    # ... and an explicit ELSE NULL is the same CASE.
    ("SUM(x) FILTER (WHERE p = 'paid')", "SUM(CASE WHEN p = 'paid' THEN x ELSE NULL END)", False),
    # Conditional counts: COUNT(*) FILTER <-> COUNT(CASE WHEN p THEN <any non-null literal> END).
    ("COUNT(*) FILTER (WHERE p = 'paid')", "COUNT(CASE WHEN p = 'paid' THEN 1 END)", False),
    ("COUNT(*) FILTER (WHERE p = 'paid')", "COUNT(CASE WHEN p = 'paid' THEN 'y' END)", False),
    ("COUNT(*)", "COUNT(1)", False),
    # The ELSE 0 spellings, where a COALESCE(…, 0) makes the empty case 0 on both sides.
    (
        "COUNT(*) FILTER (WHERE p = 'paid')",
        "COALESCE(SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END), 0)",
        False,
    ),
    (
        "COALESCE(SUM(x) FILTER (WHERE p = 'paid'), 0)",
        "COALESCE(SUM(CASE WHEN p = 'paid' THEN x ELSE 0 END), 0)",
        False,
    ),
    (
        "COUNT(*) FILTER (WHERE p = 'paid')",
        "COALESCE(COUNT(*) FILTER (WHERE p = 'paid'), 0)",
        False,
    ),
    # ... and under a GROUP BY, where every group has a row so SUM of 1s and 0s is never NULL.
    ("COUNT(*) FILTER (WHERE p = 'paid')", "SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)", True),
    ("SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)", "COUNT(*) FILTER (WHERE p = 'paid')", True),
    # NULLIF(COUNT(*), 0) under a GROUP BY, where COUNT(*) is at least 1.
    ("SUM(x) * 1.0 / NULLIF(COUNT(*), 0)", "SUM(x) * 1.0 / COUNT(*)", True),
    # Order that does not change a value: AND terms and multiplication (not `=`: see DIFFERENT).
    (
        "SUM(x) FILTER (WHERE p = 'paid' AND g > 1)",
        "SUM(CASE WHEN g > 1 AND p = 'paid' THEN x END)",
        False,
    ),
    ("SUM(x) * 1.0 / COUNT(*)", "1.0 * SUM(x) / COUNT(*)", False),
    ("COUNT(*) FILTER (WHERE p <> 'open')", "COUNT(*) FILTER (WHERE p != 'open')", False),
]

# Moved to the shared trick list (ACE-177 criterion 11): one place holds every false match.
DIFFERENT = SPELLINGS_DIFFERENT


def _ids(cases):
    return [f"{c[0]} | {c[1]}" for c in cases]


@pytest.mark.parametrize("binding,written,grouped_only", SAME, ids=_ids(SAME))
def test_an_equivalent_spelling_is_matched(tmp_path, binding, written, grouped_only):
    assert _status(tmp_path, binding, written, grouped=True) == (rt.MATCHED, "m")
    if not grouped_only:
        assert _status(tmp_path, binding, written, grouped=False) == (rt.MATCHED, "m")


@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize("binding,written,grouped_only", SAME, ids=_ids(SAME))
def test_an_equivalent_spelling_returns_the_same_rows(engine, binding, written, grouped_only):
    for name, rows in DATASETS.items():
        for grouped in (True,) if grouped_only else (True, False):
            conn = ENGINES[engine](rows)
            try:
                want = _run(conn, _statement(binding, grouped))
                got = _run(conn, _statement(written, grouped))
            finally:
                conn.close()
            assert not isinstance(want, str), (name, grouped, want)
            assert got == want, (name, grouped)


@pytest.mark.parametrize(
    "binding,written,grouped_only", [c for c in SAME if c[2]], ids=_ids([c for c in SAME if c[2]])
)
def test_a_grouped_only_rule_does_not_match_a_global_aggregate(
    tmp_path, binding, written, grouped_only
):
    """Without a GROUP BY the aggregate runs over a possibly empty set, where the two differ."""
    status, name = _status(tmp_path, binding, written, grouped=False)
    assert status == rt.COMPUTED_DIFFERENTLY and name is None


@pytest.mark.parametrize("binding,written,why,witness", DIFFERENT, ids=_ids(DIFFERENT))
def test_a_spelling_that_can_differ_is_not_matched(tmp_path, binding, written, why, witness):
    engines, dataset, grouped = witness
    for engine in engines if isinstance(engines, tuple) else (engines,):
        conn = WITNESS_ENGINES[engine](WITNESS_DATASETS[dataset])
        try:
            assert _run(conn, _statement(binding, grouped)) != _run(
                conn, _statement(written, grouped)
            ), (engine, why)
        finally:
            conn.close()
    # Asserted where the witness shows the difference: `NULLIF(COUNT(*), 0)` is, rightly, the same
    # number under a GROUP BY.
    assert _status(tmp_path, binding, written, grouped=grouped)[0] == rt.COMPUTED_DIFFERENTLY


def test_a_numeric_cast_is_one_spelling_in_any_form(tmp_path):
    """`::numeric`, `CAST(… AS NUMERIC)` and `CAST(… AS DECIMAL)` already parse to one node."""
    for written in ("SUM(x)::numeric / COUNT(*)", "CAST(SUM(x) AS DECIMAL) / COUNT(*)"):
        assert _status(tmp_path, "CAST(SUM(x) AS NUMERIC) / COUNT(*)", written, False) == (
            rt.MATCHED,
            "m",
        )


# --- the new status ------------------------------------------------------------


def test_the_same_columns_written_differently_is_not_called_a_different_definition(tmp_path):
    """`unmatched` says "every declared binding was read and none is this value". When a declared
    metric reads exactly these columns and is written differently, that claim is not earned: the
    two may compute the same number in a form no rule here recognises. The receipt says so."""
    assert _status(
        tmp_path,
        "SUM(x) FILTER (WHERE p = 'paid')",
        "SUM(x) - SUM(x) FILTER (WHERE p <> 'paid')",
        False,
    ) == (rt.COMPUTED_DIFFERENTLY, None)


def test_different_columns_still_read_unmatched(tmp_path):
    """The settled negative keeps its meaning: no declared metric reads these columns."""
    assert _status(tmp_path, "SUM(x) FILTER (WHERE p = 'paid')", "SUM(x)", False) == (
        rt.UNMATCHED,
        None,
    )
    assert _status(tmp_path, "SUM(x) FILTER (WHERE p = 'paid')", "MAX(g)", False) == (
        rt.UNMATCHED,
        None,
    )


def test_a_plain_column_is_never_computed_differently(tmp_path):
    """A list query's columns compute nothing a metric defines, so they are never the new state."""
    assert _status(tmp_path, "MAX(x)", "x", False)[0] == rt.UNMATCHED


def test_the_marker_says_which_columns_were_not_compared(tmp_path):
    org = _org(tmp_path, "SUM(x) FILTER (WHERE p = 'paid')")
    section = rt.assemble_receipt(
        org, _statement("SUM(CASE WHEN p = 'paid' THEN x ELSE 0 END)", False)
    )["columns"]
    assert "computed differently" in section["undetermined"]
    assert "1 of the listed output column(s)" in section["undetermined"]


def test_a_match_does_not_reach_inside_a_subquery_for_the_grouped_rule(tmp_path):
    """The outer GROUP BY says nothing about a scalar subquery's own aggregate, which runs over its
    own, possibly empty, set."""
    org = _org(tmp_path, "COUNT(*) FILTER (WHERE p = 'paid')")
    sql = (
        "SELECT g, (SELECT SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END) FROM t) AS v "
        "FROM t GROUP BY g"
    )
    item = next(
        i for i in rt.assemble_receipt(org, sql)["columns"]["items"] if i.get("column") == "v"
    )
    assert item["status"] != rt.MATCHED


@pytest.mark.parametrize("group_by", ["ROLLUP(g)", "CUBE(g)", "GROUPING SETS ((g), ())", "()"])
def test_a_grand_total_grouping_does_not_count_as_grouped(tmp_path, group_by):
    """These can return a row aggregated over nothing — the grand total of an empty table — so the
    grouped-only rules do not hold there."""
    org = _org(tmp_path, "COUNT(*) FILTER (WHERE p = 'paid')")
    sql = f"SELECT SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END) AS v FROM t GROUP BY {group_by}"
    item = next(
        i for i in rt.assemble_receipt(org, sql)["columns"]["items"] if i.get("column") == "v"
    )
    assert item["status"] == rt.COMPUTED_DIFFERENTLY


def test_a_rollup_grand_total_is_where_the_grouped_rule_breaks():
    """The witness for the test above: on an empty table ROLLUP's grand-total row gives 0 and NULL."""
    conn = _duckdb([])
    try:
        rows = [
            _run(conn, f"SELECT {e} FROM t GROUP BY ROLLUP(g)")
            for e in (
                "COUNT(*) FILTER (WHERE p = 'paid')",
                "SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)",
            )
        ]
    finally:
        conn.close()
    assert rows == [[(0,)], [(None,)]]


def test_group_by_all_does_not_count_as_grouped(tmp_path):
    """`GROUP BY ALL` over aggregates alone groups by nothing: one row, possibly over no rows."""
    org = _org(tmp_path, "COUNT(*) FILTER (WHERE p = 'paid')")
    sql = "SELECT SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END) AS v FROM t GROUP BY ALL"
    item = next(
        i for i in rt.assemble_receipt(org, sql)["columns"]["items"] if i.get("column") == "v"
    )
    assert item["status"] == rt.COMPUTED_DIFFERENTLY


def test_a_multi_column_distinct_filter_is_left_as_written(tmp_path):
    """`COUNT(DISTINCT a, b) FILTER (…)` has no single-value CASE form, so it is not rewritten — and
    the same text on both sides still matches."""
    binding = "COUNT(DISTINCT x, g) FILTER (WHERE p = 'paid')"
    assert _status(tmp_path, binding, binding, False) == (rt.MATCHED, "m")


@pytest.mark.parametrize(
    "sql,dialect,grouped",
    [
        ("SELECT g, SUM(x) FROM t GROUP BY g", "postgres", True),
        ("SELECT g, SUM(x) FROM t GROUP BY g WITH ROLLUP", "mysql", False),
        ("SELECT g, SUM(x) FROM t GROUP BY g WITH TOTALS", "clickhouse", False),
        ("SELECT SUM(x) FROM t", "postgres", False),
    ],
)
def test_a_grand_total_modifier_in_any_dialect_is_not_grouped(sql, dialect, grouped):
    """MySQL's WITH ROLLUP and ClickHouse's WITH TOTALS add a grand-total row beside plain groups."""
    import sqlglot

    assert rt._arm_is_grouped(sqlglot.parse_one(sql, read=dialect)) is grouped


def test_an_empty_string_literal_is_not_a_row_count(tmp_path):
    """Oracle reads `''` as NULL, so `COUNT('')` is 0 there and not `COUNT(*)`. Neither SQLite nor
    DuckDB does, so there is no executed witness here; a non-empty string still counts rows."""
    assert _status(tmp_path, "COUNT(*)", "COUNT('')", False)[0] != rt.MATCHED
    assert (
        _status(
            tmp_path,
            "COUNT(*) FILTER (WHERE p = 'paid')",
            "COUNT(CASE WHEN p = 'paid' THEN '' END)",
            False,
        )[0]
        != rt.MATCHED
    )


@pytest.mark.parametrize(
    "written", ["MAX(x)", "COUNT(DISTINCT x)", "MIN(x) FILTER (WHERE p = 'paid')"]
)
def test_a_different_aggregate_over_the_same_columns_reads_unmatched(tmp_path, written):
    """Same columns is not enough for `computed_differently`: a MAX is not a way of writing a SUM."""
    binding = "SUM(x)" if "FILTER" not in written else "SUM(x) FILTER (WHERE p = 'paid')"
    assert _status(tmp_path, binding, written, False) == (rt.UNMATCHED, None)


@pytest.mark.parametrize(
    "binding,written",
    [
        ("SUM(x) * 1.0 / NULLIF(COUNT(x), 0)", "AVG(x)"),
        ("COUNT(*) FILTER (WHERE p = 'paid')", "SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)"),
    ],
)
def test_the_same_aggregate_family_written_differently_is_not_compared(tmp_path, binding, written):
    """An average is a sum over a count, and a sum of 1s and 0s is a count."""
    assert _status(tmp_path, binding, written, False) == (rt.COMPUTED_DIFFERENTLY, None)


def test_count_big_keeps_its_result_type():
    """T-SQL's COUNT returns int and COUNT_BIG bigint, and sqlglot says which only in `big_int`:
    `COUNT_BIG(*) * 50000` returns 2,500,000,000 over 50,000 rows where `COUNT(*) * 50000`
    overflows. No SQL Server here to run it on; the two must simply not reduce alike."""
    import sqlglot

    def reduced(text: str):
        return rt._reduced_projection(sqlglot.parse_one(text, read="tsql"), "tsql")

    assert reduced("COUNT_BIG(*) * 50000") != reduced("COUNT(*) * 50000")
    assert rt._reduced_binding("COUNT_BIG(*) * 50000", "tsql") != rt._reduced_binding(
        "COUNT(*) * 50000", "tsql"
    )
    # A count the normaliser builds takes the dialect's own COUNT type.
    assert rt._reduced_binding(
        "COALESCE(SUM(CASE WHEN p = 1 THEN 1 END), 0)", "tsql"
    ) == rt._reduced_binding("COUNT(CASE WHEN p = 1 THEN 1 END)", "tsql")
    assert rt._reduced_binding(
        "COALESCE(SUM(CASE WHEN p = 1 THEN 1 END), 0)", "postgres"
    ) == rt._reduced_binding("COUNT(CASE WHEN p = 1 THEN 1 END)", "postgres")


def test_two_metrics_equal_under_group_by_leave_a_grouped_column_undetermined(tmp_path):
    """`COUNT(*) FILTER` and `SUM(CASE … 1 ELSE 0)` are one number under a GROUP BY, so a grouped
    column computing either cannot name one of them — even though only one is equal everywhere."""
    org = _org(
        tmp_path,
        "COUNT(*) FILTER (WHERE p = 'paid')",
        extra={"m2": "SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)"},
    )
    for written, grouped, want in [
        ("COUNT(*) FILTER (WHERE p = 'paid')", True, (rt.UNDETERMINED, None)),
        ("SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)", True, (rt.UNDETERMINED, None)),
        ("COUNT(*) FILTER (WHERE p = 'paid')", False, (rt.MATCHED, "m")),
        ("SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)", False, (rt.MATCHED, "m2")),
    ]:
        items = rt.assemble_receipt(org, _statement(written, grouped))["columns"]["items"]
        item = next(i for i in items if i.get("column") == "v")
        assert (item["status"], item["name"]) == want, (written, grouped)
