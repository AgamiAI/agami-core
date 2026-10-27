"""Adversarial suite for the SELECT * ban + column-scope guard.

Each case is an attempt to slip an undeclared column or a `*` past the gate. A
green happy-path suite (test_column_scope_gate.py) does NOT prove the gate holds
against evasion — these do. Cases fall into four groups:

  * star-ban evasion            -> must refuse (rule: select_star)
  * column-scope evasion        -> must refuse (rule: column_scope)
  * documented accepted fail-open -> asserts *allow*, so a future narrowing of the
                                    boundary is a conscious, test-breaking decision
  * upstream-owned              -> asserts a different layer already catches it

The set-operation cases (UNION arm) are the regressions that prove the fix for the
`parse_one -> exp.Union is not exp.Select` bypass that also affected check_table_scope.

Both gates return `guardrail.Refusal | None`, so "must refuse" reads `is not None` and
"accepted fail-open" reads `is None`. The rule id on every refusal is asserted too: an
evasion caught by the WRONG gate would otherwise pass as if the intended one held.
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


def _scope_org():
    """Org declaring orders(id, amount, customer_id, status, payload) + customers(id, name, region).

    `payload` is typed `json` because the semi-structured residual below needs a declared column
    whose contents the model cannot describe. Every other test here ignores it.
    """
    def _t(name, cols):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name,
                       columns=[m.Column(name=c, type=typ) for c, typ in cols])
    orders = _t("orders", [("id", "integer"), ("amount", "decimal"),
                           ("customer_id", "integer"), ("status", "string"),
                           ("payload", "json")])
    customers = _t("customers", [("id", "integer"), ("name", "string"), ("region", "string")])
    return m.Datasource(datasource="Shop",
                          subject_areas=[m.SubjectArea(name="sales",
                              tables_defined=[orders, customers])])


def _assert_columns_refused(refusal, *columns: str) -> None:
    """The refusal names exactly `columns` and nothing else — the successor to `.columns == [...]`.

    Exact rather than substring: an `in` check would also pass for a refusal that had listed the
    columns the model DOES declare, which is precisely the enumeration the contract forbids.
    """
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert refusal.detail == ("query references column(s) not in the semantic model: "
                              + ", ".join(columns)
                              + " — only columns declared on the model's tables may be queried.")


# ===========================================================================
# Star-ban evasion -> must refuse
# ===========================================================================

STAR_EVASIONS = [
    "SELECT id FROM (SELECT * FROM orders) x",            # star hidden in a subquery
    "WITH t AS (SELECT * FROM orders) SELECT id FROM t",  # star hidden in a CTE body
    "SELECT id FROM orders UNION SELECT * FROM customers",  # star in a set-operation arm
    "SELECT o.* FROM orders o",                            # qualified star
    "SELECT (SELECT * FROM customers LIMIT 1) FROM orders",  # star in a scalar subquery
    "SELECT/**/ * FROM orders",                            # comment obfuscation
    "select * from orders",                                # lowercase
]


@pytest.mark.parametrize("sql", STAR_EVASIONS)
def test_star_evasion_refused(sql):
    refusal = rt.check_no_select_star(sql)
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_SELECT_STAR


# COUNT(*) / agg(*) must NOT be over-blocked by the star ban.
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders",
    "SELECT COUNT(DISTINCT id) FROM orders",
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
])
def test_aggregate_star_allowed(sql):
    assert rt.check_no_select_star(sql) is None


# ===========================================================================
# Column-scope evasion -> must refuse
# ===========================================================================

# An undeclared column smuggled into a non-SELECT clause.
CLAUSE_SMUGGLES = [
    ("SELECT id FROM orders WHERE bogus > 1", "bogus"),
    ("SELECT id FROM orders GROUP BY bogus", "bogus"),
    ("SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id HAVING SUM(bogus) > 0",
     "bogus"),
    ("SELECT id FROM orders ORDER BY bogus", "bogus"),
    ("SELECT o.id FROM orders o JOIN customers c ON o.bogus = c.id", "orders.bogus"),
]


@pytest.mark.parametrize("sql,offender", CLAUSE_SMUGGLES)
def test_undeclared_column_in_any_clause_refused(sql, offender):
    _assert_columns_refused(rt.check_column_scope(sql, _scope_org()), offender)


# An undeclared column wrapped in an expression / function / window.
EXPR_WRAPS = [
    "SELECT UPPER(bogus) FROM orders",
    "SELECT amount + bogus FROM orders",
    "SELECT SUM(bogus) FROM orders",
    "SELECT CASE WHEN bogus > 0 THEN 1 ELSE 0 END FROM orders",
    "SELECT ROW_NUMBER() OVER (ORDER BY bogus) FROM orders",
]


@pytest.mark.parametrize("sql", EXPR_WRAPS)
def test_undeclared_column_in_expression_refused(sql):
    _assert_columns_refused(rt.check_column_scope(sql, _scope_org()), "bogus")


def test_alias_masquerade_refused():
    # The OUTPUT alias `id` is declared, but the underlying `bogus` is not — we
    # validate the underlying column, not the alias it is renamed to.
    _assert_columns_refused(
        rt.check_column_scope("SELECT bogus AS id FROM orders", _scope_org()), "bogus")


def test_undeclared_column_in_union_arm_refused():
    _assert_columns_refused(rt.check_column_scope(
        "SELECT id FROM orders UNION SELECT bogus FROM customers", _scope_org()), "bogus")


def test_correlated_subquery_qualified_smuggle_refused():
    # `o.bogus` is qualified to the physical `orders` — caught regardless of the
    # surrounding subquery.
    _assert_columns_refused(rt.check_column_scope(
        "SELECT o.id FROM orders o "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.bogus)", _scope_org()),
        "orders.bogus")


def test_undeclared_column_alongside_where_subquery_refused():
    # A WHERE/IN subquery adds no columns to the outer select's scope, so a bare
    # undeclared column in the outer query is still caught.
    _assert_columns_refused(rt.check_column_scope(
        "SELECT bogus FROM orders WHERE id IN (SELECT id FROM customers)", _scope_org()), "bogus")


def test_quoted_identifier_undeclared_refused():
    # Documents the case-insensitive-match behavior: a quoted undeclared name is
    # still refused — and echoed back with the caller's own casing.
    _assert_columns_refused(
        rt.check_column_scope('SELECT "BOGUS" FROM orders', _scope_org()), "BOGUS")


# ===========================================================================
# Per-SELECT scope correctness (regressions for the global-map bugs)
# ===========================================================================

def test_alias_reused_across_scopes_resolves_locally():
    # `o` aliases orders in the outer query and customers in the correlated
    # subquery. A global alias map would resolve outer `o.amount` against the wrong
    # table (last-write-wins) and false-refuse; per-select resolution keeps it valid.
    res = rt.check_column_scope(
        "SELECT o.amount FROM orders o "
        "WHERE EXISTS (SELECT 1 FROM customers o WHERE o.id = 1)", _scope_org())
    assert res is None


def test_nested_output_alias_does_not_mask_outer_column():
    # An inner `AS bogus` must NOT let an unrelated outer `bogus` slip through. A
    # global output-alias set would skip the outer column; per-select scoping refuses.
    _assert_columns_refused(rt.check_column_scope(
        "SELECT bogus FROM orders WHERE id IN (SELECT id AS bogus FROM customers)",
        _scope_org()), "bogus")


# An unqualified undeclared column beside a CTE / derived source (#339). The gate used to skip it
# because the source MIGHT supply it; it is bound to the source only when the source's projection
# names it, so each of these reads the physical table's undeclared column.
BESIDE_A_SOURCE = [
    ("WITH c AS (SELECT 1 AS n) SELECT secret_col FROM orders, c", "secret_col"),
    ("SELECT secret_col FROM orders JOIN (SELECT id AS k FROM customers) s ON s.k = orders.id",
     "secret_col"),
    ("WITH c AS (SELECT id FROM customers) SELECT orders.secret_col FROM orders, c",
     "orders.secret_col"),
    # An output with no readable name cannot vouch for a column: fail closed.
    ("WITH c AS (SELECT count(*) FROM customers) SELECT count FROM orders, c", "count"),
    ("WITH c AS (VALUES (1)) SELECT column1 FROM orders, c", "column1"),
    # Correlated outward from a scope with no source supplying the name.
    ("SELECT (SELECT secret_col) FROM orders", "secret_col"),
    ("SELECT id FROM orders WHERE EXISTS "
     "(SELECT 1 FROM (SELECT 1 AS n) z WHERE secret_col = 1)", "secret_col"),
    # A CTE or derived body cannot see its owner's sources but does see the queries further out.
    ("SELECT (WITH c AS (SELECT secret_col AS n) SELECT n FROM c) FROM orders", "secret_col"),
    ("SELECT (SELECT n FROM (SELECT secret_col AS n) z) FROM orders", "secret_col"),
    # An alias list renames by position: `x` supplies `n`, not the `secret_col` it renamed.
    ("WITH c(secret_col) AS (SELECT 1) SELECT secret_col FROM orders, c AS x(n)", "secret_col"),
    # A physical table in parentheses: its alias names the table, not a column it supplies.
    ("SELECT secret_col FROM orders, (customers) AS secret_col", "secret_col"),
    # An output alias names the select's result, not its input: everywhere but ORDER BY / GROUP BY
    # / HAVING the engine reads the input column of that name.
    ("SELECT secret_col AS secret_col FROM orders", "secret_col"),
    ("SELECT max(secret_col) AS secret_col FROM orders", "secret_col"),
    ("SELECT 1 AS secret_col FROM orders WHERE secret_col = 'x'", "secret_col"),
    ("SELECT id, row_number() OVER (ORDER BY secret_col) AS secret_col FROM orders", "secret_col"),
    ("SELECT secret_col FROM (SELECT secret_col AS secret_col FROM orders) d", "secret_col"),
    ("SELECT max(id) AS secret_col FROM orders ORDER BY max(secret_col)", "secret_col"),
    # Inside an expression, HAVING or FILTER the engine reads the input column, not the alias.
    ("SELECT id AS secret_col FROM orders ORDER BY CASE WHEN secret_col = 'x' THEN 0 ELSE 1 END",
     "secret_col"),
    ("SELECT 1 AS secret_col FROM orders GROUP BY id HAVING secret_col = 'x'", "secret_col"),
    ("SELECT count(*) AS secret_col FROM orders GROUP BY id "
     "HAVING count(*) FILTER (WHERE secret_col = 'x') > 0", "secret_col"),
    # An alias inside a function argument is not one the select returns.
    ("SELECT xmlforest(id AS secret_col) AS x FROM orders ORDER BY secret_col", "secret_col"),
    # A USING / NATURAL join merges the name with the physical table's column, and compares it.
    ("SELECT secret_col FROM orders LEFT JOIN (SELECT 'x' AS secret_col) d USING (secret_col)",
     "secret_col"),
    ("SELECT secret_col FROM orders NATURAL LEFT JOIN (SELECT 'x' AS secret_col) d", "secret_col"),
    ("WITH c(secret_col) AS (SELECT 'x') SELECT secret_col FROM orders LEFT JOIN c "
     "USING (secret_col)", "secret_col"),
    ("SELECT o.id FROM orders o JOIN (SELECT 'x' AS secret_col) d USING (secret_col)", "secret_col"),
    ("SELECT id FROM orders NATURAL JOIN (SELECT 'x' AS secret_col) d", "secret_col"),
    # The same shapes must still refuse what they do not supply.
    ("WITH c AS (SELECT 1 AS n) SELECT secret_col FROM orders, (c)", "secret_col"),
    ("WITH c AS (SELECT 1 AS secret_col) SELECT secret_col FROM orders, (c) AS x(n)", "secret_col"),
    # Inside an expression neither name is the alias, so both are judged as input columns.
    ("SELECT amount AS doubled FROM orders ORDER BY (doubled + secret_col)", "doubled, secret_col"),
    # Alias names past a CTE's outputs rename nothing (DuckDB ignores them).
    ("WITH c(a, secret_col) AS (SELECT 1) SELECT secret_col FROM orders, c", "secret_col"),
    # VALUES and a table function name their columns their own way, not after the alias.
    ("SELECT secret_col FROM orders, (VALUES (1)) AS secret_col", "secret_col"),
]


@pytest.mark.parametrize("sql,offender", BESIDE_A_SOURCE)
def test_undeclared_column_beside_a_source_refused(sql, offender):
    _assert_columns_refused(rt.check_column_scope(sql, _scope_org()), offender)


def _postgres_org():
    org = _scope_org()
    org.storage_connections = [m.StorageConnection(name="c", storage_type="PostgreSQL")]
    return org


@pytest.mark.parametrize("sql", [
    "SELECT x FROM orders, (SELECT secret_col AS x) d",
    "SELECT x FROM orders o JOIN (SELECT upper(secret_col) AS x) d ON true",
])
def test_duckdb_reads_an_earlier_from_item_inside_a_derived_table(sql):
    """DuckDB makes a FROM / JOIN subquery lateral without the keyword."""
    org = _scope_org()
    org.storage_connections = [m.StorageConnection(name="c", storage_type="DuckDB")]
    _assert_columns_refused(rt.check_column_scope(sql, org), "secret_col")


def test_a_bare_group_by_alias_is_the_alias():
    """By decision, not oversight: `GROUP BY day` over `... AS day` is everyday SQL, and refusing it
    to cover an alias that collides with an undeclared input column would refuse that. The residual
    is the grouping alone (how many rows share a value), never a value."""
    assert rt.check_column_scope(
        "SELECT date(id) AS day, count(*) AS n FROM orders GROUP BY day ORDER BY day",
        _scope_org()) is None


def _duckdb_org():
    org = _scope_org()
    org.storage_connections = [m.StorageConnection(name="c", storage_type="DuckDB")]
    return org


def test_qualify_reads_a_window_alias():
    """The dedup idiom on DuckDB, Snowflake, BigQuery and Databricks."""
    assert rt.check_column_scope(
        "SELECT id, row_number() OVER (PARTITION BY customer_id ORDER BY amount DESC) AS rn "
        "FROM orders QUALIFY rn = 1", _duckdb_org()) is None


def test_qualify_does_not_trust_any_other_alias():
    _assert_columns_refused(rt.check_column_scope(
        "SELECT 1 AS secret_col FROM orders QUALIFY secret_col = 'x'", _duckdb_org()), "secret_col")
    _assert_columns_refused(rt.check_column_scope(
        "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM orders QUALIFY rn = 1",
        _scope_org()), "rn")


def test_an_alias_elsewhere_is_the_input_column_on_engines_that_also_read_aliases():
    """DuckDB reads a select-list alias in WHERE, and a window alias nested in another window in
    QUALIFY, only when no input column has that name; the gate cannot see the input columns, so it
    holds both to the model. Write the expression out, or filter in an outer query."""
    _assert_columns_refused(rt.check_column_scope(
        "SELECT amount * 2 AS dbl FROM orders WHERE dbl > 10", _duckdb_org()), "dbl")
    _assert_columns_refused(rt.check_column_scope(
        "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM orders QUALIFY max(rn) OVER () = 1",
        _duckdb_org()), "rn")


def test_mysql_column_names_fold_whatever_their_case():
    """MySQL's table names can be case-sensitive; its column names never are."""
    org = _scope_org()
    org.storage_connections = [m.StorageConnection(name="c", storage_type="MySQL")]
    assert rt.check_column_scope("WITH c AS (SELECT 1 AS N) SELECT n, amount FROM orders, c",
                                 org) is None
    _assert_columns_refused(rt.check_column_scope(
        "WITH c AS (SELECT 1 AS N) SELECT secret_col FROM orders, c", org), "secret_col")


