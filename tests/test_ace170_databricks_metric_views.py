"""`databricks` target (ACE-170): one Databricks metric view per fact table.

Databricks publishes no schema for the metric view YAML, so `_validate` below is written from its
documented rules (the YAML reference for version 1.1: top-level keys, joins, fields, measures,
windows, synonym limits, unique names, `MEASURE()` only of an earlier measure). The open-source
Spark parser accepts only version 0.1, and sqlglot cannot read `WITH METRICS`, so neither can stand
in for it: sqlglot checks each SQL expression on its own instead.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

from sqlglot import exp  # noqa: E402
from sqlglot.errors import ErrorLevel  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model import export_databricks as DB  # noqa: E402  (registers `databricks`)
from semantic_model.loader import load_datasource  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "sales"


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="databricks", error_level=ErrorLevel.RAISE)


# ---------------------------------------------------------------------------
# A structural validator, from Databricks' metric view YAML reference (spec version 1.1)
# ---------------------------------------------------------------------------

TOP = {"version", "comment", "source", "parameters", "filter", "joins", "fields", "dimensions",
       "measures", "materialization"}
JOIN = {"name", "source", "on", "using", "cardinality", "joins", "rely"}
FIELD = {"name", "expr", "comment", "display_name", "format", "synonyms"}
MEASURE = FIELD | {"window", "partition"}
WINDOW = {"order", "range", "semiadditive", "offset"}
# A finer level-of-detail measure, from "Use level of detail (LOD) expressions in metric views".
PARTITION = {"include", "outer_aggregate"}
OUTER_AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "MEDIAN"}
# Format specifications, from "Agent metadata in metric views".
FORMAT = {"number": {"decimal_places", "hide_group_separator", "abbreviation"},
          "currency": {"currency_code", "decimal_places", "hide_group_separator", "abbreviation"},
          "percentage": {"decimal_places", "hide_group_separator"},
          "byte": {"decimal_places", "hide_group_separator"},
          "date": {"date_format", "leading_zeros"},
          "date_time": {"date_format", "time_format", "leading_zeros"}}
RANGE = re.compile(r"current|cumulative|all|(trailing|leading) \w+( \w+)?( inclusive| exclusive)?")


def _validate_join(j: dict, parent_one_to_many: bool, paths: set[str], prefix: str) -> list[str]:
    errs = [f"join key {k}" for k in set(j) - JOIN]
    if not j.get("name") or not j.get("source"):
        errs.append("join without name or source")
    if ("on" in j) == ("using" in j):
        errs.append(f"join {j.get('name')} needs exactly one of on / using")
    card = j.get("cardinality", "many_to_one")
    if card not in ("many_to_one", "one_to_many"):
        errs.append(f"cardinality {card}")
    if parent_one_to_many and card != "one_to_many":
        errs.append("a one_to_many join's descendants must be one_to_many")
    if set(j.get("rely", {})) - {"at_most_one_match"}:
        errs.append("rely key")
    path = f"{prefix}{j.get('name')}"
    paths.add(path)
    nested = j.get("joins", [])
    if len({n.get("name") for n in nested}) != len(nested):
        errs.append("sibling joins share a name")
    for n in nested:
        errs += _validate_join(n, card == "one_to_many", paths, f"{path}.")
    return errs


def _format(item: dict) -> list[str]:
    if "format" not in item:
        return []
    fmt = item["format"]
    kind = fmt.get("type") if isinstance(fmt, dict) else None
    if kind not in FORMAT:
        return [f"{item.get('name')}: format type {kind}"]
    errs = [f"{item.get('name')}: format key {k}" for k in set(fmt) - {"type"} - FORMAT[kind]]
    if kind == "currency" and not re.fullmatch(r"[A-Z]{3}", str(fmt.get("currency_code", ""))):
        errs.append(f"{item.get('name')}: a currency format needs an ISO-4217 currency_code")
    return errs


def _texts(item: dict) -> list[str]:
    errs = _format(item)
    syn = item.get("synonyms", [])
    if len(syn) > 10 or any(not isinstance(s, str) or len(s) > 255 for s in syn):
        errs.append(f"{item.get('name')}: at most 10 synonyms of at most 255 characters")
    if len(item.get("display_name", "")) > 255:
        errs.append("display_name over 255")
    return errs


def _refs_ok(expr: str, paths: set[str]) -> bool:
    for col in _parse(expr).find_all(exp.Column):
        qual = ".".join(p for p in (col.catalog, col.db, col.table) if p)
        if qual and qual != "source" and qual not in paths:
            return False
    return True


def _validate(doc: dict) -> list[str]:
    """Every documented rule a definition breaks; empty when it passes."""
    errs = [f"top-level key {k}" for k in set(doc) - TOP]
    if doc.get("version") not in (1.1, "1.1"):
        errs.append("version must be 1.1")
    if not isinstance(doc.get("source"), str) or not doc["source"]:
        errs.append("source is required")
    fields, measures = doc.get("fields", doc.get("dimensions", [])), doc.get("measures", [])
    if not fields and not measures:
        errs.append("at least one field or measure")
    paths: set[str] = set()
    joins = doc.get("joins", [])
    if len({j.get("name") for j in joins}) != len(joins):
        errs.append("sibling joins share a name")
    for j in joins:
        errs += _validate_join(j, False, paths, "")
    names: set[str] = set()
    for f in fields:
        errs += [f"field key {k}" for k in set(f) - FIELD] + _texts(f)
        if not f.get("expr") or not f.get("name"):
            errs.append("a field needs name and expr")
        elif "MEASURE(" in f["expr"].upper():
            errs.append("a field cannot use MEASURE()")
        elif not _refs_ok(f["expr"], paths):
            errs.append(f"field {f['name']} reaches a table by no join")
    field_names = {f.get("name", "").casefold() for f in fields}
    earlier: set[str] = set()
    finer: set[str] = set()  # window and partition measures, which a partition measure cannot use
    for m in measures:
        errs += [f"measure key {k}" for k in set(m) - MEASURE] + _texts(m)
        if not m.get("expr") or not m.get("name"):
            errs.append("a measure needs name and expr")
            continue
        for call in _parse(m["expr"]).find_all(exp.Anonymous):
            if call.name.upper() == "MEASURE" and call.expressions[0].name.casefold() not in earlier:
                errs.append(f"{m['name']}: MEASURE() of a measure not defined before it")
        if not _refs_ok(m["expr"], paths):
            errs.append(f"measure {m['name']} reaches a table by no join")
        if "partition" in m:
            p = m["partition"]
            if not isinstance(p, dict) or set(p) != PARTITION:
                errs.append(f"{m['name']}: partition keys")
            elif not p["include"] or any(f.casefold() not in field_names for f in p["include"]):
                errs.append(f"{m['name']}: partition include names no field")
            elif p["outer_aggregate"] not in OUTER_AGGREGATES:
                errs.append(f"{m['name']}: outer_aggregate {p['outer_aggregate']}")
            if "window" in m:
                errs.append(f"{m['name']}: a partition measure cannot also have a window")
            if any(c.name.upper() == "MEASURE" for c in _parse(m["expr"]).find_all(exp.Anonymous)
                   if c.expressions[0].name.casefold() in finer):
                errs.append(f"{m['name']}: a partition measure cannot reference another")
            finer.add(m["name"].casefold())
        for w in m.get("window", []):
            if set(w) - WINDOW or not {"order", "range"} <= set(w):
                errs.append(f"{m['name']}: window keys")
            elif w["order"].casefold() not in field_names or not RANGE.fullmatch(w["range"]):
                errs.append(f"{m['name']}: window order or range")
            if w.get("semiadditive", "last") not in ("first", "last"):
                errs.append("semiadditive is first or last")
            finer.add(m["name"].casefold())
        earlier.add(m["name"].casefold())
    for item in [*fields, *measures]:
        key = str(item.get("name", "")).casefold()
        if key in names:
            errs.append(f"name {key} is not unique")
        names.add(key)
    return errs


def _statement(sql: str) -> str:
    """The CREATE statement without the SQL comment lines a view with a level-of-detail measure
    carries under its first line (YAML comments are stripped at 1.1, so the note is SQL's)."""
    head, *lines = sql.split("\n")
    while lines and lines[0].startswith("-- "):
        lines.pop(0)
    return "\n".join([head, *lines])


def _definition(sql: str) -> dict:
    head, _, rest = _statement(sql).partition("AS $$\n")
    assert head.startswith("CREATE OR REPLACE VIEW ") and "WITH METRICS\nLANGUAGE YAML" in head
    body, _, tail = rest.partition("$$")
    assert tail == ";\n"
    return yaml.safe_load(body)


def _join_conditions(joins: list[dict]) -> list[str]:
    return [c for j in joins for c in [j["on"], *_join_conditions(j.get("joins", []))]]


def _expressions(doc: dict) -> list[str]:
    exprs = [x["expr"] for x in (*doc.get("fields", []), *doc.get("measures", []))]
    return exprs + _join_conditions(doc.get("joins", [])) + ([doc["filter"]] if "filter" in doc else [])


# ---------------------------------------------------------------------------
# A synthetic model: acme's SALES_DATA, written for Databricks
# ---------------------------------------------------------------------------

SYNONYMS = [f"takings {i}" for i in range(11)] + ["x" * 256]


def _col(name, type_="string", **kw):
    return {"name": name, "type": type_, **kw}


def _metric(name, binding, tables, **kw):
    return {"name": name, "calculation": f"The {name}.", "bindings": {"Databricks": binding},
            "source_tables": tables, "review_state": "approved", **kw}


def _rel(a, b, card="many_to_one", **kw):
    return {"from_table": a, "to_table": b, "relationship": card, "review_state": "approved", **kw}


def _write_model(root: Path) -> Path:
    def put(rel, doc):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    def table(name, *cols, **kw):
        put(f"subject_areas/{AREA}/tables/{name}.yaml",
            {"name": name, "schema": "SALES_DATA", "columns": list(cols), **kw})

    put("datasource.yaml", {
        "datasource": "acme", "description": "Acme demo sales.",
        "storage_connections": [{"name": "acme_dbx", "storage_type": "Databricks", "storage_config": {}}],
        "subject_areas": [f"subject_areas/{AREA}"]})
    put(f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA, "description": "Orders and money."})
    table("orders", _col("id", "integer"), _col("customer_id", "integer"), _col("bill_to_id", "integer"),
          _col("store_id", "integer"), _col("amount", "decimal", description="Order total: in USD."),
          _col("revenue", "decimal"), _col("placed_at", "date"),
          description="One row per order.", default_filters=["{alias}.status != 'void'"])
    table("customers", _col("id", "integer"), _col("region_id", "integer"), _col("Segment Name"))
    table("regions", _col("id", "integer"), _col("name"))
    table("stores", _col("id", "integer"), _col("name"))
    table("returns", _col("id", "integer"), _col("customer_id", "integer"), _col("amount", "decimal"))
    table("balances", _col("account_id", "integer"), _col("customer_id", "integer"),
          _col("snapshot_date", "date"), _col("balance", "decimal"))
    put(f"subject_areas/{AREA}/relationships.yaml", [
        _rel("orders", "customers", from_column="customer_id", to_column="id"),
        _rel("orders", "customers", from_column="bill_to_id", to_column="id"),
        _rel("customers", "regions", from_column="region_id", to_column="id"),
        _rel("orders", "stores", on="orders.store_id = stores.id"),
        _rel("orders", "regions", on="orders.store_id = regions.id AND stores.id IS NOT NULL"),
        _rel("customers", "returns", "one_to_many", from_column="id", to_column="customer_id"),
        _rel("balances", "customers", from_column="customer_id", to_column="id", join_type="INNER"),
    ])
    metrics = [
        _metric("revenue", "SUM(orders.amount)", ["orders"], other_names=SYNONYMS, unit="USD"),
        _metric("order count", "COUNT(DISTINCT orders.id)", ["orders"]),
        _metric("order-count", "COUNT(orders.id)", ["orders"]),
        _metric("average order value", "{revenue} / {order count}", ["orders"],
                base_metrics=["revenue", "order count"]),
        _metric("west revenue", "SUM(CASE WHEN regions.name = 'west' THEN orders.amount END)",
                ["orders", "regions"], unit="USD_millions"),
        _metric("segment revenue", "SUM(CASE WHEN `Customers`.`Segment Name` = 'a.b' THEN orders.amount END)",
                ["orders", "customers"], unit="percent"),
        _metric("store revenue","SUM(CASE WHEN stores.name IS NOT NULL THEN orders.amount END)",
                ["orders", "stores"]),
        _metric("net revenue", "SUM(orders.amount) - SUM(returns.amount)", ["orders", "returns"]),
        _metric("return total", "SUM(returns.amount)", ["returns"]),
        _metric("closing balance", "SUM(balances.balance)", ["balances"],
                non_additive_dimensions=["snapshot_date"], semi_additive_agg="last"),
        _metric("opening balance", "SUM(balances.balance)", ["balances"],
                non_additive_dimensions=["time"], semi_additive_agg="first"),
        _metric("average balance", "SUM(balances.balance)", ["balances"],
                non_additive_dimensions=["snapshot_date"], semi_additive_agg="average"),
        _metric("held balance", "SUM(balances.balance)", ["balances"],
                non_additive_dimensions=["balances.nowhere"], semi_additive_agg="last"),
        _metric("peak daily revenue", "MAX({revenue})", ["orders"], base_metrics=["revenue"],
                inner_grain=["placed_at"], unit="USD"),
        # Second-order metrics: an aggregate of a per-grain aggregate.
        _metric("average customer revenue", "AVG({revenue})", ["orders"], base_metrics=["revenue"],
                inner_grain=["orders.customer_id", "customer_id"]),
        _metric("revenue spread", "STDDEV({revenue})", ["orders"], base_metrics=["revenue"],
                inner_grain=["placed_at"]),
        _metric("peak store revenue", "MAX({store revenue})", ["orders"],
                base_metrics=["store revenue"], inner_grain=["placed_at"]),
        _metric("peak region revenue", "MAX({revenue})", ["orders"], base_metrics=["revenue"],
                inner_grain=["customers.region_id"]),
        _metric("draft revenue", "SUM(orders.revenue)", ["orders"], review_state="unreviewed"),
        _metric("peak draft revenue", "MAX({draft revenue})", ["orders"],
                base_metrics=["draft revenue"], inner_grain=["placed_at"]),
        _metric("peak closing balance", "MAX({closing balance})", ["balances"],
                base_metrics=["closing balance"], inner_grain=["snapshot_date"]),
        _metric("ghost total", "SUM(ghosts.amount)", ["orders"]),
        # regions' only metric cannot be carried, so regions gets no metric view
        _metric("region count", "COUNT(regions.id)", ["regions"],
                non_additive_dimensions=["time"], semi_additive_agg="max"),
    ]
    for m in metrics:
        put(f"subject_areas/{AREA}/metrics/{m['name'].replace(' ', '_')}.yaml", m)
    return root


