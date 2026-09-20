"""A star is refused when it cannot be RESOLVED, not merely because it is a star (#387, #339).

The gate used to refuse every `*` on the stated grounds that "the column list behind `*` lives in
the catalog". That is true of a star over a table and false of a star over a CTE or derived table,
whose columns are written out in the query itself — so a recursive-hierarchy query carrying an
already-spelled-out projection forward with `SELECT p.*` was refused for being unknowable when it
was knowable, and the rewrite demanded was longer than what was refused.

**This file is the matrix, and the half that must REFUSE is the half that matters.** A gate that
became more permissive is only safe if the permissiveness is exactly the resolvable cases, so every
class below is asserted in both directions: the named projection passes, the same shape over a table
does not.

The tables here (`orders`, `customers`) are the shared fixture's declared ones; `secret_table` is
deliberately undeclared, so a statement reaching it would be refused by table scope as well — the
star assertions are made against `check_no_select_star` alone, so they say what that gate did rather
than what some other gate caught first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

import guardrail  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _star(sql: str):
    return rt.check_no_select_star(sql)


def _refused(sql: str) -> bool:
    refusal = _star(sql)
    return refusal is not None and refusal.rule == guardrail.RULE_SELECT_STAR


# ---------------------------------------------------------------------------------------------
# Resolvable — the columns are written down in the statement, so the star may be read
# ---------------------------------------------------------------------------------------------

RESOLVABLE = [
    pytest.param(
        "WITH p AS (SELECT a, b FROM orders) SELECT p.* FROM p",
        id="qualified star over a CTE that names its columns",
    ),
    pytest.param(
        "WITH p AS (SELECT a, b FROM orders) SELECT * FROM p",
        id="bare star over a CTE that names its columns",
    ),
    pytest.param(
        "SELECT * FROM (SELECT id FROM orders) x",
        id="star over a derived table that names its column",
    ),
    pytest.param(
        "SELECT x.* FROM (SELECT id FROM orders) x",
        id="qualified star over a derived table",
    ),
    pytest.param(
        "WITH a AS (SELECT id FROM orders), b AS (SELECT * FROM a) SELECT * FROM b",
        id="a chain of CTEs, each naming or forwarding a named projection",
    ),
    pytest.param(
        "WITH p AS (SELECT id FROM orders UNION ALL SELECT id FROM customers) SELECT * FROM p",
        id="both set-operation arms name their columns",
    ),
    pytest.param(
        "WITH p AS (SELECT id, total FROM orders) "
        "SELECT p.*, ROW_NUMBER() OVER (ORDER BY total) AS rn FROM p",
        id="the shape that provoked this — a projection carried forward beside a window function",
    ),
    pytest.param(
        "WITH a AS (SELECT id FROM orders), b AS (SELECT id FROM customers) "
        "SELECT * FROM a JOIN b ON a.id = b.id",
        id="bare star over two joined CTEs, both named",
    ),
]


@pytest.mark.parametrize("sql", RESOLVABLE)
def test_a_star_the_statement_spells_out_is_allowed(sql: str) -> None:
    assert _star(sql) is None, (
        "every column this star stands for is written in the statement, so refusing it asserts "
        "we could not determine something determinable by reading the query"
    )


# ---------------------------------------------------------------------------------------------
# Unresolvable — refuse, and this is the half that keeps the gate a gate
# ---------------------------------------------------------------------------------------------

UNRESOLVABLE = [
    pytest.param("SELECT * FROM orders", id="the plain case: a star over a table"),
    pytest.param("SELECT o.* FROM orders o", id="qualified star over a table"),
    pytest.param(
        "SELECT id FROM (SELECT * FROM orders) x",
        id="a star over a table, hidden one level down in a derived table",
    ),
    pytest.param(
        "WITH t AS (SELECT * FROM orders) SELECT id FROM t",
        id="#339: a star over a table inside a CTE body — used to pass, now refuses",
    ),
    pytest.param(
        "WITH t AS (SELECT * FROM orders) SELECT * FROM t",
        id="#339: and the outer star over it, which cannot be resolved either",
    ),
    pytest.param(
        "SELECT id FROM orders UNION SELECT * FROM customers",
        id="a star in one set-operation arm",
    ),
    pytest.param(
        "WITH p AS (SELECT id FROM orders UNION ALL SELECT * FROM customers) SELECT * FROM p",
        id="one arm of a CTE's union stars over a table, so the CTE is not fully named",
    ),
    pytest.param(
        "SELECT (SELECT * FROM customers LIMIT 1) FROM orders",
        id="a star inside a scalar subquery",
    ),
    pytest.param(
        "WITH p AS (SELECT a FROM orders) SELECT q.* FROM p",
        id="a qualifier naming no source is unresolved, never ignored",
    ),
    pytest.param(
        "SELECT * FROM (SELECT id FROM orders) x JOIN orders o ON o.id = x.id",
        id="a bare star expands over EVERY source, and one of these is a table",
    ),
    pytest.param("SELECT/**/ * FROM orders", id="comment obfuscation"),
    pytest.param("select * from orders", id="lowercase"),
]


@pytest.mark.parametrize("sql", UNRESOLVABLE)
def test_a_star_whose_columns_are_not_written_down_is_refused(sql: str) -> None:
    assert _refused(sql), (
        "the columns behind this star live in the catalog, which the guard never reads — "
        "allowing it would let an undeclared column through unchecked"
    )


# ---------------------------------------------------------------------------------------------
# The walk itself: every uncertainty must resolve to a refusal
# ---------------------------------------------------------------------------------------------


def test_a_shadowed_cte_name_refuses_rather_than_guessing() -> None:
    """Two CTEs bound to one name. Deciding which the star reads means re-deriving the lexical
    visibility rules `_cte_references` owns, and a second copy of that reasoning is how one of them
    goes wrong quietly. Ambiguity refuses — both simpler and the safe direction."""
    sql = (
        "WITH p AS (SELECT a FROM orders) "
        "SELECT * FROM (WITH p AS (SELECT b FROM customers) SELECT * FROM p) z"
    )
    assert _refused(sql)


def test_a_self_referencing_body_terminates_and_refuses() -> None:
    """A `WITH RECURSIVE` body naming itself is a cycle in the walk. The guard must not hang, and
    a cycle it cannot resolve is a refusal like any other."""
    sql = (
        "WITH RECURSIVE t AS ("
        "  SELECT id FROM orders UNION ALL SELECT * FROM t"
        ") SELECT * FROM t"
    )
    assert _refused(sql)


def test_an_aggregate_star_is_not_a_projection_star() -> None:
    """`COUNT(*)` names no columns of its own — the star sits inside the call, and the projection
    is the aggregate. Over-blocking these would refuse the commonest query there is."""
    for sql in (
        "SELECT COUNT(*) FROM orders",
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
        "WITH p AS (SELECT id FROM orders) SELECT COUNT(*) FROM p",
    ):
        assert _star(sql) is None, sql


def test_a_star_over_nothing_refuses() -> None:
    """A select with no FROM has no source to resolve against. Rare, and it must not read as
    'no sources, nothing unresolvable, allow' — which is what an `all()` over an empty list says."""
    assert _refused("SELECT *")


def test_the_gate_still_degrades_to_allow_on_unparseable_input() -> None:
    """Unchanged, and stated here because the resolution walk is new code on that path: a
    statement that does not parse is the read-only guard's refusal, not this one's."""
    assert _star("SELECT * FROM (((") is None
