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


@pytest.mark.parametrize("sql", [
    "SELECT amount FROM orders WHERE EXISTS (SELECT 1 FROM stock_moves)",
    "SELECT o.amount FROM orders o WHERE EXISTS (SELECT 1 FROM stock_moves o)",
    "SELECT amount FROM orders WHERE id IN (SELECT id FROM stock_moves)",
    # a derived table or a CTE still hands its table's column through
    "SELECT amount FROM (SELECT amount FROM orders) t",
    "WITH x AS (SELECT amount FROM orders) SELECT amount FROM x",
])
def test_a_subquery_in_a_predicate_binds_nothing_outside(tmp_path, sql):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql) == {"amount": "USD", "#0": "USD"}


def test_a_predicate_subquery_never_lends_a_label(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    # stock_moves declares an id too, but only stores' id can reach the result
    assert RT.resolve_result_labels(
        org, "SELECT id FROM stores WHERE EXISTS (SELECT 1 FROM stock_moves WHERE amount > 0)") == {"id", "#0"}


def test_a_quoted_column_matches_as_the_engine_matches_it(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    # SQLite reads "POSTCODE" as postcode, so it is the model's string column
    assert RT.resolve_result_labels(org, 'SELECT s."POSTCODE" AS site FROM stores s') == {"site", "#0"}
    assert RT.resolve_result_units(org, 'SELECT SUM("REVENUE") AS r FROM stores') == {"r": "USD", "#0": "USD"}


def test_a_quoted_column_is_exact_where_the_engine_is(tmp_path):
    _model(tmp_path)
    storage = tmp_path / "datasources" / "c" / "storage.yaml"
    storage.write_text(yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    org = load_datasource(tmp_path)
    assert RT.resolve_result_labels(org, 'SELECT s."POSTCODE" AS site FROM stores s') == set()
    assert RT.resolve_result_labels(org, 'SELECT s."postcode" AS site FROM stores s') == {"site", "#0"}


@pytest.mark.parametrize("expr", [
    "COALESCE(SUM(revenue), CAST(0 AS DECIMAL(18,2)))",
    "GREATEST(revenue, -1)",
    "LEAST(revenue, (-1))",
    "COALESCE(NULL, revenue)",
    "SUM(DISTINCT CASE WHEN postcode = '1' THEN revenue ELSE 0 END)",
    "SUM(DISTINCT revenue)",
])
def test_a_constant_or_distinct_takes_no_unit_away(tmp_path, expr):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, f"SELECT {expr} AS v FROM stores") == {"v": "USD", "#0": "USD"}


@pytest.mark.parametrize("expr, label", [
    ("COALESCE(NULL, postcode)", True),
    ("COALESCE(postcode, 'none')", True),
    ("MAX(DISTINCT postcode)", True),
    ("COALESCE(NULL, EXTRACT(YEAR FROM opened_on))", True),
    # a value that may come from a quantity is not a label
    ("COALESCE(postcode, net_change)", False),
    ("COALESCE(NULL, net_change)", False),
])
def test_a_value_function_is_a_label_when_everything_it_returns_is(tmp_path, expr, label):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert (RT.resolve_result_labels(org, f"SELECT {expr} AS v FROM stores") == {"v", "#0"}) is label


@pytest.mark.parametrize("sql, expected", [
    # the subquery's own WHERE and correlation columns carry no unit and take none away
    ("SELECT o.id, (SELECT SUM(amount) FROM orders WHERE id > 0) AS total FROM orders o", {"total": "USD", "#1": "USD"}),
    ("SELECT (SELECT SUM(revenue) FROM stores WHERE year_opened > 2000) AS recent_revenue",
     {"recent_revenue": "USD", "#0": "USD"}),
    ("SELECT s.id, (SELECT MAX(o.amount) FROM orders o WHERE o.id = s.id) AS max_order FROM stock_moves s",
     {"max_order": "USD", "#1": "USD"}),
    # a correlated subquery reads the outer row's column and its own
    ("SELECT o2.id, (SELECT o2.amount - MIN(o.amount) FROM orders o) AS above_min FROM orders o2",
     {"above_min": "USD", "#1": "USD"}),
    # an inner alias shadows the outer one
    ("SELECT (SELECT MAX(o.amount) FROM stock_moves o) AS m FROM orders o", {}),
    ("SELECT (SELECT MAX(x.amount) FROM (SELECT amount FROM orders) x) AS m FROM stock_moves", {"m": "USD", "#0": "USD"}),
])
def test_a_scalar_subquery_is_its_own_projection(tmp_path, sql, expected):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql) == expected


@pytest.mark.parametrize("sql, expected", [
    ("WITH t AS (SELECT amount FROM orders) SELECT t.amount FROM t", {"amount": "USD", "#0": "USD"}),
    ("SELECT x.amount FROM (SELECT amount FROM orders) x", {"amount": "USD", "#0": "USD"}),
    ("WITH a AS (SELECT amount FROM orders), b AS (SELECT a.amount AS amt FROM a) SELECT b.amt FROM b",
     {"amt": "USD", "#0": "USD"}),
    ("WITH t(v) AS (SELECT revenue FROM stores) SELECT v FROM t", {"v": "USD", "#0": "USD"}),
    # a name is what the body computed, not another table's column of that name
    ("WITH t AS (SELECT net_change AS revenue FROM stores) SELECT t.revenue FROM t", {}),
    ("SELECT amount FROM (SELECT id AS amount FROM stock_moves) x", {}),
    ("WITH t AS (SELECT amount FROM stock_moves) SELECT t.amount FROM t", {}),
    # every arm of a UNION must agree
    ("SELECT amount FROM orders UNION ALL SELECT amount FROM stock_moves", {}),
    ("SELECT amount FROM orders UNION ALL SELECT NULL", {"amount": "USD", "#0": "USD"}),
    ("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t", {}),
    # a comparison is true or false, never an amount
    ("SELECT amount > 100 AS big FROM orders", {}),
])
def test_a_cte_or_derived_column_is_what_its_body_computed(tmp_path, sql, expected):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql) == expected


