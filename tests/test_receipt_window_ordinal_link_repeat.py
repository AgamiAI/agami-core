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
from test_ace178_receipt_audit_fixes import _audit, _rel  # noqa: E402


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


# --- 2. a window calculation over an aggregate --------------------------------------------------


@pytest.mark.parametrize(
    "sql, metric",
    [
        (
            "SELECT month, SUM(COUNT(DISTINCT number)) OVER (ORDER BY month) AS v "
            "FROM incidents GROUP BY month",
            "incident_count",
        ),
        (
            "SELECT status, SUM(SUM(total_amount)) OVER (ORDER BY status) AS v "
            "FROM orders GROUP BY status",
            "total_sales",
        ),
        (
            "SELECT status, SUM(total_amount) * 1.0 / SUM(SUM(total_amount)) OVER () AS v "
            "FROM orders GROUP BY status",
            "total_sales",
        ),
        ("SELECT opened, SUM(total_amount) OVER (ORDER BY opened) AS v FROM orders", "total_sales"),
        (
            "SELECT month, LAG(COUNT(DISTINCT number)) OVER (ORDER BY month) AS v "
            "FROM incidents GROUP BY month",
            "incident_count",
        ),
    ],
    ids=["running-count", "running-sum", "share", "running-over-rows", "lag"],
)
def test_a_window_over_a_matching_aggregate_is_undetermined_and_says_so(org, sql, metric):
    item = _one(org, sql, "v")
    assert (item["status"], item["name"]) == (rt.UNDETERMINED, None)
    assert "window calculation" in item["reason"] and metric in item["reason"]


@pytest.mark.parametrize(
    "sql",
    [
        # A count of rows where the metric counts distinct records.
        "SELECT month, SUM(COUNT(*)) OVER (ORDER BY month) AS v FROM incidents GROUP BY month",
        # A metric's expression over a table the metric is not defined on.
        "SELECT status, SUM(COUNT(DISTINCT number)) OVER (ORDER BY status) AS v "
        "FROM orders GROUP BY status",
        # A different aggregate.
        "SELECT status, SUM(MAX(total_amount)) OVER (ORDER BY status) AS v "
        "FROM orders GROUP BY status",
    ],
    ids=["count-star-vs-distinct", "wrong-table", "other-aggregate"],
)
def test_a_window_over_a_wrong_base_is_still_not_the_metric(org, sql):
    """The window does not excuse its base: when the inner aggregate is not the metric, the item
    says so exactly as it would for the inner aggregate alone."""
    item = _one(org, sql, "v")
    assert item["status"] in (rt.UNMATCHED, rt.COMPUTED_DIFFERENTLY)
    assert item["name"] is None
    assert "window calculation" not in (item["reason"] or "")


def test_a_window_over_a_wrong_base_reports_what_the_inner_aggregate_lacks(org):
    """`SUM(total_amount)` over `payments` only: the metric's table is never read, and the item
    names it, as the plain aggregate would."""
    window = _one(
        org,
        "SELECT SUM(SUM(amount)) OVER () AS v FROM payments",
        "v",
    )
    plain = _one(org, "SELECT SUM(amount) AS v FROM payments", "v")
    assert plain["status"] == rt.MATCHED  # payment_total: the control
    assert window["status"] == rt.UNDETERMINED and "payment_total" in window["reason"]
    wrong = _one(org, "SELECT SUM(SUM(total_amount)) OVER () AS v FROM payments", "v")
    wrong_plain = _one(org, "SELECT SUM(total_amount) AS v FROM payments", "v")
    assert wrong["status"] == wrong_plain["status"] != rt.UNDETERMINED
    assert wrong["missing_tables"] == wrong_plain["missing_tables"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT status, COUNT(*) AS v FROM orders o JOIN payments p ON p.order_id = o.id "
        "GROUP BY status",
        "SELECT COUNT(DISTINCT number) AS v FROM orders",
        "SELECT MAX(total_amount) AS v FROM orders",
        "SELECT COUNT(*) AS v FROM incidents",
    ],
    ids=["through-a-join", "wrong-table", "other-aggregate", "count-star"],
)
def test_a_plain_aggregate_that_is_not_the_metric_is_still_not_undetermined(org, sql):
    """Only window outputs move: a plain aggregate's error is still reported as one."""
    item = _one(org, sql, "v")
    assert item["status"] != rt.UNDETERMINED or "window" not in (item["reason"] or "")
    assert item["status"] != rt.MATCHED or item["name"] == "order_count"


def test_a_plain_count_star_on_the_wrong_table_stays_unmatched(org):
    assert _one(org, "SELECT COUNT(*) AS v FROM incidents", "v")["status"] == rt.UNMATCHED


# --- 3. which of several links a join went through ----------------------------------------------


def _join(org, sql: str) -> dict:
    (item,) = rt.assemble_receipt(org, sql)["joins"]["items"]
    return item


def test_a_join_through_one_of_several_links_names_its_column(tmp_path):
    org = _audit(tmp_path)
    item = _join(org, "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.managed_by_id")
    assert item["status"] == rt.DECLARED
    assert item["via"] == ["managed_by_id"]
    assert item["summary"] == "assets linked to users through managed_by_id"


def test_a_join_with_one_declared_link_names_none(tmp_path):
    org = _audit(tmp_path)
    item = _join(
        org,
        "SELECT SUM(e.amount) AS n FROM expense_lines e "
        "JOIN award_departments d ON d.award_id = e.award_id",
    )
    assert item["status"] == rt.DECLARED
    assert (item["via"], item["summary"]) == (None, None)


def test_a_join_link_written_as_on_names_its_columns(tmp_path):
    rel = _rel("assets", "owned_by_id", "users", "id")
    del rel["from_column"], rel["to_column"]
    rel["on"] = "assets.owned_by_id = users.id"
    org = _audit(tmp_path, relationships=[_rel("assets", "assigned_to_id", "users", "id"), rel])
    item = _join(org, "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.owned_by_id")
    assert item["via"] == ["owned_by_id"]
    assert "through owned_by_id" in item["summary"]


def test_a_link_declared_the_other_way_round_is_still_told_apart(tmp_path):
    org = _audit(
        tmp_path,
        relationships=[
            _rel("assets", "assigned_to_id", "users", "id"),
            _rel("users", "id", "assets", "owned_by_id"),
        ],
    )
    item = _join(org, "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.owned_by_id")
    assert item["via"] == ["owned_by_id"]


def test_two_links_on_the_same_columns_name_none(tmp_path):
    """Nothing tells them apart by their columns, so nothing is claimed."""
    twin = _rel("assets", "assigned_to_id", "users", "id")
    twin["relationship"] = "one_to_one"
    org = _audit(tmp_path, relationships=[_rel("assets", "assigned_to_id", "users", "id"), twin])
    item = _join(org, "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.assigned_to_id")
    assert (item["via"], item["summary"]) == (None, None)


def test_an_undeclared_join_names_no_link(tmp_path):
    org = _audit(tmp_path)
    item = _join(org, "SELECT COUNT(*) AS n FROM assets a JOIN users u ON u.id = a.status")
    assert item["status"] != rt.DECLARED
    assert (item["via"], item["summary"]) == (None, None)
