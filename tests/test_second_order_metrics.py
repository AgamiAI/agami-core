"""Case (b): second-order statistics — an aggregate OF an aggregate at a finer grain
(AVG of a daily SUM). The engine deterministically synthesizes the CTE from the
declaration (base metric + inner_grain + outer agg); illegal AVG(SUM(...)) is never
emitted, and the same question yields the same defensible SQL every time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import derived as D  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402
from semantic_model import validator as V  # noqa: E402


def _metric(name, binding, *, base=None, inner_grain=None, tables=("orders",)):
    return m.Metric(name=name, calculation="c", bindings={"PostgreSQL": binding},
                    base_metrics=list(base or []), inner_grain=list(inner_grain or []),
                    source_tables=list(tables))


def _org(metrics):
    sa = m.SubjectArea(name="sales", description="d", metrics=metrics)
    return m.Datasource(datasource="o", version=1, subject_areas=[sa])


def test_synthesizes_avg_daily_revenue_cte():
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    idx = D.metric_index(_org([rev, avg]))
    sql = D.resolve_metric_sql(avg, "PostgreSQL", idx)
    assert sql == (
        "(SELECT AVG(daily_revenue) FROM ("
        "SELECT order_date, SUM(amount) AS daily_revenue "
        "FROM orders GROUP BY order_date) _inner)"
    )


def test_synthesized_sql_is_valid_and_not_nested():
    import sqlglot
    rev = _metric("monthly_orders", "COUNT(*)")
    peak = _metric("peak_monthly_orders", "MAX({monthly_orders})",
                   base=["monthly_orders"], inner_grain=["order_month"])
    idx = D.metric_index(_org([rev, peak]))
    sql = D.resolve_metric_sql(peak, "PostgreSQL", idx)
    # parses cleanly and contains NO aggregate-inside-aggregate
    tree = sqlglot.parse_one(sql)
    assert tree is not None
    # judged per SELECT, the synthesized statement itself passes the shared rule (#519)
    assert D.binding_problems(sql, "PostgreSQL") == []


def test_is_second_order_detection():
    assert D.is_second_order(_metric("x", "AVG({y})", base=["y"], inner_grain=["d"]))
    assert not D.is_second_order(_metric("z", "SUM(amount)"))


def test_missing_inner_grain_refuses_nested():
    # nested aggregate WITHOUT inner_grain stays an error (we can't guess the grain)
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})", base=["daily_revenue"])
    idx = D.metric_index(_org([rev, avg]))
    with pytest.raises(D.DerivedError):
        D.resolve_metric_sql(avg, "PostgreSQL", idx)


def test_bad_shape_refused():
    # second-order binding must be exactly OUTERAGG({base})
    rev = _metric("daily_revenue", "SUM(amount)")
    bad = _metric("x", "AVG({daily_revenue}) + 1", base=["daily_revenue"], inner_grain=["d"])
    idx = D.metric_index(_org([rev, bad]))
    with pytest.raises(D.DerivedError):
        D.resolve_metric_sql(bad, "PostgreSQL", idx)


def test_multi_table_inner_refused_without_a_declared_join():
    rev = _metric("daily_revenue", "SUM(amount)", tables=("orders", "refunds"))
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    org = _org([rev, avg])
    # No table declares `amount`, so nothing says which table the base measures.
    with pytest.raises(D.DerivedError, match="set primary_table"):
        D.resolve_metric_sql(avg, "PostgreSQL", D.metric_index(org), org)
    rev.primary_table = "orders"
    with pytest.raises(D.DerivedError, match="no declared relationship joins 'orders' directly"):
        D.resolve_metric_sql(avg, "PostgreSQL", D.metric_index(org), org)
    # Without the model there are no relationships to join on at all.
    with pytest.raises(D.DerivedError, match="needs the model's relationships"):
        D.resolve_metric_sql(avg, "PostgreSQL", D.metric_index(org))


def test_validator_accepts_well_formed_second_order():
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    res = V.validate(_org([rev, avg]))
    assert res.ok, res.errors


def test_validator_blocks_second_order_missing_grain_via_bad_binding():
    # inner_grain set but binding isn't OUTERAGG({base}) → deploy-blocking error
    rev = _metric("daily_revenue", "SUM(amount)")
    bad = _metric("x", "SUM(amount) / 2", inner_grain=["order_date"])
    res = V.validate(_org([rev, bad]))
    assert not res.ok


def test_resolve_metrics_surfaces_synthesized_cte():
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("average daily revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    hits = RT.resolve_metrics("average daily revenue", _org([rev, avg]))
    top = next(h for h in hits if h["metric"] == "average daily revenue")
    assert top["bindings"]["PostgreSQL"].startswith("(SELECT AVG(daily_revenue) FROM (")


# --- multi-table bases and identifier quoting (#525) ---


def _col(name, type_="string"):
    return m.Column(name=name, type=type_)


def _rel(from_table, from_column, to_table, to_column, card="many_to_one", **kw):
    return m.Relationship(from_table=from_table, from_column=from_column, to_table=to_table,
                          to_column=to_column, relationship=card, **kw)


def _two_table_org(*, rels=None, base_primary=None, grain=("placed_at",), stype="PostgreSQL",
                   items_table="OrderItems", cross_rels=None):
    """A line-item base metric (on a mixed-case table) joined to orders for its grain."""
    units = m.Metric(name="daily_units", calculation="c", bindings={stype: "SUM(quantity)"},
                     source_tables=[items_table, "orders"], primary_table=base_primary)
    avg = m.Metric(name="avg_daily_units", calculation="c",
                   bindings={stype: "AVG({daily_units})"}, base_metrics=["daily_units"],
                   inner_grain=list(grain), source_tables=[items_table, "orders"])
    tables = [
        m.Table(name=items_table, grain=["id"],
                columns=[_col("id", "integer"), _col("order_id", "integer"),
                         _col("quantity", "integer")]),
        m.Table(name="orders", grain=["id"],
                columns=[_col("id", "integer"), _col("placed_at", "date")]),
    ]
    if rels is None:
        rels = [_rel(items_table, "order_id", "orders", "id")]
    sa = m.SubjectArea(name="sales", description="d", tables_defined=tables,
                       metrics=[units, avg], relationships=rels)
    org = m.Datasource(datasource="o", version=1, subject_areas=[sa],
                       cross_subject_area_relationships=list(cross_rels or []))
    return org, avg


def _sql(org, metric, stype="PostgreSQL"):
    return D.resolve_metric_sql(metric, stype, D.metric_index(org), org)


def test_two_table_base_joins_on_the_declared_edge_and_quotes_mixed_case():
    import sqlglot

    org, avg = _two_table_org()
    sql = _sql(org, avg)
    assert sql == (
        '(SELECT AVG(daily_units) FROM (SELECT orders.placed_at, SUM(quantity) AS daily_units '
        'FROM "OrderItems" INNER JOIN orders ON "OrderItems".order_id = orders.id '
        'GROUP BY orders.placed_at) _inner)'
    )
    tree = sqlglot.parse_one(sql, read="postgres")
    assert {t.name for t in tree.find_all(sqlglot.exp.Table)} == {"OrderItems", "orders"}
    assert V.validate(org).ok, V.validate(org).errors


def test_two_table_base_runs_on_a_real_engine():
    import sqlite3

    org, avg = _two_table_org(stype="SQLite")
    sql = _sql(org, avg, "SQLite")
    con = sqlite3.connect(":memory:")
    con.executescript(
        'CREATE TABLE "OrderItems" (id INTEGER, order_id INTEGER, quantity INTEGER);'
        "CREATE TABLE orders (id INTEGER, placed_at TEXT);"
        "INSERT INTO orders VALUES (1, '2026-01-01'), (2, '2026-01-01'), (3, '2026-01-02');"
        'INSERT INTO "OrderItems" VALUES (1, 1, 2), (2, 2, 4), (3, 3, 3), (4, 3, 1);'
    )
    # day 1: 2 + 4 = 6 units, day 2: 3 + 1 = 4 units -> average 5
    assert con.execute(f"SELECT {sql}").fetchone()[0] == 5


def test_join_that_fans_out_from_the_anchor_is_refused():
    org, avg = _two_table_org(base_primary="orders")
    with pytest.raises(D.DerivedError, match="fans out"):
        _sql(org, avg)
    # The same edge declared from the orders side is still a fan-out from orders.
    org, avg = _two_table_org(base_primary="orders",
                              rels=[_rel("orders", "id", "OrderItems", "order_id", "one_to_many")])
    with pytest.raises(D.DerivedError, match="fans out"):
        _sql(org, avg)


def test_edge_declared_from_the_other_side_joins_from_the_anchor():
    org, avg = _two_table_org(rels=[_rel("orders", "id", "OrderItems", "order_id", "one_to_many")])
    assert 'FROM "OrderItems" INNER JOIN orders ON orders.id = "OrderItems".order_id' in _sql(org, avg)


def test_two_edges_between_the_tables_are_ambiguous():
    org, avg = _two_table_org(rels=[_rel("OrderItems", "order_id", "orders", "id"),
                                    _rel("OrderItems", "return_order_id", "orders", "id")])
    with pytest.raises(D.DerivedError, match="ambiguous"):
        _sql(org, avg)


def test_rejected_edge_is_not_joined_on():
    org, avg = _two_table_org(rels=[_rel("OrderItems", "order_id", "orders", "id",
                                         review_state="rejected")])
    with pytest.raises(D.DerivedError, match="no declared relationship"):
        _sql(org, avg)


def test_cross_subject_area_edge_and_on_expression_are_used():
    cross = m.CrossSubjectAreaRelationship(
        from_table="OrderItems", to_table="orders", on='"OrderItems".order_id = orders.id',
        relationship="many_to_one", from_subject_area="sales", to_subject_area="sales")
    org, avg = _two_table_org(rels=[], cross_rels=[cross])
    assert 'INNER JOIN orders ON "OrderItems".order_id = orders.id' in _sql(org, avg)


def test_grain_column_must_resolve_to_one_source_table():
    org, avg = _two_table_org(grain=("id",))
    with pytest.raises(D.DerivedError, match="ambiguous"):
        _sql(org, avg)
    org, avg = _two_table_org(grain=("region",))
    with pytest.raises(D.DerivedError, match="not declared"):
        _sql(org, avg)
    org, avg = _two_table_org(grain=("DATE(placed_at)",))
    with pytest.raises(D.DerivedError, match="must name a column"):
        _sql(org, avg)
    org, avg = _two_table_org(grain=("orders.id",))
    assert "SELECT orders.id, SUM(quantity)" in _sql(org, avg)


def test_validator_reports_an_unjoinable_two_table_base():
    org, avg = _two_table_org(rels=[])
    res = V.validate(org)
    assert not res.ok
    assert any("no declared relationship" in e for e in res.errors), res.errors


def test_resolve_metrics_surfaces_the_joined_cte():
    org, _ = _two_table_org()
    hits = RT.resolve_metrics("avg_daily_units", org)
    top = next(h for h in hits if h["metric"] == "avg_daily_units")
    assert 'INNER JOIN orders ON "OrderItems".order_id = orders.id' in top["bindings"]["PostgreSQL"]


@pytest.mark.parametrize("name, stype, expected", [
    ("orders", "PostgreSQL", "orders"),
    ("OrderItems", "PostgreSQL", '"OrderItems"'),
    ("order", "PostgreSQL", '"order"'),
    ("line items", "PostgreSQL", '"line items"'),
    ("OrderItems", "MySQL", "`OrderItems`"),
    ("OrderItems", "Snowflake", '"OrderItems"'),
    ("ORDERS", "Snowflake", "ORDERS"),
    ("orders", "Snowflake", "orders"),
    ("OrderItems", "SQLite", "OrderItems"),
    ("sales.orders", "PostgreSQL", "sales.orders"),
    ("Sales.OrderItems", "PostgreSQL", '"Sales"."OrderItems"'),
])
def test_identifiers_are_quoted_only_where_the_engine_needs_it(name, stype, expected):
    assert D._table_ref(name, stype) == expected


def test_single_table_base_quotes_a_mixed_case_table_and_reserved_grain():
    rev = _metric("daily_revenue", "SUM(amount)", tables=("Sales.Orders",))
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order"])
    sql = D.resolve_metric_sql(avg, "PostgreSQL", D.metric_index(_org([rev, avg])))
    assert sql == (
        '(SELECT AVG(daily_revenue) FROM (SELECT "order", SUM(amount) AS daily_revenue '
        'FROM "Sales"."Orders" GROUP BY "order") _inner)'
    )


@pytest.mark.parametrize("name, stype, expected", [
    ("index", "SQLite", '"index"'),
    ("key", "MySQL", "`key`"),
    ("rank", "MySQL", "`rank`"),
    ("lateral", "PostgreSQL", '"lateral"'),
    # Upper-folding engines keep today's output: quoting would pin the lower-case name.
    ("index", "Snowflake", "index"),
    ("INDEX", "Snowflake", "INDEX"),
    ("index", "Oracle", "index"),
])
def test_engine_specific_reserved_words_are_quoted(name, stype, expected):
    assert D._table_ref(name, stype) == expected


def test_a_table_named_like_a_sqlite_keyword_runs():
    import sqlite3

    rev = m.Metric(name="daily_hits", calculation="c", bindings={"SQLite": "COUNT(*)"},
                   source_tables=["index"])
    avg = m.Metric(name="avg_daily_hits", calculation="c", bindings={"SQLite": "AVG({daily_hits})"},
                   base_metrics=["daily_hits"], inner_grain=["day"], source_tables=["index"])
    sql = D.resolve_metric_sql(avg, "SQLite", D.metric_index(_org([rev, avg])))
    con = sqlite3.connect(":memory:")
    con.executescript('CREATE TABLE "index" (day TEXT); INSERT INTO "index" VALUES (1), (1), (2);')
    assert con.execute(f"SELECT {sql}").fetchone()[0] == 1.5


def test_same_named_tables_from_two_schemas_are_refused():
    org, avg = _two_table_org(items_table="archive.orders")
    org.subject_areas[0].metrics[0].source_tables = ["archive.orders", "sales.orders"]
    avg.source_tables = ["archive.orders", "sales.orders"]
    with pytest.raises(D.DerivedError, match="same name from different schemas"):
        _sql(org, avg)


def test_the_base_metrics_own_area_defines_the_grain_column():
    org, avg = _two_table_org()
    # An earlier area keeps its own copy of `orders` without `placed_at`.
    other = m.SubjectArea(name="ops", description="d", tables_defined=[
        m.Table(name="orders", grain=["id"], columns=[_col("id", "integer")])])
    org = m.Datasource(datasource="o", version=1, subject_areas=[other, *org.subject_areas])
    assert "SELECT orders.placed_at, SUM(quantity)" in _sql(org, avg)


def test_a_declared_grain_column_with_a_space_is_quoted():
    org, avg = _two_table_org(grain=("order date",))
    org.subject_areas[0].tables_defined[1].columns.append(_col("order date", "date"))
    assert 'SELECT orders."order date", SUM(quantity)' in _sql(org, avg)
    assert 'GROUP BY orders."order date"' in _sql(org, avg)


def test_a_single_table_grain_column_with_a_space_is_quoted():
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order date", "DATE(placed_at)"])
    sa = m.SubjectArea(name="sales", description="d", metrics=[rev, avg], tables_defined=[
        m.Table(name="orders", grain=["id"], columns=[_col("order date", "date")])])
    org = m.Datasource(datasource="o", version=1, subject_areas=[sa])
    sql = _sql(org, avg)
    # The declared column is quoted; an expression is still passed through as written.
    assert 'SELECT "order date", DATE(placed_at), SUM(amount)' in sql


def _orders_first(org, avg, items_table="OrderItems"):
    """List orders first in the base's source tables, so the first table is not the measured one."""
    org.subject_areas[0].metrics[0].source_tables = ["orders", items_table]
    avg.source_tables = ["orders", items_table]
    return org, avg