@pytest.mark.parametrize("sql", [
    "WITH c(a) AS (VALUES ROW(1)) SELECT a, amount FROM orders, c",
    "WITH c(a) AS (SELECT * FROM (VALUES ROW(1)) v) SELECT a, amount FROM orders, c",
])
def test_a_values_cte_is_named_by_its_column_list_on_mysql(sql):
    org = _scope_org()
    org.storage_connections = [m.StorageConnection(name="c", storage_type="MySQL")]
    assert rt.check_column_scope(sql, org) is None


def test_a_bare_values_body_holds_one_position_per_column():
    """Some dialects parse a CTE's `VALUES` body as `exp.Values` itself: its row's arity is the
    positions a column list can name, and a name past them still names nothing."""
    values = rt.exp.Values(expressions=[rt.exp.Tuple(expressions=[rt.exp.Literal.number(1),
                                                                  rt.exp.Literal.number(2)])])
    assert rt._query_column_names(values, None) == [None, None]


def test_an_empty_quoted_name_names_nothing():
    """`""` is a zero-length identifier: it is no column, and as a source's output it vouches for
    none."""
    assert rt.check_column_scope('SELECT "" FROM orders', _scope_org()) is None
    _assert_columns_refused(rt.check_column_scope(
        'WITH c AS (SELECT 1 AS "") SELECT secret_col FROM orders, c', _scope_org()), "secret_col")


