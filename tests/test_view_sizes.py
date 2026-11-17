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
        dry_run=True,
    )
    tables = {t.name: t for sa in org.subject_areas for t in sa.tables_defined}
    rels = [r for sa in org.subject_areas for r in sa.relationships] + list(
        org.cross_subject_area_relationships
    )
    return tables, rels, report, seen


def _about(view: str, seen: list[str]) -> list[str]:
    return [s for s in seen if f'"{view}"' in s or f" {view}" in s]


def test_a_small_view_gets_its_exact_count_and_every_full_probe(built):
    tables, rels, report, seen = built
    hints = tables["small_orders_v"].performance_hints
    assert (hints.estimated_row_count, hints.estimated_row_count_kind) == (600, None)
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
    assert (
        t.performance_hints.estimated_row_count,
        t.performance_hints.estimated_row_count_kind,
    ) == (CAP + 1, kind)
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


def test_each_unknown_table_costs_exactly_one_capped_count(built):
    _, _, _, seen = built
    capped = [s for s in seen if CAPPED in s]
    assert sorted(v for s in capped for v in VIEWS if v in s) == sorted(VIEWS)
    # The base tables answer the plain estimate and are never counted again.
    assert not any("customers" in s or '"orders"' in s for s in capped)


def test_the_report_says_how_each_size_was_read_and_notes_a_failed_count_once(built):
    _, _, report, _ = built
    text = report.render()
    assert f"big_orders_v: at least {CAP:,} rows (counted up to the limit)" in text
    assert "small_orders_v: 600 rows" in text
    failed = [n for n in report.notes if n.startswith("size unknown")]
    assert (
        len(failed) == 1
        and "treated as large" in failed[0]
        and failed[0].endswith(": lost_orders_v")
    )


def test_the_answer_receipt_says_unknown_only_when_the_count_failed(tmp_path):
    from semantic_model import runtime as rt

    org, _ = I.introspect(
        "demo",
        "sqlite",
        runner=_runner(_db(tmp_path), []),
        artifacts_dir=tmp_path,
        out_dir=tmp_path / "demo",
        dry_run=True,
    )
    rows = {}
    for view in VIEWS:
        items = rt.assemble_receipt(org, f"SELECT COUNT(*) FROM main.{view}")["tables"]["items"]
        rows[view] = items[0]["rows"]
    assert rows == {"small_orders_v": 600, "big_orders_v": CAP + 1, "lost_orders_v": None}


def test_the_model_page_says_at_least_or_unknown():
    from types import SimpleNamespace as NS

    import admin

    def size(n, kind):
        return admin._table_size(
            NS(performance_hints=NS(estimated_row_count=n, estimated_row_count_kind=kind))
        )

    assert size(CAP + 1, "at_least") == "≥ 5M"
    assert size(CAP + 1, "unknown") == "size unknown"
    assert size(6591, None) == "≈ 6.6k"
    assert admin._table_size(NS(performance_hints=None)) == ""


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
    assert I._estimate_rows(
        d, lambda sql: [{"estimated_rows": str(CAP)}] if "LIMIT" in sql else [], "s", "v"
    ) == (CAP, None)
    assert I._estimate_rows(
        d, lambda sql: [{"estimated_rows": str(CAP + 1)}] if "LIMIT" in sql else [], "s", "v"
    ) == (CAP + 1, "at_least")


def test_a_spent_budget_treats_the_table_as_large_without_counting():
    seen: list[str] = []
    budget = I._CountBudget(seconds_left=0.0)
    assert I._estimate_rows(
        D.get_dialect("redshift"), lambda sql: seen.append(sql) or [], "s", "v", budget
    ) == (CAP + 1, "unknown")
    assert not any("LIMIT" in s for s in seen) and budget.skipped == 1


@pytest.mark.parametrize("engine", ["trino", "mysql"])
def test_an_engine_that_offers_no_estimate_or_no_count_leaves_the_size_unknown(engine):
    """Trino offers no estimate query at all; MySQL's view has none and MySQL gets no count (#459).
    Neither is counted, and neither is guessed large: the size stays unsized, as before."""
    seen: list[str] = []
    assert I._estimate_rows(
        D.get_dialect(engine), lambda sql: seen.append(sql) or [], "s", "v"
    ) == (None, None)
    assert not any("capped" in s for s in seen)
