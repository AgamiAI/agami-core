"""Table and column scope identify a table by (schema, name), and CTEs by the WITH enclosing them (#332).

The gates used to compare a referenced table's bare name alone, so with only `sales_data.orders`
declared, `SELECT ... FROM staging.orders` passed. And a CTE name was skipped wherever it appeared,
so a WITH anywhere in a statement could hide a physical table of the same name elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import guardrail  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402
from semantic_model import validator as V  # noqa: E402


def _table(name: str, schema: str | None, *cols: str) -> m.Table:
    return m.Table(
        name=name,
        schema=schema,
        storage_connection="c",
        grain=["id"],
        description=name,
        columns=[m.Column(name=c, type="integer") for c in ("id", *cols)],
    )


def _org(*areas: m.SubjectArea) -> m.Datasource:
    return m.Datasource(
        datasource="acme",
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=list(areas),
    )


def _single_schema_org() -> m.Datasource:
    """Only `sales_data.orders` and `sales_data.customers` are declared."""
    return _org(
        m.SubjectArea(
            name="sales",
            tables_defined=[
                _table("orders", "sales_data", "amount"),
                _table("customers", "sales_data"),
            ],
        )
    )


def _clash_org() -> m.Datasource:
    """`orders` under `sales_data` and `staging`, with a column only the staging one has."""
    return _org(
        m.SubjectArea(
            name="sales",
            tables_defined=[
                _table("orders", "sales_data", "amount"),
                _table("customers", "sales_data"),
            ],
        ),
        m.SubjectArea(name="landing", tables_defined=[_table("orders", "staging", "raw_payload")]),
    )


def _both_paths(sql: str, org: m.Datasource, gate):
    """The gate's verdict, asserted identical with and without a shared guard context."""
    alone = gate(sql, org)
    assert gate(sql, org, ctx=rt.build_guard_context(sql, org)) == alone
    return alone


# ---------------------------------------------------------------- table scope: schemas


def test_same_name_in_another_schema_is_refused():
    refusal = _both_paths(
        "SELECT id FROM staging.orders", _single_schema_org(), rt.check_table_scope
    )
    assert refusal is not None and refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert "staging.orders" in refusal.detail
    assert "sales_data" not in refusal.detail + refusal.remediation


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM sales_data.orders",
        "SELECT id FROM SALES_DATA.ORDERS",
        "SELECT id FROM orders",
    ],
)
def test_declared_schema_or_unique_bare_name_passes(sql):
    assert _both_paths(sql, _single_schema_org(), rt.check_table_scope) is None


def test_each_clashing_table_is_reachable_qualified():
    for sql in (
        "SELECT id FROM sales_data.orders",
        "SELECT id FROM staging.orders",
        "SELECT s.id FROM sales_data.orders s JOIN staging.orders g ON g.id = s.id",
        "SELECT id FROM customers",
    ):
        assert _both_paths(sql, _clash_org(), rt.check_table_scope) is None, sql


def test_unqualified_name_under_two_schemas_is_refused_without_naming_them():
    refusal = _both_paths("SELECT id FROM orders", _clash_org(), rt.check_table_scope)
    assert refusal is not None and refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert "cannot be decided: orders." in refusal.detail
    assert "not in the semantic model" not in refusal.detail
    assert refusal.remediation == "Qualify each of those tables with its schema (schema.table)."
    for schema in ("sales_data", "staging"):
        assert schema not in refusal.detail + refusal.remediation


def test_ambiguous_and_undeclared_in_one_statement_report_both():
    refusal = rt.check_table_scope(
        "SELECT o.id FROM orders o JOIN ghost g ON g.id = o.id", _clash_org()
    )
    assert refusal is not None
    assert "not in the semantic model: ghost" in refusal.detail
    assert "cannot be decided: orders." in refusal.detail


def test_a_schemaless_table_is_reachable_only_unqualified():
    org = _org(m.SubjectArea(name="sales", tables_defined=[_table("orders", None, "amount")]))
    assert _both_paths("SELECT id FROM orders", org, rt.check_table_scope) is None
    refusal = _both_paths("SELECT id FROM public.orders", org, rt.check_table_scope)
    assert refusal is not None and "public.orders" in refusal.detail


def test_schemaless_beside_a_schema_is_ambiguous_unqualified():
    org = _org(
        m.SubjectArea(name="a", tables_defined=[_table("orders", None)]),
        m.SubjectArea(name="b", tables_defined=[_table("orders", "staging")]),
    )
    assert "cannot be decided: orders." in rt.check_table_scope("SELECT id FROM orders", org).detail
    assert rt.check_table_scope("SELECT id FROM staging.orders", org) is None


def test_catalog_does_not_rescue_a_mismatched_schema():
    org = _single_schema_org()
    assert rt.check_table_scope("SELECT id FROM warehouse.sales_data.orders", org) is None
    assert rt.check_table_scope("SELECT id FROM warehouse.staging.orders", org) is not None


# ---------------------------------------------------------------- table scope: CTEs