@pytest.fixture()
def model(tmp_path: Path) -> Path:
    return _write_model(tmp_path / "art" / "acme")


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(root: Path, out: Path, *flags: str) -> tuple[dict, dict[str, str]]:
    rc, text = _sm(str(root), "--target", "databricks", "--out", str(out), *flags)
    assert rc == 0, text
    files = {p.name: p.read_text(encoding="utf-8")
             for p in sorted((out / "databricks" / DB.METRIC_VIEW_DIR).glob("*.sql"))}
    return json.loads(text)["targets"]["databricks"], files


def _losses(section: dict) -> dict[tuple[str, str], list[tuple[str, str]]]:
    out: dict = {}
    for x in section["losses"]:
        out.setdefault((x["kind"], x["name"]), []).append((x["outcome"], x["reason"]))
    return out


def _measures(files: dict[str, str]) -> dict[str, dict]:
    return {m.get("display_name", m["name"]): m for sql in files.values()
            for m in _definition(sql)["measures"]}


@pytest.fixture()
def sample(tmp_path):
    return _export(SAMPLE, tmp_path / "s", "--location", "DEMO.SALES_DATA")


@pytest.fixture()
def synthetic(model, tmp_path):
    return _export(model, tmp_path / "y")


# ---------------------------------------------------------------------------
# The spec's success criteria
# ---------------------------------------------------------------------------