def test_postgres_names_a_one_column_function_after_its_alias():
    org = _postgres_org()
    assert rt.check_column_scope(
        "SELECT g, amount FROM generate_series(1, 3) AS g, orders", org) is None


def test_a_quoted_source_output_is_not_the_unquoted_column_on_postgres():
    """`"SECRET_COL"` keeps its case on Postgres, so the bare `secret_col` is `orders`' column."""
    org = _postgres_org()
    _assert_columns_refused(rt.check_column_scope(
        'WITH c AS (SELECT 1 AS "SECRET_COL") SELECT secret_col FROM orders, c', org),
        "secret_col")
    assert rt.check_column_scope(
        'WITH c AS (SELECT 1 AS "SECRET_COL") SELECT "SECRET_COL", amount FROM orders, c',
        org) is None


def test_refusal_beside_a_source_says_how_to_name_it():
    res = rt.check_column_scope(
        "WITH c AS (SELECT 1 AS n) SELECT secret_col FROM orders, c", _scope_org())
    assert "qualify it with that source's alias" in res.remediation


# The other direction: a column one source names is bound to it, so none of these is refused.
NAMED_BY_A_SOURCE = [
    "WITH c AS (SELECT 1 AS n) SELECT n, amount FROM orders, c",
    "WITH c(k) AS (SELECT id FROM customers) SELECT k, amount FROM orders "
    "JOIN c ON c.k = orders.customer_id",
    "SELECT k, amount FROM orders JOIN (SELECT id AS k FROM customers) s "
    "ON s.k = orders.customer_id",
    "SELECT region, amount FROM orders JOIN (SELECT id, region FROM customers) s "
    "ON s.id = orders.customer_id",
    "WITH c AS (SELECT id FROM customers UNION SELECT id FROM orders) SELECT c.id, amount "
    "FROM orders, c",
    "SELECT a, amount FROM orders JOIN LATERAL (SELECT 1 AS a) l ON true",
    "SELECT a, amount FROM (VALUES (1)) AS v(a), orders",
    "WITH c(a) AS (VALUES (1)) SELECT a, amount FROM orders, c",
    "SELECT u, amount FROM orders o CROSS JOIN UNNEST(ARRAY[1]) AS t(u)",
    # A partial alias list renames only its positions; the rest keep their names.
    "WITH c(k) AS (SELECT id, region FROM customers) SELECT region, amount FROM orders, c",
    "SELECT region, amount FROM orders, (SELECT id, region FROM customers) AS s(k)",
    "WITH c(secret_col) AS (SELECT 1) SELECT n, amount FROM orders, c AS x(n)",
    # Output aliases where the engine reads them, and USING / NATURAL on declared columns.
    "SELECT amount AS a FROM orders ORDER BY a",
    "SELECT status AS s, count(*) AS n FROM orders GROUP BY s HAVING count(*) > 1 ORDER BY n",
    # A USING key is held only to that join's two sides; NATURAL only to its own operands.
    "SELECT name FROM customers c JOIN orders o ON o.customer_id = c.id "
    "JOIN (SELECT 1 AS amount) d USING (amount)",
    "SELECT n FROM orders NATURAL JOIN customers, (SELECT 1 AS n) d",
    "SELECT id FROM orders JOIN customers USING (id)",
    "SELECT id FROM orders NATURAL JOIN customers",
    "SELECT n FROM orders JOIN (SELECT 1 AS id, 2 AS n) d USING (id)",
    # Between CTEs / subqueries alone, a USING-merged name is theirs (no table in that scope).
    "SELECT (SELECT n FROM (SELECT 1 AS n) a JOIN (SELECT 1 AS n) b USING (n)) FROM orders",
    # An alias in parentheses is still the whole ORDER BY item.
    "SELECT amount * 2 AS doubled FROM orders ORDER BY (doubled)",
    # A CTE in parentheses is that CTE, renamed by each wrapper's alias list in turn.
    "WITH c AS (SELECT 1 AS n) SELECT n, amount FROM orders, (c)",
    "WITH c AS (SELECT 1 AS n) SELECT n, amount FROM orders, (c) AS x",
    "WITH c AS (SELECT 1 AS n, 2 AS m) SELECT k, m, amount FROM orders, (c) AS x(k)",
    # A union's ORDER BY inside a derived table names the union's output, not the outer query's.
    "SELECT x.id FROM (SELECT id FROM orders UNION SELECT id FROM customers ORDER BY id) x",
]