def test_labels_trace_through_ctes_and_subqueries(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_labels(org, "WITH t AS (SELECT postcode FROM stores) SELECT t.postcode FROM t") == {
        "postcode", "#0"}
    assert RT.resolve_result_labels(org, "SELECT (SELECT MAX(postcode) FROM stores) AS p") == {"p", "#0"}
    assert RT.resolve_result_labels(org, "SELECT x.v FROM (SELECT net_change AS v FROM stores) x") == set()
    assert RT.resolve_result_labels(
        org, "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t") == set()


def _currency_model(root: Path) -> None:
    """`orders.amount` in dollars and `eu_orders.amount` in euros; only `orders.created_ts` is a date."""
    _model(root)
    (root / "subject_areas" / "s" / "tables" / "eu_orders.yaml").write_text(yaml.safe_dump({
        "name": "eu_orders", "schema": "main", "storage_connection": "c", "grain": ["id"],
        "description": "eu orders", "columns": [{"name": "id", "type": "integer", "primary_key": True},
                                               {"name": "amount", "type": "decimal", "unit": "EUR"},
                                               {"name": "created_ts", "type": "integer"}]}))
    area = root / "subject_areas" / "s" / "subject_area.yaml"
    doc = yaml.safe_load(area.read_text())
    doc["tables"].append({"storage_connection": "c", "schema": "main", "table": "eu_orders"})
    area.write_text(yaml.safe_dump(doc))


@pytest.mark.parametrize("sql, expected", [
    ("SELECT amount, (SELECT SUM(amount) FROM eu_orders) AS eur FROM orders",
     {"amount": "USD", "#0": "USD", "eur": "EUR", "#1": "EUR"}),
    ("SELECT o.id, (SELECT MAX(e.amount) FROM eu_orders e WHERE e.id = o.id) AS eur FROM orders o",
     {"eur": "EUR", "#1": "EUR"}),
    ("WITH t AS (SELECT amount FROM eu_orders) SELECT t.amount FROM t", {"amount": "EUR", "#0": "EUR"}),
    ("WITH u AS (SELECT amount FROM orders), e AS (SELECT amount FROM eu_orders) "
     "SELECT (SELECT SUM(amount) FROM u) AS usd, (SELECT SUM(amount) FROM e) AS eur",
     {"usd": "USD", "#0": "USD", "eur": "EUR", "#1": "EUR"}),
    ("SELECT amount FROM orders UNION ALL SELECT amount FROM eu_orders", {}),
    ("WITH t AS (SELECT created_ts FROM orders) SELECT t.created_ts FROM t",
     {"created_ts": "epoch_s", "#0": "epoch_s"}),
    ("SELECT MAX(x.created_ts) FROM (SELECT created_ts FROM orders) x", {"#0": "epoch_s"}),
    ("WITH t AS (SELECT created_ts FROM eu_orders) SELECT t.created_ts FROM t", {}),
])
def test_each_subquery_keeps_its_own_unit(tmp_path, sql, expected):
    _currency_model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql) == expected


