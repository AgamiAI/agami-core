"""The examples preview (`sm seed-validate`) and the query answer format a number by its column's
type and unit (#547): a year, a ZIP code or an id is shown as written, a negative keeps its sign
before the digits, a NUMERIC value loses its trailing zeros, and only a column that has a unit gets
one, so a duration in days never reads as money."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import cli, curate, units  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

STORES = [
    {"name": "id", "type": "integer", "primary_key": True},
    {"name": "year_opened", "type": "integer"},
    {"name": "opened_on", "type": "date"},
    {"name": "postcode", "type": "string"},
    {"name": "net_change", "type": "integer"},
    {"name": "rate", "type": "decimal"},
    {"name": "days_open", "type": "integer"},
    {"name": "revenue", "type": "decimal", "unit": "USD"},
]
STOCK_MOVES = [
    {"name": "id", "type": "integer", "primary_key": True},
    {"name": "amount", "type": "integer"},
]
ORDERS = [
    {"name": "id", "type": "integer", "primary_key": True},
    {"name": "amount", "type": "decimal", "unit": "USD"},
    {"name": "created_ts", "type": "integer", "date_format": "epoch_s"},
]


def _model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "SQLite"}))
    tables = {"orders": ORDERS, "stock_moves": STOCK_MOVES, "stores": STORES}
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "main", "table": t}
                                for t in tables]}))
    for name, cols in tables.items():
        (root / "subject_areas" / "s" / "tables" / f"{name}.yaml").write_text(yaml.safe_dump({
            "name": name, "schema": "main", "storage_connection": "c", "grain": ["id"],
            "description": name, "columns": cols}))


PREVIEW_SQL = ("SELECT year_opened, postcode, net_change, rate, "
               "AVG(CASE WHEN revenue > 0 THEN days_open END) AS avg_days_open, "
               "SUM(revenue) AS revenue FROM stores GROUP BY year_opened, postcode, net_change, rate")
# what execute_sql.py prints for that statement: CSV text, a NUMERIC(38,20) value with all its zeros
PREVIEW_CSV = ("year_opened,postcode,net_change,rate,avg_days_open,revenue\n"
               "2011,07094,-719,0.12500000000000000000,12.5,1234567.50000000000000000000\n")


def _seed_validate(root: Path, monkeypatch, sql: str, csv_out: str) -> dict:
    curate.add_examples(root, "s", [{"question": "store figures", "sql": sql}])

    class _Done:
        returncode, stdout, stderr = 0, csv_out, ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli.main(["seed-validate", str(root), "--area", "s", "--profile", "demo"]) == 0
    return json.loads(buf.getvalue())[0]


def test_preview_formats_each_column_by_its_type_and_unit(tmp_path, monkeypatch):
    _model(tmp_path)
    item = _seed_validate(tmp_path, monkeypatch, PREVIEW_SQL, PREVIEW_CSV)
    assert item["row_preview"] == [["2011", "07094", "-719", "0.125", "12.5", "$1,234,567.50"]]


def test_preview_keeps_a_year_taken_out_of_a_date_ungrouped(tmp_path, monkeypatch):
    _model(tmp_path)
    sql = ("SELECT strftime('%Y', opened_on) AS cohort, COUNT(*) AS stores "
           "FROM stores GROUP BY 1")
    item = _seed_validate(tmp_path, monkeypatch, sql, "cohort,stores\n2011,1500\n")
    assert item["row_preview"] == [["2011", "1,500"]]


def test_a_days_column_gets_no_unit_from_elsewhere(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    # a CASE takes its unit from what it returns, not from the money column in its condition
    assert "avg_days_open" not in RT.resolve_result_units(org, PREVIEW_SQL)
    # a bare `amount` is the one its own table declares, not another table's money column
    assert RT.resolve_result_units(org, "SELECT amount FROM stock_moves") == {}
    assert RT.resolve_result_units(org, "SELECT amount FROM orders") == {"amount": "USD", "#0": "USD"}
    # date − date is a duration, never a date
    assert RT.resolve_result_units(
        org, "SELECT (MAX(created_ts) - MIN(created_ts)) / 86400 AS span_days FROM orders") == {}


def test_labels_come_from_the_model(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    labels = RT.resolve_result_labels(
        org, "SELECT id, postcode, MAX(net_change) AS biggest, EXTRACT(YEAR FROM opened_on) AS y "
             "FROM stores GROUP BY id, postcode")
    assert labels == {"id", "#0", "postcode", "#1", "y", "#3"}
    # a unit wins over the model calling the column a label
    assert units.format_rows(["fee_code"], [[1000]], {"fee_code": "USD"}, labels={"fee_code"}) == [
        ["$1,000.00"]]


def test_number_cells_are_exact_and_signed():
    assert units.format_cell("-719", None) == "-719"
    assert units.format_cell(-719, None) == "-719"
    assert units.format_cell("-123456", None) == "-123,456"
    assert units.format_cell("07094", None) == "07094"
    assert units.format_cell(Decimal("0.12500000000000000000"), None) == "0.125"
    assert units.format_cell("4.00000000", None) == "4"
    assert units.format_cell(12345678901234567890, None) == "12,345,678,901,234,567,890"
    assert units.format_cell(Decimal("NaN"), None) == "NaN"
    assert units.format_cell(float("inf"), None) == "inf"
    assert units.format_value(-719, "days") == "-719 days"
    assert units.format_value(Decimal("1234.50000"), "days") == "1,234.5 days"


def test_a_year_is_trusted_from_a_name_only_when_the_values_look_like_years():
    out = units.format_rows(["year_built", "sales_last_year"], [["2011", "1234567"]])
    assert out == [["2011", "1,234,567"]]


def test_money_with_a_unit_keeps_its_symbol_grouping_and_cents():
    out = units.format_rows(["revenue", "fee_code"], [[Decimal("1234567.50000000000000"), 1000]],
                            {"revenue": "USD", "fee_code": "USD"})
    assert out == [["$1,234,567.50", "$1,000.00"]]
    assert units.format_value(-2500, "INR") == "-₹2,500.00"


def test_query_answer_markdown_uses_the_same_rules(tmp_path, monkeypatch):
    import tools

    _model(tmp_path)
    org = load_datasource(tmp_path)
    monkeypatch.setattr(tools, "get_cached_org", lambda profile: org)
    rows = [["2011", "07094", "-719", "0.12500000000000000000", "12.5", "1234567.50000000000000000000"]]
    payload = json.loads(tools._finalize_execution(
        PREVIEW_CSV.splitlines()[0].split(","), rows, profile="demo", sql=PREVIEW_SQL,
        execution_ms=1))
    assert payload["markdown"].splitlines()[2] == (
        "| 2011 | 07094 | -719 | 0.125 | 12.5 | $1,234,567.50 |")
    # unchanged where it was right: the raw rows and the unit map the payload carries
    assert payload["rows"] == rows
    assert payload["units"] == {"revenue": "USD", "#5": "USD"}
    # a plain count and a plain amount format as before
    assert units.format_table(["n", "total"], [["1234567", "2500.5"]]).splitlines()[2] == (
        "| 1,234,567 | 2,500.5 |")


def _cli(args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


def test_prepare_returns_labels_that_format_table_takes(tmp_path):
    _model(tmp_path)
    rc, out = _cli(["prepare", str(tmp_path), "--sql",
                    "SELECT strftime('%Y', opened_on) AS cohort, SUM(revenue) AS revenue FROM stores GROUP BY 1"])
    prep = json.loads(out)
    assert rc == 0 and prep["labels"] == ["#0", "cohort"]
    csv_file = tmp_path / "r.csv"
    csv_file.write_text("cohort,revenue\n2011,1234.5\n")
    rc, out = _cli(["format-table", "--csv-file", str(csv_file), "--units", json.dumps(prep["units"]),
                    "--labels", json.dumps(prep["labels"])])
    assert rc == 0 and out.splitlines()[2] == "| 2011 | $1,234.50 |"


def _two_schema_model(root: Path) -> None:
    """`orders` in two schemas: `sales.orders.amount` is money, `archive.orders.amount` is a code."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "DuckDB"}))
    tables = {"sales": [{"name": "id", "type": "integer", "primary_key": True},
                        {"name": "amount", "type": "decimal", "unit": "USD"}],
              "archive": [{"name": "id", "type": "integer", "primary_key": True},
                          {"name": "amount", "type": "string"}]}
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": schema, "table": "orders"}
                                for schema in tables]}))
    for schema, cols in tables.items():
        (root / "subject_areas" / "s" / "tables" / f"{schema}_orders.yaml").write_text(yaml.safe_dump({
            "name": "orders", "schema": schema, "storage_connection": "c", "grain": ["id"],
            "description": f"{schema} orders", "columns": cols}))


