"""A table the catalog cannot size (a view) gets one count that stops at the size guard.

Every cost guard in a build keys on a table's row estimate: the overlap probe (3M), the grain and
unenforced-key checks (5M), the value reads' sample. A view has no estimate on any engine, and an
exact COUNT(*) over a 100M-row view runs out of time, so the size stayed unknown and every guard read
unknown as small: full scans ran over the biggest views. Now the build takes ONE count capped at
`_SIZE_COUNT_CAP + 1` rows. Over the cap it records "at least" and the guards see a large table; a
count that fails reads as large too, and says so once. Synthetic, generic names only.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402

CAP = I._SIZE_COUNT_CAP
CAPPED = f"LIMIT {CAP + 1}"

# Three views over the same rows: one small (counted exactly), one the fake warehouse calls huge
# (its capped count comes back at the cap + 1), one whose count fails. Each holds a customer_id the
# name-based join inference points at `customers`, so each would get an overlap probe; each has an
# id-named column but no primary key, so each would get a grain probe.
_SCHEMA = (
    "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, placed_on DATE);"
    "CREATE VIEW small_orders_v AS SELECT id AS order_id, customer_id, status, placed_on FROM orders;"
    "CREATE VIEW big_orders_v AS SELECT id AS order_id, customer_id, status, placed_on FROM orders;"
    "CREATE VIEW lost_orders_v AS SELECT id AS order_id, customer_id, status, placed_on FROM orders;"
    "INSERT INTO customers VALUES (1, 'a'), (2, 'b'), (3, 'c');"
    # More rows than one sample read takes, so a sample of a view never passes for the whole of it.
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 600)"
    " INSERT INTO orders SELECT i, 1 + i % 3, CASE i % 3 WHEN 0 THEN 'open' WHEN 1 THEN 'shipped'"
    " ELSE 'closed' END, '2026-01-0' || (1 + i % 5) FROM n;"
)
VIEWS = ("small_orders_v", "big_orders_v", "lost_orders_v")


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return db


def _runner(db: Path, seen: list[str]):
    """SQLite, except that a view has no row estimate (the plain count comes back empty, as a
    warehouse's statistics table has no row for a view), `big_orders_v` answers a capped count with
    the cap + 1, and `lost_orders_v`'s capped count fails."""

    def run(sql: str) -> list[dict]:
        s = " ".join(sql.split())
        seen.append(s)
        view = next((v for v in VIEWS if v in s), None)
        if view and s.startswith("SELECT COUNT(*) AS estimated_rows FROM") and CAPPED not in s:
            return []
        if view == "big_orders_v" and CAPPED in s:
            return [{"estimated_rows": str(CAP + 1)}]
        if view == "lost_orders_v" and CAPPED in s:
            raise RuntimeError("statement timed out")
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            con.close()

    return run


@pytest.fixture
def built(tmp_path):
    seen: list[str] = []
    org, report = I.introspect(
        "demo",
        "sqlite",
        runner=_runner(_db(tmp_path), seen),
        artifacts_dir=tmp_path,
        out_dir=tmp_path / "demo",
    )
    tables = {t.name: t for sa in org.subject_areas for t in sa.tables_defined}
    rels = [r for sa in org.subject_areas for r in sa.relationships] + list(
        org.cross_subject_area_relationships
    )
    return tables, rels, report, seen


def _about(view: str, seen: list[str]) -> list[str]:
    return [s for s in seen if f'"{view}"' in s or f" {view}" in s]


def _kind(v):
    return None if v is None else (int(v), getattr(v, "kind", None))


def test_a_small_view_gets_its_exact_count_and_every_full_probe(built):
    tables, rels, report, seen = built
    assert tables["small_orders_v"].performance_hints.estimated_row_count == 600
    assert report.counted_sizes["main.small_orders_v"] == (600, None)
    mine = _about("small_orders_v", seen)
    assert any("COUNT(DISTINCT" in s for s in mine)  # the grain probe ran
    assert tables["small_orders_v"].grain == ["order_id"]
    # The overlap probe ran over the whole view, not a bounded read of it.
    assert any("AS matched" in s and "rows_read" not in s for s in mine)
    assert "small_orders_v" not in report.large_tables


@pytest.mark.parametrize("view, kind", [("big_orders_v", "at_least"), ("lost_orders_v", "unknown")])
def test_a_view_over_the_cap_or_with_a_failed_count_gets_every_guard(built, view, kind):
    tables, rels, report, seen = built
    t = tables[view]
    assert t.performance_hints.estimated_row_count == CAP + 1
    assert report.counted_sizes[f"main.{view}"] == (CAP + 1, kind)
    mine = _about(view, seen)
    assert not any("AS distinct_count" in s for s in mine)  # the key check is skipped
    assert t.grain == []
    # Its join to customers is still probed, on a bounded read of it rather than the whole view.
    probes = [s for s in mine if "AS matched" in s]
    assert probes and all(f"LIMIT {I.PF.JOIN_SAMPLE_ROWS}) rows_read" in s for s in probes)
    # No read of the whole view at all: every statement that names it is bounded, past the estimate
    # (on SQLite the plain count, standing in for a warehouse's catalog read, which the fake empties).
    reads = [
        s
        for s in mine
        if not s.startswith(f'SELECT COUNT(*) AS estimated_rows FROM "main"."{view}"')
    ]
    assert all("LIMIT" in s or "WHERE 1=0" in s for s in reads), [
        s for s in reads if "LIMIT" not in s
    ]
    assert I._too_big_to_probe(t)
    assert report.large_tables[view] == (CAP + 1, ["placed_on"])  # the date window is seeded
    assert t.performance_hints.recommended_filters == ["placed_on"]


def test_the_written_model_carries_no_new_key(built, tmp_path):
    """An older core forbids unknown keys, so a model built here must load there: the size is the
    one field it already reads, and how it was counted stays in this run's report."""
    texts = [f.read_text() for f in (tmp_path / "demo").rglob("*.yaml")]
    big = next(
        t for f, t in zip((tmp_path / "demo").rglob("*.yaml"), texts) if f.stem == "big_orders_v"
    )
    assert f"estimated_row_count: {CAP + 1}" in big
    assert not any("kind" in t or "lower_bound" in t for t in texts)


def test_a_failed_count_writes_no_size_and_reads_as_size_unknown(built, tmp_path):
    """A count that failed says nothing about the size, so the model records none: a small view whose
    count timed out must not be shown as "at least 5,000,000 rows". The build itself still treated it
    as large (the test above); only what is written, and so displayed, changes."""
    import admin
    import tools
    from semantic_model import loader

    tables, _, _, _ = built
    assert tables["lost_orders_v"].performance_hints.estimated_row_count == CAP + 1  # in memory
    written = {f.stem: f.read_text() for f in (tmp_path / "demo").rglob("*.yaml")}
    assert "estimated_row_count" not in written["lost_orders_v"]
    assert f"estimated_row_count: {CAP + 1}" in written["big_orders_v"]
    org = loader.load_datasource(tmp_path / "demo")
    stored = {t.name: t for sa in org.subject_areas for t in sa.tables_defined}
    assert admin._table_size(stored["lost_orders_v"]) == "size unknown"
    assert admin._table_size(stored["big_orders_v"]) == "≥ 5M"
    assert admin._table_size(stored["small_orders_v"]) == "≈ 600"
    large = tools._large_tables(org)
    assert "lost_orders_v" not in large and large["big_orders_v"] == f"at least {CAP:,}"


def test_each_unknown_table_costs_exactly_one_capped_count(built):
    _, _, _, seen = built
    capped = [s for s in seen if CAPPED in s]
    assert sorted(v for s in capped for v in VIEWS if v in s) == sorted(VIEWS)
    # The base tables answer the plain estimate and are never counted again.
    assert not any("customers" in s or '"orders"' in s for s in capped)


def test_the_report_says_how_each_size_was_read_and_notes_a_failed_count_once(built):
    _, _, report, _ = built
    text = report.render()
    assert f"main.big_orders_v: at least {CAP:,} rows (counted up to the limit)" in text
    assert "main.small_orders_v: 600 rows" in text
    assert f"large table big_orders_v: at least {CAP:,} rows;" in text
    failed = [n for n in report.notes if n.startswith("size unknown")]
    assert (
        len(failed) == 1
        and "treated as large" in failed[0]
        and failed[0].endswith(": main.lost_orders_v")
    )


def test_a_size_at_the_cap_is_never_quoted_as_a_count(built):
    """The receipt's scan-risk sentence, the schema reply's large tables and the model page say "at
    least" for the cap + 1, never a measured 5,000,001."""
    import admin
    import tools
    from semantic_model import runtime as rt

    tables, _, _, _ = built
    assert f"at least {CAP:,} rows" in rt.scan_sentence({"table": "big_orders_v", "rows": CAP + 1})
    assert "about 1,200 rows" in rt.scan_sentence({"table": "t", "rows": 1200})
    org = SimpleNamespace(subject_areas=[SimpleNamespace(tables_defined=list(tables.values()))])
    large = tools._large_tables(org)
    assert large["big_orders_v"] == f"at least {CAP:,}" and "small_orders_v" not in large
    assert admin._table_size(tables["big_orders_v"]) == "≥ 5M"
    assert admin._table_size(tables["small_orders_v"]) == "≈ 600"


def test_the_cap_is_at_least_every_size_guard():
    assert I._SIZE_COUNT_CAP >= max(I._GRAIN_PROBE_MAX_ROWS, I._OVERLAP_PROBE_MAX_ROWS)
    assert I._SIZE_COUNT_CAP >= I.PF.JOIN_TARGET_MAX_ROWS


def test_a_refused_sample_clause_falls_back_to_the_first_rows():
    """Postgres refuses TABLESAMPLE on a view. The overlap probe of a large view then reads its first
    rows, as a join check does, instead of reading the refusal as "no values shared"."""
    from semantic_model import models as m

    pg = D.get_dialect("postgres")
    big = m.Table(
        name="big_v", schema="s", performance_hints=m.PerformanceHints(estimated_row_count=CAP + 1)
    )
    dim = m.Table(name="customers", schema="s", grain=["id"])
    seen: list[str] = []

    def run(sql):
        seen.append(sql)
        if "TABLESAMPLE" in sql:
            raise RuntimeError("TABLESAMPLE clause can only be applied to tables")
        return [{"matched": "3"}]

    assert I._overlap_count(pg, run, big, "customer_id", dim, "id") == 3
    assert "TABLESAMPLE" in seen[0] and "TABLESAMPLE" not in seen[1]
    assert f"LIMIT {I.PF.JOIN_SAMPLE_ROWS}) rows_read" in seen[1]


def test_a_stand_in_estimate_that_returns_a_plain_int_still_fits(tmp_path, monkeypatch):
    """Other tests patch `_estimate_rows` with `fake(dialect, runner, schema, table, budget=None) -> int`;
    the build calls it that way and takes a plain int as a catalog estimate."""
    real = I._estimate_rows

    def fake(dialect, runner, schema, table, budget=None):
        return (
            10_000_000
            if table == "small_orders_v"
            else real(dialect, runner, schema, table, budget)
        )

    monkeypatch.setattr(I, "_estimate_rows", fake)
    org, report = I.introspect(
        "demo",
        "sqlite",
        runner=_runner(_db(tmp_path), []),
        artifacts_dir=tmp_path,
        out_dir=tmp_path / "demo",
        dry_run=True,
    )
    t = next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == "small_orders_v")
    assert t.performance_hints.estimated_row_count == 10_000_000
    assert "main.small_orders_v" not in report.counted_sizes