def test_a_subquery_answer_keeps_its_unit_and_its_rows(tmp_path, monkeypatch):
    import tools

    _model(tmp_path)
    org = load_datasource(tmp_path)
    monkeypatch.setattr(tools, "get_cached_org", lambda profile: org)
    sql = "SELECT s.id, (SELECT SUM(o.amount) FROM orders o WHERE o.id = s.id) AS paid FROM stock_moves s"
    rows = [[1, "714.23"]]
    payload = json.loads(tools._finalize_execution(["id", "paid"], rows, profile="demo", sql=sql, execution_ms=1))
    assert payload["markdown"].splitlines()[2] == "| 1 | $714.23 |"
    assert payload["rows"] == [[1, "714.23"]]


@pytest.mark.parametrize("sql, expected", [
    # a CTE of a table's name is the CTE; one defined inside a subquery is seen there
    ("WITH orders AS (SELECT id AS amount FROM stock_moves) SELECT amount FROM orders", {}),
    ("SELECT x.v FROM (WITH t AS (SELECT amount AS v FROM orders) SELECT v FROM t) x", {"v": "USD", "#0": "USD"}),
    # a recursive CTE's reference to itself takes away nothing its seed gives
    ("WITH RECURSIVE t(i, v) AS (SELECT id, amount FROM orders UNION ALL SELECT i + 1, v * 2 FROM t WHERE i < 3) "
     "SELECT v FROM t", {"v": "USD", "#0": "USD"}),
    # a star expands its own table, by its qualifier
    ("WITH t AS (SELECT * FROM orders) SELECT t.amount FROM t", {"amount": "USD", "#0": "USD"}),
    ("WITH t AS (SELECT * FROM stock_moves) SELECT t.amount FROM t", {}),
    ("WITH t AS (SELECT a.* FROM orders a JOIN stock_moves b ON a.id = b.id) SELECT t.amount FROM t",
     {"amount": "USD", "#0": "USD"}),
    ("WITH t AS (SELECT b.* FROM orders a JOIN stock_moves b ON a.id = b.id) SELECT t.amount FROM t", {}),
    ("WITH t AS (SELECT a.*, b.* FROM orders a JOIN stock_moves b ON a.id = b.id) SELECT t.amount FROM t", {}),
    ("WITH t AS (SELECT * FROM orders UNION ALL SELECT * FROM orders) SELECT amount FROM t",
     {"amount": "USD", "#0": "USD"}),
    ("WITH t AS (SELECT * FROM orders UNION ALL SELECT * FROM stock_moves) SELECT amount FROM t", {}),
    ("WITH t(revenue) AS (SELECT * FROM stock_moves) SELECT revenue FROM t", {}),
    # no table the star expands has it, so it is the outer row's
    ("SELECT (SELECT revenue FROM (SELECT * FROM stock_moves) x) AS v FROM stores", {"v": "USD", "#0": "USD"}),
    # LATERAL reads its sibling; VALUES and a table function compute their own columns
    ("SELECT l.a FROM orders o, LATERAL (SELECT o.amount AS a) l", {"a": "USD", "#0": "USD"}),
    ("SELECT revenue FROM (VALUES (1)) v(revenue)", {}),
    ("SELECT revenue FROM generate_series(1, 3) revenue", {}),
    ("SELECT amount FROM orders, generate_series(1, 3) g", {"amount": "USD", "#0": "USD"}),
    # a table the model doesn't know may hold the column, so the outer row can't claim it
    ("SELECT (SELECT days_open FROM undeclared) AS v FROM stores", {}),
    # an earlier alias in the same select list, not another table's column of that name
    ("SELECT id AS revenue, revenue * 2 AS doubled FROM stock_moves", {}),
    ("SELECT SUM(amount) AS total, total * 2 AS doubled FROM orders",
     {"total": "USD", "#0": "USD", "doubled": "USD", "#1": "USD"}),
])
def test_what_a_column_reads_through_stars_laterals_and_aliases(tmp_path, sql, expected):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql) == expected