def test_the_sample_writes_one_view_per_measured_fact_table_and_none_for_other_tables(sample):
    section, files = sample
    facts = {"invoices", "order_items", "orders", "refunds", "subscriptions"}
    assert set(files) == {f"agami_example__{t}.sql" for t in facts}
    measured = {m.primary_table for sa in load_datasource(SAMPLE).subject_areas for m in sa.metrics}
    assert measured == facts  # plans is a source table of MRR, but MRR is measured from subscriptions
    for t in ("categories", "customers", "payments", "plans", "products", "subscription_events"):
        assert f"agami_example__{t}.sql" not in files
    assert section["carried"]["metric"] == 8 and len(_measures(files)) == 8
    assert not [x for x in section["losses"] if x["kind"] == "metric"]


def test_every_view_passes_the_structural_validator(sample, synthetic):
    for _, files in (sample, synthetic):
        for name, sql in files.items():
            assert _validate(_definition(sql)) == [], name


def test_every_expr_join_condition_and_the_create_statement_parse_as_databricks(sample, synthetic):
    for _, files in (sample, synthetic):
        for name, sql in files.items():
            doc = _definition(sql)
            for text in _expressions(doc):
                assert _parse(text) is not None, (name, text)
            # sqlglot has no `$$` strings in this dialect: it tokenizes the YAML as SQL, so a quote
            # in a description fails the whole statement, and without one it falls back to an
            # unchecked command. The statement is parsed around a minimal body instead, and its
            # head as a plain view, which checks the view's name.
            head, _, rest = _statement(sql).partition(" $$\n")[0].partition("\n")
            assert rest == "WITH METRICS\nLANGUAGE YAML\nAS"
            assert _parse(f"{head}\n{rest} $$\nversion: 1.1\n$$") is not None
            created = _parse(f"{head} AS SELECT 1")
            assert isinstance(created, exp.Create)
            assert created.this.name == name.removesuffix(".sql")