@pytest.mark.parametrize("sql", NAMED_BY_A_SOURCE)
def test_column_named_by_a_source_allowed(sql):
    assert rt.check_column_scope(sql, _scope_org()) is None


# ===========================================================================
# Documented accepted fail-opens -> allow (the intended boundary)
# ===========================================================================

def test_fail_open_derived_alias_qualified_column():
    # `x.whatever` is qualified by a derived-table alias, not a physical table —
    # validated at the subquery's own body; the outer reference is not re-checked.
    res = rt.check_column_scope(
        "SELECT x.whatever FROM (SELECT id AS whatever FROM orders) x", _scope_org())
    assert res is None


def test_fail_open_cte_shadowing_table_name():
    # A CTE named after a real table shadows it; its body defines its own columns,
    # so the outer reference traces to no physical table (DB is the backstop).
    res = rt.check_column_scope(
        "WITH orders AS (SELECT 1 AS bogus) SELECT bogus FROM orders", _scope_org())
    assert res is None


# ===========================================================================
# The semi-structured residual -> the gate's unit of exposure is the COLUMN
#
# `SELECT payload:ssn` reaches `payload`, which the model declares. Nothing undeclared
# was reached, so this gate allows it and is right to: principle 4b refuses a reach to a
# table or column the model does not expose, and a field inside a declared column is
# neither. Making the unit finer than a column would amend 4b rather than fix a gate.
#
# The residual is bounded by the modelling rule REQ-021 states — a column carrying values
# that must not be readable is not declared — and the second test is that bound holding:
# undeclare the root and the reach is refused like any other. Both directions are pinned
# so a future narrowing is a conscious, test-breaking decision rather than a silent one.
# Affects every engine with path access: Snowflake VARIANT, BigQuery JSON, Postgres jsonb.
#
# **The bound holds for the spellings measured here and NOT for the nested one.** A
# `payload:cust.ssn` does not parse to a tree the statement said, so no gate judges it at
# all — see tests/test_parse_fidelity_gaps.py, where that is a strict xfail owned by the
# parse-fidelity work. Do not widen these lists to the nested form to make them pass.
# ===========================================================================