def test_a_very_long_cte_chain_says_nothing_rather_than_failing(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    bodies = ["c0 AS (SELECT amount AS v FROM orders)"]
    bodies += [f"c{i} AS (SELECT c{i - 1}.v + c{i - 1}.v AS v FROM c{i - 1})" for i in range(1, 400)]
    sql = f"WITH {', '.join(bodies)} SELECT v FROM c399"
    assert RT.resolve_result_units(org, sql) == {}
    assert RT.resolve_result_labels(org, sql) == set()
    short = f"WITH {', '.join(bodies[:30])} SELECT v FROM c29"
    assert RT.resolve_result_units(org, short) == {"v": "USD", "#0": "USD"}


@pytest.mark.parametrize("sql, expected", [
    # the other arguments say which row, not what unit
    ("SELECT ARG_MIN(amount, id) AS x FROM orders", "USD"),
    ("SELECT MAX_BY(amount, created_ts) AS x FROM orders", "USD"),
    ("SELECT ARG_MAX(created_ts, id) AS x FROM orders", "epoch_s"),
    ("SELECT ARG_MAX(id, amount) AS x FROM orders", None),
    ("SELECT FIRST(amount ORDER BY id) AS x FROM orders", "USD"),
    ("SELECT LAST(created_ts ORDER BY id) AS x FROM orders", "epoch_s"),
    ("SELECT STRING_AGG(id, ',' ORDER BY amount) AS x FROM orders", None),
    ("SELECT LAG(created_ts, id) OVER (ORDER BY id) AS x FROM orders", "epoch_s"),
    ("SELECT NTH_VALUE(amount, id) OVER (ORDER BY id) AS x FROM orders", "USD"),
    ("SELECT ROW_NUMBER() OVER (ORDER BY amount) AS x FROM orders", None),
    ("SELECT MEDIAN(created_ts) AS x FROM orders", "epoch_s"),
    ("SELECT PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY amount) AS x FROM orders", "USD"),
    ("SELECT PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY id) AS x FROM orders", None),
    # an unknown function's argument is a value expression, whatever it multiplies or tests
    ("SELECT TOTAL(amount * id) AS x FROM orders", "USD"),
    ("SELECT TOTAL(amount + id) AS x FROM orders", None),
    ("SELECT TOTAL(CASE id WHEN 1 THEN amount END) AS x FROM orders", "USD"),
    ("SELECT TOTAL(CASE WHEN id > 0 THEN id ELSE 0 END) AS x FROM orders", None),
    ("SELECT TOTAL(id > 0) AS x FROM orders", None),
    ("SELECT TOTAL((SELECT SUM(amount) FROM orders)) AS x", "USD"),
    ("SELECT TOTAL(amount / 2) AS x FROM orders", None),
    ("SELECT TRUNC(amount, id) AS x FROM orders", "USD"),
    ("SELECT amount % id AS x FROM orders", "USD"),
    ("SELECT NVL2(id, amount, 0) AS x FROM orders", "USD"),
    ("SELECT NVL2(amount, id, 0) AS x FROM orders", None),
    ("SELECT SAFE_DIVIDE(amount, id) AS x FROM orders", "USD"),
    ("SELECT SAFE_DIVIDE(amount, amount) AS x FROM orders", None),
    ("SELECT DECODE(id, 1, amount, 2, amount, 0) AS x FROM orders", "USD"),
    ("SELECT DECODE(amount, 1, id) AS x FROM orders", None),
])
def test_a_function_has_the_unit_of_the_value_it_returns(tmp_path, sql, expected):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql).get("x") == expected


@pytest.mark.parametrize("expr, label", [
    ("ARG_MAX(postcode, revenue)", True),
    ("LAG(postcode) OVER (ORDER BY id)", True),
    ("ARG_MAX(net_change, postcode)", False),
])
def test_a_picked_value_stays_a_label(tmp_path, expr, label):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert ("v" in RT.resolve_result_labels(org, f"SELECT {expr} AS v FROM stores")) is label


