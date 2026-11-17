"""Five receipt fixes found by an automated question test, every one generic and failing closed.

1. A positional `GROUP BY 1` behind a built-on number is named by the expression it points at,
   never reported as the literal `1`; when the position cannot be read, the grouping is unknown.
2. A window calculation over an aggregate (a running total, a share of a total) is `undetermined`
   when its inner aggregate would match a metric — and stays `unmatched` when that inner aggregate
   is itself not the metric: the window does not excuse a wrong base.
3. A join through one of several declared relationships between the same two tables names the
   column(s) it went through.
4. A row count over a table whose rows repeat per record says so, when the agreed metric counts
   each record once.
5. `sm examples --metric` works without `--query`: it lists the examples tagged with that metric.
"""

from __future__ import annotations

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

from semantic_model import runtime as rt  # noqa: E402

from test_ace178_metrics_traced_through_steps import _org  # noqa: E402


@pytest.fixture()
def org(tmp_path):
    return _org(tmp_path)


def _one(org, sql: str, column: str) -> dict:
    items = rt.assemble_receipt(org, sql)["columns"]["items"]
    (item,) = [i for i in items if i["kind"] == "output" and i["column"] == column]
    return item


# --- 1. a positional GROUP BY behind a built-on number ------------------------------------------

AVG_DAILY_BY_POSITION = (
    "SELECT AVG(daily) AS avg_daily FROM (SELECT DATE_TRUNC('day', opened) AS d, "
    "COUNT(DISTINCT number) AS daily FROM incidents GROUP BY 1) x"
)


def test_a_positional_group_by_is_named_by_the_expression_it_points_at(org):
    item = _one(org, AVG_DAILY_BY_POSITION, "avg_daily")
    built = item["built_from"]
    assert (built["metric"], built["aggregate"]) == ("incident_count", "AVG")
    assert built["by"] == ["DATE_TRUNC('DAY', opened)"]
    assert "1" not in built["by"]


def test_a_named_group_by_is_unchanged(org):
    sql = AVG_DAILY_BY_POSITION.replace("GROUP BY 1", "GROUP BY DATE_TRUNC('day', opened)")
    assert _one(org, sql, "avg_daily")["built_from"]["by"] == ["DATE_TRUNC('DAY', opened)"]


@pytest.mark.parametrize(
    "group",
    ["GROUP BY 3", "GROUP BY 0", "GROUP BY opened, 7"],
    ids=["past-the-end", "zero", "one-of-two"],
)
def test_a_position_that_points_at_nothing_leaves_the_grouping_unknown(org, group):
    """Never the literal number, and never a guess: the whole grouping is unknown."""
    sql = AVG_DAILY_BY_POSITION.replace("GROUP BY 1", group)
    built = _one(org, sql, "avg_daily")["built_from"]
    assert built is None or built["by"] is None


def test_a_group_by_position_is_read_in_its_own_select_list():
    """The inner query's position, not the outer one's: position 1 here is `d`, not `avg_daily`."""
    select = rt.sqlglot.parse_one(AVG_DAILY_BY_POSITION, read="postgres")
    inner = next(s for s in select.find_all(rt.exp.Select) if s is not select)
    (by,) = rt._group_by_items(inner)
    assert by.sql(dialect="postgres") == "DATE_TRUNC('DAY', opened)"
    assert (
        rt._group_by_items(
            rt.sqlglot.parse_one("SELECT * FROM incidents GROUP BY 1", read="postgres")
        )
        is None
    )
