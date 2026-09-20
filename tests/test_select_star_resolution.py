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


# ---------------------------------------------------------------------------------------------
# What the review found: the argument for reading a star has to actually hold
# ---------------------------------------------------------------------------------------------


def test_a_star_over_a_source_whose_columns_were_never_checked_is_refused() -> None:
    """**The regression this change introduced, and the reason the rule is not simply "named".**

    Reading a star is justified by the claim that its columns were already judged where they were
    written. `check_column_scope` fails open on an unqualified column in a select that mixes a
    physical table with a CTE or derived table — #339's open half — so in that one shape the
    columns are written and NOT judged, and the claim is false.

    Left alone, this change would have turned a statement the blanket ban refused into one that
    forwards an undeclared column through both gates. Confirmed at the time: before the fix, this
    SQL was allowed by the star gate and by column scope together.
    """
    sql = "SELECT * FROM (WITH c AS (SELECT 1 AS n) SELECT secret_col FROM orders, c) x"
    assert _refused(sql)


def test_a_skipped_column_forwarded_up_through_named_projections_is_refused() -> None:
    """**The same hole one level deeper, and why the restriction is whole-statement.**

    The first fix scoped the mixed-source check to the relation a star named. The second scoped it
    to that relation's own select. Both missed this: the innermost select mixes a table with a CTE,
    so `check_column_scope` skips `secret_col`; the select above it names that column explicitly,
    with no star of its own to catch anything; the outer star then forwards it. Each fix was right
    about the shape in front of it and wrong about the next one, so the check now asks the question
    of the whole statement, which has no next one.
    """
    sql = (
        "WITH c AS (SELECT 1 AS n) "
        "SELECT * FROM (SELECT x.secret_col FROM (SELECT secret_col FROM orders, c) x) p"
    )
    assert _refused(sql)


def test_the_real_hierarchy_query_still_resolves() -> None:
    """The statement this whole change exists for, in the shape it actually takes: levels built in
    CTEs over physical tables, then a projection carried forward beside a window function. No
    select in it mixes a table with a derived source, so the whole-statement restriction above
    costs it nothing — which is the margin that makes that restriction affordable."""
    sql = (
        "WITH tree AS ("
        "  SELECT d.id, d.name, d.parent_id FROM departments d "
        "  LEFT JOIN departments d2 ON d2.id = d.parent_id"
        "), pathed AS (SELECT id, name FROM tree), "
        "ranked AS (SELECT p.*, ROW_NUMBER() OVER (ORDER BY name) AS rn FROM pathed p) "
        "SELECT rn, name FROM ranked"
    )
    assert _star(sql) is None


def test_the_mixed_source_restriction_is_about_the_shape_not_the_column() -> None:
    """It refuses on the SHAPE, without needing to know which columns are declared — this gate
    judges no model. A star over a CTE that joins a table to another CTE is refused too, which is
    conservative and deliberate: the cost is a refusal the caller repairs by naming columns."""
    sql = (
        "WITH a AS (SELECT id FROM customers), "
        "     b AS (SELECT a.id FROM orders JOIN a ON a.id = orders.id) "
        "SELECT * FROM b"
    )
    assert _refused(sql)


def test_a_chain_far_past_the_budget_refuses_instead_of_raising() -> None:
    """The read-only guard accepts 50,000 characters, which is room for a CTE chain long enough to
    exhaust Python's stack. The contract here is a refusal, not a `RecursionError`, so the walk
    carries a budget and an overflow answers "not named" like any other thing it cannot establish.
    """
    chain = "WITH a0 AS (SELECT id FROM orders)" + "".join(
        f", a{i} AS (SELECT * FROM a{i - 1})" for i in range(1, 400)
    ) + " SELECT * FROM a399"

    assert _refused(chain), "a statement past the budget must refuse, not raise"


def test_a_chain_of_a_realistic_depth_still_resolves() -> None:
    """The budget exists for pathological input and must not refuse ordinary queries. The deepest
    real chain seen is five links; thirty still resolves, so there is a wide margin between what
    people write and where this gives up."""
    chain = "WITH a0 AS (SELECT id FROM orders)" + "".join(
        f", a{i} AS (SELECT * FROM a{i - 1})" for i in range(1, 30)
    ) + " SELECT * FROM a29"

    assert _star(chain) is None
