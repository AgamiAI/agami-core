"""A join reached through a step, keyed on exactly a declared relationship's key, names it.

A common shape: a step lists the distinct parent keys a child table holds
(`SELECT DISTINCT oi.order_id FROM order_items oi WHERE ...`), and the parent table is LEFT JOINed to
it on that key. The step de-duplicates, so it never stands for `order_items` in the join's status or
the double-counting analysis (`undeclarable`, as before). But the key it hands on is
`order_items.order_id`, so the join is the declared `order_items → orders` relationship's key, and
the item says which relationship that is (`follows`). A reader that said "not through the model's
relationship" was telling the person the model had been bypassed.

Fail closed: a key the step computed, renamed from another column, or joined on a column the
declaration does not name follows nothing.
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

from test_semantic_model_runtime import _sales_org  # noqa: E402

# A common shape: a de-duplicated key list, joined to its parent inside a second
# step that groups, read by an outer query.
LIVE_SHAPE = (
    "WITH orders_with_items AS ("
    "  SELECT DISTINCT oi.order_id FROM order_items oi WHERE oi.order_id IS NOT NULL"
    "), by_customer AS ("
    "  SELECT o.customer_id, COUNT(DISTINCT o.id) AS orders,"
    "         COUNT(DISTINCT CASE WHEN w.order_id IS NOT NULL THEN o.id END) AS with_items"
    "  FROM orders o LEFT JOIN orders_with_items w ON w.order_id = o.id GROUP BY 1"
    ") SELECT customer_id, orders, with_items FROM by_customer"
)


def _only_join(sql: str, org: m.Datasource | None = None) -> dict:
    """The one join written outside a step (a step may write its own)."""
    items = rt.assemble_receipt(org or _sales_org(), sql)["joins"]["items"]
    (item,) = [it for it in items if not it["scope"].startswith("cte:w")]
    return item


def test_a_join_to_a_key_list_names_the_relationship_it_is_keyed_on():
    item = _only_join(LIVE_SHAPE)
    assert item["status"] == rt.UNDECLARABLE
    assert item["follows"] == {
        "name": "order_items_to_orders",
        "from_to": "order_items → orders",
        "review_state": "unreviewed",
    }


def test_following_a_relationship_claims_no_sign_off_and_no_status():
    """The trust block stays null: `follows` is the one place the relationship is named, so no
    reader that filters on `review_state` takes a step join for a matched one."""
    item = _only_join(LIVE_SHAPE)
    assert item["name"] is None and item["review_state"] is None and item["resolves_to"] is None


def test_the_sign_off_state_it_carries_is_the_relationship_s():
    org = _sales_org()
    for area in org.subject_areas:
        for rel in area.relationships:
            if rel.from_table == "order_items":
                rel.review_state = "approved"
    assert _only_join(LIVE_SHAPE, org)["follows"]["review_state"] == "approved"


@pytest.mark.parametrize(
    "step, on",
    [
        ("SELECT DISTINCT product_id AS order_id FROM order_items", "w.order_id = o.id"),
        ("SELECT DISTINCT order_id + 0 AS order_id FROM order_items", "w.order_id = o.id"),
        ("SELECT DISTINCT order_id FROM order_items", "w.order_id = o.customer_id"),
        (
            "SELECT DISTINCT oi.order_id FROM order_items oi JOIN orders x ON x.id = oi.order_id",
            "w.order_id = o.id",
        ),
        ("SELECT DISTINCT order_id FROM order_items LIMIT 5", "w.order_id = o.id"),
        ("SELECT DISTINCT order_id FROM (SELECT order_id FROM order_items) s", "w.order_id = o.id"),
    ],
    ids=["renamed-key", "computed-key", "wrong-column", "step-joins", "cut-rows", "nested-source"],
)
def test_a_step_that_does_not_hand_on_the_key_follows_nothing(step, on):
    item = _only_join(f"WITH w AS ({step}) SELECT COUNT(o.id) AS n FROM orders o JOIN w ON {on}")
    assert item["follows"] is None


def test_a_declaration_with_a_condition_on_the_step_s_side_follows_nothing():
    """The step's WHERE is not read, so a declared fixed-value condition on its table cannot be seen
    to hold: no claim."""
    org = _sales_org()
    for area in org.subject_areas:
        area.relationships = [
            m.Relationship(
                from_table="order_items",
                to_table="orders",
                on="order_items.order_id = orders.id AND order_items.product_id = 7",
                relationship="many_to_one",
            )
            if rel.from_table == "order_items"
            else rel
            for rel in area.relationships
        ]
    sql = (
        "WITH w AS (SELECT DISTINCT order_id FROM order_items WHERE product_id = 7) "
        "SELECT COUNT(o.id) AS n FROM orders o JOIN w ON w.order_id = o.id"
    )
    assert _only_join(sql, org)["follows"] is None


def test_a_join_between_two_tables_or_a_row_for_row_step_carries_no_follows():
    """Those already name their relationship the usual way; `follows` is null beside them."""
    direct = _only_join(
        "SELECT COUNT(o.id) AS n FROM orders o JOIN order_items oi ON oi.order_id = o.id"
    )
    assert direct["status"] == rt.DECLARED and direct["follows"] is None
    stood = _only_join(
        "WITH oi AS (SELECT order_id, quantity FROM order_items) "
        "SELECT COUNT(o.id) AS n FROM orders o JOIN oi ON oi.order_id = o.id"
    )
    assert stood["status"] == rt.DECLARED and stood["follows"] is None


def test_a_sampled_key_list_still_follows_the_relationship():
    """Decision: sampling only drops rows, like a WHERE or a DISTINCT, so every key the step hands on
    still comes from `order_items.order_id`; the join is keyed on the relationship. The status stays
    `undeclarable`, so the sample still never stands for the table in the double-counting check."""
    item = _only_join(
        "WITH w AS (SELECT DISTINCT order_id FROM order_items TABLESAMPLE BERNOULLI (10)) "
        "SELECT COUNT(o.id) AS n FROM orders o JOIN w ON w.order_id = o.id"
    )
    assert item["status"] == rt.UNDECLARABLE
    assert item["follows"]["name"] == "order_items_to_orders"
