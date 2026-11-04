"""Model identifiers in plain words, for the text of a question card."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

from semantic_model.plain_names import column_label, join_label, plain_words  # noqa: E402


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
    ("", ""),
])
def test_plain_words(identifier, words):
    assert plain_words(identifier) == words


def test_labels_read_as_words():
    assert column_label("widgets", "price") == "Price on Widgets"
    assert column_label("widgets", "in_stock") == "In stock on Widgets"
    assert join_label("orders", "customers") == "Orders to Customers"
