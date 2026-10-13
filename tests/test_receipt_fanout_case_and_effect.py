"""The receipt's fan/chasm reporting: name case (#201), whether a fan can move the value (#450),
and which shared dimension a chasm names (#202).

Each of these is about what the receipt SAYS, not what the guard enforces. #201 was a false
clean: a statement written `FROM ORDERS` against a model that says `orders` reported a multiplied
SUM as `not_multiplied`. #450 was a false alarm: a MAX over a fan carries `status: multiplied`,
correctly, because the rows were duplicated, but nothing said the value could not have moved.
#202 was the one detector choice still made by set iteration order.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

# Imported FROM the fixture's module, for the reason `test_ace083_trap_soundness` gives: a second
# import path would yield a second copy of the module and its status constants.
from test_semantic_model_runtime import _sales_org, m, rt  # noqa: E402

FAN = "FROM orders JOIN order_items ON order_items.order_id = orders.id"


def _aggs(sql: str) -> list[dict]:
    return rt.assemble_receipt(_sales_org(), sql)["aggregates"]["items"]


# --- #201: the fan is found whatever case the statement spells the tables in ---------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(ORDERS.total_amount) FROM ORDERS JOIN ORDER_ITEMS ON ORDER_ITEMS.order_id = ORDERS.id",
        "SELECT SUM(o.total_amount) FROM Orders o JOIN Order_Items oi ON oi.order_id = o.id",
        f"SELECT SUM(orders.total_amount) {FAN}",
    ],
)
def test_a_fan_is_reported_in_any_unquoted_case(sql):
    [agg] = _aggs(sql)
    assert agg["status"] == rt.MULTIPLIED
    assert [f["risk"] for f in agg["findings"]] == ["fan_trap"]
    assert agg["result_affected"] is True


def test_folding_does_not_change_the_aggregate_as_written():
    """The analysis compares folded names; the receipt still echoes what the caller wrote."""
    [agg] = _aggs(
        "SELECT SUM(ORDERS.total_amount) FROM ORDERS JOIN ORDER_ITEMS ON ORDER_ITEMS.order_id = ORDERS.id"
    )
    assert agg["aggregate"] == "SUM(ORDERS.total_amount)"


def test_folding_leaves_the_model_relationships_as_declared():
    org = _sales_org()
    before = [(r.from_table, r.to_table) for r in rt._cardinality_index(org)]
    rt.assemble_receipt(
        org,
        "SELECT SUM(ORDERS.total_amount) FROM ORDERS JOIN ORDER_ITEMS "
        "ON ORDER_ITEMS.order_id = ORDERS.id",
    )
    assert [(r.from_table, r.to_table) for r in rt._cardinality_index(org)] == before


# --- #450: whether the duplication can have changed the value ------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "MAX(orders.total_amount)",
        "MIN(orders.total_amount)",
        "COUNT(DISTINCT orders.id)",
    ],
)
def test_an_immune_aggregate_stays_multiplied_but_is_not_affected(expr):
    [agg] = _aggs(f"SELECT {expr} {FAN}")
    # The rows really were duplicated, so `not_multiplied` would be a false claim (ACE-083).
    assert agg["status"] == rt.MULTIPLIED
    assert agg["result_affected"] is False


def test_a_sum_and_a_max_over_one_fan_are_told_apart():
    by_expr = {
        a["aggregate"]: a
        for a in _aggs(f"SELECT SUM(orders.total_amount), MAX(orders.total_amount) {FAN}")
    }
    assert by_expr["SUM(orders.total_amount)"]["result_affected"] is True
    assert by_expr["MAX(orders.total_amount)"]["result_affected"] is False


def test_a_clean_aggregate_is_not_affected():
    [agg] = _aggs("SELECT SUM(orders.total_amount) FROM orders")
    assert agg["status"] == rt.NOT_MULTIPLIED and agg["result_affected"] is False


def test_an_immune_outer_aggregate_over_an_inflated_subquery_is_never_cleared():
    """The nested shape: the inner SUM is inflated by the fan, the outer MAX is immune to
    duplication of ITS rows. Immunity must not reach past its own SELECT, so the outer value reads
    as undetermined, never as unaffected, and the section says the inner aggregate was not read."""
    receipt = rt.assemble_receipt(
        _sales_org(),
        (f"SELECT MAX(t.total) FROM (SELECT SUM(orders.total_amount) AS total {FAN}) t"),
    )
    [agg] = receipt["aggregates"]["items"]
    assert agg["status"] == rt.UNDETERMINED and agg["result_affected"] is None
    assert "inside a CTE or a subquery is not reported" in receipt["aggregates"]["undetermined"]


def test_a_downgraded_report_cannot_keep_a_stale_answer():
    """`result_affected` is derived, so a report rewritten to `undetermined` after it was built
    (the cap path does this) cannot still claim the value was unaffected."""
    from dataclasses import replace

    report = rt.AggregateReport(
        aggregate="MAX(x)",
        scope="main",
        status=rt.MULTIPLIED,
        findings=[rt.Finding("fan_out_invariant", reason="r")],
    )
    assert report.result_affected is False
    assert replace(report, status=rt.UNDETERMINED).result_affected is None


def test_any_trap_beside_an_immune_finding_counts_as_affected():
    report = rt.AggregateReport(
        aggregate="MAX(x)",
        scope="main",
        status=rt.MULTIPLIED,
        findings=[
            rt.Finding("fan_out_invariant", reason="r"),
            rt.Finding("chasm_trap", reason="r"),
        ],
    )
    assert report.result_affected is True


# --- #202: the shared dimension a chasm names does not depend on hash order ----------------------

_TWO_DIMS = """
import sys
sys.path[:0] = {paths!r}
from semantic_model import models as m, runtime as rt
rels = [m.Relationship(from_table=a, to_table=d, from_column="k", to_column="k",
                       relationship="many_to_one")
        for a in ("fact_a", "fact_b") for d in ("dim_one", "dim_two")]