SEMI_STRUCTURED_INTO_DECLARED = [
    "SELECT payload:ssn FROM orders",                 # Snowflake colon path
    "SELECT payload['ssn'] FROM orders",              # bracket subscript
    "SELECT id FROM orders WHERE payload:ssn = 'x'",  # in a predicate, not a projection
]
# NOT here: the NESTED spelling, `payload:cust.ssn`. It is allowed too, but for an unrelated
# reason — the generic parse of `x:a.b` drops the FROM clause, so the gate judges a statement
# with no tables in it and fails open. Putting it here would make this section's claim
# ("allowed because the root is declared") true of a case where the root is irrelevant, and
# would make the bound below look like it held when it does not. It lives in
# tests/test_parse_fidelity_gaps.py as a strict xfail instead.


@pytest.mark.parametrize("sql", SEMI_STRUCTURED_INTO_DECLARED)
def test_path_into_a_declared_column_is_allowed(sql):
    """The residual, stated as a test. This is not the gate failing — it is the gate's unit."""
    assert rt.check_column_scope(sql, _scope_org()) is None


SEMI_STRUCTURED_INTO_UNDECLARED = [
    "SELECT nope:ssn FROM orders",
    "SELECT nope['ssn'] FROM orders",
]


@pytest.mark.parametrize("sql", SEMI_STRUCTURED_INTO_UNDECLARED)
def test_path_into_an_undeclared_column_is_refused(sql):
    """The bound. Not declaring the root column is what closes the reach, and it closes it
    through the ordinary column-scope path with no semi-structured machinery at all."""
    _assert_columns_refused(rt.check_column_scope(sql, _scope_org()), "nope")


# ===========================================================================
# Upstream-owned -> a different layer catches it
# ===========================================================================

def test_multistatement_stacking_caught_by_read_only_guard():
    # Column smuggled into a stacked second statement is rejected before
    # _model_safety runs, by the read-only guard.
    import sql_guard
    assert sql_guard.check_read_only(
        "SELECT id FROM orders; SELECT * FROM secret") is not None
