"""Scan risk on a large table (#451), kept to what the model knows for certain.

The receipt flags one structural shape: a large table on the side of an outer join that is not
preserved, from a side holding no large table. A timeout refusal names that, and every other large
table the statement reads with the filter the model recommends for it, instead of the generic
advice. Advisory everywhere; nothing here refuses. Sizes come from
`performance_hints.estimated_row_count`, so a table the model holds no size for is never named.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

import execute_sql  # noqa: E402
from sqlglot import parse_one  # noqa: E402

from test_semantic_model_runtime import m, rt  # noqa: E402


def _table(name, rows=None, cols=(), filters=()):
    return m.Table(
        name=name,
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            *[m.Column(name=c, type="integer") for c in cols],
        ],
        performance_hints=(
            m.PerformanceHints(estimated_row_count=rows, recommended_filters=list(filters))
            if rows
            else None
        ),
    )


ORG = m.Datasource(
    datasource="shop",
    subject_areas=[
        m.SubjectArea(
            name="s",
            tables_defined=[
                _table("fact_lines", 7_000_000, ["period_key", "date_key", "amount"], ["date_key"]),
                _table("events", 3_000_000, ["period_key"]),
                _table("periods", 2_000, ["period_key", "fiscal_year"]),
                _table("big_unsized", None, ["k"]),
            ],
        )
    ],
)

OUTER = (
    "SELECT p.fiscal_year, SUM(f.amount) FROM periods p LEFT JOIN fact_lines f "
    "ON f.period_key = p.period_key GROUP BY 1"
)


def _risks(sql):
    return [(f["risk"], f["table"]) for f in rt.scan_findings(parse_one(sql, read="postgres"), ORG)]


@pytest.mark.parametrize(
    "sql, expected",
    [
        (OUTER, [("outer_join", "fact_lines")]),
        (
            "SELECT SUM(f.amount) FROM fact_lines f RIGHT JOIN periods p ON f.period_key = p.period_key",
            [("outer_join", "fact_lines")],
        ),
        # Driven from the fact table, or with an inner join: nothing to flag.
        (
            "SELECT SUM(f.amount) FROM fact_lines f LEFT JOIN periods p ON p.period_key = f.period_key",
            [],
        ),
        ("SELECT SUM(f.amount) FROM periods p JOIN fact_lines f ON f.period_key = p.period_key", []),
        # Large on both sides: the preserved side is already the big read.
        ("SELECT 1 FROM events e LEFT JOIN fact_lines f ON f.period_key = e.period_key", []),
        # No size in the model, never flagged; a CTE named like the table is not the table.
        ("SELECT 1 FROM periods p LEFT JOIN big_unsized b ON b.k = p.period_key", []),
        (
            "WITH fact_lines AS (SELECT 1 AS period_key) "
            "SELECT 1 FROM periods p LEFT JOIN fact_lines f ON f.period_key = p.period_key",
            [],
        ),
        # A filtered or unfiltered read of one table is not judged at all.
        ("SELECT COUNT(*) FROM fact_lines", []),
    ],
)
def test_scan_findings(sql, expected):
    assert _risks(sql) == expected


def test_the_receipt_names_the_risk_on_the_table_it_is_about():
    by_ref = {item["ref"]: item for item in rt.assemble_receipt(ORG, OUTER)["tables"]["items"]}
    assert "outer side of a join from `periods`" in by_ref["fact_lines"]["scan_risk"]
    assert "about 7,000,000 rows" in by_ref["fact_lines"]["scan_risk"]
    assert by_ref["periods"]["scan_risk"] is None


def test_the_risk_lands_on_that_reference_and_no_other():
    """The same table read in a CTE body and in the outer join: only the outer join's reference is
    flagged, and a schema-qualified one is matched as well."""
    sql = (
        "WITH x AS (SELECT amount FROM public.fact_lines WHERE date_key = 1) "
        "SELECT SUM(x.amount) FROM x CROSS JOIN periods p LEFT JOIN public.fact_lines "
        "ON fact_lines.period_key = p.period_key"
    )
    items = rt.assemble_receipt(ORG, sql)["tables"]["items"]
    assert len([i for i in items if i["ref"] == "public.fact_lines"]) == 2
    assert len([i for i in items if i["scan_risk"]]) == 1


def test_an_inner_join_read_carries_no_scan_risk():
    receipt = rt.assemble_receipt(
        ORG, "SELECT SUM(f.amount) FROM fact_lines f JOIN periods p ON p.period_key = f.period_key"
    )
    assert all(item["scan_risk"] is None for item in receipt["tables"]["items"])


def test_a_timeout_names_every_large_table_read_and_its_filter():
    sql = OUTER.replace("GROUP BY 1", "CROSS JOIN events e GROUP BY 1")
    risks = rt.scan_risks(rt.build_guard_context(sql, ORG))
    assert len(risks) == 2
    assert "outer side of a join from `periods`" in risks[0]
    assert risks[1].startswith("`events` is large (about 3,000,000 rows)")
    assert "a date or period range" in risks[1]  # no recommended filter declared


def test_a_repeated_shape_is_named_once():
    risks = rt.scan_risks(rt.build_guard_context(f"{OUTER} UNION ALL {OUTER}", ORG))
    assert len(risks) == 1


def test_a_timeout_names_the_recommended_filter():
    risks = rt.scan_risks(rt.build_guard_context("SELECT COUNT(*) FROM fact_lines", ORG))
    assert risks == [
        "`fact_lines` is large (about 7,000,000 rows); narrow it with a range on `date_key` "
        "if the question allows."
    ]
    assert rt.scan_risks(rt.build_guard_context("SELECT COUNT(*) FROM periods", ORG)) == []


def test_a_timeout_refusal_names_the_large_table_and_the_fix():
    token = execute_sql._guard_scan.set(("`fact_lines` is large (about 7,000,000 rows).",))
    try:
        remediation = execute_sql._resource_limit_refusal(execute_sql._ResourceLimit("x")).remediation
    finally:
        execute_sql._guard_scan.reset(token)
    assert remediation.startswith("`fact_lines` is large")
    assert remediation.endswith("Then run it again.")


def test_without_a_known_large_table_the_advice_stays_generic():
    remediation = execute_sql._resource_limit_refusal(execute_sql._ResourceLimit("x")).remediation
    assert remediation == execute_sql._NARROW_IT
