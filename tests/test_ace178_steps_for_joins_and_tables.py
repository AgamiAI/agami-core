"""ACE-178 item 7 — the joins, aggregates and tables sections read a WITH step by one rule.

Part 1 taught the `columns` section to follow a number through the steps of a query, binding each
WITH name by the scope gate's own lexical rule. The other sections each answered "what does this
name read" their own way — the aggregates section by a name-keyed walk, the joins and tables sections
by a statement-wide set of WITH names and a bare-name index — so one receipt could describe one step
two ways (#204), let a step named like a table borrow that table's edges (#200) or row (#462), and
let a join in a step nobody reads downgrade every number (#429). They now share one rule: a WITH
reference stands for the declared table it hands back row for row, with every column passed on
under its own name, and for nothing otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

from receipt_trick_queries import STEP_JOIN_TRICKS  # noqa: E402
from test_ace178_metrics_traced_through_steps import _related  # noqa: E402
from test_semantic_model_runtime import _sales_org  # noqa: E402
from timing_bound import assert_assembly_within  # noqa: E402

FAN_EDGE = "orders (1) <- order_items (N)"


def _join(receipt: dict, from_to: str) -> dict:
    hits = [it for it in receipt["joins"]["items"] if it["from_to"] == from_to]
    assert len(hits) == 1, [it["from_to"] for it in receipt["joins"]["items"]]
    return hits[0]


def _agg(receipt: dict, label: str) -> dict:
    hits = [it for it in receipt["aggregates"]["items"] if it["aggregate"] == label]
    assert len(hits) == 1, [it["aggregate"] for it in receipt["aggregates"]["items"]]
    return hits[0]


# --- #204: one edge, described one way --------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "WITH o AS (SELECT * FROM orders) "
        "SELECT SUM(o.total_amount) FROM o JOIN order_items ON order_items.order_id = o.id",
        "WITH o AS (SELECT id, total_amount FROM orders WHERE status = 'paid') "
        "SELECT SUM(o.total_amount) FROM o JOIN order_items ON order_items.order_id = o.id",
        "WITH a AS (SELECT * FROM orders), o AS (SELECT id, total_amount FROM a) "
        "SELECT SUM(o.total_amount) FROM o JOIN order_items ON order_items.order_id = o.id",
    ],
)
def test_a_join_to_a_step_is_the_join_to_the_table_it_reads(sql):
    """#204. The step hands back `orders` row for row, so the join is the declared
    `order_items → orders` edge, and both sections say so. What was written stays in `from_to`."""
    r = rt.assemble_receipt(_sales_org(), sql)
    join = _join(r, "o → order_items")
    assert join["status"] == rt.DECLARED
    assert join["resolves_to"] == "orders → order_items"
    assert join["cardinality"] == "many_to_one"
    agg = _agg(r, "SUM(o.total_amount)")
    assert agg["status"] == rt.MULTIPLIED and agg["joins"] == [FAN_EDGE]


def test_a_join_between_two_tables_resolves_to_nothing_new():
    r = rt.assemble_receipt(
        _sales_org(),
        "SELECT SUM(o.total_amount) FROM orders o JOIN customers c ON c.id = o.customer_id",
    )
    assert _join(r, "orders → customers")["resolves_to"] is None


def test_a_parenthesized_table_is_the_table_at_either_end_of_a_join():
    r = rt.assemble_receipt(
        _sales_org(),
        "SELECT COUNT(oi.id) FROM (orders) JOIN order_items oi ON oi.order_id = orders.id",
    )
    join = _join(r, "orders → order_items")
    assert (join["status"], join["resolves_to"]) == (rt.DECLARED, None)


# Each row: a step joined to a table, and whether the step stands for the table it reads. The joins
# section and the aggregates section must agree on every row.
STEP_ENDPOINTS = [
    ("every column of one table", "WITH s AS (SELECT * FROM orders)", True),
    ("named columns, unchanged", "WITH s AS (SELECT id, total_amount FROM orders)", True),
    ("a filtered step", "WITH s AS (SELECT id, total_amount FROM orders WHERE status = 'x')", True),
    (
        "a step on a step",
        "WITH a AS (SELECT * FROM orders), s AS (SELECT id, total_amount FROM a)",
        True,
    ),
    (
        "a step reading its own name, which is the table",
        "WITH orders AS (SELECT id, total_amount FROM orders), s AS (SELECT * FROM orders)",
        True,
    ),
    ("a renamed key", "WITH s AS (SELECT customer_id AS id, total_amount FROM orders)", False),
    (
        "a column renamed to its own name's neighbour",
        "WITH s AS (SELECT id, revenue AS total_amount FROM orders)",
        False,
    ),
    (
        "a computed column",
        "WITH s AS (SELECT id, total_amount * 2 AS total_amount FROM orders)",
        False,
    ),
    (
        "an alias column list",
        "WITH s(id, total_amount) AS (SELECT id, total_amount FROM orders)",
        False,
    ),
    ("DISTINCT", "WITH s AS (SELECT DISTINCT id, total_amount FROM orders)", False),
    (
        "grouped",
        "WITH s AS (SELECT id, SUM(total_amount) AS total_amount FROM orders GROUP BY id)",
        False,
    ),
    (
        "a join in the body",
        "WITH s AS (SELECT o.id, o.total_amount FROM orders o JOIN customers c ON c.id = o.customer_id)",
        False,
    ),
    ("a LIMIT", "WITH s AS (SELECT id, total_amount FROM orders LIMIT 5)", False),
    ("an undeclared table", "WITH s AS (SELECT id, total_amount FROM shipments)", False),
]


@pytest.mark.parametrize("label,with_,resolves", STEP_ENDPOINTS, ids=[r[0] for r in STEP_ENDPOINTS])
def test_the_joins_and_aggregates_sections_resolve_a_step_one_way(label, with_, resolves):
    sql = (
        f"{with_} SELECT SUM(s.total_amount) FROM s JOIN order_items ON order_items.order_id = s.id"
    )
    r = rt.assemble_receipt(_sales_org(), sql)
    join = _join(r, "s → order_items")
    agg = _agg(r, "SUM(s.total_amount)")
    if resolves:
        assert (join["status"], join["resolves_to"]) == (rt.DECLARED, "orders → order_items")
        assert (agg["status"], agg["joins"]) == (rt.MULTIPLIED, [FAN_EDGE])
    else:
        # Not the table: the aggregates section names no edge of `orders` (a grouped step is a
        # source of its own, with a derived edge; anything else is undetermined).
        assert (join["status"], join["resolves_to"]) == (rt.UNDECLARABLE, None)
        assert FAN_EDGE not in agg["joins"] and agg["status"] != rt.NOT_MULTIPLIED, agg


@pytest.mark.parametrize(
    "sql",
    [
        # The step renames customer_id to id: the join is on a customer, and each order item meets
        # every order of that customer.
        "WITH o AS (SELECT customer_id AS id, total_amount FROM orders) "
        "SELECT SUM(oi.quantity) FROM order_items oi JOIN o ON o.id = oi.order_id",
        "WITH oi AS (SELECT product_id AS order_id, quantity FROM order_items) "
        "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id",
    ],
)
def test_a_step_that_renames_its_key_is_never_called_clean(sql):
    """Found building item 7: the aggregates section rebound a step to its table by name and read a
    renamed column as the table's key, so a fanned number read `not_multiplied`."""
    for item in rt.pre_flight_check(sql, _sales_org()).aggregates:
        assert item.status != rt.NOT_MULTIPLIED, item
    r = rt.assemble_receipt(_sales_org(), sql)
    assert all(it["status"] != rt.DECLARED for it in r["joins"]["items"]), r["joins"]


