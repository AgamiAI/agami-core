"""`sm export` core (ACE-167): eligibility, binding resolution, ids and the report every target shares.

No target writer ships with this slice, so the tests register their own: `dump` writes the whole
`ExportView` it is handed as JSON. The sensitive-column property is then proved the way the spec
asks, by scanning every byte the run emitted (the dump and the report) for the column's name rather
than by inspecting the view, since a writer that reads around the view would pass a view check.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import derived as D  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "sales"
SECRET = "tax_ref"  # the sensitive column's name; it must reach no emitted byte by default
SECRET_RE = re.compile(rf"(?<![\w$]){SECRET}(?![\w$])", re.IGNORECASE)


# ---------------------------------------------------------------------------
# A synthetic model: acme's SALES_DATA, in SQLite
# ---------------------------------------------------------------------------


def _col(name, type_="string", **kw):
    return {"name": name, "type": type_, **kw}


def _metric(name, sqlite=None, state="approved", **kw):
    bindings = kw.pop("bindings", {} if sqlite is None else {"SQLite": sqlite})
    return {"name": name, "calculation": f"The {name}.", "bindings": bindings,
            "source_tables": kw.pop("source_tables", ["orders"]), "review_state": state, **kw}


def _write_model(root: Path) -> Path:
    def put(rel, doc):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put("datasource.yaml", {
        "datasource": "acme", "description": "Acme demo sales.",
        "storage_connections": [{"name": "acme_db", "storage_type": "SQLite", "storage_config": {}}],
        "subject_areas": [f"subject_areas/{AREA}", "subject_areas/ops"],
        "key_terminology": {"GMV": "gross merchandise value", "ref code": f"the {SECRET} on a customer"},
        "ambiguities": [
            {"name": "which amount", "when": "a question says amount",
             "options": [{"label": "net", "how": "use orders.amount", "default": True},
                         {"label": "gross", "how": "add tax"}]},
            {"name": "which customer key", "when": "a question names a customer key",
             "columns": [f"customers.{SECRET}"],
             "options": [{"label": "id", "how": "use customers.id"},
                         {"label": "ref", "how": f"use customers.{SECRET}"}]},
        ],
        "cross_subject_area_relationships": [
            {"from_table": "orders", "to_table": "shipments", "from_column": "id",
             "to_column": "order_id", "relationship": "one_to_many", "review_state": "approved",
             "from_subject_area": AREA, "to_subject_area": "ops"}],
    })
    put(f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA, "description": "Orders and customers."})
    put(f"subject_areas/{AREA}/tables/orders.yaml", {
        "name": "orders", "schema": "SALES_DATA", "grain": ["id"],
        "default_filters": ["{alias}.status != 'void'"],
        "columns": [_col("id", "integer", primary_key=True), _col("customer_id", "integer"),
                    _col("cust_ref", foreign_key={"table": "customers", "column": SECRET}),
                    _col("amount", "decimal"), _col("status"),
                    _col("channel", review_state="unreviewed", choice_field={"w": "web"}),
                    _col("ref_flag", value_transform=f"CASE WHEN {SECRET} IS NULL THEN 'n' END")]})
    put(f"subject_areas/{AREA}/tables/customers.yaml", {
        "name": "customers", "schema": "SALES_DATA", "grain": ["id"],
        "default_filters": [f"{{alias}}.{SECRET} IS NOT NULL", "{alias}.region != 'test'"],
        "columns": [_col("id", "integer", primary_key=True), _col("region"),
                    _col(SECRET, sensitive=True, choice_field={"A": "Alpha", "B": "Beta"})]})
    put(f"subject_areas/{AREA}/relationships.yaml", [
        {"from_table": "orders", "to_table": "customers", "from_column": "customer_id",
         "to_column": "id", "relationship": "many_to_one", "review_state": "approved"},
        {"from_table": "orders", "to_table": "customers", "from_column": "cust_ref",
         "to_column": SECRET, "relationship": "many_to_one", "review_state": "approved"},
        {"from_table": "orders", "to_table": "customers", "on": "orders.customer_id = customers.id",
         "relationship": "many_to_one", "review_state": "approved"},
        {"from_table": "customers", "to_table": "orders", "from_column": "id",
         "to_column": "customer_id", "relationship": "one_to_many"},
    ])
    put(f"subject_areas/{AREA}/entities/customer.yaml", {
        "name": "customer", "review_state": "approved",
        "maps_to": [{"table": "customers", "column": "id", "primary": True}]})
    put(f"subject_areas/{AREA}/entities/account.yaml", {
        "name": "account", "review_state": "approved",
        "maps_to": [{"table": "customers", "column": SECRET}]})
    put(f"subject_areas/{AREA}/entities/client.yaml", {
        "name": "client", "review_state": "approved",
        "maps_to": [{"table": "customers", "column": "id"}, {"table": "customers", "column": SECRET}]})
    put(f"subject_areas/{AREA}/entities/prospect.yaml", {
        "name": "prospect", "maps_to": [{"table": "customers", "column": "id"}]})
    metrics = [
        _metric("revenue", "SUM(orders.amount)",
                bindings={"SQLite": "SUM(orders.amount)", "Snowflake": "SUM(orders.amount)"}),
        _metric("order count", "COUNT(DISTINCT orders.id)"),
        _metric("average order value", "{revenue} / {order count}", base_metrics=["revenue", "order count"]),
        _metric("draft total", "SUM(orders.amount)", state="unreviewed"),
        _metric("stale total", "SUM(orders.amount)", state="stale"),
        _metric("rejected total", "SUM(orders.amount)", state="rejected"),
        _metric("ref count", f'COUNT(DISTINCT "Customers"."{SECRET.upper()}")', source_tables=["customers"]),
        _metric("refs per order", "{ref count} / {order count}", base_metrics=["ref count", "order count"]),
        _metric("snowflake only", bindings={"Snowflake": "SUM(orders.amount)"}),
        _metric("broken", "SUM(orders.amount"),
        _metric("half bound", bindings={"SQLite": "SUM(orders.amount)", "Snowflake": "{nowhere} + ("}),
    ]
    for m in metrics:
        put(f"subject_areas/{AREA}/metrics/{m['name'].replace(' ', '_')}.yaml", m)
    put("subject_areas/ops/subject_area.yaml", {"name": "ops", "description": "Shipping."})
    put("subject_areas/ops/tables/shipments.yaml", {
        "name": "shipments", "schema": "SALES_DATA",
        "columns": [_col("id", "integer"), _col("order_id", "integer")]})
    put(f"prompt_examples/{AREA}/examples.yaml", [
        {"question": "What is revenue?", "sql": "SELECT SUM(amount) FROM orders", "status": "confirmed"},
        {"question": "Orders by region?", "status": "proposed",
         "sql": "SELECT c.region, COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id GROUP BY 1"},
        {"question": "Which customers are flagged?", "status": "confirmed",
         "sql": f"SELECT CASE WHEN c.`{SECRET.title()}` = 'A' THEN 1 END AS f FROM customers AS c"},
        {"question": "Rejected one", "sql": "SELECT 1", "status": "rejected"},
    ])
    put("golden_datasets/core.yaml", {"test_cases": [
        {"id": "g1", "query": "Total revenue", "expected": {"sql": "SELECT SUM(amount) FROM orders",
                                                             "sql_confirmed": True}},
        {"id": "g2", "query": "Draft count", "expected": {"sql": "SELECT COUNT(*) FROM orders",
                                                           "sql_confirmed": False}},
        {"id": "g3", "query": "Refs", "expected": {
            "sql": f"SELECT r FROM (SELECT {SECRET} AS r FROM customers) s", "sql_confirmed": True}},
        {"id": "g4", "query": "Shipped orders", "expected": {
            "sql": "SELECT COUNT(*) FROM orders o JOIN shipments s ON s.order_id = o.id",
            "sql_confirmed": True}},
        {"id": "g5", "query": "Unreadable", "expected": {"sql": "SELECT FROM ((", "sql_confirmed": True}},
    ]})
    return root


@pytest.fixture()
def model(tmp_path: Path) -> Path:
    return _write_model(tmp_path / "art" / "acme")


# ---------------------------------------------------------------------------
# A test target: the whole view, as JSON
# ---------------------------------------------------------------------------


def _jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(obj):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (frozenset, set)):
        return sorted(_jsonable(x) for x in obj)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


def _dump(view: X.ExportView, options: X.ExportOptions) -> X.WriterOutput:
    body = json.dumps({"view": _jsonable(view), "args": dict(options.target_args)}, sort_keys=True)
    return X.WriterOutput({f"{view.area}.json": body.encode("utf-8")})


@pytest.fixture(autouse=True)
def test_targets(monkeypatch):
    monkeypatch.setitem(X.TARGETS, "dump", X.Target("dump", ("SQLite",), _dump))
    monkeypatch.setitem(X.TARGETS, "snowx", X.Target("snowx", ("Snowflake",), _dump))


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(root: Path, out: Path, *flags: str, target: str = "dump") -> tuple[dict, dict]:
    rc, text = _sm(str(root), "--target", target, "--out", str(out), *flags)
    assert rc == 0, text
    report = json.loads(text)
    view = json.loads((out / target / f"{AREA}.json").read_text(encoding="utf-8"))["view"]
    return report, view


def _losses(report: dict, target: str = "dump") -> dict[tuple[str, str], str]:
    return {(x["kind"], x["name"]): x["reason"] for x in report["targets"][target]["losses"]}


def _names(view: dict, kind: str) -> set[str]:
    if kind == "metric":
        return {m["metric"]["name"] for m in view["metrics"]}
    return {q["name"] for q in view[kind]}


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_only_approved_metrics_by_default_and_the_flag_adds_the_others(model, tmp_path):
    four = {"revenue", "draft total", "stale total", "rejected total"}
    report, view = _export(model, tmp_path / "a")
    assert _names(view, "metric") & four == {"revenue"}
    assert _losses(report)[("metric", "draft total")] == "not_approved"
    assert _losses(report)[("metric", "stale total")] == "not_approved"

    report, view = _export(model, tmp_path / "b", "--include-unreviewed")
    assert _names(view, "metric") & four == {"revenue", "draft total", "stale total"}
    added = {(x["kind"], x["name"]) for x in report["targets"]["dump"]["included_unreviewed"]}
    assert {("metric", "draft total"), ("metric", "stale total")} <= added
    for text in (json.dumps(report), json.dumps(view)):
        assert "rejected total" not in text


def test_golden_item_counts_as_approved_only_when_its_sql_is_confirmed(model, tmp_path):
    report, view = _export(model, tmp_path / "a")
    assert "core:g1" in _names(view, "golden") and "core:g2" not in _names(view, "golden")
    assert _losses(report)[("golden", "core:g2")] == "not_approved"
    _, view = _export(model, tmp_path / "b", "--include-unreviewed")
    assert "core:g2" in _names(view, "golden")


def test_a_golden_item_no_one_area_holds_is_reported_as_spanning(model, tmp_path):
    report, _ = _export(model, tmp_path / "a")
    assert _losses(report)[("golden", "core:g4")] == "spans_subject_areas"
    assert _losses(report)[("relationship", f"{AREA}/orders->ops/shipments")] == "spans_subject_areas"


def test_a_golden_file_or_case_that_does_not_load_is_reported(model, tmp_path):
    # Left alone, a golden file that fails to load would just be missing from the export, unsaid.
    (model / "golden_datasets" / "broken.yaml").write_text("test_cases: [\n", encoding="utf-8")
    _put(model, "golden_datasets/partial.yaml", {"test_cases": [
        {"id": "ok", "query": "Total revenue again", "expected": {
            "sql": "SELECT SUM(amount) FROM orders", "sql_confirmed": True}},
        {"id": f"bad_{SECRET}", "query": "No expected block"}]})
    report, view = _export(model, tmp_path / "a")
    assert "partial:ok" in _names(view, "golden")
    lost = [x for x in report["targets"]["dump"]["losses"] if x["kind"] == "golden"
            and x["outcome"] == "dropped" and x["reason"] == "unparseable_for_target"
            and x["area"] == ""]
    names = sorted(x["name"] for x in lost)
    assert names[0] == "broken.yaml" and len(names) == 2
    assert "does not load" in lost[0]["detail"]
    # The broken case's id names a sensitive column: the report withholds it, as for any loss.
    assert not SECRET_RE.search(json.dumps(report))


def test_golden_fields_no_target_carries_are_reported_once_per_item(model, tmp_path):
    def add(doc):
        case = doc["test_cases"][0]
        case.update(match="bounded", must_filter=["orders.status"], tags=["smoke"],
                    bounds={"min_rows": 1})
        case["expected"]["validation_notes"] = "Checked by hand."
    _edit(model / "golden_datasets" / "core.yaml", add)
    report, view = _export(model, tmp_path / "a")
    assert "core:g1" in _names(view, "golden")
    (entry,) = [x for x in report["targets"]["dump"]["losses"] if x["name"] == "core:g1"]
    assert (entry["outcome"], entry["reason"]) == ("degraded", "detail_lost")
    assert entry["detail"].startswith("its match, bounds, must_filter, tags, expected.validation_notes ")
    # An item with only the question and SQL loses nothing, so it gets no entry.
    report, _ = _export(_write_model(tmp_path / "art2" / "acme"), tmp_path / "b")
    assert not [x for x in report["targets"]["dump"]["losses"] if x["reason"] == "detail_lost"
                and x["kind"] == "golden"]


def test_example_status_decides_approval(model, tmp_path):
    report, view = _export(model, tmp_path / "a")
    assert _names(view, "examples") == {"What is revenue?"}
    assert _losses(report)[("example", "Orders by region?")] == "not_approved"
    assert "Rejected one" not in json.dumps(report)


# ---------------------------------------------------------------------------
# Sensitive columns: scanned in every emitted byte
# ---------------------------------------------------------------------------


def _emitted(out: Path) -> dict[str, str]:
    return {str(p.relative_to(out)): p.read_text(encoding="utf-8") for p in sorted(out.rglob("*")) if p.is_file()}


def test_no_emitted_byte_names_a_sensitive_column_by_default(model, tmp_path):
    out = tmp_path / "out"
    report, _ = _export(model, out)
    files = _emitted(out)
    assert set(files) == {"dump/sales.json", "dump/ops.json", "export-report.json"}
    for name, text in files.items():
        assert not SECRET_RE.search(text), f"{name} names the sensitive column"

    losses = _losses(report)
    dependants = {
        ("metric", "ref count"),                # a binding, quoted and upper-cased
        ("metric", "refs per order"),           # reached only through another metric
        ("example", "Which customers are flagged?"),   # back-ticked, case variant, inside CASE
        ("golden", "core:g3"),                  # through a derived table's alias
        ("default_filter", "customers#1"),
        ("relationship", "orders->customers#2"),  # keyed on it
        ("entity", "account"),
        ("ambiguity", "which customer key"),
        ("terminology", "ref code"),
    }
    for item in dependants:
        assert losses.get(item) == "depends_on_excluded", item
    sensitive = [n for (k, n), r in losses.items() if k == "column" and r == "sensitive"]
    assert len(sensitive) == 1 and re.fullmatch(r"customers\.[0-9a-f]{32}", sensitive[0])
    assert losses[("choice_field", sensitive[0])] == "depends_on_excluded"
    degraded = {(x["kind"], x["name"]): x for x in report["targets"]["dump"]["losses"]}
    assert degraded[("column", "orders.cust_ref")]["outcome"] == "degraded"  # its foreign key went
    assert degraded[("entity", "client")]["outcome"] == "degraded"  # one of its two mappings went
    assert degraded[("column", "orders.ref_flag")]["outcome"] == "degraded"  # its value_transform went
    # Another engine's binding that does not expand cannot be read, so it cannot be cleared.
    assert losses[("metric", "half bound")] == "depends_on_excluded"


def test_unreviewed_columns_relationships_and_entities_stay_home(model, tmp_path):
    report, view = _export(model, tmp_path / "out")
    losses = _losses(report)
    assert losses[("column", "orders.channel")] == "not_approved"
    assert losses[("choice_field", "orders.channel")] == "depends_on_excluded"
    assert losses[("relationship", "customers->orders")] == "not_approved"
    assert losses[("entity", "prospect")] == "not_approved"
    assert losses[("golden", "core:g5")] == "spans_subject_areas"


def test_include_sensitive_carries_the_column_and_its_dependants(model, tmp_path):
    report, view = _export(model, tmp_path / "out", "--include-sensitive")
    customers = next(t["table"] for t in view["tables"] if t["table"]["name"] == "customers")
    col = next(c for c in customers["columns"] if c["name"] == SECRET)
    assert col["choice_field"] == {"A": "Alpha", "B": "Beta"}
    assert len(customers["default_filters"]) == 2
    assert {"ref count", "refs per order"} <= _names(view, "metric")
    assert "Which customers are flagged?" in _names(view, "examples")
    assert "core:g3" in _names(view, "golden")
    assert {e["name"] for e in view["entities"]} == {"account", "client", "customer"}
    assert len(view["relationships"]) == 3
    assert [r["name"] for r in view["relationships"]] == [
        "orders->customers", "orders->customers#2", "orders->customers#3"]
    losses = _losses(report)
    assert "sensitive" not in losses.values()
    assert not {k for k, r in losses.items() if r == "depends_on_excluded"} - {
        ("choice_field", "orders.channel"), ("metric", "half bound")}  # those two are not about the column


@pytest.mark.parametrize("sql, engine", [
    ('SELECT "TAX_REF" FROM customers', "Snowflake"),
    ("SELECT `Tax_Ref` FROM customers", "Databricks"),
    ("SELECT [tax_ref] FROM customers", "SQLServer"),
    ("SELECT c.Tax_Ref AS r FROM customers AS c", "PostgreSQL"),
    ("SELECT CASE WHEN tax_ref IS NULL THEN 0 ELSE 1 END FROM customers", "SQLite"),
    ("SELECT r FROM (SELECT tax_ref AS r FROM customers) AS s", "MySQL"),
    ("SELECT region FROM customers WHERE tax_ref IN (SELECT 1)", "SQLite"),
    ("SELECT region FROM customers -- tax_ref", "SQLite"),
    ("SELECT region FROM WHERE ((", "SQLite"),           # unparseable counts as naming it
    ("SELECT region FROM customers", "NotAnEngine"),     # no dialect: read generically, still clean
])
def test_every_spelling_of_an_excluded_name_matches(sql, engine):
    expected = "NotAnEngine" not in engine
    assert X.sql_names_any(sql, frozenset({SECRET}), engine) is expected


@pytest.mark.parametrize("sql", ["SELECT tax_reference FROM customers", "SELECT region FROM customers"])
def test_a_longer_or_other_name_does_not_match(sql):
    assert X.sql_names_any(sql, frozenset({SECRET}), "SQLite") is False
    assert X.sql_names_any("SELECT FROM ((", frozenset(), "SQLite") is False


def test_an_unapproved_table_takes_what_names_it_with_it(model, tmp_path):
    doc = yaml.safe_load((model / f"subject_areas/{AREA}/tables/customers.yaml").read_text())
    doc["review_state"] = "unreviewed"
    (model / f"subject_areas/{AREA}/tables/customers.yaml").write_text(yaml.safe_dump(doc))
    report, view = _export(model, tmp_path / "out", "--include-sensitive")
    losses = _losses(report)
    assert losses[("table", "customers")] == "not_approved"
    assert losses[("relationship", "orders->customers")] == "depends_on_excluded"
    assert losses[("entity", "customer")] == "depends_on_excluded"
    assert losses[("example", "Orders by region?")] == "not_approved"


# ---------------------------------------------------------------------------
# Binding resolution
# ---------------------------------------------------------------------------


def test_missing_or_unparseable_binding_is_skipped_and_reported(model, tmp_path):
    report, view = _export(model, tmp_path / "out")
    assert _losses(report)[("metric", "snowflake only")] == "no_binding_for_target"
    assert _losses(report)[("metric", "broken")] == "unparseable_for_target"
    assert not {"snowflake only", "broken"} & _names(view, "metric")


def test_a_derived_metric_carries_its_expanded_binding(model, tmp_path):
    _, view = _export(model, tmp_path / "out")
    sql = next(m["sql"]["SQLite"] for m in view["metrics"] if m["metric"]["name"] == "average order value")
    org = load_datasource(model)
    metric = D.metric_index(org)["average order value"]
    assert "{" not in sql
    assert sql == D.expanded_bindings(metric, D.metric_index(org), org)["SQLite"]


def test_a_target_with_several_engines_degrades_rather_than_drops(model):
    org = load_datasource(model)
    both = X.Target("both", ("SQLite", "Snowflake"), _dump)
    view, losses = X.build_view(org, org.subject_area(AREA), both, X.ExportOptions(("both",)),
                                "SQLite", [], [])
    revenue = next(m for m in view.metrics if m.metric.name == "revenue")
    assert set(revenue.sql) == {"SQLite", "Snowflake"}
    entry = next(x for x in losses if x.name == "order count")
    assert (entry.outcome, entry.reason) == ("degraded", "no_binding_for_target")
    assert view.sql_engine == "SQLite"


def test_a_metric_no_engine_of_a_target_can_carry_is_reported_once(model):
    org = load_datasource(model)
    other = X.Target("other", ("Databricks", "Snowflake"), _dump)
    view, losses = X.build_view(org, org.subject_area(AREA), other, X.ExportOptions(("other",)),
                                "SQLite", [], [])
    metric: dict[str, list] = {}
    for x in losses:
        if x.kind == "metric":
            metric.setdefault(x.name, []).append(x)
    # No binding for either engine: one entry naming both.
    (count,) = metric["order count"]
    assert (count.outcome, count.reason) == ("dropped", "no_binding_for_target")
    assert "no Databricks binding" in count.detail and "no Snowflake binding" in count.detail
    # Missing on one engine and broken on the other: still one entry, named for the broken one.
    (half,) = metric["half bound"]
    assert (half.outcome, half.reason) == ("dropped", "unparseable_for_target")
    assert "no Databricks binding" in half.detail and "its Snowflake binding" in half.detail
    # Bound for one engine only: carried, with one degraded entry for the other.
    assert [(x.outcome, x.reason) for x in metric["snowflake only"]] == [
        ("degraded", "no_binding_for_target")]
    assert {m.metric.name for m in view.metrics} == {"revenue", "snowflake only"}


def test_sqlite_sql_reaches_a_snowflake_view_as_its_question_only(model, tmp_path):
    report, view = _export(model, tmp_path / "out", "--location", "DEMO.SALES_DATA", target="snowx")
    example = next(q for q in view["examples"] if q["name"] == "What is revenue?")
    assert example["sql"] is None and example["question"] == "What is revenue?"
    losses = _losses(report, "snowx")
    assert losses[("example", "What is revenue?")] == "sql_not_for_target"
    assert losses[("default_filter", "orders#1")] == "sql_not_for_target"
    assert losses[("relationship", "orders->customers#3")] == "sql_not_for_target"
    assert losses[("metric", "order count")] == "no_binding_for_target"
    assert _names(view, "metric") == {"revenue", "snowflake only"}
    orders = next(t for t in view["tables"] if t["table"]["name"] == "orders")
    assert orders["physical"] == ["DEMO", "SALES_DATA", "orders"]


# ---------------------------------------------------------------------------
# Ids, determinism, refusals
# ---------------------------------------------------------------------------


def test_export_id_is_stable_hex_across_processes():
    args = ("snowflake", "metric", X.qualified_name("sales", "metric", "revenue"))
    here = X.export_id(*args)
    code = f"from semantic_model.export import export_id; print(export_id{args!r})"
    there = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                           env={"PYTHONPATH": str(PKG_SRC)}).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{32}", here) and here == there
    assert X.export_id("databricks", *args[1:]) != here


def test_cross_engine_without_location_refuses_and_writes_nothing(model, tmp_path):
    out = tmp_path / "out"
    rc, text = _sm(str(model), "--target", "snowx", "--out", str(out))
    assert rc != 0 and "--location" in json.loads(text)["detail"]
    assert not out.exists()


@pytest.mark.parametrize("argv, needle", [
    (["--target", "nope"], "--target takes one of"),
    ([], "--target takes one of"),
    (["--target", "dump", "--area", "nope"], "--area takes one of"),
    (["--target", "snowx", "--location", "just_one"], "<database>.<schema>"),
])
def test_refusals_name_the_fix_and_write_nothing(model, tmp_path, argv, needle):
    out = tmp_path / "out"
    rc, text = _sm(str(model), "--out", str(out), *argv)
    assert rc == 2 and needle in json.loads(text)["detail"]
    assert not out.exists()


def test_a_model_that_does_not_load_is_refused(model, tmp_path):
    (model / f"subject_areas/{AREA}/metrics/revenue.yaml").write_text("name: [unclosed\n")
    rc, text = _sm(str(model), "--target", "dump", "--out", str(tmp_path / "out"))
    assert rc == 2 and "sm validate" in json.loads(text)["detail"]
    rc, text = _sm(str(tmp_path / "nowhere"), "--target", "dump", "--out", str(tmp_path / "out"))
    assert rc == 2 and not (tmp_path / "out").exists()


def test_several_engines_in_one_model_are_refused(model, tmp_path):
    doc = yaml.safe_load((model / "datasource.yaml").read_text())
    doc["storage_connections"].append({"name": "other", "storage_type": "Snowflake"})
    (model / "datasource.yaml").write_text(yaml.safe_dump(doc))
    rc, text = _sm(str(model), "--target", "dump", "--out", str(tmp_path / "out"))
    assert rc == 2 and "exactly one" in json.loads(text)["detail"]


def _tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def test_two_runs_give_byte_identical_reports_and_leave_the_model_alone(model, tmp_path):
    before = _tree(model)
    _export(model, tmp_path / "a", "--include-unreviewed")
    _export(model, tmp_path / "b", "--include-unreviewed")
    a, b = (tmp_path / d / "export-report.json" for d in "ab")
    assert a.read_bytes() == b.read_bytes()
    assert _tree(model) == before


def test_each_run_replaces_the_target_directory_whole_and_nothing_else(model, tmp_path):
    out = tmp_path / "out"
    (out / "dump").mkdir(parents=True)
    (out / "dump" / "old.json").write_text("{}")
    (out / "keep.txt").write_text("mine")
    (out / "snowx").write_text("a file where a directory goes")
    rc, _ = _sm(str(model), "--target", "dump", "--target", "snowx", "--location", "D.S", "--out", str(out))
    assert rc == 0
    assert not (out / "dump" / "old.json").exists() and (out / "keep.txt").read_text() == "mine"
    assert (out / "snowx" / f"{AREA}.json").exists()


def test_a_writer_cannot_escape_its_directory_or_invent_a_code(model, tmp_path, monkeypatch):
    escape = X.Target("esc", ("SQLite",), lambda v, o: X.WriterOutput({"../x": b""}))
    monkeypatch.setitem(X.TARGETS, "esc", escape)
    with pytest.raises(ValueError):
        _sm(str(model), "--target", "esc", "--out", str(tmp_path / "out"))
    with pytest.raises(ValueError):
        X.dropped("a", "metric", "m", "translated", "")
    # Two paths that normalise to one, from one area, are the writer's own bug.
    twice = X.Target("twice", ("SQLite",), lambda v, o: X.WriterOutput({"a/b": b"", "a//b": b""}))
    monkeypatch.setitem(X.TARGETS, "twice", twice)
    with pytest.raises(ValueError, match="twice"):
        _sm(str(model), "--target", "twice", "--area", AREA, "--out", str(tmp_path / "out"))


def test_two_areas_named_alike_by_a_target_refuse_before_anything_is_written(model, tmp_path):
    # The ops area becomes `Sales`: the dump target's files then differ from sales' only in case,
    # one file where case is ignored, and the real Databricks writer lowercases both to one name.
    (model / "subject_areas/ops/subject_area.yaml").write_text(
        yaml.safe_dump({"name": "Sales", "description": "Shipping."}), encoding="utf-8")
    out = tmp_path / "out"
    for target in ("dump", "databricks"):
        rc, text = _sm(str(model), "--target", target, "--location", "D.S", "--out", str(out))
        refusal = json.loads(text)
        assert rc == 2 and refusal["error"] == "export_refused", text
        assert "subject areas Sales and sales" in refusal["detail"] and target in refusal["detail"]
        assert not out.exists()
    # Either area alone still exports.
    rc, _ = _sm(str(model), "--target", "databricks", "--area", "Sales", "--location", "D.S",
                "--out", str(out))
    assert rc == 0


def test_a_target_named_twice_refuses(model, tmp_path):
    rc, text = _sm(str(model), "--target", "dump", "--target", "snowx", "--target", "dump",
                   "--out", str(tmp_path / "out"))
    assert rc == 2 and "--target dump is given more than once" in json.loads(text)["detail"]
    assert not (tmp_path / "out").exists()


def _edit(path: Path, change) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc = change(doc) or doc
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _put(model: Path, rel: str, doc) -> None:
    (model / rel).parent.mkdir(parents=True, exist_ok=True)
    (model / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _scan(out: Path, pattern: re.Pattern = SECRET_RE) -> None:
    for name, text in _emitted(out).items():
        assert not pattern.search(text), f"{name}: {pattern.search(text)}"


def _query_table(name: str, sql: str, *cols: str, **kw) -> dict:
    return {"name": name, "source_type": "sql", "sql": sql,
            "columns": [_col(c) for c in cols], **kw}


# ---------------------------------------------------------------------------
# A table defined by a query is SQL like any other
# ---------------------------------------------------------------------------


def test_a_table_whose_query_reads_a_sensitive_column_stays_home_with_what_names_it(model, tmp_path):
    _put(model, f"subject_areas/{AREA}/tables/customer_contacts.yaml", _query_table(
        "customer_contacts", f"SELECT id, {SECRET} AS contact FROM customers", "id", "contact"))
    # A query over that table, one over every column of the sensitive one, an unreadable one and
    # one over a table left home all go; a clean one travels with its query.
    _put(model, f"subject_areas/{AREA}/tables/contact_list.yaml", _query_table(
        "contact_list", "SELECT contact FROM customer_contacts", "contact"))
    _put(model, f"subject_areas/{AREA}/tables/everything.yaml", _query_table(
        "everything", "SELECT * FROM customers", "id"))
    _put(model, f"subject_areas/{AREA}/tables/unreadable.yaml", _query_table(
        "unreadable", "SELECT id FROM ((", "id"))
    _put(model, f"subject_areas/{AREA}/tables/drafts.yaml", {
        "name": "drafts", "review_state": "unreviewed", "columns": [_col("id")]})
    _put(model, f"subject_areas/{AREA}/tables/draft_view.yaml", _query_table(
        "draft_view", "SELECT id FROM drafts", "id"))
    _put(model, f"subject_areas/{AREA}/tables/big_orders.yaml", _query_table(
        "big_orders", "SELECT id, amount FROM orders WHERE amount > 100", "id", "amount"))
    _edit(model / f"subject_areas/{AREA}/relationships.yaml", lambda rels: rels + [
        {"from_table": "orders", "to_table": "customer_contacts", "from_column": "customer_id",
         "to_column": "id", "relationship": "many_to_one", "review_state": "approved"}])
    _edit(model / f"prompt_examples/{AREA}/examples.yaml", lambda exs: exs + [
        {"question": "Who can we reach?", "status": "confirmed",
         "sql": "SELECT contact FROM customer_contacts"}])
    out = tmp_path / "out"
    report, view = _export(model, out)
    _scan(out)
    _scan(out, re.compile(r"\bcontact\b"))
    gone = {"customer_contacts", "contact_list", "everything", "unreadable", "draft_view"}
    by = _losses(report)
    assert {n for (k, n), r in by.items() if k == "table" and r == "depends_on_excluded"} == gone
    big = next(t["table"] for t in view["tables"] if t["table"]["name"] == "big_orders")
    assert big["sql"] == "SELECT id, amount FROM orders WHERE amount > 100"
    assert not gone & {t["table"]["name"] for t in view["tables"]}
    assert by[("relationship", "orders->customer_contacts")] == "depends_on_excluded"
    assert by[("example", "Who can we reach?")] == "depends_on_excluded"

    # With the flags, the same tables travel.
    _, view = _export(model, tmp_path / "all", "--include-sensitive", "--include-unreviewed")
    assert {"customer_contacts", "contact_list", "everything", "draft_view"} <= {
        t["table"]["name"] for t in view["tables"]}


def test_a_table_query_for_another_engine_is_never_carried(model, tmp_path):
    _put(model, f"subject_areas/{AREA}/tables/big_orders.yaml", _query_table(
        "big_orders", "SELECT id, amount FROM orders WHERE amount > 100", "id", "amount"))
    report, view = _export(model, tmp_path / "out", "--location", "DEMO.SALES_DATA", target="snowx")
    assert _losses(report, "snowx")[("table", "big_orders")] == "sql_not_for_target"
    assert "big_orders" not in {t["table"]["name"] for t in view["tables"]}


# ---------------------------------------------------------------------------
# --out never reaches the model
# ---------------------------------------------------------------------------


def _refused(model: Path, out: Path, *flags: str, target: str = "dump") -> str:
    rc, text = _sm(str(model), "--target", target, "--out", str(out), *flags)
    assert rc == 2, text
    return json.loads(text)["detail"]


def test_an_out_that_would_replace_the_model_is_refused_and_the_model_survives(tmp_path):
    model = _write_model(tmp_path / "par" / "dump")
    before = _tree(model)
    detail = _refused(model, tmp_path / "par")
    assert "holds the semantic model" in detail and "--out" in detail
    assert _tree(model) == before and not (tmp_path / "par" / "export-report.json").exists()


@pytest.mark.parametrize("where", ["equal", "inside", "holds", "link", "link_inside", "golden"])
def test_an_out_overlapping_the_model_is_refused_before_any_write(model, tmp_path, where):
    golden = tmp_path / "kept_golden"
    if where == "golden":
        # The golden datasets may be a link to a directory kept beside the model.
        (model / "golden_datasets").rename(golden)
        (model / "golden_datasets").symlink_to(golden, target_is_directory=True)
    link = tmp_path / "link"
    link.symlink_to(model, target_is_directory=True)
    out = {"equal": model, "inside": model / "exports", "holds": model.parent, "link": link,
           "link_inside": link / "exports", "golden": golden / "exports"}[where]
    before = _tree(model), _tree(golden) if golden.exists() else None
    detail = _refused(model, out)
    assert "--out that neither holds the model nor lies inside it" in detail
    assert (_tree(model), _tree(golden) if golden.exists() else None) == before
    assert not (model / "exports").exists() and not (model / "dump").exists()


def test_an_out_that_is_a_file_is_refused(model, tmp_path):
    out = tmp_path / "out"
    out.write_text("not a directory")
    assert "is not a directory" in _refused(model, out)
    assert "is not a directory" in _refused(model, out / "below")
    assert out.read_text() == "not a directory"


def test_refusals_leave_an_existing_out_untouched(model, tmp_path, monkeypatch):
    out = tmp_path / "out"
    (out / "dump").mkdir(parents=True)
    (out / "dump" / "old.json").write_text("{}")
    (out / "export-report.json").write_text("old report")
    before = _tree(out)
    (model / "subject_areas/ops/subject_area.yaml").write_text(
        yaml.safe_dump({"name": "Sales", "description": "Shipping."}), encoding="utf-8")
    for flags in (["--target", "dump"],                                   # areas named alike
                  ["--target", "dump", "--target", "snowx", "--location", "D.S"],
                  ["--target", "snowx"],                                  # no --location
                  ["--target", "dump", "--area", "nope"],
                  ["--target", "nope"],
                  ["--target", "dump", "--target", "dump"]):
        rc, text = _sm(str(model), "--out", str(out), *flags)
        assert rc == 2, (flags, text)
        assert _tree(out) == before, flags


def _two_files(view: X.ExportView, options: X.ExportOptions) -> X.WriterOutput:
    # The second file's name is longer than any file system takes (255 bytes a name).
    return X.WriterOutput({f"{view.area}.json": b"{}", view.area + "q" * 300 + ".json": b"{}"})


def test_a_write_that_fails_part_way_is_refused_and_leaves_out_as_it_was(model, tmp_path, monkeypatch):
    out = tmp_path / "out"
    (out / "dump").mkdir(parents=True)
    (out / "dump" / "old.json").write_text("{}")
    (out / "export-report.json").write_text("old report")
    before = _tree(out)
    monkeypatch.setitem(X.TARGETS, "long", X.Target("long", ("SQLite",), _two_files))
    detail = _refused(model, out, "--target", "dump", target="long")
    assert detail.startswith("could not write the export under") and "nothing" in detail
    assert _tree(out) == before
    assert sorted(p.name for p in out.iterdir()) == ["dump", "export-report.json"]
    # A new --out is not left behind empty either.
    _refused(model, tmp_path / "fresh", target="long")
    assert not (tmp_path / "fresh").exists()


def test_a_failed_move_puts_every_target_back(model, tmp_path, monkeypatch):
    out = tmp_path / "out"
    _export(model, out)
    before = _tree(out)
    real = Path.rename
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] == 4:   # after the target's directory moved in, before the report did
            raise OSError(28, "No space left on device", str(target))
        return real(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    _edit(model / f"prompt_examples/{AREA}/examples.yaml",
          lambda exs: exs + [{"question": "A new one?", "status": "confirmed"}])
    detail = _refused(model, out)
    assert "No space left on device" in detail
    monkeypatch.setattr(Path, "rename", real)
    assert _tree(out) == before


def test_a_target_directory_that_is_a_link_is_replaced_not_followed(model, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep")
    out = tmp_path / "out"
    out.mkdir()
    (out / "dump").symlink_to(elsewhere, target_is_directory=True)
    _export(model, out)
    assert (elsewhere / "precious.txt").read_text() == "keep"
    assert not (out / "dump").is_symlink() and (out / "dump" / f"{AREA}.json").exists()


# ---------------------------------------------------------------------------
# The report never prints a sensitive name
# ---------------------------------------------------------------------------


def test_no_report_field_prints_a_sensitive_name(model, tmp_path, monkeypatch):
    question = f"Which customers have a {SECRET} on file?"
    _edit(model / f"prompt_examples/{AREA}/examples.yaml", lambda exs: exs + [
        {"question": question, "status": "confirmed", "sql": f"SELECT id FROM customers WHERE {SECRET} > 0"},
        {"question": f"Count of {SECRET} values", "status": "proposed", "sql": "SELECT 1"}])

    def terms(doc):
        doc["key_terminology"][SECRET.upper()] = "a reference number"
        doc["ambiguities"][1]["name"] = f"which {SECRET}"
    _edit(model / "datasource.yaml", terms)
    _put(model, f"subject_areas/{AREA}/metrics/secret_total.yaml",
         _metric(f"{SECRET} total", f"COUNT(customers.{SECRET})", source_tables=["customers"]))
    _put(model, f"subject_areas/{AREA}/metrics/secret_draft.yaml",
         _metric(f"{SECRET} draft", "COUNT(*)", state="unreviewed"))

    def write(view, options):
        # A writer's own loss quoting the name, in its name and its detail.
        loss = X.dropped(view.area, "metric", f"{SECRET} metric", "target_rule", f"reads {SECRET}")
        return X.WriterOutput({f"{view.area}.json": b"{}"}, (loss,))

    monkeypatch.setitem(X.TARGETS, "leaky", X.Target("leaky", ("SQLite",), write))
    out = tmp_path / "out"
    rc, text = _sm(str(model), "--target", "dump", "--target", "leaky", "--out", str(out))
    assert rc == 0, text
    assert not SECRET_RE.search(text)
    _scan(out)
    report = json.loads(text)
    ids = {(x["kind"], x["name"]): x for x in report["targets"]["dump"]["losses"]}
    for kind, name in (("example", question), ("example", f"Count of {SECRET} values"),
                       ("terminology", SECRET.upper()), ("ambiguity", f"which {SECRET}"),
                       ("metric", f"{SECRET} total"), ("metric", f"{SECRET} draft")):
        withheld = X.export_id("dump", kind, X.qualified_name(AREA, kind, name))
        assert (kind, withheld) in ids, (kind, name)
    leaky = report["targets"]["leaky"]["losses"]
    entry = next(x for x in leaky if x["reason"] == "target_rule")
    assert re.fullmatch(r"[0-9a-f]{32}", entry["name"]) and SECRET not in entry["detail"]


# ---------------------------------------------------------------------------
# Prose that mentions an excluded column travels blank
# ---------------------------------------------------------------------------


MENTION = f"See the {SECRET} column."


def _fill_prose(model: Path) -> None:
    """A mention of the sensitive column in every prose field the model's YAML can hold."""
    def datasource(doc):
        doc["description"] = MENTION
    _edit(model / "datasource.yaml", datasource)
    _edit(model / f"subject_areas/{AREA}/subject_area.yaml", lambda d: {**d, "description": MENTION})

    def orders(doc):
        doc.update(description=MENTION, caveats=[MENTION, "Amounts are net."],
                   column_groups={"keys": ["id", "customer_id"]},
                   column_group_descriptions={"keys": MENTION},
                   performance_hints={"estimated_row_count": 10, "recommended_filters": [MENTION],
                                      "indexes": [{"columns": ["id"]}]})
        for c in doc["columns"]:
            if c["name"] == "status":
                c.update(description=MENTION, caveats=[MENTION], unit=SECRET,
                         choice_field={"v": f"void ({SECRET})", "p": "paid"},
                         placeholders=[{"value": "-1", "reason": MENTION}],
                         denormalized_from={"table": "orders", "column": "status",
                                            "via": SECRET, "freshness": MENTION})
    _edit(model / f"subject_areas/{AREA}/tables/orders.yaml", orders)

    def customers(doc):
        doc["performance_hints"] = {"indexes": [{"columns": ["id", SECRET]}, {"columns": ["id"]}]}
    _edit(model / f"subject_areas/{AREA}/tables/customers.yaml", customers)
    _edit(model / f"subject_areas/{AREA}/metrics/revenue.yaml", lambda d: {
        **d, "description": MENTION, "calculation": MENTION, "business_question": MENTION,
        "caveats": [MENTION], "other_names": [f"{SECRET} sales", "sales"]})

    def rels(doc):
        doc[0].update(description=MENTION, for_questions_about=[MENTION])
    _edit(model / f"subject_areas/{AREA}/relationships.yaml", rels)
    _edit(model / f"subject_areas/{AREA}/entities/customer.yaml", lambda d: {
        **d, "description": MENTION, "value_format_hint": MENTION, "caveats": [MENTION],
        "plural": f"{SECRET}s holders", "other_names": [SECRET.upper()], "value_pattern": SECRET})


