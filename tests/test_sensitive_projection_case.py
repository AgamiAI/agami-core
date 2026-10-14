"""The receipt's sensitive-projection fact matches names in any case the statement writes (#456).

With `orders.ssn` declared sensitive, `SELECT ORDERS.SSN FROM ORDERS` returns that column as
surely as the lowercase spelling, and the receipt has to say so. Before, the check compared names
exactly, so every other spelling reported no sensitive column projected.

A miss here is the harmful direction (the receipt claims nothing sensitive came back), so the
match is deliberately broad: quoted names fold too, and an alias the statement leaves ambiguous
still counts.

Synthetic, generic names only.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

from test_semantic_model_runtime import m, rt  # noqa: E402


def _table(name, *cols, sensitive=()):
    return m.Table(
        name=name,
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[m.Column(name="id", type="integer")]
        + [m.Column(name=c, type="string", sensitive=c in sensitive) for c in cols],
    )


def _org(*tables):
    return m.Datasource(
        datasource="shop",
        subject_areas=[m.SubjectArea(name="s", tables_defined=list(tables))],
    )


ORG = _org(
    _table("orders", "ssn", "region", sensitive=("ssn",)),
    _table("regions", "ssn", "name"),  # same column name, not sensitive here
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT orders.ssn FROM orders",
        "SELECT O.ssn FROM ORDERS o",
        "SELECT ORDERS.SSN FROM ORDERS",
        "SELECT Ssn FROM Orders",
        'SELECT "SSN" FROM orders',
        "SELECT MAX(o.SSN) FROM orders o",
        'SELECT "O".ssn FROM orders o',
        "SELECT public.ORDERS.SSN FROM public.ORDERS",
    ],
)
def test_a_sensitive_column_in_any_case_is_reported(sql):
    assert rt.projected_sensitive_columns(sql, ORG) == ["orders.ssn"]


@pytest.mark.parametrize("sql", ["SELECT * FROM ORDERS", "SELECT O.* FROM orders o"])
def test_a_star_in_any_case_reports_the_tables_sensitive_columns(sql):
    assert rt.projected_sensitive_columns(sql, ORG) == ["orders.ssn"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(DISTINCT ORDERS.SSN) FROM ORDERS",  # a count discloses no value
        "SELECT R.SSN FROM REGIONS r",  # same name, but the table's column isn't sensitive
        "SELECT o.region FROM orders o WHERE o.SSN = 'x'",  # filtered on, not returned
    ],
)
def test_what_is_not_a_sensitive_projection_still_is_not(sql):
    assert rt.projected_sensitive_columns(sql, ORG) == []


def test_a_column_the_statement_cannot_pin_to_a_table_is_reported_by_its_own_name():
    """Unqualified across two tables that both have `ssn`: which one is unknown, so it counts."""
    sql = "SELECT SSN FROM orders JOIN regions ON regions.id = orders.id"
    assert rt.projected_sensitive_columns(sql, ORG) == ["SSN"]


@pytest.mark.parametrize(
    "sql, label",
    [
        ("SELECT O.SSN, o.region FROM ORDERS o", "public.orders.SSN"),
        ("SELECT Ssn, region FROM Orders", "public.orders.Ssn"),
        ("SELECT o.ssn FROM orders o", "public.orders.ssn"),
    ],
)
def test_the_receipt_flags_the_column_however_it_was_written(sql, label):
    receipt = rt.assemble_receipt(ORG, sql)
    flagged = [i["column"] for i in receipt["columns"]["items"] if i.get("sensitive")]
    assert flagged == [label]