@pytest.mark.parametrize("sql", STEP_JOIN_TRICKS)
def test_no_join_through_a_step_passes_for_a_declared_one_it_is_not(tmp_path, sql):
    """The columns section reads the joins section's statuses, so a step the joins section wrongly
    resolved would turn a number counted over the wrong rows into a plain match."""
    section = rt.assemble_receipt(_related(tmp_path), sql)["columns"]
    (item,) = [i for i in section["items"] if i["kind"] == "output" and i["column"] == "n"]
    if item["status"] == rt.MATCHED:
        assert item["restrictions"] or item["joins_not_declared"], sql


def test_a_schema_qualified_read_beside_a_step_of_its_name_is_the_table():
    """The step `orders` and the table `public.orders` are two relations; only the second is the
    table, in the joins section as in the scope gate."""
    sql = (
        "WITH orders AS (SELECT id FROM public.orders WHERE status = 'x') "
        "SELECT SUM(oi.quantity) FROM public.orders po JOIN order_items oi ON oi.order_id = po.id"
    )
    r = rt.assemble_receipt(_sales_org(), sql)
    assert _join(r, "orders → order_items")["status"] == rt.DECLARED


# --- #200: a grouped step named like a table is not that table ---------------


def test_a_grouped_step_named_like_a_table_is_not_that_table():
    sql = (
        "WITH order_items AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
        "SELECT SUM(orders.total_amount) FROM orders "
        "JOIN order_items ON order_items.order_id = orders.id"
    )
    for surface in (
        rt.assemble_receipt(_sales_org(), sql)["aggregates"]["items"],
        [a.as_dict() for a in rt.pre_flight_check(sql, _sales_org()).aggregates],
    ):
        assert [(a["status"], a["joins"]) for a in surface] == [(rt.NOT_MULTIPLIED, [])], surface