@pytest.mark.parametrize("sql, expected", [
    ("WITH t AS (SELECT id AS x, * FROM orders) SELECT amount FROM t", {"amount": "USD", "#0": "USD"}),
    ("WITH t AS (SELECT * REPLACE (id AS amount) FROM orders) SELECT amount FROM t", {}),
    # a name the body gives twice, or a position no arm has, can't be traced
    ("WITH t AS (SELECT id AS amount, amount FROM orders) SELECT t.amount FROM t", {}),
    ("WITH t(a, b) AS (SELECT amount FROM orders) SELECT b FROM t", {}),
    ("WITH t AS (VALUES (1)) SELECT t.amount FROM t", {}),
    # the derived table lacks it, so it is the outer row's
    ("SELECT (SELECT revenue FROM (SELECT id FROM stock_moves) x) AS v FROM stores", {"v": "USD", "#0": "USD"}),
    ("SELECT a.amount FROM (orders a JOIN stock_moves b ON a.id = b.id)", {"amount": "USD", "#0": "USD"}),
    ("SELECT b.amount FROM (orders a JOIN stock_moves b ON a.id = b.id)", {}),
    ("SELECT nope.amount FROM orders", {}),
    ("SELECT (SELECT * FROM orders) AS v", {}),
    ("DELETE FROM orders", {}),
])
def test_what_cannot_be_traced_says_nothing(tmp_path, sql, expected):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql) == expected


