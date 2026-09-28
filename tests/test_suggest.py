"""`semantic_model.suggest.did_you_mean`: the one name matcher every wrong-name answer uses.

Each tier is pinned by the case it exists for. Synthetic names throughout.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from semantic_model.suggest import did_you_mean  # noqa: E402

NAMES = ["orders", "order_items", "users", "order count"]


def test_an_exact_match_ignoring_case_and_separators_comes_alone():
    assert did_you_mean("Order_Items", NAMES) == ["order_items"]
    assert did_you_mean("order_count", NAMES) == ["order count"]


def test_the_same_words_in_another_order_count_as_exact():
    assert did_you_mean("items_order", NAMES) == ["order_items"]


def test_a_typo_finds_its_name():
    assert did_you_mean("ordrs", NAMES)[0] == "orders"


def test_the_name_asked_for_is_never_suggested_back():
    assert "orders" not in did_you_mean("orders", NAMES)


@pytest.mark.parametrize("name", ["", "   ", "zzzzzz", "sandcastles"])
def test_nothing_close_suggests_nothing(name):
    assert did_you_mean(name, NAMES) == []