def test_a_grouped_step_named_like_a_table_still_fans_below_its_join_key():
    sql = (
        "WITH order_items AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
        "GROUP BY order_id, product_id) SELECT SUM(orders.total_amount) FROM orders "
        "JOIN order_items ON order_items.order_id = orders.id"
    )
    [item] = rt.pre_flight_check(sql, _sales_org()).aggregates
    assert item.status == rt.MULTIPLIED
    assert item.joins == ["orders (1) <- order_items_step (N)"]


# --- #429: a join in a step reaches only the numbers that read it -------------

UNDECLARED_STEP = (
    "WITH x AS (SELECT o.id FROM orders o JOIN tickets t ON t.customer_id = o.customer_id) "
)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT SUM(total_amount) FROM orders", [("SUM(total_amount)", rt.NOT_MULTIPLIED)]),
        (
            "SELECT SUM(total_amount) FROM orders UNION ALL SELECT SUM(o.total_amount) FROM orders o "
            "JOIN x ON x.id = o.id",
            [("SUM(total_amount)", rt.NOT_MULTIPLIED), ("SUM(o.total_amount)", rt.UNDETERMINED)],
        ),
        # Read only by an IN-subquery, which cannot repeat a row: still downgraded, the safe side.
        (
            "SELECT SUM(total_amount) FROM orders WHERE id IN (SELECT id FROM x)",
            [("SUM(total_amount)", rt.UNDETERMINED)],
        ),
        # Reached through another step.
        (
            "SELECT SUM(total_amount) FROM orders WHERE id IN (SELECT id FROM y)",
            [("SUM(total_amount)", rt.UNDETERMINED)],
        ),
    ],
)
def test_a_join_in_a_step_only_reaches_the_numbers_that_read_it(sql, expected):
    full = UNDECLARED_STEP.rstrip() + ", y AS (SELECT id FROM x) " + sql
    for surface in (
        rt.assemble_receipt(_sales_org(), full)["aggregates"]["items"],
        [a.as_dict() for a in rt.pre_flight_check(full, _sales_org()).aggregates],
    ):
        assert [(a["aggregate"], a["status"]) for a in surface] == expected, surface


def test_a_keyed_join_is_not_held_back_by_a_step_nobody_reads():
    """The other path of the same shape: whether an undeclared join keyed on both sides is one to
    one asks about the other joins that can feed the same numbers, and an unread step feeds none."""
    sql = UNDECLARED_STEP + (
        "SELECT SUM(o.total_amount) FROM orders o JOIN customers c ON c.id = o.id"
    )
    r = rt.assemble_receipt(_sales_org(), sql)
    assert _agg(r, "SUM(o.total_amount)")["status"] == rt.NOT_MULTIPLIED


# --- #462: a table item resolves where it is written ------------------------


def _two_schema_org() -> m.Datasource:
    def orders(schema: str, rows: int, filters: list[str]) -> m.Table:
        return m.Table(
            name="orders",
            schema=schema,
            storage_connection="c",
            grain=["id"],
            description=f"{schema} orders",
            default_filters=filters,
            performance_hints=m.PerformanceHints(estimated_row_count=rows),
            columns=[m.Column(name="id", type="integer"), m.Column(name="status", type="string")],
        )

    return m.Datasource(
        datasource="acme",
        subject_areas=[
            m.SubjectArea(
                name="s",
                tables_defined=[
                    orders("sales", 1000, ["{alias}.status <> 'void'"]),
                    orders("staging", 7, []),
                ],
            )
        ],
    )


def _tables(sql: str, org: m.Datasource) -> list[tuple]:
    r = rt.assemble_receipt(org, sql)
    return [
        (
            it["ref"],
            it["scope"],
            it["declared"],
            it["qname"],
            it["rows"],
            [f["status"] for f in it["filters"]],
        )
        for it in r["tables"]["items"]
    ]