def test_a_column_is_read_from_its_own_schema(tmp_path):
    _two_schema_model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, "SELECT SUM(amount) AS total FROM sales.orders") == {
        "total": "USD", "#0": "USD"}
    assert RT.resolve_result_units(org, "SELECT amount FROM archive.orders") == {}
    assert RT.resolve_result_labels(org, "SELECT amount FROM archive.orders") == {"amount", "#0"}
    assert RT.resolve_result_labels(org, "SELECT amount FROM sales.orders") == set()
    both = ("SELECT sales.orders.amount AS s_amt, archive.orders.amount AS a_amt "
            "FROM sales.orders JOIN archive.orders ON sales.orders.id = archive.orders.id")
    assert RT.resolve_result_units(org, both) == {"s_amt": "USD", "#0": "USD"}
    assert RT.resolve_result_labels(org, both) == {"a_amt", "#1"}
    aliased = "SELECT s.amount AS s_amt, a.amount AS a_amt FROM sales.orders s JOIN archive.orders a ON s.id = a.id"
    assert RT.resolve_result_units(org, aliased) == {"s_amt": "USD", "#0": "USD"}
    assert RT.resolve_result_labels(org, aliased) == {"a_amt", "#1"}


@pytest.mark.parametrize("expr", [
    "SUM(CASE WHEN postcode = '1' THEN revenue ELSE 0 END)",
    "SUM(CASE WHEN postcode = '1' THEN revenue ELSE NULL END)",
    "SUM(CASE WHEN postcode = '1' THEN 0 ELSE revenue END)",
    "SUM(IF(postcode = '1', revenue, 0))",
    "SUM(IIF(postcode = '1', revenue, NULL))",
    "SUM(revenue) OVER (PARTITION BY postcode)",
    "SUM(revenue) OVER (PARTITION BY postcode ORDER BY days_open)",
    "SUM(revenue) FILTER (WHERE postcode = '1')",
    "GREATEST(revenue, 0)",
    "COALESCE(SUM(revenue), 0)",
])
def test_money_keeps_its_unit_through_a_condition_or_window(tmp_path, expr):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, f"SELECT {expr} AS v FROM stores") == {"v": "USD", "#0": "USD"}