def test_the_capped_count_is_written_in_each_engines_own_limit():
    for name, bound in (
        ("postgres", "LIMIT 11"),
        ("redshift", "LIMIT 11"),
        ("sqlserver", "TOP 11"),
        ("oracle", "FETCH FIRST 11 ROWS ONLY"),
    ):
        sql = D.get_dialect(name).sql_row_count_capped("s", "v", 10)
        assert sql.startswith("SELECT COUNT(*) AS estimated_rows FROM (") and bound in sql, name
    # Engines that never get a count (#459) get no capped one either.
    assert D.get_dialect("mysql").sql_row_count_capped("s", "v", 10) is None
    assert D.get_dialect("bigquery").sql_row_count_capped("s", "v", 10) is None


def test_a_count_under_the_cap_is_exact_and_one_over_is_a_lower_bound():
    d = D.get_dialect("redshift")
    under = I._estimate_rows(
        d, lambda sql: [{"estimated_rows": str(CAP)}] if "LIMIT" in sql else [], "s", "v"
    )
    over = I._estimate_rows(
        d, lambda sql: [{"estimated_rows": str(CAP + 1)}] if "LIMIT" in sql else [], "s", "v"
    )
    assert (_kind(under), _kind(over)) == ((CAP, None), (CAP + 1, "at_least"))


def test_a_spent_budget_treats_the_table_as_large_without_counting():
    seen: list[str] = []
    budget = I._CountBudget(seconds_left=0.0)
    got = I._estimate_rows(
        D.get_dialect("redshift"), lambda sql: seen.append(sql) or [], "s", "v", budget
    )
    assert _kind(got) == (CAP + 1, "unknown")
    assert not any("LIMIT" in s for s in seen) and budget.skipped == 1


@pytest.mark.parametrize("engine", ["trino", "mysql"])
def test_an_engine_that_offers_no_estimate_or_no_count_leaves_the_size_unknown(engine):
    """Trino offers no estimate query at all; MySQL's view has none and MySQL gets no count (#459).
    Neither is counted, and neither is guessed large: the size stays unsized, as before."""
    seen: list[str] = []
    assert (
        I._estimate_rows(D.get_dialect(engine), lambda sql: seen.append(sql) or [], "s", "v")
        is None
    )
    assert not any("capped" in s for s in seen)