def test_each_schema_qualified_read_gets_its_own_tables_row():
    assert sorted(_tables("SELECT COUNT(*) FROM staging.orders", _two_schema_org())) == [
        ("staging.orders", "main", True, "staging.orders", 7, [])
    ]
    assert sorted(
        _tables("SELECT COUNT(*) FROM sales.orders o WHERE o.status <> 'void'", _two_schema_org())
    ) == [("sales.orders", "main", True, "sales.orders", 1000, ["applied"])]


def test_a_table_read_beside_a_step_of_its_name_is_the_table():
    sql = (
        "WITH orders AS (SELECT id FROM sales.orders o WHERE o.status <> 'void') "
        "SELECT COUNT(*) FROM orders JOIN sales.orders so ON so.id = orders.id"
    )
    rows = _tables(sql, _two_schema_org())
    assert ("sales.orders", "main", True, "sales.orders", 1000, ["omitted"]) in rows
    assert ("orders", "main", False, None, None, []) in rows
    r = rt.assemble_receipt(_two_schema_org(), sql)
    assert r["tables"]["undetermined"] is None


def test_an_ambiguous_table_name_borrows_no_row():
    """`orders` is declared in two schemas; which one the engine reads depends on a search path the
    receipt cannot see, so neither row is borrowed, and the marker counts the reference."""
    r = rt.assemble_receipt(_two_schema_org(), "SELECT COUNT(*) FROM orders")
    [item] = r["tables"]["items"]
    assert (item["declared"], item["qname"], item["rows"], item["filters"]) == (
        False,
        None,
        None,
        [],
    )
    assert "1 of the listed reference(s) could not be resolved" in r["tables"]["undetermined"]


# --- review of part 2: names the receipt cannot pin to one relation -------------------------------

QUOTED_STEP = (
    "WITH customers AS (SELECT 1 AS id, status AS region FROM orders) SELECT SUM(o.total_amount) "
    'AS n FROM orders o JOIN "customers" c ON c.id = o.customer_id'
)


def test_a_quoted_name_folded_onto_a_step_is_neither_the_table_nor_the_step(tmp_path):
    """`"customers"` beside `WITH customers`: the scope gate reads the table, Postgres and DuckDB fold
    the name onto the step. What it reads is not established, so no section claims the table."""
    r = rt.assemble_receipt(_related(tmp_path), QUOTED_STEP)
    join = _join(r, "orders → customers")
    assert (join["status"], join["resolves_to"]) == (rt.UNDECLARABLE, None)
    (agg,) = r["aggregates"]["items"]
    assert agg["status"] != rt.NOT_MULTIPLIED, agg
    (item,) = [i for i in r["columns"]["items"] if i.get("column") == "n"]
    assert item["status"] != rt.MATCHED or item["joins_not_declared"], item
    quoted = [t for t in r["tables"]["items"] if t["scope"] == "main" and t["ref"] != "orders"]
    assert [(t["declared"], t["qname"]) for t in quoted] == [(False, None)], quoted
    assert "could not be resolved" in (r["tables"]["undetermined"] or "")


def test_a_recursive_forward_reference_borrows_no_row(tmp_path):
    sql = (
        "WITH RECURSIVE a AS (SELECT id, total_amount FROM orders), orders AS (SELECT id, 1000 AS "
        "total_amount FROM public.orders) SELECT SUM(total_amount) AS n FROM a"
    )
    r = rt.assemble_receipt(_related(tmp_path), sql)
    inner = [t for t in r["tables"]["items"] if t["scope"] == "cte:a"]
    assert [(t["declared"], t["qname"]) for t in inner] == [(False, None)], inner
    assert "could not be resolved" in (r["tables"]["undetermined"] or "")


def test_a_table_whose_alias_renames_its_columns_is_not_read_as_the_declared_table():
    sql = (
        "SELECT SUM(oi.quantity) FROM order_items oi JOIN orders AS t(customer_id, id) "
        "ON t.id = oi.order_id"
    )
    r = rt.assemble_receipt(_sales_org(), sql)
    assert all(it["status"] != rt.DECLARED for it in r["joins"]["items"]), r["joins"]
    assert all(a["status"] != rt.NOT_MULTIPLIED for a in r["aggregates"]["items"])