def test_a_derived_metric_carries_its_expanded_sql(sample, synthetic):
    for _, files in (sample, synthetic):
        aov = _measures(files)["average order value"]
        assert "{" not in aov["expr"] and "MEASURE" not in aov["expr"].upper()
        assert re.fullmatch(r"\(SUM\(source\.\w+\)\) / \(COUNT\(DISTINCT source\.id\)\)", aov["expr"])


def test_a_metric_spanning_two_fact_tables_is_target_rule_and_in_no_view(synthetic):
    section, files = synthetic
    assert _losses(section)[("metric", "net revenue")] == [("dropped", "target_rule")]
    assert "net revenue" not in _measures(files)
    assert {"sales__orders.sql", "sales__returns.sql", "sales__balances.sql"} == set(files)


def test_no_default_filter_reaches_a_view_and_the_writer_reports_none(synthetic):
    section, files = synthetic
    assert section["carried"]["default_filter"] == 1  # it travelled in the view, for ACE-171
    for sql in files.values():
        assert "filter" not in _definition(sql) and "void" not in sql
    assert not [x for x in section["losses"] if x["kind"] == "default_filter"]


def test_two_runs_write_byte_identical_files(model, tmp_path):
    for root, flags in ((SAMPLE, ("--location", "DEMO.SALES_DATA")), (model, ())):
        a = _export(root, tmp_path / "one", *flags)
        b = _export(root, tmp_path / "two", *flags)
        assert a == b and a[1]


# ---------------------------------------------------------------------------
# How agami's model maps
# ---------------------------------------------------------------------------


