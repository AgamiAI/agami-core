"""Scan risk on a large table (#451), kept to what the model knows for certain.

The receipt flags one structural shape: a large table on the side of an outer join that is not
preserved, from plain tables the model sizes as small. A timeout refusal describes any outer join's
shape in the caller's own table names, never a model fact, because a refusal may only echo what the
caller wrote. Advisory everywhere; nothing here refuses.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

import execute_sql  # noqa: E402
from sqlglot import parse_one  # noqa: E402

from test_semantic_model_runtime import m, rt  # noqa: E402


def _table(name, rows=None, cols=(), filters=(), schema="public"):
    return m.Table(
        name=name,
        schema=schema,
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
    tree = parse_one(sql, read="spark" if " SEMI " in sql or " ANTI " in sql else "postgres")
    return [(f["risk"], f["table"]) for f in rt.scan_findings(tree, ORG)]


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
        # A CTE hides the table only where it is visible: a schema-qualified read is the table.
        (
            "WITH fact_lines AS (SELECT 1 AS period_key) "
            "SELECT 1 FROM periods p LEFT JOIN public.fact_lines f ON f.period_key = p.period_key",
            [("outer_join", "public.fact_lines")],
        ),
        # A subquery on the preserved side may hold the large read: not looked into, not flagged.
        (
            "SELECT 1 FROM (SELECT period_key FROM events) e "
            "LEFT JOIN fact_lines f ON f.period_key = e.period_key",
            [],
        ),
        # Semi and anti joins carry a side but are not outer joins.
        ("SELECT 1 FROM periods p LEFT SEMI JOIN fact_lines f ON f.period_key = p.period_key", []),
        ("SELECT 1 FROM periods p LEFT ANTI JOIN fact_lines f ON f.period_key = p.period_key", []),
        # A preserved table with no known size is not assumed small.
        ("SELECT 1 FROM big_unsized b LEFT JOIN fact_lines f ON f.period_key = b.k", []),
        # A filtered or unfiltered read of one table is not judged at all.
        ("SELECT COUNT(*) FROM fact_lines", []),
    ],
)
def test_scan_findings(sql, expected):
    assert _risks(sql) == expected


def test_a_table_is_sized_by_its_own_schema():
    """`staging.orders` is small; it must not borrow the large `sales.orders` row count, and the large
    one is found though it is the second table of that name the model declares."""
    org = m.Datasource(
        datasource="shop",
        subject_areas=[
            m.SubjectArea(
                name="s",
                tables_defined=[
                    _table("orders", 10, ["k"], schema="staging"),
                    _table("orders", 5_000_000, ["k"], schema="sales"),
                    _table("periods", 2_000, ["k"]),
                ],
            )
        ],
    )
    sql = "SELECT 1 FROM periods p LEFT JOIN {}.orders o ON o.k = p.k"
    found = [f["table"] for f in rt.scan_findings(parse_one(sql.format("sales"), read="postgres"), org)]
    assert found == ["sales.orders"]
    assert rt.scan_findings(parse_one(sql.format("staging"), read="postgres"), org) == []


def test_a_table_declared_twice_keeps_its_largest_estimate():
    """Declared in two subject areas, the later copy unsized: declaration order must not hide it."""
    org = m.Datasource(
        datasource="shop",
        subject_areas=[
            m.SubjectArea(name="a", tables_defined=[_table("fact_lines", 7_000_000, ["k"]),
                                                    _table("periods", 2_000, ["k"])]),
            m.SubjectArea(name="b", tables_defined=[_table("fact_lines", None, ["k"])]),
        ],
    )
    sql = "SELECT 1 FROM periods p LEFT JOIN fact_lines f ON f.k = p.k"
    assert [f["table"] for f in rt.scan_findings(parse_one(sql, read="postgres"), org)] == ["fact_lines"]


def test_many_outer_joins_stay_bounded():
    """The pairs are produced lazily and capped, however many arms carry the shape."""
    arm = "SELECT 1 FROM periods p LEFT JOIN fact_lines f ON f.period_key = p.period_key"
    sql = " UNION ALL ".join([arm] * 200)
    assert sum(1 for _ in rt._outer_joins(parse_one(sql, read="postgres"))) == rt._SCAN_MAX_PAIRS
    assert len(rt.scan_risks(rt.build_guard_context(sql, ORG))) == 1


def test_only_a_direct_two_table_join_is_described():
    """With two tables on the preserved side, the one the outer table joins is in the ON clause, so
    advice naming either could be wrong: say nothing."""
    sql = ("SELECT 1 FROM events e JOIN periods p ON p.period_key = e.period_key "
           "LEFT JOIN fact_lines f ON f.period_key = p.period_key")
    assert _risks(sql) == []
    assert rt.scan_risks(rt.build_guard_context(sql, ORG)) == []


def test_a_cte_is_not_described_as_a_table():
    sql = ("WITH fact_lines AS (SELECT 1 AS period_key) "
           "SELECT 1 FROM periods p LEFT JOIN fact_lines f ON f.period_key = p.period_key")
    assert rt.scan_risks(rt.build_guard_context(sql, ORG)) == []


def test_tables_are_named_as_written():
    sql = "SELECT 1 FROM public.periods p LEFT JOIN public.fact_lines f ON f.period_key = p.period_key"
    assert "INNER JOIN `public.periods`" in rt.scan_risks(rt.build_guard_context(sql, ORG))[0]
    found = rt.scan_findings(parse_one(sql, read="postgres"), ORG)
    assert "INNER JOIN `public.periods`" in rt.scan_sentence(found[0])
    assert "Start FROM `public.fact_lines`" in rt.scan_sentence(found[0])


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
        "SELECT 1 FROM periods p LEFT JOIN public.fact_lines "
        "ON fact_lines.period_key = p.period_key WHERE EXISTS (SELECT 1 FROM x)"
    )
    items = rt.assemble_receipt(ORG, sql)["tables"]["items"]
    assert len([i for i in items if i["ref"] == "public.fact_lines"]) == 2
    assert len([i for i in items if i["scan_risk"]]) == 1


def test_an_inner_join_read_carries_no_scan_risk():
    receipt = rt.assemble_receipt(
        ORG, "SELECT SUM(f.amount) FROM fact_lines f JOIN periods p ON p.period_key = f.period_key"
    )
    assert all(item["scan_risk"] is None for item in receipt["tables"]["items"])


def _refusal(sql):
    token = execute_sql._guard_scan.set(tuple(rt.scan_risks(rt.build_guard_context(sql, ORG))))
    try:
        return execute_sql._resource_limit_refusal(execute_sql._ResourceLimit("x")).remediation
    finally:
        execute_sql._guard_scan.reset(token)


def test_a_timeout_describes_the_outer_join_in_the_callers_names():
    remediation = _refusal(OUTER)
    assert remediation.startswith(
        "The statement keeps every `periods` row and pulls `fact_lines` in from the outer side of a "
        "LEFT JOIN"
    )
    assert "INNER JOIN `periods`" in remediation
    assert remediation.endswith(execute_sql._NARROW_IT)


def test_a_timeout_refusal_names_no_model_fact():
    """No row count and no recommended filter, and the same wording whatever the sizes: a forced
    timeout must not read what the model declares (SECURITY.md, "What a refusal may name")."""
    remediation = _refusal(OUTER)
    assert "7,000,000" not in remediation and "date_key" not in remediation
    small_outer = "SELECT 1 FROM fact_lines f LEFT JOIN periods p ON p.period_key = f.period_key"
    assert "pulls `periods` in from the outer side" in _refusal(small_outer)
    assert _refusal("SELECT COUNT(*) FROM fact_lines") == execute_sql._NARROW_IT


def test_a_repeated_shape_is_named_once():
    assert len(rt.scan_risks(rt.build_guard_context(f"{OUTER} UNION ALL {OUTER}", ORG))) == 1


def test_without_an_outer_join_the_advice_stays_generic():
    remediation = execute_sql._resource_limit_refusal(execute_sql._ResourceLimit("x")).remediation
    assert remediation == execute_sql._NARROW_IT