def test_a_step_over_a_name_two_schemas_declare_stands_for_no_table():
    tree = rt._parse_sql("WITH o AS (SELECT * FROM staging.orders) SELECT COUNT(*) FROM o", None)
    org = _two_schema_org()
    steps = rt._cte_steps(tree)
    assert rt._step_table(steps["o"], {}, rt._schema_index(org)) is None
    org = _sales_org()
    assert (
        rt._step_table(steps["o"], {}, rt._schema_index(org)) is None
    )  # `staging` is not a schema this model declares `orders` in
    tree = rt._parse_sql("WITH o AS (SELECT * FROM public.orders) SELECT COUNT(*) FROM o", None)
    assert rt._step_table(rt._cte_steps(tree)["o"], {}, rt._schema_index(org)) == "orders"


def test_a_step_with_no_columns_stands_for_no_table():
    tree = rt._parse_sql("WITH o AS (SELECT FROM orders) SELECT COUNT(*) FROM o", "postgres")
    org = _sales_org()
    step = rt._cte_steps(tree)["o"]
    assert rt._step_table(step, {}, rt._schema_index(org)) is None


# --- review of part 2: the cost of following steps stays linear -----------------------------------


def _untraced() -> None:
    try:
        import coverage

        if coverage.Coverage.current() is not None:
            pytest.skip("a clock under coverage measures the tracer; the untraced pass checks this")
    except ImportError:
        pass


def _long_chain_read_by_many_arms() -> str:
    steps = ["s0 AS (SELECT o.id FROM orders o JOIN customers k ON k.id = o.customer_id)"]
    steps += [f"s{i} AS (SELECT id FROM s{i - 1})" for i in range(1, 700)]
    return (
        "WITH "
        + ", ".join(steps)
        + " "
        + " UNION ALL ".join("SELECT id FROM s699" for _ in range(400))
    )


def _resolvable_chain_joined_in_many_arms() -> str:
    steps = ["c0 AS (SELECT id, customer_id, total_amount FROM orders)"]
    steps += [f"c{i} AS (SELECT id, customer_id, total_amount FROM c{i - 1})" for i in range(1, 63)]
    arm = "SELECT SUM(c62.total_amount) FROM c62 JOIN order_items i ON i.order_id = c62.id"
    return "WITH " + ", ".join(steps) + " " + " UNION ALL ".join(arm for _ in range(500))


def test_each_step_is_read_once_however_many_arms_reach_it(monkeypatch):
    """Which arms reach which step, and what a step stands for, are worked out once per statement:
    700 chained steps read by 400 arms classify each step once, not once per arm."""
    calls = {"classify": 0}
    real = rt._classify_step

    def counted(*args, **kwargs):
        calls["classify"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(rt, "_classify_step", counted)
    rt.assemble_receipt(_sales_org(), _resolvable_chain_joined_in_many_arms())
    assert calls["classify"] <= 4 * 63, calls


@pytest.mark.parametrize(
    "build,bound",
    [(_long_chain_read_by_many_arms, 25), (_resolvable_chain_joined_in_many_arms, 20)],
    ids=["700 steps x 400 arms", "63 steps x 500 joined arms"],
)
def test_steps_read_by_many_arms_stay_inside_the_bound(build, bound):
    """Relative to a parse-and-walk of the same statement on this machine (tests/timing_bound.py),
    so a loaded runner slows both sides. Locally ~8x and ~6.5x that; the bound is ~3x each. Before
    the fix the first took 5.5x its present time, well past 25x. Untraced pass only, best of three."""
    _untraced()
    sql = build()
    assert len(sql) <= 50_000
    assert_assembly_within(lambda: rt.assemble_receipt(_sales_org(), sql), sql, None,
                           repeats=3, ratio=bound)


def test_past_the_reach_budget_every_arm_is_fed(monkeypatch):
    """The reach walk is bounded; past its budget a join in a step downgrades every arm's numbers,
    the answer the receipt gave before it followed reach at all."""
    monkeypatch.setattr(rt, "_MAX_REACH_VISITS", 1)
    sql = UNDECLARED_STEP + "SELECT SUM(total_amount) FROM orders"
    [item] = rt.pre_flight_check(sql, _sales_org()).aggregates
    assert item.status == rt.UNDETERMINED


def test_only_a_with_reference_can_stand_for_a_table():
    org = _sales_org()
    tree = rt._parse_sql("SELECT COUNT(*) FROM orders", None)
    table = tree.find(rt.exp.Table)
    assert rt._step_table(table, {}, rt._schema_index(org)) is None
    assert rt._step_table(tree, {}, rt._schema_index(org)) is None