def test_labels_of_a_year_part_and_of_what_does_not_parse(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert "v" in RT.resolve_result_labels(org, "SELECT YEAR(opened_on) AS v FROM stores")
    assert RT.resolve_result_labels(org, "SELECT (") == set()


def test_a_label_cell_as_written():
    assert units.format_rows(["store_id"], [[5.0]]) == [["5"]]
    assert units.format_rows(["store_id"], [[Decimal("5.00")]]) == [["5"]]
    assert units.format_rows(["postal_area"], [["07094"]]) == [["07094"]]
    assert units.format_rows(["flag"], [[True]]) == [["True"]]


# --- two columns of one name, constant branches, EXCEPT, runaway exponents and depth ---


JOIN_ON = "FROM orders o JOIN eu_orders e ON e.id = o.id"
JOIN_NO_UNIT = "FROM orders o JOIN stock_moves s ON s.id = o.id"


@pytest.mark.parametrize("sql, expected, row", [
    (f"SELECT o.amount, e.amount {JOIN_ON}", {"#0": "USD", "#1": "EUR"}, ["$5.00", "€5.00"]),
    (f"SELECT e.amount, o.amount {JOIN_ON}", {"#0": "EUR", "#1": "USD"}, ["€5.00", "$5.00"]),
    (f"SELECT o.amount, s.amount {JOIN_NO_UNIT}", {"#0": "USD"}, ["$5.00", "5"]),
    (f"SELECT s.amount, o.amount {JOIN_NO_UNIT}", {"#1": "USD"}, ["5", "$5.00"]),
    (f"SELECT o.amount AS x, e.amount AS X {JOIN_ON}", {"#0": "USD", "#1": "EUR"}, ["$5.00", "€5.00"]),
])
def test_each_of_two_columns_of_one_name_has_its_own_unit(tmp_path, sql, expected, row):
    _currency_model(tmp_path)
    resolved = RT.resolve_result_units(load_datasource(tmp_path), sql)
    assert resolved == expected
    headers = ["amount", "amount"]
    assert units.format_rows(headers, [["5", "5"]], resolved) == [row]
    table = units.format_table(headers, [["5", "5"]], resolved)
    assert table.splitlines()[-1] == f"| {row[0]} | {row[1]} |"


def test_a_name_two_columns_share_gives_neither_a_unit_or_a_label():
    # a star leaves no positions to go by, whatever the map was built from
    assert units.format_rows(["v", "v"], [["5", "5"]], {"v": "EUR"}) == [["5", "5"]]
    assert units.format_rows(["v", "v"], [[1234, 1234]], {}, {"v"}) == [["1,234", "1,234"]]
    assert units.format_rows(["v", "v"], [[1234, 1234]], {}, {"#1"}) == [["1,234", "1234"]]
    assert units.format_rows(["v", "w"], [["5", "5"]], {"v": "EUR"}) == [["€5.00", "5"]]


def test_a_label_of_a_repeated_name_stays_with_its_position(tmp_path):
    _currency_model(tmp_path)
    labels = RT.resolve_result_labels(load_datasource(tmp_path), "SELECT o.id, s.amount AS id " + JOIN_NO_UNIT)
    assert labels == {"#0"}


def test_a_report_reads_two_columns_of_one_name_by_position(tmp_path):
    import csv_to_sections as C

    path = tmp_path / "r.csv"
    path.write_text("amount,amount\n5,5\n")
    unit_map = {"#0": "USD", "#1": "EUR"}
    section = C._build_section({"csv_file": str(path), "units": unit_map, "label_col": 0}, 0, [])
    assert section["table_rows"] == [["$5.00", "€5.00"]]
    assert section["labels"] == ["$5.00"]
    assert C._unit_for(unit_map, ["amount", "amount"], 1) == "EUR"


@pytest.mark.parametrize("branch", ["100", "-1", "'n/a'", "CAST(100 AS DECIMAL(18,2))", "0", "NULL"])
def test_a_constant_branch_takes_no_unit_away(tmp_path, branch):
    _currency_model(tmp_path)
    org = load_datasource(tmp_path)
    for expr in (f"CASE WHEN id > 1 THEN amount ELSE {branch} END",
                 f"CASE WHEN id > 1 THEN {branch} ELSE amount END",
                 f"IIF(id > 1, amount, {branch})"):
        assert RT.resolve_result_units(org, f"SELECT {expr} AS v FROM orders") == {"v": "USD", "#0": "USD"}


@pytest.mark.parametrize("expr", [
    "CASE WHEN id > 1 THEN 1 ELSE 100 END",     # nothing but constants
    "CASE WHEN id > 1 THEN amount ELSE id END",  # a value with no unit beside one
])
def test_a_branch_without_a_known_unit_still_gives_none(tmp_path, expr):
    _currency_model(tmp_path)
    assert RT.resolve_result_units(load_datasource(tmp_path), f"SELECT {expr} AS v FROM orders") == {}


@pytest.mark.parametrize("sql, expected", [
    ("SELECT amount FROM orders EXCEPT SELECT amount FROM eu_orders", {"amount": "USD", "#0": "USD"}),
    ("SELECT amount FROM eu_orders EXCEPT SELECT amount FROM orders", {"amount": "EUR", "#0": "EUR"}),
    ("SELECT id FROM orders EXCEPT SELECT amount FROM orders", {}),
    ("WITH t AS (SELECT amount FROM orders EXCEPT SELECT amount FROM eu_orders) SELECT amount FROM t",
     {"amount": "USD", "#0": "USD"}),
    ("SELECT (SELECT amount FROM orders EXCEPT SELECT amount FROM eu_orders) AS v", {"v": "USD", "#0": "USD"}),
    ("SELECT amount FROM orders INTERSECT SELECT amount FROM eu_orders", {}),
    ("(SELECT amount FROM orders EXCEPT SELECT amount FROM eu_orders) UNION ALL SELECT amount FROM orders",
     {"amount": "USD", "#0": "USD"}),
    ("(SELECT amount FROM orders EXCEPT SELECT amount FROM orders) UNION ALL SELECT amount FROM eu_orders", {}),
])
def test_an_except_returns_only_its_left_arm(tmp_path, sql, expected):
    _currency_model(tmp_path)
    assert RT.resolve_result_units(load_datasource(tmp_path), sql) == expected


RUNAWAY = ["12e345678901", "1e1234567890", "1e-1234567890", "0e1234567890", "1e401",
           "1e99999999999999999999", "1e" + "9" * 5000]


@pytest.mark.parametrize("text", RUNAWAY)
def test_a_runaway_exponent_is_shown_as_written(text):
    for unit in (None, "USD", "INR", "USD_millions", "kg", "percent", "epoch_s", "epoch_ms", "yyyymmdd"):
        if unit == "percent" and text.startswith(("1e-", "0e")):
            continue    # a finite value, in `%g`
        if unit and text in ("1e-1234567890", "0e1234567890"):
            continue    # reads as 0.0, which is a number any unit can show
        assert units.format_cell(text, unit) == text
    assert units.format_rows(["v"], [[text]]) == [[text]]
    assert units.format_rows(["store_id"], [[text]]) == [[text]]
    assert text in units.format_table(["v"], [[text]])


@pytest.mark.parametrize("value", [Decimal("1E+1234567890"), Decimal("1E-1234567890")])
def test_a_runaway_decimal_is_shown_as_written(value):
    assert units.format_cell(value, None) == str(value)
    assert units.format_rows(["store_id"], [[value]]) == [[str(value)]]


def test_an_exponent_in_a_doubles_range_still_expands():
    assert units.format_cell("1e400", None) == "10" + ",000" * 133
    assert units.format_cell("1e3", None) == "1,000"
    assert units.format_cell(1e22, None) == "10,000,000,000,000,000,000,000"
    assert units.format_cell("1e-3", None) == "0.001"


def test_a_date_out_of_range_is_shown_as_written():
    assert units.format_date("1e300", "epoch_s") == "1e300"
    assert units.format_date("1e999", "yyyymmdd") == "1e999"
    assert units.format_date("1e308", "yyyymmdd") == "1e308"


def test_a_report_leaves_a_runaway_exponent_out_of_the_chart():
    import csv_to_sections as C

    assert C._raw_number("1e1234567890") is None


def _star_chain(n: int, body: str = "SELECT * FROM c{p}") -> str:
    bodies = ["c0 AS (SELECT * FROM orders)"] + [f"c{i} AS ({body.format(p=i - 1)})" for i in range(1, n)]
    return f"WITH {', '.join(bodies)} SELECT id, amount FROM c{n - 1}"


@pytest.mark.parametrize("body", [
    "SELECT * FROM c{p}",
    "SELECT * FROM c{p} UNION ALL SELECT * FROM c{p}",
    "SELECT a.* FROM c{p} a JOIN c{p} b ON a.id = b.id",
])
def test_a_deep_chain_of_stars_is_read_in_one_pass(tmp_path, body, monkeypatch):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    reads = []
    original = RT._DerivedSource._read
    monkeypatch.setattr(RT._DerivedSource, "_read",
                        lambda self, *a: reads.append(a) or original(self, *a))
    sql = _star_chain(40, body)
    assert RT.resolve_result_units(org, sql) == {"amount": "USD", "#1": "USD"}
    assert len(reads) <= 2 * 40 * 2     # two names, per level; doubling per level never finishes


@pytest.mark.parametrize("sql", [
    _star_chain(600),
    "SELECT " + "+".join(["amount"] * 3000) + " AS s FROM orders",
])
def test_a_statement_too_deep_to_trace_loses_its_units_not_the_answer(tmp_path, sql):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, sql) == {}
    assert RT.resolve_result_labels(org, sql) == set()