def test_semi_additive_metrics_carry_a_semiadditive_window(synthetic):
    section, files = synthetic
    m = _measures(files)
    assert m["closing balance"]["window"] == [
        {"order": "snapshot_date", "range": "current", "semiadditive": "last"}]
    assert m["opening balance"]["window"] == [
        {"order": "snapshot_date", "range": "current", "semiadditive": "first"}]
    losses = _losses(section)
    for name in ("average balance", "held balance"):
        assert losses[("metric", name)] == [("dropped", "target_rule")] and name not in m


# ---------------------------------------------------------------------------
# Second-order metrics as finer level-of-detail measures, and measure formats
# ---------------------------------------------------------------------------


def test_a_second_order_metric_is_a_finer_level_of_detail_measure(synthetic):
    """`OUTERAGG({base})` at `inner_grain` is the base's aggregate at the query's grain plus the
    inner grain, rolled up by the outer aggregate: Databricks' `partition` measure exactly."""
    section, files = synthetic
    m = _measures(files)
    assert m["peak daily revenue"]["expr"] == m["revenue"]["expr"] == "SUM(source.amount)"
    assert m["peak daily revenue"]["partition"] == {"include": ["placed_at"], "outer_aggregate": "MAX"}
    # `orders.customer_id` and `customer_id` are one column, so one field.
    assert m["average customer revenue"]["partition"] == {
        "include": ["customer_id"], "outer_aggregate": "AVG"}
    assert "window" not in m["peak daily revenue"]
    losses = _losses(section)
    assert ("metric", "peak daily revenue") not in losses
    assert ("metric", "average customer revenue") not in losses
    # The view that holds one says what it needs, under the statement's first line.
    head, _, rest = files["sales__orders.sql"].partition("\n")
    assert head == "CREATE OR REPLACE VIEW SALES_DATA.sales__orders" and rest.startswith(DB._FINER_NOTE)
    assert "partition" not in files["sales__returns.sql"] and "--" not in files["sales__returns.sql"]


@pytest.mark.parametrize("name, needle", [
    ("revenue spread", "outer aggregate STDDEV"),           # not a level-of-detail roll-up
    ("peak region revenue", "inner grain customers.region_id"),  # grouped where agami does not
    ("peak draft revenue", "base metric draft revenue"),     # its base did not travel
    ("peak closing balance", "semi-additive"),               # a base with a window
])
def test_a_second_order_metric_the_construct_would_change_is_reported(synthetic, name, needle):
    section, files = synthetic
    (loss,) = [x for x in section["losses"] if (x["kind"], x["name"]) == ("metric", name)]
    assert (loss["outcome"], loss["reason"]) == ("dropped", "target_rule") and needle in loss["detail"]
    assert name not in _measures(files)


def test_a_second_order_metric_over_several_tables_is_reported(model, tmp_path):
    # agami inner-joins a base's tables before grouping; a metric view joins left outer.
    path = model / f"subject_areas/{AREA}/relationships.yaml"
    rels = yaml.safe_load(path.read_text())
    for r in rels:
        if r["to_table"] == "stores":
            r.pop("on")
            r.update(from_column="store_id", to_column="id")
    path.write_text(yaml.safe_dump(rels))
    base = model / f"subject_areas/{AREA}/metrics/store_revenue.yaml"
    base.write_text(yaml.safe_dump({**yaml.safe_load(base.read_text()), "primary_table": "orders"}))
    section, files = _export(model, tmp_path / "s")
    (loss,) = [x for x in section["losses"] if x["name"] == "peak store revenue"]
    assert loss["reason"] == "target_rule" and "several tables" in loss["detail"]
    assert "store revenue" in _measures(files) and "peak store revenue" not in _measures(files)


def test_a_second_order_metric_bound_another_way_is_reported(model):
    import dataclasses

    org = load_datasource(model)
    opts = X.ExportOptions(("databricks",))
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "Databricks",
                           [], [])
    (em,) = [em for em in view.metrics if em.metric.name == "peak daily revenue"]
    odd = em.metric.model_copy(update={"bindings": {"Databricks": "MAX({revenue}) + 1"}})
    losses: list = []
    planner = DB._Planner(dataclasses.replace(view, metrics=(dataclasses.replace(em, metric=odd),)),
                          losses)
    assert planner.finer_home(planner.view.metrics[0]) is None
    assert [(x.name, x.reason) for x in losses if x.kind == "metric"] == [
        ("peak daily revenue", "target_rule")]


def test_a_semi_additive_second_order_metric_is_reported(model, tmp_path):
    path = model / f"subject_areas/{AREA}/metrics/peak_daily_revenue.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.update(non_additive_dimensions=["placed_at"], semi_additive_agg="last")
    path.write_text(yaml.safe_dump(doc))
    section, _ = _export(model, tmp_path / "s")
    (loss,) = [x for x in section["losses"] if x["name"] == "peak daily revenue"]
    assert loss["reason"] == "target_rule" and "no window" in loss["detail"]