# Each of these read an undeclared physical table beside a same-named CTE that does not enclose it.
_CTE_ESCAPES = {
    "qualified_name_is_never_a_cte": "WITH orders AS (SELECT 1 AS id) SELECT id FROM staging.orders",
    "inner_with_hides_outer": (
        "SELECT id FROM secret WHERE EXISTS "
        "(WITH secret AS (SELECT id FROM orders) SELECT id FROM secret)"
    ),
    "forward_sibling": "WITH a AS (SELECT id FROM secret), secret AS (SELECT 1 AS id) SELECT id FROM a",
    "non_recursive_self_reference": "WITH secret AS (SELECT id FROM secret) SELECT id FROM secret",
    "recursive_non_union_body": "WITH RECURSIVE secret AS (SELECT id FROM secret) SELECT id FROM secret",
    "recursive_anchor_arm": (
        "WITH RECURSIVE secret AS "
        "(SELECT id FROM secret UNION ALL SELECT id + 1 FROM secret WHERE id < 0) SELECT id FROM secret"
    ),
    "quoted_cte_unquoted_reference": 'WITH "secret" AS (SELECT id FROM orders) SELECT id FROM secret',
    "unquoted_cte_quoted_reference": 'WITH secret AS (SELECT id FROM orders) SELECT id FROM "SECRET"',
}


@pytest.mark.parametrize("sql", list(_CTE_ESCAPES.values()), ids=list(_CTE_ESCAPES))
def test_a_cte_hides_only_references_its_with_encloses(sql):
    refusal = _both_paths(sql, _single_schema_org(), rt.check_table_scope)
    assert refusal is not None and "not in the semantic model" in refusal.detail


_CTE_LEGITIMATE = {
    "plain": "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
    "earlier_sibling": "WITH a AS (SELECT id FROM orders), b AS (SELECT id FROM a) SELECT id FROM b",
    "recursive_term": (
        "WITH RECURSIVE t AS (SELECT id FROM orders UNION ALL SELECT id + 1 FROM t WHERE id < 3) "
        "SELECT id FROM t"
    ),
    "inside_a_subquery": (
        "SELECT id FROM orders WHERE EXISTS (WITH c AS (SELECT id FROM customers) SELECT id FROM c)"
    ),
    "both_unquoted_any_case": "WITH X AS (SELECT id FROM orders) SELECT id FROM x",
    "both_quoted_exact": 'WITH "Recent" AS (SELECT id FROM orders) SELECT id FROM "Recent"',
}


@pytest.mark.parametrize("sql", list(_CTE_LEGITIMATE.values()), ids=list(_CTE_LEGITIMATE))
def test_a_cte_its_with_encloses_is_still_not_a_table(sql):
    # Parsed first, so a statement the parser cannot read never passes by degrading to allow.
    assert rt._parse_sql(sql, "postgres") is not None
    assert _both_paths(sql, _single_schema_org(), rt.check_table_scope) is None


# ---------------------------------------------------------------- column scope


def test_column_scope_binds_to_the_schema_read_not_the_union():
    org = _clash_org()
    refusal = _both_paths(
        "SELECT o.raw_payload FROM sales_data.orders o", org, rt.check_column_scope
    )
    assert refusal is not None and refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert "orders.raw_payload" in refusal.detail and "sales_data" not in refusal.detail
    assert (
        _both_paths("SELECT raw_payload FROM sales_data.orders", org, rt.check_column_scope)
        is not None
    )
    assert (
        _both_paths("SELECT o.raw_payload FROM staging.orders o", org, rt.check_column_scope)
        is None
    )
    assert _both_paths("SELECT amount FROM sales_data.orders", org, rt.check_column_scope) is None


def test_fully_qualified_columns_bind_to_their_own_unaliased_table():
    join = "FROM sales_data.orders JOIN staging.orders ON sales_data.orders.id = staging.orders.id"
    org = _clash_org()
    assert (
        _both_paths(f"SELECT sales_data.orders.amount {join}", org, rt.check_column_scope) is None
    )
    assert (
        _both_paths(f"SELECT staging.orders.amount {join}", org, rt.check_column_scope) is not None
    )


def test_column_scope_binds_an_outer_table_an_inner_with_shadows_by_name():
    sql = "SELECT o.bogus FROM orders o WHERE EXISTS (WITH orders AS (SELECT 1 AS id) SELECT id FROM orders)"
    refusal = _both_paths(sql, _single_schema_org(), rt.check_column_scope)
    assert refusal is not None and "orders.bogus" in refusal.detail


# ---------------------------------------------------------------- validator


def _schema_findings(org):
    return [f for f in V.validate(org).findings if f.code == "table_name_in_multiple_schemas"]


def test_validator_warns_on_a_name_under_two_schemas():
    (finding,) = _schema_findings(_clash_org())
    assert finding.severity == "warning"
    assert "orders" in finding.message and "qualified" in finding.message


def test_validator_is_silent_for_unique_names():
    assert _schema_findings(_single_schema_org()) == []
