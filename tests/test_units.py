"""Tests for semantic_model/units.py (deterministic currency/unit formatting) and the
`unit` field surfacing through the model + get_table_context."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from semantic_model import units  # noqa: E402


def test_currency_symbol_and_is_currency():
    assert units.currency_symbol("inr") == "₹"
    assert units.currency_symbol("USD") == "$"
    assert units.currency_symbol("not-a-currency") is None
    assert units.is_currency("eur") and not units.is_currency("days")


def test_inr_uses_indian_grouping():
    assert units.format_value(123456.5, "INR") == "₹1,23,456.50"
    assert units.format_value(10000000, "INR") == "₹1,00,00,000.00"
    assert units.format_value(-2500, "INR") == "-₹2,500.00"


def test_western_grouping_and_zero_decimal():
    assert units.format_value(123456.5, "USD") == "$123,456.50"
    assert units.format_value(1234, "JPY") == "¥1,234"     # no minor unit
    assert units.format_value(50, "EUR") == "€50.00"


def test_non_currency_units_and_passthrough():
    assert units.format_value(98.6, "percent") == "98.6%"
    assert units.format_value(1234, "days") == "1,234 days"
    assert units.format_value("N/A", "USD") == "N/A"        # non-numeric passthrough
    assert units.format_value(None, "USD") == ""


def test_unit_round_trips_on_column_and_surfaces_in_context(tmp_path):
    import yaml
    from semantic_model.loader import load_organization, get_table_context
    root = tmp_path
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "o",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "unit": "INR"}]}))
    org = load_organization(root)
    assert org.subject_areas[0].defined_table("orders").get_column("amount").unit == "INR"
    ctx = get_table_context(org, ["orders"], area="s")
    amount = next(c for c in ctx["tables"]["orders"]["columns"] if c["name"] == "amount")
    assert amount["unit"] == "INR"