def test_a_plain_currency_unit_is_a_currency_format_and_no_other_unit_is(sample, synthetic):
    m = _measures(synthetic[1])
    assert m["revenue"]["format"] == {"type": "currency", "currency_code": "USD"}
    # A scaled currency would print 12.5 (million) as $12.50; agami's percent is 0-100, while
    # Databricks' percentage format is for ratios. Both keep their unit in the comment.
    for name, unit in (("west revenue", "USD_millions"), ("segment revenue", "percent")):
        assert "format" not in m[name] and m[name]["comment"].endswith(f"Unit: {unit}.")
    assert "format" not in m["order count"]
    sample_measures = _measures(sample[1])
    units = {m.name: m.unit for sa in load_datasource(SAMPLE).subject_areas for m in sa.metrics}
    for name, measure in sample_measures.items():
        want = {"type": "currency", "currency_code": units[name]} if units[name] else None
        assert measure.get("format") == want, name
    assert sum("format" in x for x in sample_measures.values()) == 5


def test_measure_format_reads_agamis_unit_grammar():
    assert DB.measure_format("eur") == {"type": "currency", "currency_code": "EUR"}
    for unit in (None, "", "USD/day", "INR_crores", "percent", "%", "days", "XYZ", "cents"):
        assert DB.measure_format(unit) is None, unit


def test_joins_follow_many_to_one_paths_once_and_report_what_they_lose(synthetic):
    section, files = synthetic
    orders = _definition(files["sales__orders.sql"])
    joins = {j["name"]: j for j in orders["joins"]}
    assert joins["customers"]["on"] == "source.customer_id = customers.id"
    assert joins["customers"]["joins"][0]["on"] == "customers.region_id = regions.id"
    assert joins["stores"]["on"] == "source.store_id = stores.id"  # an `on:` relationship
    assert _measures(files)["west revenue"]["expr"] == (
        "SUM(CASE WHEN customers.regions.name = 'west' THEN source.amount END)")
    # Only qualifiers change: the rest is the SQL as written, not sqlglot's rendering of it.
    assert _measures(files)["store revenue"]["expr"] == (
        "SUM(CASE WHEN stores.name IS NOT NULL THEN source.amount END)")
    assert _measures(files)["segment revenue"]["expr"] == (
        "SUM(CASE WHEN customers.`Segment Name` = 'a.b' THEN source.amount END)")
    returns = _definition(files["sales__returns.sql"])  # a one_to_many relationship, read backwards
    assert returns["joins"][0]["on"] == "source.customer_id = customers.id"
    losses = _losses(section)
    assert ("degraded", "target_rule") in losses[("relationship", "orders->customers#2")]
    assert losses[("relationship", "orders->regions")] == [("degraded", "target_rule")]
    assert losses[("relationship", "balances->customers")] == [("degraded", "detail_lost")]
    assert losses[("metric", "ghost total")] == [("dropped", "target_rule")]
    assert losses[("metric", "region count")] == [("dropped", "target_rule")]


def test_a_join_tree_skips_tables_it_cannot_place_and_on_it_cannot_read():
    rel = X.ExportRelationship("a->b", type("R", (), {"on": "a.x = 'unterminated"})())
    assert not DB._on_reads_its_ends(rel.relationship, "a", "b")
    edge = DB._Edge("a", "b", rel, "x", "y")
    tree = DB._join_tree("a", {"a": [edge]}, {"a"}, {"a": "a", "b": "b"})
    assert tree.order == () and tree.skipped == ()


def test_names_follow_one_rule_and_collisions_are_reported(synthetic):
    section, files = synthetic
    fields = {f["name"]: f for f in _definition(files["sales__orders.sql"])["fields"]}
    assert fields["customers_segment_name"]["expr"] == "customers.`Segment Name`"
    assert fields["amount"]["comment"] == "Order total: in USD."
    assert "revenue" not in fields  # the column gives way to the metric of the same name
    losses = _losses(section)
    assert losses[("column", "orders.revenue")] == [("degraded", "name_collision")]
    assert losses[("metric", "order-count")] == [("dropped", "name_collision")]
    revenue = _measures(files)["revenue"]
    assert revenue["synonyms"] == SYNONYMS[:10] and revenue["comment"].endswith("Unit: USD.")
    assert losses[("metric", "revenue")] == [("degraded", "target_limit")] * 2