def test_prose_that_mentions_a_sensitive_column_is_blanked_and_its_item_kept(model, tmp_path):
    _fill_prose(model)
    out = tmp_path / "out"
    report, view = _export(model, out)
    _scan(out)
    assert view["description"] == "" and view["area_description"] == ""
    orders = next(t["table"] for t in view["tables"] if t["table"]["name"] == "orders")
    assert orders["description"] == "" and orders["caveats"] == ["Amounts are net."]
    assert orders["column_group_descriptions"] == {}
    status = next(c for c in orders["columns"] if c["name"] == "status")
    assert status["choice_field"] == {"p": "paid"} and "placeholders" not in status
    customers = next(t["table"] for t in view["tables"] if t["table"]["name"] == "customers")
    assert customers["performance_hints"]["indexes"] == [{"columns": ["id"], "is_unique": False}]
    revenue = next(m["metric"] for m in view["metrics"] if m["metric"]["name"] == "revenue")
    assert revenue["calculation"] == "" and revenue["other_names"] == ["sales"]
    assert {e["name"] for e in view["entities"]} >= {"customer"}
    degraded = {(x["kind"], x["name"]): x for x in report["targets"]["dump"]["losses"]
                if x["outcome"] == "degraded" and x["reason"] == "depends_on_excluded"}
    for kind, name in (("datasource", "acme"), ("subject_area", AREA), ("table", "orders"),
                       ("column", "orders.status"), ("metric", "revenue"),
                       ("relationship", "orders->customers"), ("entity", "customer")):
        assert "travels blank" in degraded[(kind, name)]["detail"], (kind, name)
    assert "performance_hints.recommended_filters" in degraded[("table", "orders")]["detail"]

    # With the flag nothing is blanked.
    _, view = _export(model, tmp_path / "all", "--include-sensitive")
    assert view["description"] == MENTION


