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


def test_format_cell_is_exact_never_abbreviated():
    # verification surface: full value, grouping, currency symbol — NO abbreviation/rounding
    assert units.format_cell("21620870000.50", "INR") == "₹21,62,08,70,000.50"
    assert units.format_cell("134100000", None) == "134,100,000"     # bare count, grouped, exact
    assert units.format_cell("684.3", None) == "684.3"                # decimals preserved
    assert units.format_cell("active", None) == "active"             # passthrough
    assert units.format_cell("", None) == ""


def test_format_table_markdown_exact():
    md = units.format_table(
        ["borrowers", "outstanding"],
        [["134100000", "21620870000.5"], ["50200000", "9876543210"]],
        {"outstanding": "INR"})
    lines = md.splitlines()
    assert lines[0] == "| borrowers | outstanding |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 134,100,000 | ₹21,62,08,70,000.50 |"
    assert lines[3] == "| 50,200,000 | ₹9,87,65,43,210.00 |"
    # no abbreviation anywhere
    assert "Cr" not in md and "L " not in md and "M" not in md


def test_units_module_has_no_heavy_deps():
    # units.py must stay import-light so the pure-stdlib MCP path can use it
    import importlib, sys as _sys
    assert "pydantic" not in _sys.modules or True  # informational
    src = (SCRIPTS / "semantic_model" / "units.py").read_text()
    assert "import pydantic" not in src and "import sqlglot" not in src


def _currency_model(root):
    import yaml
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "org.yaml").write_text(yaml.safe_dump({
        "organization": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "loans"}]}))
    (root / "subject_areas" / "s" / "tables" / "loans.yaml").write_text(yaml.safe_dump({
        "name": "loans", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "l",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "unit": "INR"}]}))


def test_resolve_result_units_traces_aggregates(tmp_path):
    # the reliability fix: a SUM/AVG over a currency column keeps the currency, even
    # under an alias; COUNT and ratios do not become currency
    pytest.importorskip("sqlglot")
    from semantic_model import runtime as RT
    from semantic_model.loader import load_organization
    _currency_model(tmp_path)
    org = load_organization(tmp_path)
    assert RT.resolve_result_units(org, "SELECT SUM(amount) AS total FROM loans") == {"total": "INR"}
    assert RT.resolve_result_units(org, "SELECT AVG(amount) AS a FROM loans") == {"a": "INR"}
    assert RT.resolve_result_units(org, "SELECT amount FROM loans") == {"amount": "INR"}
    assert RT.resolve_result_units(org, "SELECT COUNT(*) AS cnt FROM loans") == {}
    assert RT.resolve_result_units(org, "SELECT SUM(amount)/COUNT(*) AS avg_ticket FROM loans") == {}
    # unaliased aggregate → positional key (#0), since the DB invents the column name
    assert RT.resolve_result_units(org, "SELECT MAX(amount) FROM loans") == {"#0": "INR"}


def test_format_table_applies_units_by_position():
    # the positional fallback formats a DB-auto-named column (e.g. Postgres "max")
    md = units.format_table(["max"], [["250000.5"]], {"#0": "INR"})
    assert md.splitlines()[2] == "| ₹2,50,000.50 |"


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