def _put(model: Path, rel: str, doc) -> None:
    (model / rel).parent.mkdir(parents=True, exist_ok=True)
    (model / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_two_fact_tables_named_alike_keep_one_view_and_report_the_others_metrics(model, tmp_path):
    for i, name in enumerate(("order items", "order-items")):
        _put(model, f"subject_areas/{AREA}/tables/oi{i}.yaml", {
            "name": name, "schema": "SALES_DATA", "columns": [_col("qty", "integer")]})
        _put(model, f"subject_areas/{AREA}/metrics/oi{i}.yaml",
             _metric(f"units {i}", f"SUM(`{name}`.qty)", [name]))
    section, files = _export(model, tmp_path / "n", "--location", "DEMO.SALES_DATA")
    # One file, holding the first table's metric by name order ("order items" < "order-items").
    assert "sales__order_items.sql" in files
    assert set(_measures({"x": files["sales__order_items.sql"]})) == {"units 0"}
    losses = _losses(section)
    assert losses[("metric", "units 1")] == [("dropped", "name_collision")]
    detail = next(x["detail"] for x in section["losses"] if x["name"] == "units 1")
    assert "sales__order_items" in detail and "order-items" in detail
    # The Genie space lists that one view once.
    space = json.loads((tmp_path / "n" / "databricks" / "sales.geniespace.json").read_text())
    idents = [m["identifier"] for m in space["data_sources"].get("metric_views", [])]
    assert len(idents) == len(set(idents)) and idents.count("DEMO.SALES_DATA.sales__order_items") == 1


def test_two_joined_tables_named_alike_are_joined_once_and_the_other_join_reported(model, tmp_path):
    for i, name in enumerate(("store groups", "store-groups")):
        _put(model, f"subject_areas/{AREA}/tables/sg{i}.yaml", {
            "name": name, "schema": "SALES_DATA", "columns": [_col("id", "integer")]})
    rels = yaml.safe_load((model / f"subject_areas/{AREA}/relationships.yaml").read_text())
    rels += [_rel("orders", "store groups", from_column="store_id", to_column="id"),
             _rel("orders", "store-groups", from_column="store_id", to_column="id")]
    _put(model, f"subject_areas/{AREA}/relationships.yaml", rels)
    section, files = _export(model, tmp_path / "j")
    doc = _definition(files["sales__orders.sql"])
    names = [j["name"] for j in doc["joins"]]
    assert names.count("store_groups") == 1 and _validate(doc) == []
    assert ("degraded", "name_collision") in _losses(section)[("relationship", "orders->store-groups")]


def test_the_naming_rule_and_identifier_are_public(model, tmp_path):
    assert DB.metric_view_name("agami-example", "main.Order Items") == "agami_example__order_items"
    assert DB.databricks_name("2024 sales!") == "_2024_sales" and DB.databricks_name("§") == "unnamed"
    org = load_datasource(model)
    target = X.TARGETS["databricks"]
    for location, want in ((None, "SALES_DATA.sales__orders"), ("DEMO.SALES_DATA", "DEMO.SALES_DATA")):
        opts = X.ExportOptions(("databricks",), location=X.parse_location(location) if location else None)
        view, _ = X.build_view(org, org.subject_areas[0], target, opts, "Databricks", [], [])
        assert DB.metric_view_identifier(view, opts, "orders").startswith(want)
        plans, _ = DB.plan_metric_views(view)
        assert [p.name for p in plans] == ["sales__balances", "sales__orders", "sales__returns"]


def test_a_sql_table_joins_as_its_query_only_in_databricks_sql(model, tmp_path):
    path = model / f"subject_areas/{AREA}/tables/stores.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.update(source_type="sql", sql="SELECT 1 AS id, 'a' AS name")
    path.write_text(yaml.safe_dump(doc))
    _, files = _export(model, tmp_path / "q")
    joins = {j["name"]: j for j in _definition(files["sales__orders.sql"])["joins"]}
    assert joins["stores"]["source"] == "SELECT 1 AS id, 'a' AS name"
    org = load_datasource(model)
    opts = X.ExportOptions(("databricks",), location=X.parse_location("DEMO.SALES_DATA"))
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "SQLite", [], [])
    out = DB.write_metric_views(view, opts)
    assert ("metric", "store revenue", "target_rule") in {(x.kind, x.name, x.reason) for x in out.losses}


def test_a_definition_holding_dollar_quotes_is_written_with_the_dollar_escaped(model, tmp_path):
    path = model / f"subject_areas/{AREA}/metrics/return_total.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["description"] = "Costs $$ to process, or $$$."
    path.write_text(yaml.safe_dump(doc))
    section, files = _export(model, tmp_path / "d")
    sql = files["sales__returns.sql"]
    # The body holds no `$$` of its own, so the dollar quotes around it are the only two.
    assert sql.count("$$") == 2 and "\\x24" in sql
    measure = _measures({"x": sql})["return total"]
    assert measure["comment"].startswith("Costs $$ to process, or $$$.")
    assert ("metric", "return total") not in _losses(section)
    assert _validate(_definition(sql)) == []


def test_a_definition_that_cannot_be_escaped_names_the_field_that_holds_dollar_quotes(
        model, tmp_path, monkeypatch):
    path = model / f"subject_areas/{AREA}/metrics/return_total.yaml"
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), "description": "Costs $$."}))
    real = DB.definition_body
    monkeypatch.setattr(DB, "definition_body",
                        lambda d: None if DB.dollar_fields(d) else real(d))
    section, files = _export(model, tmp_path / "d")
    assert "sales__returns.sql" not in files
    loss = next(x for x in section["losses"] if x["name"] == "return total")
    assert loss["reason"] == "target_rule" and "measures.return_total.comment" in loss["detail"]