@pytest.mark.parametrize("expr", [
    "CASE WHEN postcode = '1' THEN revenue ELSE days_open END",
    "IF(revenue > 0, days_open, 0)",
    "SUM(days_open) OVER (PARTITION BY revenue)",
    "COALESCE(revenue, days_open)",
])
def test_a_condition_or_partition_never_lends_its_unit(tmp_path, expr):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, f"SELECT {expr} AS v FROM stores") == {}


@pytest.mark.parametrize("expr", [
    "GREATEST(created_ts, 1704067200)", "LEAST(created_ts, 1704067200)", "MAX(created_ts, 0)",
    "COALESCE(created_ts, 0)", "NULLIF(created_ts, 0)", "MIN(created_ts)", "MAX(created_ts)",
])
def test_a_function_that_picks_a_value_keeps_its_date_encoding(tmp_path, expr):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, f"SELECT {expr} AS v FROM orders") == {"v": "epoch_s", "#0": "epoch_s"}
    # one that makes a new value from it is no longer that date
    assert RT.resolve_result_units(
        org, "SELECT strftime('%Y', created_ts, 'unixepoch') AS v FROM orders") == {}


def test_count_star_keeps_the_positional_label(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    sql = "SELECT strftime('%Y', opened_on), COUNT(*) FROM stores GROUP BY 1"
    assert "#0" in RT.resolve_result_labels(org, sql)
    assert RT.resolve_result_units(org, "SELECT SUM(revenue), COUNT(*) FROM stores") == {"#0": "USD"}
    # a real star still turns positions off
    assert RT.resolve_result_labels(org, "SELECT s.*, postcode FROM stores s") == {"postcode"}
    assert RT.resolve_result_labels(org, "SELECT *, postcode FROM stores") == {"postcode"}
