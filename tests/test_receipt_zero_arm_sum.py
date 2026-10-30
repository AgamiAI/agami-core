"""A zero fallback under a SUM is not a fan (#579).

`SUM(CASE WHEN orders.status = 'shipped' THEN COALESCE(order_items.quantity, 0) END)` over
`orders JOIN order_items` sums an item-grain value, and the join duplicates no item. The receipt
reported a fan trap because the `0` arm belongs to no table and emptied the intersection that
decides the value's grain. A `0` or `NULL` arm adds nothing to a sum, so under a SUM it is left out.
Under COUNT or AVG it is still a counted row, and beneath arithmetic it is not the value, so those
keep reporting.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

from test_semantic_model_runtime import _sales_org, rt  # noqa: E402

FAN = "FROM orders JOIN order_items ON order_items.order_id = orders.id"


def _agg(expr: str) -> dict:
    [agg] = rt.assemble_receipt(_sales_org(), f"SELECT {expr} {FAN}")["aggregates"]["items"]
    return agg


@pytest.mark.parametrize(
    "expr",
    [
        "SUM(CASE WHEN orders.status = 'shipped' THEN COALESCE(order_items.quantity, 0) END)",
        "SUM(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE 0 END)",
        "SUM(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE NULL END)",
        "SUM(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE 0.0 END)",
        "SUM(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE (0) END)",
        "SUM(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE CAST(0 AS DECIMAL) END)",
        "SUM(IF(orders.status = 'shipped', order_items.quantity, 0))",
        "SUM(CASE WHEN orders.status = 'shipped' THEN COALESCE(order_items.quantity, 0.0) ELSE (0) END)",
        # The whole alternation wrapped: its value still reaches the SUM unchanged.
        "SUM((CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE 0 END))",
        "SUM(CAST(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE 0 END AS INT))",
    ],
)
def test_a_zero_or_null_arm_under_a_sum_is_not_a_fan(expr):
    agg = _agg(expr)
    assert agg["status"] == rt.NOT_MULTIPLIED
    assert agg["findings"] == []


@pytest.mark.parametrize(
    "expr",
    [
        # A constant counts the joined rows: item rows, where the predicate reads orders.
        "SUM(CASE WHEN orders.status = 'shipped' THEN 1 ELSE 0 END)",
        # Every arm inert leaves nothing on the value path, which is not a reason to clear it.
        "SUM(CASE WHEN orders.status = 'shipped' THEN 0 ELSE 0 END)",
        # The surviving arm is a one-side amount, which the join does repeat.
        "SUM(CASE WHEN orders.status = 'shipped' THEN orders.total_amount ELSE 0 END)",
        # Under AVG or COUNT a 0 is a counted row.
        "AVG(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE 0 END)",
        "COUNT(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE 0 END)",
        # Beneath arithmetic the 0 is not the value: where quantity is NULL it sums total_amount.
        "SUM(COALESCE(order_items.quantity, 0) + orders.total_amount)",
    ],
)
def test_a_fan_the_zero_does_not_explain_is_still_reported(expr):
    agg = _agg(expr)
    assert agg["status"] == rt.MULTIPLIED
    assert [f["risk"] for f in agg["findings"]] == ["fan_trap"]


def test_a_tiny_constant_is_not_read_as_zero():
    """1e-400 rounds to 0.0 as a float, but it is a constant added once per joined row."""
    agg = _agg("SUM(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE 1e-400 END)")
    assert agg["status"] == rt.MULTIPLIED


def test_a_string_zero_is_not_an_inert_arm():
    """'0' is a label, not a number: the arm stays in, and the fan is reported."""
    agg = _agg("SUM(CASE WHEN orders.status = 'shipped' THEN order_items.quantity ELSE '0' END)")
    assert agg["status"] == rt.MULTIPLIED