print(rt._shared_dimension({{"fact_a", "fact_b"}},
                           {{"fact_a", "fact_b", "dim_one", "dim_two"}}, rels))
"""


def test_the_shared_dimension_is_the_same_in_every_process():
    paths = [os.path.dirname(os.path.dirname(rt.__file__))]
    picks = set()
    for seed in ("0", "1", "2", "3", "4", "5"):
        out = subprocess.run(
            [sys.executable, "-c", _TWO_DIMS.format(paths=paths)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        picks.add(out.stdout.strip())
    assert picks == {"dim_one"}


_INNER = (
    "SELECT order_items.order_id, SUM(orders.total_amount) AS v "
    f"{FAN} GROUP BY order_items.order_id"
)


@pytest.mark.parametrize(
    "sql",
    [
        # A derived table beside a fan on a table the analysis did resolve: the fan makes the
        # status `multiplied`, but `t.v` is itself an inflated SUM the analysis cannot see into.
        f"SELECT MAX(orders.total_amount + t.v) {FAN} JOIN ({_INNER}) t ON t.order_id = orders.id",
        # The same shape spelled as a CTE.
        f"WITH x AS ({_INNER}) SELECT MAX(orders.total_amount + x.v) {FAN} "
        "JOIN x ON x.order_id = orders.id",
    ],
)
def test_an_immune_aggregate_with_an_unresolved_read_is_never_cleared(sql):
    [agg] = _aggs(sql)
    assert agg["status"] == rt.MULTIPLIED
    assert agg["result_affected"] is None


def test_two_aggregated_subqueries_joined_on_a_shared_key_are_never_cleared():
    """The chasm shape: each subquery aggregates against `customers`, and the two are joined on it."""
    [agg] = _aggs(
        "SELECT MAX(a.s) FROM (SELECT customers.id AS cid, SUM(orders.total_amount) AS s "
        "FROM customers JOIN orders ON orders.customer_id = customers.id GROUP BY customers.id) a "
        "JOIN (SELECT customers.id AS cid, COUNT(tickets.id) AS n FROM customers "
        "JOIN tickets ON tickets.customer_id = customers.id GROUP BY customers.id) b "
        "ON a.cid = b.cid"
    )
    assert agg["result_affected"] is not False


# --- the meaning checks still see an uppercase model -----------------------------------------------


def _upper_org():
    """Table names as an engine that stores them uppercase hands them back."""
    tables = [
        m.Table(
            name="ORDERS",
            schema="public",
            storage_connection="c",
            grain=["id"],
            columns=[
                m.Column(name="id", type="integer"),
                m.Column(name="rate", type="decimal", aggregation="averageable"),
                m.Column(name="balance", type="decimal"),
                m.Column(name="created_at", type="timestamp"),
            ],
        ),
        m.Table(
            name="ORDER_ITEMS",
            schema="public",
            storage_connection="c",
            grain=["id"],
            columns=[
                m.Column(name="id", type="integer"),
                m.Column(name="order_id", type="integer"),
            ],
        ),
    ]
    rels = [
        m.Relationship(
            from_table="ORDER_ITEMS",
            to_table="ORDERS",
            from_column="order_id",
            to_column="id",
            relationship="many_to_one",
        )
    ]
    metrics = [
        m.Metric(
            name="balance_total",
            calculation="end-of-day balance",
            bindings={"PostgreSQL": "SUM(ORDERS.balance)"},
            non_additive_dimensions=["created_at"],
            source_tables=["ORDERS"],
        )
    ]
    return m.Datasource(
        datasource="shop",
        subject_areas=[
            m.SubjectArea(name="s", tables_defined=tables, relationships=rels, metrics=metrics)
        ],
    )


def _risks(org, sql):
    return [
        [f["risk"] for f in a["findings"]]
        for a in rt.assemble_receipt(org, sql)["aggregates"]["items"]
    ]


def test_an_uppercase_model_keeps_its_aggregation_class_finding():
    """Folding the names for the fan detector must not hide a SUM of a rate."""
    assert _risks(_upper_org(), "SELECT SUM(ORDERS.rate) FROM ORDERS") == [["bad_aggregation"]]


def test_an_uppercase_model_keeps_its_semi_additive_finding():
    assert _risks(
        _upper_org(),
        "SELECT ORDERS.created_at, SUM(ORDERS.balance) FROM ORDERS GROUP BY ORDERS.created_at",
    ) == [["semi_additive"]]


def test_an_uppercase_model_still_reports_its_fan():
    assert _risks(
        _upper_org(),
        "SELECT SUM(ORDERS.balance) FROM ORDERS JOIN ORDER_ITEMS "
        "ON ORDER_ITEMS.order_id = ORDERS.id",
    ) == [["fan_trap"]]


# --- #453: a qualifier, the meaning checks and the finding text, in any case ------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # The qualifier is spelled differently from the FROM clause it refers to.
        "SELECT SUM(ORDERS.total_amount) FROM orders JOIN order_items "
        "ON order_items.order_id = orders.id",
        "SELECT SUM(O.total_amount) FROM orders o JOIN order_items oi ON oi.order_id = o.id",
    ],
)
def test_a_qualifier_in_another_case_still_finds_the_fan(sql):
    [agg] = _aggs(sql)
    assert agg["status"] == rt.MULTIPLIED
    assert [f["risk"] for f in agg["findings"]] == ["fan_trap"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(o.rate) FROM orders o",  # lowercase statement, uppercase model
        "SELECT SUM(ORDERS.RATE) FROM ORDERS",  # the column in another case too
    ],
)
def test_the_aggregation_class_check_matches_names_in_any_case(sql):
    assert _risks(_upper_org(), sql) == [["bad_aggregation"]]


def test_the_semi_additive_check_matches_names_in_any_case():
    assert _risks(
        _upper_org(),
        "SELECT orders.created_at, SUM(orders.balance) FROM orders GROUP BY orders.created_at",
    ) == [["semi_additive"]]


def test_a_finding_names_tables_as_the_model_declares_them():
    """Compared folded, shown as declared: `ORDERS` on a model that says `ORDERS`, whatever the
    statement wrote, and `orders` on a model that says `orders`."""
    upper = rt.assemble_receipt(
        _upper_org(),
        "select sum(orders.balance) from orders join order_items on order_items.order_id = orders.id",
    )["aggregates"]["items"]
    assert upper[0]["joins"] == ["ORDERS (1) <- ORDER_ITEMS (N)"]
    [lower] = _aggs(
        "SELECT SUM(ORDERS.total_amount) FROM ORDERS JOIN ORDER_ITEMS ON ORDER_ITEMS.order_id = ORDERS.id"
    )
    assert lower["joins"] == ["orders (1) <- order_items (N)"]


@pytest.mark.parametrize(
    "sql, risk",
    [
        ("SELECT SUM(O.rate) FROM orders o", "bad_aggregation"),
        ("SELECT SUM(o.rate) FROM ORDERS O", "bad_aggregation"),
        (
            "SELECT o.created_at, SUM(O.balance) FROM orders o GROUP BY o.created_at",
            "semi_additive",
        ),
        # The time grouping is found through the alias in another case too.
        (
            "SELECT O.created_at, SUM(o.balance) FROM orders o GROUP BY O.created_at",
            "semi_additive",
        ),
    ],
)
def test_the_meaning_checks_follow_an_alias_in_another_case(sql, risk):
    assert _risks(_upper_org(), sql) == [[risk]]


def test_an_unquoted_qualifier_matching_two_aliases_is_never_cleared():
    """`o` and a quoted `"O"` both in scope: which one an unquoted `O` binds to is engine folding,
    and the scope map no longer knows which alias was quoted. Guessing the quoted one attributed the
    SUM to `order_items` and reported it `not_multiplied`; it is left undetermined instead."""
    [agg] = _aggs(
        'SELECT SUM(O.total_amount) FROM orders AS o JOIN order_items AS "O" ON "O".order_id = o.id'
    )
    assert agg["status"] != rt.NOT_MULTIPLIED and agg["result_affected"] is not False


def test_a_quoted_qualifier_binds_only_its_exact_alias():
    [agg] = _aggs(
        'SELECT SUM("o".total_amount) FROM orders AS "o" JOIN order_items AS oi ON oi.order_id = "o".id'
    )
    assert agg["status"] == rt.MULTIPLIED