def test_thousands_of_union_arms_keep_their_unit(tmp_path):
    _model(tmp_path)
    sql = " UNION ALL ".join(["SELECT amount FROM orders"] * 5000)
    assert RT.resolve_result_units(load_datasource(tmp_path), sql) == {"amount": "USD", "#0": "USD"}


def test_prepare_answers_a_statement_too_deep_to_check(tmp_path, capsys):
    _model(tmp_path)
    sql = " UNION ALL ".join(["SELECT amount FROM orders"] * 5000)
    assert cli.main(["prepare", str(tmp_path), "--area", "s", "--sql", sql]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unchecked"] == RT.UNCHECKED_TOO_DEEP and out["findings"] == []
    assert out["units"] == {"amount": "USD", "#0": "USD"}


def test_prepare_answers_when_the_trace_overflows(tmp_path, capsys, monkeypatch):
    _model(tmp_path)

    def _overflow(*_a):
        raise RecursionError

    monkeypatch.setattr(RT, "_statement_outputs", _overflow)
    assert cli.main(["prepare", str(tmp_path), "--area", "s", "--sql", "SELECT amount FROM orders"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["units"] == {} and out["labels"] == []


@pytest.mark.parametrize("fn", ["DATE_PART", "date_part", "datepart"])
def test_duckdbs_date_part_is_a_year(tmp_path, fn):
    _model(tmp_path)
    storage = tmp_path / "datasources" / "c" / "storage.yaml"
    storage.write_text(yaml.safe_dump({"name": "c", "storage_type": "DuckDB"}))
    org = load_datasource(tmp_path)
    assert "v" in RT.resolve_result_labels(org, f"SELECT {fn}('year', opened_on) AS v FROM stores")
    assert "v" not in RT.resolve_result_labels(org, f"SELECT {fn}('day', opened_on) AS v FROM stores")


@pytest.mark.parametrize("cell, shown", [
    ("2011.0", "2011"),     # Postgres' DATE_PART, once the row is text
    (2011.0, "2011"),
    (Decimal("2011"), "2011"),
    ("001.0", "001.0"),     # a code
    ("2011.00", "2011.00"),  # a label cast to DECIMAL stays as cast
    ("12345.0", "12345.0"),
    (Decimal("2011.50"), "2011.50"),
])
def test_a_year_label_loses_a_doubles_zero_and_nothing_else_does(cell, shown):
    assert units.format_rows(["sold"], [[cell]], {}, {"#0"}) == [[shown]]