def test_primary_table_matches_its_schema_qualified_source_table():
    org, avg = _orders_first(*_two_table_org(items_table="sales.OrderItems",
                                             base_primary="OrderItems"), "sales.OrderItems")
    # Matched by name, not exact string: anchoring on orders would be refused as a fan-out.
    assert 'FROM sales."OrderItems" INNER JOIN orders' in _sql(org, avg)


def test_primary_table_outside_the_source_tables_is_refused():
    org, avg = _two_table_org(base_primary="customers")
    with pytest.raises(D.DerivedError, match="'customers' is not one of its source tables"):
        _sql(org, avg)


def test_without_primary_table_the_measured_table_is_the_anchor():
    org, avg = _orders_first(*_two_table_org())
    assert 'FROM "OrderItems" INNER JOIN orders' in _sql(org, avg)
    org.subject_areas[0].metrics[0].bindings = {"PostgreSQL": 'SUM("OrderItems".quantity)'}
    assert 'FROM "OrderItems" INNER JOIN orders' in _sql(org, avg)


@pytest.mark.parametrize("binding", ["SUM(quantity * discount)", "COUNT(*)", "SUM(id)",
                                     "SUM(nope)"])
def test_without_primary_table_an_unclear_measured_table_is_refused(binding):
    org, avg = _two_table_org()
    org.subject_areas[0].tables_defined[1].columns.append(_col("discount", "decimal"))
    org.subject_areas[0].metrics[0].bindings = {"PostgreSQL": binding}
    with pytest.raises(D.DerivedError, match="set primary_table to say which table"):
        _sql(org, avg)


