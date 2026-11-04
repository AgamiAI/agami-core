"""Model identifiers in plain words, for the text of a question card."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

from semantic_model.plain_names import (  # noqa: E402
    column_label,
    distinct_labels,
    join_label,
    plain_words,
)


@pytest.mark.parametrize("identifier, words", [
    ("in_stock", "In stock"),
    ("order_items", "Order items"),
    ("sales.orders", "Orders"),              # a qualified name keeps its last part
    ("ORDER_ITEMS", "Order items"),          # an all-capitals name is only the engine's case
    ("ELA_score", "ELA score"),              # capitals inside a mixed-case name are kept
    ("CustomerID", "Customer ID"),
    ("customer_id", "Customer ID"),          # a known abbreviation
    ("ipAddress", "IP address"),
    ('"line-total"', "Line total"),
    ('"sales"."Order Items"', "Order items"),  # quoted parts lose their quotes after the split
    ("[dbo].[OrderItems]", "Order items"),
    ("", ""),
])
def test_plain_words(identifier, words):
    assert plain_words(identifier) == words


def test_labels_read_as_words():
    assert column_label("widgets", "price") == "Price on Widgets"
    assert column_label("widgets", "in_stock") == "In stock on Widgets"
    assert join_label("orders", "customers") == "Orders to Customers"


def test_a_join_label_names_its_column_so_two_joins_differ():
    billing = join_label("orders", "customers", "billing_customer_id")
    shipping = join_label("orders", "customers", "shipping_customer_id")
    assert billing == "Orders to Customers by Billing customer ID"
    assert billing != shipping


def test_a_label_two_items_share_gets_its_area():
    items = [{"area": "sales", "label": "Email on Customers"},
             {"area": "support", "label": "Email on Customers"},
             {"area": "sales", "label": "Phone on Customers"}]
    assert [it["label"] for it in distinct_labels(items)] == [
        "Email on Customers (Sales)", "Email on Customers (Support)", "Phone on Customers"]