# ---------------------------------------------------------------------------
# The validator itself, and the seam later slices build on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("break_it, needle", [
    (lambda d: d.pop("source"), "source is required"),
    (lambda d: d.update(version=0.1), "version"),
    (lambda d: d.update(fields=[], measures=[]), "at least one"),
    (lambda d: d["measures"][0].update(synonyms=["s"] * 11), "synonyms"),
    (lambda d: d["measures"][0].update(expr="MEASURE(later) + 1"), "MEASURE()"),
    (lambda d: d["joins"][0].update(cardinality="one_to_one"), "cardinality"),
    (lambda d: d["joins"][0].pop("on"), "on / using"),
    (lambda d: d["fields"].append(dict(d["fields"][0])), "not unique"),
    (lambda d: d["fields"][0].update(expr="nowhere.id"), "by no join"),
    (lambda d: d["measures"][0].update(window=[{"order": "nope", "range": "current"}]), "window"),
    (lambda d: d["measures"][0].update(format={"type": "money"}), "format type"),
    (lambda d: d["measures"][0].update(format={"type": "currency"}), "currency_code"),
    (lambda d: d["measures"][0].update(format={"type": "percentage", "currency_code": "USD"}),
     "format key"),
    (lambda d: d["measures"][0].update(partition={"include": ["nope"], "outer_aggregate": "AVG"}),
     "names no field"),
    (lambda d: d["measures"][0].update(partition={"include": ["id"], "outer_aggregate": "STDDEV"}),
     "outer_aggregate"),
    (lambda d: d["measures"][0].update(partition={"include": ["id"]}), "partition keys"),
    (lambda d: d["measures"][0].update(partition={"include": ["id"], "outer_aggregate": "AVG"},
                                       window=[{"order": "id", "range": "current"}]),
     "cannot also have a window"),
    (lambda d: d["measures"].extend([
        {"name": "lod", "expr": "SUM(source.id)",
         "partition": {"include": ["id"], "outer_aggregate": "AVG"}},
        {"name": "lod2", "expr": "MEASURE(lod)",
         "partition": {"include": ["id"], "outer_aggregate": "AVG"}}]),
     "cannot reference another"),
])
def test_the_validator_catches_each_rule(sample, break_it, needle):
    doc = _definition(sample[1]["agami_example__orders.sql"])
    break_it(doc)
    assert any(needle in e for e in _validate(doc)), _validate(doc)


def test_further_databricks_artifacts_compose_into_one_output(sample, monkeypatch):
    view_seen = []

    def extra(view, options):
        view_seen.append(view.area)
        return X.WriterOutput({"genie/space.json": b"{}"},
                              (X.degraded(view.area, "metric", "m", "detail_lost", "test"),))

    # Only this slice's artifact: later slices register theirs when their module is imported.
    monkeypatch.setattr(DB, "ARTIFACTS", [DB.write_metric_views])
    DB.register_artifact(extra)
    DB.register_artifact(extra)
    assert DB.ARTIFACTS == [DB.write_metric_views, extra]
    org = load_datasource(SAMPLE)
    opts = X.ExportOptions(("databricks",), location=X.parse_location("DEMO.SALES_DATA"))
    view, _ = X.build_view(org, org.subject_areas[0], X.TARGETS["databricks"], opts, "SQLite", [], [])
    out = DB.write_databricks(view, opts)
    assert "genie/space.json" in out.files and len(out.files) == 6 and out.losses[-1].name == "m"
    DB.ARTIFACTS.append(extra)
    with pytest.raises(ValueError, match="genie/space.json"):
        DB.write_databricks(view, opts)


def test_the_target_is_registered_and_the_privacy_scan_covers_it():
    import importlib.util

    assert X.TARGETS["databricks"].engines == ("Databricks",)
    spec = importlib.util.spec_from_file_location("privacy", REPO / "tests" / "test_privacy_no_network.py")
    privacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(privacy)
    module = PKG_SRC / "semantic_model" / "export_databricks.py"
    assert module in privacy.SCRIPTS
    assert not any(rx.search(module.read_text(encoding="utf-8")) for rx in privacy._FORBIDDEN_RE)


def test_a_definition_holding_a_dollar_says_where_to_run_it(tmp_path):
    from semantic_model import models as M

    t = X.ExportTable(M.Table(name="orders", columns=[M.Column(name="amount", type="decimal")]),
                      X.PhysicalName("main", "sales", "orders"))
    view = X.ExportView(
        target="databricks", engines=("Databricks",), source_engine="Databricks",
        sql_engine="Databricks", datasource="acme", description="", area="sales",
        area_description="", tables=(t,), relationships=(), entities=(), examples=(), golden=(),
        ambiguities=(), terminology=(), metrics=(X.ExportMetric(M.Metric(
            name="revenue", calculation="Costs in ${currency}.", source_tables=["orders"]),
            {"Databricks": "SUM(orders.amount)"}),))
    opts = X.ExportOptions(("databricks",))
    text = DB.write_metric_views(view, opts).files["metric_views/sales__orders.sql"].decode()
    head, body = text.split("AS $$", 1)
    assert "${currency}" in body and "widget" in head and "on a SQL warehouse" in head
    assert text.splitlines()[0].startswith("CREATE OR REPLACE VIEW")
    plain = dataclasses.replace(view, metrics=(dataclasses.replace(
        view.metrics[0], metric=view.metrics[0].metric.model_copy(update={"calculation": "Sum."})),))
    assert "widget" not in DB.write_metric_views(plain, opts).files["metric_views/sales__orders.sql"].decode()