def _sample_value(annotation, text: str):
    """A value of `annotation` holding `text` wherever a string goes."""
    import types
    import typing

    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is typing.Literal:
        return args[0]
    if origin in (typing.Union, types.UnionType):
        return _sample_value(next(a for a in args if a is not type(None)), text)
    if origin is typing.Annotated:
        return _sample_value(args[0], text)
    if origin is list:
        return [_sample_value(args[0], text)]
    if origin is dict:
        return {_sample_value(args[0], text): _sample_value(args[1], text)}
    if hasattr(annotation, "model_fields"):
        return _sample_model(annotation, text)
    if annotation is str:
        return text
    if annotation in (bool, int, float):
        return annotation(1)
    # A new shape of field must be taught here, not skipped: skipping is how a field goes unchecked.
    assert origin is None and annotation is typing.Any, f"no sample value for {annotation}"
    return None


def _sample_model(cls, text: str):
    return cls.model_construct(**{
        f: _sample_value(info.annotation, "plain" if f in X.NOT_PROSE else text)
        for f, info in cls.model_fields.items()})


def _strings(obj, path=""):
    """(field path, string) for every string `obj` holds, at any depth."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from _strings(x, path)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k, path)
            yield from _strings(v, path)
    elif hasattr(type(obj), "model_fields"):
        for f in type(obj).model_fields:
            yield from _strings(getattr(obj, f), f"{path}.{f}" if path else f)


@pytest.mark.parametrize("cls", ["Table", "Column", "Metric", "Relationship", "Entity"])
def test_every_prose_field_of_every_model_is_checked(cls):
    """Every model the view carries, and every model nested in one, filled with the name in every
    string it can hold. The fill walks the models' own fields, so a free-text field added to one
    later is filled here and must come back blank: the check has no list of prose to forget it
    from. Ambiguities, questions and terms are left out or kept whole, tested above."""
    from semantic_model import models

    model = getattr(models, cls)
    scrubbed, blanked = X.scrub_prose(_sample_model(model, MENTION), frozenset({SECRET}))
    leaks = [p for p, s in _strings(scrubbed) if SECRET_RE.search(s)]
    assert leaks == [] and blanked


def test_the_fields_left_out_of_the_prose_check_are_exactly_the_reviewed_ones():
    # Each is a name, SQL or a key, checked where it is used. Adding one here is a decision a
    # reviewer sees: a field listed by mistake would carry its prose unchecked.
    assert X.NOT_PROSE == {
        "name", "schema_name", "storage_connection", "sql", "grain", "unique_keys",
        "default_filters", "parent_table", "child_table", "column_groups", "inherits_columns_from",
        "columns", "signed_off_by", "signed_off_at", "signed_off_role", "foreign_key",
        "value_transform", "bindings", "source_tables", "base_metrics", "subject_areas",
        "primary_table", "non_additive_dimensions", "inner_grain", "from_table", "to_table",
        "from_column", "to_column", "from_schema", "to_schema", "on", "maps_to", "key_components",
        "parent_entity", "table", "column", "where", "indexes"}


# ---------------------------------------------------------------------------
# A star returns what it does not name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql, engine, expected", [
    ("SELECT * FROM customers", "Snowflake", True),
    ('SELECT * FROM SALES_DATA."CUSTOMERS"', "Snowflake", True),
    ("SELECT c.* FROM customers AS c", "PostgreSQL", True),
    ("SELECT o.id, c.* FROM orders o JOIN customers c ON o.customer_id = c.id", "SQLite", True),
    ("SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id", "SQLite", True),
    ("SELECT s.* FROM (SELECT * FROM customers) AS s", "Databricks", True),
    ("WITH x AS (SELECT * FROM customers) SELECT id FROM x", "Snowflake", True),
    ("SELECT * FROM orders UNION ALL SELECT * FROM customers", "SQLite", True),
    ("SELECT OBJECT_CONSTRUCT(*) FROM customers", "Snowflake", True),
    ("SELECT z.* FROM customers AS c", "SQLite", True),          # an alias that resolves to nothing
    ("SELECT COUNT(*) FROM customers", "SQLite", False),
    ("SELECT o.* FROM orders o JOIN customers c ON o.customer_id = c.id", "SQLite", False),
    ("SELECT * FROM (SELECT id FROM customers) AS s", "Snowflake", False),
    ("WITH x AS (SELECT id FROM customers) SELECT * FROM x", "Databricks", False),
    ("SELECT id FROM orders WHERE EXISTS (SELECT * FROM customers)", "SQLite", False),
    ("SELECT * FROM orders", "SQLite", False),
])
def test_a_star_over_a_table_with_a_sensitive_column_names_it(sql, engine, expected):
    got = X.sql_names_any(sql, frozenset({SECRET}), engine, frozenset({"customers"}))
    assert got is expected


def test_a_star_query_stays_home_end_to_end(model, tmp_path):
    _edit(model / f"prompt_examples/{AREA}/examples.yaml", lambda exs: exs + [
        {"question": "Everything about customers", "status": "confirmed",
         "sql": "SELECT * FROM customers"},
        # Every column of `orders`, one of which is unreviewed, so that star goes too.
        {"question": "Every order", "status": "confirmed", "sql": "SELECT * FROM orders"},
        {"question": "Regions", "status": "confirmed",
         "sql": "SELECT * FROM (SELECT id, region FROM customers) AS s"}])
    _edit(model / "golden_datasets/core.yaml", lambda d: d["test_cases"].append(
        {"id": "g6", "query": "All customers", "expected": {
            "sql": "SELECT c.* FROM customers c", "sql_confirmed": True}}))
    report, view = _export(model, tmp_path / "out")
    losses = _losses(report)
    assert losses[("example", "Everything about customers")] == "depends_on_excluded"
    assert losses[("golden", "core:g6")] == "depends_on_excluded"
    assert losses[("example", "Every order")] == "depends_on_excluded"
    assert "Regions" in _names(view, "examples")


# ---------------------------------------------------------------------------
# What `carried` counts
# ---------------------------------------------------------------------------


def test_an_item_several_files_of_a_target_drop_comes_off_carried_once(model, tmp_path, monkeypatch):
    def write(view, options):
        # Three artifacts of one target, each reporting the same metric it left out.
        loss = X.dropped(view.area, "metric", "revenue", "target_rule", "a test rule")
        return X.WriterOutput({f"{view.area}.json": b"{}"}, (loss, loss, loss))

    monkeypatch.setitem(X.TARGETS, "three", X.Target("three", ("SQLite",), write))
    rc, text = _sm(str(model), "--target", "dump", "--target", "three", "--out", str(tmp_path / "o"))
    assert rc == 0, text
    carried = {t: json.loads(text)["targets"][t]["carried"]["metric"] for t in ("dump", "three")}
    assert carried["three"] == carried["dump"] - 1


def test_writer_losses_and_target_flags_reach_the_report(model, tmp_path, monkeypatch):
    def write(view, options):
        assert view.id("metric", "revenue") == X.export_id(
            "flav", "metric", X.qualified_name(AREA, "metric", "revenue"))
        assert view.approved("metric", "revenue")
        losses = (X.dropped(view.area, "metric", "revenue", "target_rule", "a test rule"),)
        return X.WriterOutput({f"{view.area}.txt": options.target_args["flavour"].encode()}, losses)

    def flags(sp):
        sp.add_argument("--flavour", default="plain")

    # A writer slice registers its target when its module is imported.
    monkeypatch.setattr(X, "_TARGET_MODULES", ("derived",))
    monkeypatch.setitem(X.TARGETS, "flav", None)
    X.register(X.Target("flav", ("SQLite",), write, flags))
    out = tmp_path / "out"
    rc, text = _sm(str(model), "--target", "flav", "--flavour", "spicy", "--area", AREA, "--out", str(out))
    report = json.loads(text)
    assert rc == 0 and (out / "flav" / f"{AREA}.txt").read_text() == "spicy"
    assert report["options"]["target_args"] == {"flavour": "spicy"}
    section = report["targets"]["flav"]
    assert section["files"] == [f"flav/{AREA}.txt"]
    assert _losses(report, "flav")[("metric", "revenue")] == "target_rule"
    plain, _ = _export(model, tmp_path / "plain")
    assert section["carried"]["metric"] == plain["targets"]["dump"]["carried"]["metric"] - 1
    assert report["does_not_travel"] == list(X.DOES_NOT_TRAVEL)


def test_signers_do_not_travel(model, tmp_path):
    path = model / f"subject_areas/{AREA}/metrics/revenue.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.update(signed_off_by="you@example.com", signed_off_at="2026-01-01T00:00:00Z", signed_off_role="owner")
    path.write_text(yaml.safe_dump(doc))
    out = tmp_path / "out"
    _, view = _export(model, out)
    revenue = next(m for m in view["metrics"] if m["metric"]["name"] == "revenue")["metric"]
    assert revenue["signed_off_at"] == "2026-01-01T00:00:00Z"
    assert all("you@example.com" not in text for text in _emitted(out).values())


# ---------------------------------------------------------------------------
# The privacy contract and the bundled sample
# ---------------------------------------------------------------------------


def test_the_privacy_scan_covers_the_export_module():
    spec = importlib.util.spec_from_file_location("privacy", REPO / "tests" / "test_privacy_no_network.py")
    privacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(privacy)
    module = PKG_SRC / "semantic_model" / "export.py"
    assert module in privacy.SCRIPTS
    assert not any(rx.search(module.read_text(encoding="utf-8")) for rx in privacy._FORBIDDEN_RE)


def test_the_sample_validates_and_binds_every_metric_for_snowflake_and_databricks(tmp_path):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli.main(["validate", str(SAMPLE)]) == 0, buf.getvalue()
    org = load_datasource(SAMPLE)
    idx = D.metric_index(org)
    metrics = [m for sa in org.subject_areas for m in sa.metrics]
    assert len(metrics) == 8
    for m in metrics:
        for engine in ("Snowflake", "Databricks"):
            sql, reason = X.resolve_binding(m, engine, idx, org)
            assert reason is None and "{" not in sql, (m.name, engine)

    out = tmp_path / "out"
    rc, text = _sm(str(SAMPLE), "--target", "snowx", "--location", "DEMO.SALES_DATA", "--out", str(out))
    assert rc == 0 and json.loads(text)["targets"]["snowx"]["carried"]["metric"] == 8


# ---------------------------------------------------------------------------
# What a writer can read beyond the items: datasource facts, review times, withheld columns,
# golden detail, physical names in SQL, and artifact flags
# ---------------------------------------------------------------------------


def _capture(monkeypatch, name: str = "grab", engines: tuple[str, ...] = ("SQLite",)) -> list:
    """A target that keeps every view it is handed, so a test reads the view object itself."""
    views: list = []

    def write(view, options):
        views.append(view)
        return X.WriterOutput({f"{view.area}.txt": b"x"})

    monkeypatch.setitem(X.TARGETS, name, X.Target(name, engines, write))
    return views


def _view(model: Path, out: Path, views: list, *flags: str, target: str = "grab") -> X.ExportView:
    rc, text = _sm(str(model), "--target", target, "--out", str(out), *flags)
    assert rc == 0, text
    return next(v for v in views if v.area == AREA)


def test_the_datasource_and_area_facts_reach_the_view(model, tmp_path, monkeypatch):
    _edit(model / "datasource.yaml", lambda d: d.update(fiscal_year_start_month=4))
    _edit(model / f"subject_areas/{AREA}/subject_area.yaml",
          lambda d: d.update(default_time_window="last 90 days"))
    views = _capture(monkeypatch)
    view = _view(model, tmp_path / "a", views)
    assert (view.fiscal_year_start_month, view.default_time_window) == (4, "last 90 days")

    # Free text like a description: a mention of a column left home blanks it, reported.
    _edit(model / f"subject_areas/{AREA}/subject_area.yaml",
          lambda d: d.update(default_time_window=f"last 90 days by {SECRET}"))
    views.clear()
    view = _view(model, tmp_path / "b", views)
    assert view.default_time_window is None
    report = json.loads((tmp_path / "b" / "export-report.json").read_text())
    entry = next(x for x in report["targets"]["grab"]["losses"]
                 if (x["kind"], x["name"]) == ("subject_area", AREA))
    assert entry["reason"] == "depends_on_excluded" and "default_time_window" in entry["detail"]
    _scan(tmp_path / "b")
    # A model that sets neither carries the defaults.
    views.clear()
    view = _view(_write_model(tmp_path / "art2" / "acme"), tmp_path / "c", views)
    assert (view.fiscal_year_start_month, view.default_time_window) == (1, None)


SIGNER, ROLE = "you@example.com", "demo-steward"
AT = "2026-03-01T09:00:00Z"


def _reachable_strings(obj, seen=None) -> list[str]:
    """Every string reachable from `obj`: dataclass attributes (fields or not), model fields,
    containers. What a writer could read, however it reads."""
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return []
    seen.add(id(obj))
    if isinstance(obj, str):
        return [obj]
    if hasattr(obj, "model_dump"):
        return _reachable_strings(obj.model_dump(mode="json"), seen)
    if dataclasses.is_dataclass(obj):
        return _reachable_strings(vars(obj), seen)
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in (*_reachable_strings(k, seen),
                                                    *_reachable_strings(v, seen))]
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [s for x in obj for s in _reachable_strings(x, seen)]
    return []


def _sign_everything(model: Path) -> None:
    signer = {"signed_off_by": SIGNER, "signed_off_at": AT, "signed_off_role": ROLE}

    def table(doc):
        doc.update(signer)
        amount = next(c for c in doc["columns"] if c["name"] == "amount")
        amount.update(signer, placeholders=[{"value": "-1", "meaning": "not_set",
                                             "review_state": "approved", **signer}])
    _edit(model / f"subject_areas/{AREA}/tables/orders.yaml", table)
    for rel in ("metrics/revenue.yaml", "metrics/stale_total.yaml", "entities/customer.yaml"):
        _edit(model / f"subject_areas/{AREA}/{rel}", lambda d: d.update(signer))
    _edit(model / f"subject_areas/{AREA}/relationships.yaml", lambda rs: rs[0].update(signer))
    _edit(model / f"prompt_examples/{AREA}/examples.yaml", lambda exs: exs[0].update(signer))
    _edit(model / "golden_datasets" / "core.yaml",
          lambda d: d["test_cases"][0].update(confirmed_by={"method": SIGNER, "at": AT}))


def test_each_item_says_when_it_was_signed_off_and_never_who(model, tmp_path, monkeypatch):
    _sign_everything(model)
    views = _capture(monkeypatch)
    view = _view(model, tmp_path / "a", views, "--include-unreviewed")
    for kind, name in (("table", "orders"), ("column", "orders.amount"), ("metric", "revenue"),
                       ("relationship", "orders->customers"), ("entity", "customer"),
                       ("example", "What is revenue?"), ("golden", "core:g1")):
        assert view.approved(kind, name) and view.signed_off_at(kind, name) == AT, (kind, name)
    # Approved with no recorded time, and a stale item carried by the flag: no time either way.
    assert view.approved("metric", "order count") and view.signed_off_at("metric", "order count") is None
    assert not view.approved("metric", "stale total")
    assert view.signed_off_at("metric", "stale total") is None
    assert view.signed_off_at("ambiguity", "which amount") is None
    # No signer is reachable from the view, by any path a writer could take.
    strings = _reachable_strings(view)
    assert AT in strings
    assert not [s for s in strings if SIGNER in s or ROLE in s]
    # Including through a placeholder, whose meaning is signed off on its own.
    orders = next(t.table for t in view.tables if t.table.name == "orders")
    (mark,) = next(c for c in orders.columns if c.name == "amount").placeholders
    assert (mark.signed_off_at, mark.signed_off_by, mark.signed_off_role) == (AT, None, None)


def test_a_sensitive_column_is_named_to_a_writer_only_through_withheld_columns(model, tmp_path, monkeypatch):
    views = _capture(monkeypatch)
    view = _view(model, tmp_path / "a", views)
    tables = {bare: t for t in view.tables for bare in [t.table.name]}
    assert tables["customers"].withheld_columns == (SECRET,)
    assert tables["customers"].withheld_count == 1 and view.withheld_count == 1
    # Not approved is not sensitive: `channel` stays home, and is not withheld.
    assert tables["orders"].withheld_columns == () and tables["orders"].withheld_count == 0
    # Off the dataclass fields, so a whole-view dump does not carry the names, and a copy made
    # with `replace` drops them rather than carrying them somewhere unexpected.
    assert "withheld" not in {f.name for f in dataclasses.fields(X.ExportTable)}
    assert not SECRET_RE.search(json.dumps(_jsonable(view)))
    copy = dataclasses.replace(tables["customers"])
    assert copy.withheld_columns == () and copy.withheld_count == 0
    # Carried by the flag, a column is not withheld.
    views.clear()
    view = _view(model, tmp_path / "b", views, "--include-sensitive")
    assert view.withheld_count == 0 and all(t.withheld_columns == () for t in view.tables)


def _golden_detail(doc):
    doc["test_cases"][0].update(match="bounded", bounds={"min_rows": 1, "max_value": 9.5},
                                must_filter=["orders.status"], tags=["smoke", f"{SECRET} check"])
    doc["test_cases"][0]["expected"].update(chart_type="bar", validation_notes="Checked by hand.",
                                            data_shape=f"one row per {SECRET}")


def test_a_golden_item_carries_its_answer_key_detail_typed_and_scrubbed(model, tmp_path, monkeypatch):
    _edit(model / "golden_datasets" / "core.yaml", _golden_detail)
    views = _capture(monkeypatch)
    view = _view(model, tmp_path / "a", views)
    (g1,) = [q for q in view.golden if q.name == "core:g1"]
    assert (g1.match, g1.must_filter, g1.tags) == ("bounded", ("orders.status",), ("smoke",))
    assert (g1.bounds.min_rows, g1.bounds.max_rows, g1.bounds.max_value) == (1, None, 9.5)
    assert (g1.chart_type, g1.data_shape, g1.validation_notes) == ("bar", None, "Checked by hand.")
    assert g1.detail_fields == ("match", "bounds", "must_filter", "tags", "expected.chart_type",
                                "expected.data_shape", "expected.validation_notes")
    report = json.loads((tmp_path / "a" / "export-report.json").read_text())
    entries = {x["reason"]: x for x in report["targets"]["grab"]["losses"] if x["kind"] == "golden"
               and x["name"] == "core:g1"}
    assert set(entries) == {"depends_on_excluded", "detail_lost"}
    assert entries["depends_on_excluded"]["outcome"] == "degraded"
    assert entries["detail_lost"]["detail"].endswith("has no field in this target's files and stays in agami")
    _scan(tmp_path / "a")
    # An example carries no detail, and a plain golden item reads as its defaults.
    assert view.examples and all(q.match is None and q.detail_fields == () for q in view.examples)
    views.clear()
    (g1,) = _view(_write_model(tmp_path / "art2" / "acme"), tmp_path / "b", views).golden
    assert (g1.match, g1.bounds, g1.tags, g1.detail_fields) == ("exact", None, (), ())


def test_a_writer_that_carries_golden_detail_takes_it_off_the_report(model, tmp_path, monkeypatch):
    _edit(model / "golden_datasets" / "core.yaml", _golden_detail)
    declared: dict = {}

    def write(view, options):
        return X.WriterOutput({f"{view.area}.txt": b"x"}, (), declared.get(view.area, {}))

    monkeypatch.setitem(X.TARGETS, "carry", X.Target("carry", ("SQLite",), write))

    def detail(out: Path):
        rc, text = _sm(str(model), "--target", "carry", "--out", str(out))
        assert rc == 0, text
        lost = [x for x in json.loads(text)["targets"]["carry"]["losses"]
                if x["reason"] == "detail_lost" and x["name"] == "core:g1"]
        return lost[0]["detail"] if lost else None

    assert detail(tmp_path / "a").startswith("its match, bounds, must_filter, tags, ")
    declared[AREA] = {"core:g1": frozenset({"match", "bounds"})}
    assert detail(tmp_path / "b").startswith("its must_filter, tags, expected.chart_type, ")
    declared[AREA] = {"core:g1": frozenset(X.GOLDEN_DETAIL_FIELDS)}
    assert detail(tmp_path / "c") is None
    # A field outside the closed set, or an item the view does not hold, is a writer bug.
    for bad in ({"core:g1": frozenset({"bound"})}, {"core:nope": frozenset({"match"})}):
        declared[AREA] = bad
        with pytest.raises(ValueError, match="declared carried golden detail"):
            _sm(str(model), "--target", "carry", "--out", str(tmp_path / "d"))


_PHYSICAL = {"orders": "`main`.`sales_data`.`orders`", "customers": "`main`.`sales_data`.`customers`"}
# Where each model table lives: a reference that names a schema or catalog must name this one.
_HOMES = {t: X.PhysicalName("main", "sales_data", t) for t in _PHYSICAL}


def _physical(engine: str) -> dict:
    # The caller quotes the new names for its engine, and the rewritten SQL must parse there.
    return _PHYSICAL if engine == "Databricks" else {
        k: v.replace("`", '"') for k, v in _PHYSICAL.items()}


@pytest.mark.parametrize("sql, expected", [
    ("SELECT SUM(amount) FROM orders", "SELECT SUM(amount) FROM `main`.`sales_data`.`orders`"),
    # An alias, a case variant and a column qualified by the table's own name stay as written.
    ("SELECT o.id, orders.amount FROM orders o JOIN Customers AS c ON o.cid = c.id",
     "SELECT o.id, orders.amount FROM `main`.`sales_data`.`orders` o "
     "JOIN `main`.`sales_data`.`customers` AS c ON o.cid = c.id"),
    # A CTE named like a table shadows it; inside its own body the name is still the table.
    ("WITH orders AS (SELECT id FROM orders) SELECT id FROM orders",
     "WITH orders AS (SELECT id FROM `main`.`sales_data`.`orders`) SELECT id FROM orders"),
    ("WITH orders AS (SELECT id FROM customers) SELECT id FROM orders",
     "WITH orders AS (SELECT id FROM `main`.`sales_data`.`customers`) SELECT id FROM orders"),
    ("SELECT id FROM orders WHERE cid IN (SELECT id FROM customers)  -- keep me",
     "SELECT id FROM `main`.`sales_data`.`orders` WHERE cid IN "
     "(SELECT id FROM `main`.`sales_data`.`customers`)  -- keep me"),
    ("SELECT a FROM SALES_DATA.orders UNION ALL SELECT a FROM customers",
     "SELECT a FROM `main`.`sales_data`.`orders` UNION ALL SELECT a FROM `main`.`sales_data`.`customers`"),
    ("SELECT 1 FROM shipments", None),                              # a table the mapping lacks
    ("SELECT SALES_DATA.orders.amount FROM SALES_DATA.orders", None),  # schema-qualified column
    ("SELECT FROM ((", None),                                       # does not parse
])
@pytest.mark.parametrize("engine", ["Databricks", "Snowflake"])
def test_rewrite_table_refs_qualifies_tables_by_token_position(sql, expected, engine):
    if expected is not None and engine != "Databricks":
        expected = expected.replace("`", '"')
    assert X.rewrite_table_refs(sql, _physical(engine), engine, _HOMES) == expected


def test_rewrite_table_refs_refuses_a_mapping_whose_names_fold_together():
    homes = {"orders": _HOMES["orders"], "ORDERS": _HOMES["orders"]}
    assert X.rewrite_table_refs("SELECT 1 FROM orders", {"orders": "a", "ORDERS": "b"}, "Databricks",
                                homes) is None


def test_a_databricks_artifact_declares_its_own_flags(model, tmp_path, monkeypatch):
    from semantic_model import export_databricks as DB

    X.targets()
    seen = []

    def artifact(view, options):
        seen.append(options.target_args.get("genie_demo"))
        return X.WriterOutput({}, (), {})

    def flags(sp):
        sp.add_argument("--genie-demo", dest="genie_demo", default=argparse.SUPPRESS)

    monkeypatch.setattr(DB, "ARTIFACTS", [artifact])
    monkeypatch.setattr(DB, "ARTIFACT_ARGUMENTS", {})
    DB.register_artifact(artifact, flags)
    base = ("--target", "databricks", "--location", "DEMO.SALES_DATA", "--area", AREA)
    rc, text = _sm(str(model), *base, "--genie-demo", "on", "--out", str(tmp_path / "a"))
    assert rc == 0, text
    assert seen == ["on"] and json.loads(text)["options"]["target_args"] == {"genie_demo": "on"}
    rc, text = _sm(str(model), *base, "--out", str(tmp_path / "b"))
    assert seen[-1] is None and json.loads(text)["options"]["target_args"] == {}


def test_databricks_merges_the_golden_detail_its_artifacts_carry(monkeypatch):
    from semantic_model import export_databricks as DB

    def one(view, options):
        return X.WriterOutput({"a.json": b"1"}, (), {"core:g1": frozenset({"bounds"})})

    def two(view, options):
        return X.WriterOutput({"b.json": b"2"}, (), {"core:g1": frozenset({"tags"}),
                                                     "core:g4": frozenset({"match"})})

    monkeypatch.setattr(DB, "ARTIFACTS", [one, two])
    out = DB.write_databricks(None, X.ExportOptions(targets=("databricks",)))
    assert dict(out.carried_golden_fields) == {"core:g1": frozenset({"bounds", "tags"}),
                                               "core:g4": frozenset({"match"})}


# ---------------------------------------------------------------------------
# A reference is the model's table only when its schema, catalog and case say so
# ---------------------------------------------------------------------------

_SF = {"orders": "DB.S.ORDERS", "customers": "DB.S.CUSTOMERS"}
_SF_HOMES = {t: X.PhysicalName("DB", "S", t) for t in _SF}


@pytest.mark.parametrize("sql, engine", [
    # Another schema's or catalog's table of the same name is another table.
    ("SELECT amount FROM archive.orders", "Snowflake"),
    ("SELECT SUM(amount) AS s FROM otherdb.otherschema.orders", "Snowflake"),
    ("SELECT amount FROM archive.orders", "Databricks"),
    ("SELECT amount FROM otherdb.S.orders", "Snowflake"),
    # Quoted lower case is a different, case-sensitive object in Snowflake.
    ('SELECT amount FROM "orders"', "Snowflake"),
    # Table-valued and dynamic forms name no plain table.
    ("SELECT amount FROM IDENTIFIER('orders')", "Snowflake"),
    ("SELECT amount FROM a.b.c.orders", "Databricks"),
    # Not a query that reads, or not one statement.
    ("INSERT INTO orders SELECT * FROM customers", "Snowflake"),
])
def test_rewrite_table_refs_refuses_another_schemas_table_and_dynamic_names(sql, engine):
    assert X.rewrite_table_refs(sql, _SF, engine, _SF_HOMES) is None


@pytest.mark.parametrize("sql, expected, engine", [
    # The table's own schema and catalog, in any spelling that reaches them, are the table.
    ("SELECT amount FROM s.orders", "SELECT amount FROM DB.S.ORDERS", "Snowflake"),
    ("SELECT amount FROM db.s.orders", "SELECT amount FROM DB.S.ORDERS", "Snowflake"),
    ('SELECT amount FROM "ORDERS"', "SELECT amount FROM DB.S.ORDERS", "Snowflake"),
    ("SELECT `amount` FROM `Orders`", "SELECT `amount` FROM DB.S.ORDERS", "Databricks"),
    # An alias named like another table is the alias; a column named like a table is a column.
    ("SELECT customers.id FROM orders customers", "SELECT customers.id FROM DB.S.ORDERS customers",
     "Snowflake"),
    ("SELECT orders.customers FROM orders", "SELECT orders.customers FROM DB.S.ORDERS", "Snowflake"),
])
def test_rewrite_table_refs_rewrites_the_models_own_table(sql, expected, engine):
    assert X.rewrite_table_refs(sql, _SF, engine, _SF_HOMES) == expected


def test_rewrite_table_refs_refuses_a_name_that_breaks_the_statement():
    # The caller's name is inserted verbatim; SQL that no longer parses is never returned.
    assert X.rewrite_table_refs("SELECT 1 FROM orders", {"orders": "DB.(", "customers": "c"},
                                "Snowflake", _SF_HOMES) is None


_DECLARED = {"orders": ("id", "customer_id", "amount"), "customers": ("id", "region")}


@pytest.mark.parametrize("sql", [
    "SELECT region, COUNT(*) AS n FROM customers GROUP BY region",
    "SELECT c.region, SUM(o.amount) AS rev FROM orders AS o JOIN customers AS c "
    "ON o.customer_id = c.id GROUP BY c.region ORDER BY rev DESC",
    "WITH big AS (SELECT id, amount FROM orders WHERE amount > 0) SELECT SUM(amount) AS s FROM big",
    "SELECT r FROM (SELECT region AS r FROM customers) t ORDER BY r",
    "SELECT COUNT(*) AS n FROM orders",
    "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers)",
])
def test_undeclared_reads_clears_sql_that_reads_only_declared_columns(sql):
    assert X.undeclared_reads(sql, _DECLARED, "Snowflake", _SF_HOMES) is None


@pytest.mark.parametrize("sql, why", [
    ("SELECT region, salary FROM customers", "salary"),
    ("SELECT c.salary FROM customers c", "salary"),
    ("SELECT OBJECT_CONSTRUCT(*) AS o FROM orders", "*"),
    ("SELECT * FROM customers", "*"),
    ("SELECT a.region FROM (SELECT * FROM customers) a", "*"),
    ("SELECT TO_JSON(c) AS j FROM customers c", "c"),
    ("SELECT id FROM orders JOIN customers ON orders.customer_id = customers.id", "several"),
    ("SELECT region FROM orders JOIN customers USING (region)", "USING"),
    ("SELECT region FROM orders NATURAL JOIN customers", "NATURAL"),
    ("SELECT amount FROM archive.orders", "amount"),
    ("SELECT region AS salary FROM customers WHERE salary > 0", "salary"),
    ("SELECT f.value FROM orders o, LATERAL FLATTEN(input => o.id) f", "value"),
])
def test_undeclared_reads_refuses_anything_it_cannot_place_on_a_declared_column(sql, why):
    reason = X.undeclared_reads(sql, _DECLARED, "Snowflake", _SF_HOMES)
    assert reason is not None and why in reason, reason


# ---------------------------------------------------------------------------
# Review records, tables shared across areas, flags for an unselected target
# ---------------------------------------------------------------------------


def test_a_kind_with_no_review_state_is_approved_but_never_reviewed(model, tmp_path):
    _, view = _export(model, tmp_path / "out")
    term = view["terminology"][0][0]
    v = X.ExportView(**{**{f.name: None for f in dataclasses.fields(X.ExportView)},
                        "unreviewed": frozenset({("metric", "draft")})})
    assert v.approved("terminology", term) and not v.reviewed("terminology", term)
    assert v.approved("ambiguity", "which amount") and not v.reviewed("ambiguity", "which amount")
    assert v.reviewed("metric", "revenue") and not v.reviewed("metric", "draft")


def test_a_table_two_areas_carry_is_claimed_by_the_first(model, tmp_path, monkeypatch):
    seen = {}

    def write(view, options):
        seen[view.area] = {bare: view.claimed_by(et.physical) for et in view.tables
                           for bare in [et.table.name]}
        return X.WriterOutput({}, (), {})

    # ops also lists sales' orders table, so both views carry it.
    _edit(model / "subject_areas/ops/subject_area.yaml", lambda d: {**d, "tables": [
        {"storage_connection": "acme_db", "schema": "SALES_DATA", "table": "orders"}]})
    monkeypatch.setitem(X.TARGETS, "claim", X.Target("claim", ("SQLite",), write))
    rc, text = _sm(str(model), "--target", "claim", "--out", str(tmp_path / "out"))
    assert rc == 0, text
    first, second = sorted(seen)
    assert set(seen[first].values()) == {None}
    shared = set(seen[first]) & set(seen[second])
    assert shared and all(seen[second][t] == first for t in shared)
    assert all(seen[second][t] is None for t in set(seen[second]) - shared)


@pytest.mark.parametrize("flag, owner", [
    ("--genie-hide-sensitive", "databricks"), ("--uc-comments", "databricks"),
    ("--pages-publish", "databricks"), ("--snowflake-mcp-server", "snowflake"),
    ("--snowflake-certification-tags", "snowflake"),
])
def test_a_flag_for_a_target_not_selected_is_refused_before_writing(model, tmp_path, flag, owner):
    out = tmp_path / "out"
    rc, text = _sm(str(model), "--target", "ossie", "--location", "DEMO.SALES_DATA", flag,
                   "--out", str(out))
    assert rc == 2, text
    detail = json.loads(text)["detail"]
    assert flag in detail and f"--target {owner}" in detail
    assert not out.exists()


def test_a_database_named_local_is_refused_with_its_cause(tmp_path):
    # agami keeps its state in <artifacts dir>/local/, so a database named `local` is that folder
    # and the export folder agami-export uses lies inside it.
    model = _write_model(tmp_path / "art" / "local")
    detail = _refused(model, model / "exports" / "context" / "local")
    assert detail.startswith("the database is named local") and "rename the database" in detail
    # It still exports to a folder outside it.
    report, _ = _export(model, tmp_path / "elsewhere")
    assert report["source"]["datasource"] == "acme"


def test_the_report_opens_with_a_summary_and_the_models_validation(model, tmp_path):
    rc, text = _sm(str(model), "--target", "dump", "--out", str(tmp_path / "o"))
    assert rc == 0
    report = json.loads(text)
    assert list(report)[:3] == ["summary", "model_validation", "source"]
    section = report["targets"]["dump"]
    assert report["summary"] == {"dump": section["summary"]}
    # The summary counts the entries, by reason and outcome; no entry is lost to it.
    total = sum(n for counts in section["summary"].values() for n in counts.values())
    assert total == len(section["losses"])
    assert section["summary"]["not_approved"]["dropped"] == sum(
        1 for x in section["losses"] if (x["reason"], x["outcome"]) == ("not_approved", "dropped"))
    # The model has validation errors: the export still runs, and says so with codes only.
    v = report["model_validation"]
    assert v["checked"] is True and v["errors"] > 0 and "derived_metric" in v["error_codes"]
    assert "sm validate" in v["detail"]
    assert not SECRET_RE.search(json.dumps(v))


def test_a_loss_reported_word_for_word_twice_is_one_entry_with_its_count():
    a = X.dropped("sales", "metric", "revenue", "target_rule", "no home")
    b = X.degraded("sales", "metric", "revenue", "target_rule", "no home")
    entries = X.collapse_losses([a, b, a, a])
    assert entries == [b.to_dict(), {**a.to_dict(), "repeats": 3}]   # degraded sorts first
    assert X.loss_summary(entries) == {"target_rule": {"dropped": 1, "degraded": 1}}


def test_a_clean_model_reports_no_validation_error(tmp_path):
    out = tmp_path / "o"
    rc, text = _sm(str(SAMPLE), "--target", "dump", "--out", str(out))
    assert rc == 0, text
    v = json.loads(text)["model_validation"]
    assert v["checked"] is True and v["errors"] == 0 and "detail" not in v