def test_a_dotted_grain_names_a_column_of_that_source_table():
    org, avg = _two_table_org(grain=("orders.order date",))
    for t in org.subject_areas[0].tables_defined:
        t.columns.append(_col("order date", "date"))
    assert 'GROUP BY orders."order date"' in _sql(org, avg)
    org, avg = _two_table_org(grain=("line items.id",), items_table="line items")
    assert 'SELECT "line items".id, SUM(quantity)' in _sql(org, avg)


@pytest.mark.parametrize("grain", ["orders.nope", "customers.id", "sales.orders.nope"])
def test_a_dotted_grain_outside_the_source_tables_is_refused(grain):
    org, avg = _two_table_org(grain=(grain,))
    with pytest.raises(D.DerivedError, match="does not name a column declared"):
        _sql(org, avg)


@pytest.mark.parametrize("name, stype, expected", [
    ("limit", "Oracle", '"LIMIT"'),
    ("order", "Oracle", '"ORDER"'),
    ("limit", "Snowflake", '"LIMIT"'),
    ("order", "Snowflake", '"ORDER"'),
    ("limit", "PostgreSQL", '"limit"'),
    ("orders", "Oracle", "orders"),
    ("orders", "Snowflake", "orders"),
])
def test_a_reserved_name_on_an_upper_folding_engine_is_quoted_upper_case(name, stype, expected):
    assert D._table_ref(name, stype) == expected
