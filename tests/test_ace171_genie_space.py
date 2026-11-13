"""`databricks` target, Genie space (ACE-171): a space file and bundle resource per subject area.

No free tool checks the space itself: `databricks bundle validate` needs credentials and never reads
the `.geniespace.json`, so malformed JSON, a wrong version, bad ids and unsorted arrays all pass it.
`validate` below is therefore the offline check, written from Databricks' documented rules for the
`serialized_space` ("Validation rules for serialized_space" in the Genie conversation API page, and
the limits on its set-up and knowledge-store pages). It is strict, and the last tests prove it
rejects each kind of bad file. Order is checked by rule, never against a stored file: a golden file
would pass a space whose order was wrong from the start.
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
from semantic_model import export_databricks as DB  # noqa: E402
from semantic_model import export_databricks_genie as G  # noqa: E402  (registers the artifact)
from semantic_model.loader import load_datasource  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "sales"
LOCATION = ("--location", "DEMO.SALES_DATA")


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="databricks", error_level=ErrorLevel.RAISE)


# ---------------------------------------------------------------------------
# A structural validator, from Databricks' documented rules for the serialized space
# ---------------------------------------------------------------------------

HEX = re.compile(r"[0-9a-f]{32}")
RT = {f"--rt=FROM_RELATIONSHIP_TYPE_{c}--" for c in ("MANY_TO_ONE", "ONE_TO_MANY", "ONE_TO_ONE",
                                                      "MANY_TO_MANY")}
TEXT_FIELDS = {"description", "question", "content", "sql", "comment", "instruction", "synonyms",
               "usage_guidance"}
KEYS = {
    "space": {"version", "config", "data_sources", "instructions", "benchmarks"},
    "config": {"sample_questions"},
    "sample": {"id", "question"},
    "data_sources": {"tables", "metric_views"},
    "source": {"identifier", "description", "column_configs"},
    "column": {"column_name", "description", "synonyms", "exclude", "enable_entity_matching",
               "enable_format_assistance"},
    "instructions": {"text_instructions", "example_question_sqls", "sql_functions", "join_specs",
                     "sql_snippets"},
    "text": {"id", "content"},
    "example": {"id", "question", "sql", "parameters", "usage_guidance"},
    "function": {"id", "identifier"},
    "join": {"id", "left", "right", "sql", "comment", "instruction"},
    "end": {"identifier", "alias"},
    "snippets": {"filters", "expressions", "measures"},
    "filter": {"id", "sql", "display_name", "synonyms", "instruction"},
    "measure": {"id", "alias", "sql", "display_name", "synonyms", "instruction"},
    "benchmarks": {"questions"},
    "benchmark": {"id", "question", "answer"},
    "answer": {"format", "content"},
}


def _keys(errs: list[str], obj, kind: str) -> None:
    if not isinstance(obj, dict):
        errs.append(f"{kind} is not an object")
    else:
        errs += [f"{kind} key {k}" for k in set(obj) - KEYS[kind]]


def _walk(obj, errs: list[str], path: str = "") -> None:
    """Text fields are lists of strings; no string over 25,000 characters, no array over 10,000."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TEXT_FIELDS and not (isinstance(v, list) and all(isinstance(s, str) for s in v)):
                errs.append(f"{path}.{k} is not a list of strings")
            _walk(v, errs, f"{path}.{k}")
    elif isinstance(obj, list):
        if len(obj) > 10_000:
            errs.append(f"{path} has more than 10,000 items")
        for i, v in enumerate(obj):
            _walk(v, errs, f"{path}[{i}]")
    elif isinstance(obj, str) and len(obj) > 25_000:
        errs.append(f"{path} is over 25,000 characters")


def _sorted(errs: list[str], items: list, key, what: str) -> None:
    keys = [key(x) for x in items]
    if keys != sorted(keys):
        errs.append(f"{what} is not sorted")


def _ids(errs: list[str], items: list, what: str) -> list[str]:
    ids = [x.get("id") for x in items]
    errs += [f"{what} id {i!r}" for i in ids if not (isinstance(i, str) and HEX.fullmatch(i))]
    _sorted(errs, items, lambda x: str(x.get("id")), what)
    return [str(i) for i in ids]


def _parts(t) -> tuple[str, str, str]:
    return (t.catalog.casefold(), t.db.casefold(), t.name.casefold())


def _three_part(ident) -> bool:
    try:
        t = _parse(f"SELECT 1 FROM {ident}").find(exp.Table)
    except Exception:
        return False
    return bool(t and t.catalog and t.db and t.name)


def _table_ref_errors(sql: str, idents: set) -> list[str]:
    """Each table example and benchmark SQL reads, a CTE aside, is written catalog.schema.table
    and is one the space lists."""
    listed = {_parts(_parse(f"SELECT 1 FROM {i}").find(exp.Table)) for i in idents if _three_part(i)}
    try:
        tree = _parse(sql)
    except Exception:
        return ["a SQL answer does not parse"]
    ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
    errs = []
    for t in tree.find_all(exp.Table):
        if not t.db and t.name.casefold() in ctes:
            continue
        if not (t.catalog and t.db):
            errs.append(f"table {t.name} is not catalog.schema.table")
        elif _parts(t) not in listed:
            errs.append(f"table {t.name} is not a listed data source")
    return errs


def _column_errors(c: dict) -> list[str]:
    errs = [f"column config {k} is not a boolean" for k in
            ("exclude", "enable_entity_matching", "enable_format_assistance")
            if k in c and not isinstance(c[k], bool)]
    # "To enable entity matching, Format assistance must be turned on."
    if c.get("enable_entity_matching") is True and c.get("enable_format_assistance") is not True:
        errs.append("entity matching needs format assistance on")
    return errs


def validate(space) -> list[str]:
    """Every documented rule `space` breaks; empty when it passes."""
    errs: list[str] = []
    _keys(errs, space, "space")
    if not isinstance(space, dict):
        return errs
    if space.get("version") != 2 or isinstance(space.get("version"), bool):
        errs.append("version must be 2")
    _walk(space, errs)
    config = space.get("config", {})
    _keys(errs, config, "config")
    samples = config.get("sample_questions", [])
    for s in samples:
        _keys(errs, s, "sample")
    question_ids = _ids(errs, samples, "sample_questions")

    ds = space.get("data_sources", {})
    _keys(errs, ds, "data_sources")
    sources = [*ds.get("tables", []), *ds.get("metric_views", [])]
    if len(sources) > 50:
        errs.append("more than 50 tables and metric views")
    for key in ("tables", "metric_views"):
        _sorted(errs, ds.get(key, []), lambda x: str(x.get("identifier")), key)
    idents = set()
    matching = 0
    for src in sources:
        _keys(errs, src, "source")
        idents.add(src.get("identifier"))
        if not _three_part(src.get("identifier")):
            errs.append(f"identifier {src.get('identifier')!r} is not catalog.schema.table")
        configs = src.get("column_configs", [])
        for c in configs:
            _keys(errs, c, "column")
            errs += _column_errors(c)
            matching += c.get("enable_entity_matching") is True
        _sorted(errs, configs, lambda c: str(c.get("column_name")), "column_configs")
        names = [c.get("column_name") for c in configs]
        if len(set(names)) != len(names):
            errs.append("a column config repeats a column")
    if len(idents) != len(sources):
        errs.append("a data source is listed twice")
    if matching > 120:  # "entity matching ... for up to 120 columns"
        errs.append("entity matching on more than 120 columns")

    ins = space.get("instructions", {})
    _keys(errs, ins, "instructions")
    texts, examples = ins.get("text_instructions", []), ins.get("example_question_sqls", [])
    functions, joins = ins.get("sql_functions", []), ins.get("join_specs", [])
    snippets = ins.get("sql_snippets", {})
    _keys(errs, snippets, "snippets")
    if len(texts) > 1:
        errs.append("more than one text instruction")
    if len(texts) + len(examples) + len(functions) > 100:
        errs.append("more than 100 instructions")
    instruction_ids = []
    for kind, items in (("text", texts), ("example", examples), ("join", joins)):
        for x in items:
            _keys(errs, x, kind)
        instruction_ids += _ids(errs, items, kind)
    for f in functions:
        _keys(errs, f, "function")
    _sorted(errs, functions, lambda f: (str(f.get("id")), str(f.get("identifier"))), "sql_functions")
    instruction_ids += [str(f.get("id")) for f in functions]
    for e in examples:
        if not e.get("question") or not e.get("sql"):
            errs.append("an example needs question and sql")
        else:
            errs += _table_ref_errors("".join(e["sql"]), idents)
    for j in joins:
        errs += _join_errors(j, idents)
    snippet_count = len(joins) + sum(1 for s in sources if s.get("description"))
    for kind in ("filters", "expressions", "measures"):
        items = snippets.get(kind, [])
        snippet_count += len(items)
        instruction_ids += _ids(errs, items, kind)
        for s in items:
            _keys(errs, s, "filter" if kind == "filters" else "measure")
            if not "".join(s.get("sql") or []).strip():
                errs.append(f"{kind} sql is empty")
            if (kind != "filters") != bool(s.get("alias")):
                errs.append(f"{kind}: an alias on measures and expressions only")
    if snippet_count > 200:
        errs.append("more than 200 knowledge-store snippets")
    if len(set(instruction_ids)) != len(instruction_ids):
        errs.append("instruction ids are not unique")

    bench = space.get("benchmarks", {})
    _keys(errs, bench, "benchmarks")
    questions = bench.get("questions", [])
    if len(questions) > 500:
        errs.append("more than 500 benchmark questions")
    question_ids += _ids(errs, questions, "benchmarks")
    for q in questions:
        _keys(errs, q, "benchmark")
        answers = q.get("answer") or []
        if len(answers) != 1 or answers[0].get("format") != "SQL" or not answers[0].get("content"):
            errs.append("a benchmark needs exactly one SQL answer")
        for a in answers:
            _keys(errs, a, "answer")
            if isinstance(a, dict) and isinstance(a.get("content"), list):
                errs += _table_ref_errors("".join(a["content"]), idents)
    if len(set(question_ids)) != len(question_ids):
        errs.append("question ids are not unique")
    return errs


def _join_errors(j: dict, idents: set) -> list[str]:
    errs = []
    left, right, sql = j.get("left", {}), j.get("right", {}), j.get("sql", [])
    for end in (left, right):
        _keys(errs, end, "end")
        if end.get("identifier") not in idents or not end.get("alias"):
            errs.append("a join end is not a listed data source with an alias")
    if len(sql) != 2 or sql[1] not in RT:
        errs.append("join sql must be [condition, relationship type]")
        return errs
    aliases = {str(left.get("alias")).casefold(), str(right.get("alias")).casefold()}
    try:
        cols = list(_parse(f"SELECT {sql[0]}").find_all(exp.Column))
    except Exception:
        return errs + ["join condition does not parse"]
    if not cols or any(c.table.casefold() not in aliases for c in cols):
        errs.append("a join condition reads a table besides its two ends")
    if not all(f"`{c.table}`" in sql[0] for c in cols):
        errs.append("a join condition's alias references must be back-quoted")
    return errs


def run_together(space, path: str = "") -> list[str]:
    """Each text field whose strings would run into each other: every string but the last must
    end a line, unless it is a piece of one line cut at the 25,000-character limit. Synonyms are separate words, not one text, and a
    join spec's `sql` is exactly [condition, relationship type]."""
    out = []
    if isinstance(space, dict):
        for k, v in space.items():
            here = f"{path}.{k}"
            if (k in TEXT_FIELDS and k != "synonyms" and isinstance(v, list)
                    and not (k == "sql" and ".join_specs[" in path)):
                out += [f"{here}[{i}]" for i, s in enumerate(v[:-1])
                        if not s.endswith("\n") and len(s) != G.MAX_STRING]
            out += run_together(v, here)
    elif isinstance(space, list):
        for i, v in enumerate(space):
            out += run_together(v, f"{path}[{i}]")
    return out


def sql_strings(space: dict) -> list[str]:
    ins = space.get("instructions", {})
    snip = ins.get("sql_snippets", {})
    out = [("expr", j["sql"][0]) for j in ins.get("join_specs", [])]
    out += [("expr", "".join(s["sql"])) for k in ("filters", "measures") for s in snip.get(k, [])]
    out += [("stmt", "".join(e["sql"])) for e in ins.get("example_question_sqls", [])]
    out += [("stmt", "".join(q["answer"][0]["content"]))
            for q in space.get("benchmarks", {}).get("questions", [])]
    return out


# ---------------------------------------------------------------------------
# A synthetic model: acme's SALES_DATA, written for Databricks, so its SQL travels
# ---------------------------------------------------------------------------


def _col(name, type_="string", **kw):
    return {"name": name, "type": type_, **kw}


def _metric(name, binding, tables, **kw):
    return {"name": name, "calculation": f"The {name}.", "bindings": {"Databricks": binding},
            "source_tables": tables, "review_state": "approved", **kw}


def _rel(a, b, card="many_to_one", **kw):
    return {"from_table": a, "to_table": b, "relationship": card, "review_state": "approved", **kw}


def _put(root: Path, rel: str, doc) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _write_model(root: Path) -> Path:
    def table(name, *cols, area=AREA, **kw):
        _put(root, f"subject_areas/{area}/tables/{name}.yaml",
             {"name": name, "schema": "SALES_DATA", "columns": list(cols), **kw})

    _put(root, "datasource.yaml", {
        "datasource": "acme", "description": "Acme demo sales.",
        "storage_connections": [{"name": "acme_dbx", "storage_type": "Databricks", "storage_config": {}}],
        "subject_areas": [f"subject_areas/{AREA}", "subject_areas/ops"],
        "key_terminology": {"GMV": "gross merchandise value", "AOV": "average order value"},
        "ambiguities": [{"name": "which amount", "when": "a question says amount",
                         "behaviour": "answer_and_offer",
                         "options": [{"label": "net", "how": "use orders.amount", "default": True},
                                     {"label": "gross", "how": "add tax"}]}]})
    _put(root, f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA, "description": "Orders and money."})
    table("orders", _col("id", "integer"), _col("customer_id", "integer"), _col("store_id", "integer"),
          _col("amount", "decimal", description="Order total.", unit="USD"), _col("status"),
          _col("placed_at", "date"), description="One row per order.",
          default_filters=["{alias}.status != 'void'", "{alias}.amount >"])
    table("customers", _col("id", "integer"), _col("region_id", "integer"), _col("parent_id", "integer"),
          _col("Segment Name", description="Retail or trade."), description="People who buy.")
    table("regions", _col("id", "integer"), _col("name"))
    table("stores", _col("id", "integer"), _col("name"))
    table("profiles", _col("customer_id", "integer"), _col("bio"))
    table("returns", _col("id", "integer"), _col("customer_id", "integer"), _col("amount", "decimal"))
    table("balances", _col("customer_id", "integer"), _col("snapshot_date", "date"), _col("balance", "decimal"))
    table("promos", _col("id", "integer"), source_type="sql", sql="SELECT 1 AS id")
    table("shipments", _col("id", "integer"), _col("order_id", "integer"), area="ops")
    _put(root, "subject_areas/ops/subject_area.yaml", {"name": "ops", "description": "Shipping."})
    _put(root, f"subject_areas/{AREA}/relationships.yaml", [
        _rel("orders", "customers", from_column="customer_id", to_column="id", description="Who ordered.",
             for_questions_about=["buyers"]),
        _rel("customers", "regions", from_column="region_id", to_column="id", join_type="INNER"),
        _rel("orders", "stores", on="orders.store_id = stores.id"),
        _rel("orders", "regions", on="orders.store_id = regions.id AND stores.id IS NOT NULL"),
        _rel("customers", "returns", "one_to_many", from_column="id", to_column="customer_id"),
        _rel("customers", "profiles", "one_to_one", from_column="id", to_column="customer_id"),
        _rel("customers", "customers", from_column="parent_id", to_column="id"),
        _rel("orders", "promos", from_column="id", to_column="id"),
    ])
    _put(root, f"subject_areas/{AREA}/entities/customer.yaml", {
        "name": "customer", "plural": "customers", "other_names": ["buyer", "Buyer"],
        "review_state": "approved", "maps_to": [{"table": "customers", "column": "id", "primary": True}]})
    metrics = [
        _metric("revenue", "SUM(orders.amount)", ["orders"], other_names=["sales", "takings"], unit="USD",
                description="Money in."),
        _metric("order count", "COUNT(DISTINCT orders.id)", ["orders"]),
        _metric("order-count", "COUNT(orders.id)", ["orders"]),
        _metric("average order value", "{revenue} / {order count}", ["orders"],
                base_metrics=["revenue", "order count"]),
        _metric("closing balance", "SUM(balances.balance)", ["balances"],
                non_additive_dimensions=["snapshot_date"], semi_additive_agg="last"),
        _metric("peak daily revenue", "MAX({revenue})", ["orders"], base_metrics=["revenue"],
                inner_grain=["placed_at"]),
        _metric("promo count", "COUNT(promos.id)", ["promos"]),
    ]
    for m in metrics:
        _put(root, f"subject_areas/{AREA}/metrics/{m['name'].replace(' ', '_')}.yaml", m)
    _put(root, f"prompt_examples/{AREA}/examples.yaml", [
        {"question": "What is revenue?", "sql": "SELECT SUM(amount) AS revenue\nFROM orders",
         "status": "confirmed"},
        {"question": "Revenue by region?", "status": "confirmed",
         "sql": "SELECT r.name, SUM(o.amount) FROM orders AS o JOIN customers AS c ON o.customer_id = c.id "
                "JOIN regions AS r ON c.region_id = r.id GROUP BY 1"},
        {"question": "Which stores sell most?", "status": "confirmed"},
        {"question": "Which stores sell most?", "status": "confirmed", "sql": "SELECT 1"},
        {"question": "Broken one", "sql": "SELECT FROM ((", "status": "confirmed"},
    ])
    _put(root, "golden_datasets/core.yaml", {"test_cases": [
        {"id": "g1", "query": "Total revenue", "expected": {"sql": "SELECT SUM(amount) FROM orders",
                                                             "sql_confirmed": True}},
        {"id": "g2", "query": "Customers", "expected": {"sql": "SELECT COUNT(*) FROM customers",
                                                         "sql_confirmed": True}},
        {"id": "g3", "query": "Unreadable", "expected": {"sql": "SELECT FROM ((", "sql_confirmed": True,
                                                          "tables_used": ["orders"]}},
    ]})
    return root


@pytest.fixture()
def model(tmp_path: Path) -> Path:
    return _write_model(tmp_path / "art" / "acme")


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(root: Path, out: Path, *flags: str) -> tuple[dict, dict[str, bytes]]:
    rc, text = _sm(str(root), "--target", "databricks", "--out", str(out), *flags)
    assert rc == 0, text
    base = out / "databricks"
    files = {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}
    return json.loads(text)["targets"]["databricks"], files


def _spaces(files: dict[str, bytes]) -> dict[str, dict]:
    return {k: json.loads(v) for k, v in files.items() if k.endswith(".geniespace.json")}


def _losses(section: dict) -> dict[tuple[str, str], list[tuple[str, str]]]:
    out: dict = {}
    for x in section["losses"]:
        out.setdefault((x["kind"], x["name"]), []).append((x["outcome"], x["reason"]))
    return out


@pytest.fixture()
def sample(tmp_path):
    return _export(SAMPLE, tmp_path / "s", *LOCATION)


@pytest.fixture()
def synthetic(model, tmp_path):
    return _export(model, tmp_path / "y", *LOCATION)


def _view(root: Path, area: str = AREA, location: str | None = "DEMO.SALES_DATA"):
    org = load_datasource(root)
    opts = X.ExportOptions(("databricks",), location=X.parse_location(location) if location else None)
    sa = next(s for s in org.subject_areas if s.name == area)
    view, _ = X.build_view(org, sa, X.TARGETS["databricks"], opts, X.source_engine(org), [], [])
    return view, opts


# ---------------------------------------------------------------------------
# The spec's success criteria
# ---------------------------------------------------------------------------


def test_the_sample_writes_one_space_and_one_bundle_resource_per_subject_area(sample, synthetic):
    _, files = sample
    assert {k for k in files if not k.startswith(("metric_views/", "pages/"))} == {
        "agami_example.geniespace.json", "resources/agami_example.genie.yml"}
    _, files = synthetic
    assert {k for k in files if not k.startswith(("metric_views/", "pages/"))} == {
        "sales.geniespace.json", "resources/sales.genie.yml",
        "ops.geniespace.json", "resources/ops.genie.yml"}


def test_every_space_passes_the_structural_validator(sample, synthetic):
    for _, files in (sample, synthetic):
        for name, space in _spaces(files).items():
            assert validate(space) == [], name


def test_no_text_field_runs_its_strings_together(sample, synthetic, model, tmp_path):
    # Joined as written, ["Money in.", "The revenue."] would be "Money in.The revenue.": every
    # string but the last ends a line.
    dbx_sample = tmp_path / "sample-dbx"
    import shutil
    shutil.copytree(SAMPLE, dbx_sample)
    for p in dbx_sample.rglob("storage.yaml"):
        p.write_text(p.read_text().replace("storage_type: SQLite", "storage_type: Databricks"))
    checked = 0
    for _, files in (sample, synthetic, _export(dbx_sample, tmp_path / "d", *LOCATION)):
        for name, space in _spaces(files).items():
            assert run_together(space) == [], name
            checked += 1
    assert checked == 4
    joins = _spaces(synthetic[1])["sales.geniespace.json"]["instructions"]["join_specs"]
    assert all(len(j["sql"]) == 2 and j["sql"][1].startswith("--rt=") for j in joins)


def test_the_check_for_run_together_text_catches_a_sentence_list(synthetic):
    space = _spaces(synthetic[1])["sales.geniespace.json"]
    space["instructions"]["sql_snippets"]["measures"][0]["instruction"] = ["One.", "Two."]
    assert run_together(space) == [".instructions.sql_snippets.measures[0].instruction[0]"]


def test_every_metric_view_ace170_wrote_is_listed_by_its_identifier(sample, synthetic):
    for _, files in (sample, synthetic):
        for area_file, space in _spaces(files).items():
            prefix = area_file.removesuffix(".geniespace.json") + "__"
            created = {v.decode().split("\n", 1)[0].removeprefix("CREATE OR REPLACE VIEW ")
                       for k, v in files.items() if k.startswith(f"metric_views/{prefix}")}
            listed = {m["identifier"] for m in space["data_sources"].get("metric_views", [])}
            assert listed == created
    assert len(_spaces(sample[1])["agami_example.geniespace.json"]["data_sources"]["metric_views"]) == 5


def test_every_sql_string_in_the_space_parses_as_databricks(sample, synthetic):
    for _, files in (sample, synthetic):
        for space in _spaces(files).values():
            for kind, sql in sql_strings(space):
                assert _parse(f"SELECT {sql}" if kind == "expr" else sql) is not None, sql
    kinds = {k for k, _ in sql_strings(_spaces(synthetic[1])["sales.geniespace.json"])}
    assert kinds == {"expr", "stmt"}  # join conditions and snippets, example SQL and benchmarks


def test_fifty_one_tables_write_fifty_and_report_the_last_as_target_limit(tmp_path):
    root = tmp_path / "art" / "acme"
    _put(root, "datasource.yaml", {
        "datasource": "acme", "subject_areas": [f"subject_areas/{AREA}"],
        "storage_connections": [{"name": "acme_dbx", "storage_type": "Databricks", "storage_config": {}}]})
    _put(root, f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA})
    for i in range(51):
        _put(root, f"subject_areas/{AREA}/tables/t{i:02}.yaml",
             {"name": f"t{i:02}", "schema": "SALES_DATA", "columns": [_col("id", "integer")]})
    section, files = _export(root, tmp_path / "o", *LOCATION)
    space = _spaces(files)["sales.geniespace.json"]
    assert [t["identifier"] for t in space["data_sources"]["tables"]] == [
        f"DEMO.SALES_DATA.t{i:02}" for i in range(50)]
    assert _losses(section)[("table", "t50")] == [("dropped", "target_limit")]
    assert validate(space) == []


def test_the_bundle_resource_names_the_space_by_relative_path_and_holds_no_warehouse_id(sample, tmp_path):
    _, files = sample
    text = files["resources/agami_example.genie.yml"].decode()
    doc = yaml.safe_load(text)
    assert set(doc) == {"variables", "resources"} and set(doc["resources"]) == {"genie_spaces"}
    (key, space), = doc["resources"]["genie_spaces"].items()
    assert key == "agami_example"
    assert set(space) <= {"title", "description", "warehouse_id", "file_path", "parent_path"}
    assert "serialized_space" not in space  # file_path and serialized_space are exclusive
    assert not Path(space["file_path"]).is_absolute()
    resolved = (Path("/b/resources") / space["file_path"]).resolve()
    assert resolved == Path("/b/agami_example.geniespace.json")
    assert space["warehouse_id"] == "${var.genie_warehouse_id}"
    assert "default" not in doc["variables"]["genie_warehouse_id"]
    # No id-shaped value anywhere: the warehouse is the customer's to name.
    assert not re.search(r"\b[0-9a-f]{16}\b", text)
    assert space["title"] == "agami-example" and space["description"].startswith("Acme Store")
    # A genie_spaces resource needs the direct engine, so the header says how to pick it.
    assert "direct deployment engine" in text and "CLI version" in text
    assert "#   bundle:\n#     engine: direct\n" in text


def test_two_runs_write_byte_identical_files_ids_included(model, tmp_path):
    for root in (SAMPLE, model):
        a = _export(root, tmp_path / "one", *LOCATION)
        b = _export(root, tmp_path / "two", *LOCATION)
        assert a == b and _spaces(a[1])


# ---------------------------------------------------------------------------
# How agami's model maps
# ---------------------------------------------------------------------------


def test_ids_come_from_export_id(synthetic, model):
    space = _spaces(synthetic[1])["sales.geniespace.json"]
    view, _ = _view(model)
    ins = space["instructions"]
    assert ins["text_instructions"][0]["id"] == view.id("text_instruction", AREA)
    assert view.id("relationship", "orders->customers") in {j["id"] for j in ins["join_specs"]}
    assert view.id("metric", "revenue") in {m["id"] for m in ins["sql_snippets"]["measures"]}
    assert view.id("golden", "core:g1") in {q["id"] for q in space["benchmarks"]["questions"]}
    assert view.id("example", "What is revenue?") in {e["id"] for e in ins["example_question_sqls"]}


def test_ambiguities_description_and_terminology_share_one_text_instruction(synthetic):
    section, files = synthetic
    (text,) = _spaces(files)["sales.geniespace.json"]["instructions"]["text_instructions"]
    content = "".join(text["content"])
    a, d, g = content.index("which amount"), content.index("Acme demo sales."), content.index("GMV")
    assert a < d < g and "net (default): use orders.amount" in content and "offer the others" in content
    losses = _losses(section)
    for key in (("ambiguity", "which amount"), ("datasource", "acme"), ("terminology", "GMV"),
                ("terminology", "AOV")):
        assert ("degraded", "carried_as_text") in losses[key]


def test_tables_carry_descriptions_and_column_configs(synthetic):
    section, files = synthetic
    tables = {t["identifier"]: t for t in _spaces(files)["sales.geniespace.json"]["data_sources"]["tables"]}
    orders = tables["DEMO.SALES_DATA.orders"]
    assert orders["description"] == ["One row per order."]
    assert orders["column_configs"] == [{"column_name": "amount", "description": ["Order total. Unit: USD."]}]
    customers = tables["DEMO.SALES_DATA.customers"]
    assert customers["column_configs"] == [  # sorted by code point, as Genie compares them
        {"column_name": "Segment Name", "description": ["Retail or trade."]},
        {"column_name": "id", "synonyms": ["customer", "customers", "buyer"]}]
    assert "DEMO.SALES_DATA.promos" not in tables  # defined by SQL: no Unity Catalog table
    assert _losses(section)[("table", "promos")] == [("dropped", "no_home_in_target")]


def test_relationships_become_join_specs_with_their_cardinality(synthetic):
    section, files = synthetic
    joins = {(j["left"]["alias"], j["right"]["alias"], j["sql"][1]): j
             for j in _spaces(files)["sales.geniespace.json"]["instructions"]["join_specs"]}
    oc = joins[("orders", "customers", "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--")]
    assert oc["sql"][0] == "`orders`.`customer_id` = `customers`.`id`"
    assert oc["comment"] == ["Who ordered."]
    assert oc["instruction"] == ["Join as LEFT JOIN.\n", "Use for questions about buyers."]
    assert joins[("orders", "stores", "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--")]["sql"][0] == (
        "`orders`.store_id = `stores`.id")
    assert ("customers", "returns", "--rt=FROM_RELATIONSHIP_TYPE_ONE_TO_MANY--") in joins
    assert ("customers", "profiles", "--rt=FROM_RELATIONSHIP_TYPE_ONE_TO_ONE--") in joins
    assert joins[("customers", "regions", "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--")][
        "instruction"] == ["Join as INNER JOIN."]
    losses = _losses(section)
    # ACE-170 reports what the metric views lose of these too; these entries are the space's.
    assert ("dropped", "target_rule") in losses[("relationship", "orders->regions")]
    assert ("dropped", "target_rule") in losses[("relationship", "customers->customers")]
    assert ("dropped", "depends_on_excluded") in losses[("relationship", "orders->promos")]
    # A join spec has no join type field: it travels as the spec's instruction text, and says so,
    # as the metric views and Ossie report the same field.
    assert ("degraded", "carried_as_text") in losses[("relationship", "customers->regions")]
    assert ("degraded", "carried_as_text") in losses[("relationship", "orders->customers")]


def test_default_filters_and_metrics_become_snippets(synthetic):
    section, files = synthetic
    snippets = _spaces(files)["sales.geniespace.json"]["instructions"]["sql_snippets"]
    (f,) = snippets["filters"]
    assert f["sql"] == ["`orders`.status != 'void'"] and "alias" not in f
    # Descriptive, as in agami: "apply unless the question says otherwise" can read as always on,
    # which would filter a metric whose definition is unfiltered.
    assert f["instruction"] == [X.DEFAULT_FILTER_NOTE] and f["display_name"] == "orders optional filter 1"
    assert "only when the question asks" in X.DEFAULT_FILTER_NOTE and "unless" not in X.DEFAULT_FILTER_NOTE
    measures = {m["display_name"]: m for m in snippets["measures"]}
    assert measures["revenue"]["sql"] == ["SUM(orders.amount)"] and measures["revenue"]["alias"] == "revenue"
    assert measures["revenue"]["synonyms"] == ["sales", "takings"]
    assert measures["revenue"]["instruction"] == ["Money in.\n", "The revenue.\n", "Unit: USD."]
    assert "{" not in "".join(measures["average order value"]["sql"])
    assert "last value" in " ".join(measures["closing balance"]["instruction"])
    losses = _losses(section)
    assert losses[("default_filter", "orders#2")] == [("dropped", "unparseable_for_target")]
    assert ("degraded", "carried_as_text") in losses[("metric", "closing balance")]
    assert ("dropped", "name_collision") in losses[("metric", "order-count")]
    assert ("dropped", "target_rule") in losses[("metric", "peak daily revenue")]
    assert ("dropped", "depends_on_excluded") in losses[("metric", "promo count")]
    assert {"peak daily revenue", "order-count", "promo count"}.isdisjoint(measures)


def test_examples_become_example_sql_or_sample_questions_and_golden_items_benchmarks(synthetic):
    section, files = synthetic
    space = _spaces(files)["sales.geniespace.json"]
    examples = {e["question"][0]: e for e in space["instructions"]["example_question_sqls"]}
    assert set(examples) == {"What is revenue?", "Revenue by region?"}
    # Qualified as the space lists the table, so it does not depend on a default schema.
    assert examples["What is revenue?"]["sql"] == ["SELECT SUM(amount) AS revenue\n",
                                                   "FROM DEMO.SALES_DATA.orders"]
    # Every example question is a sample question too, with or without its SQL: the Genie UI
    # offers only sample questions. An example and its sample question share an id.
    samples = {q["question"][0]: q["id"] for q in space["config"]["sample_questions"]}
    assert set(samples) == {"What is revenue?", "Revenue by region?", "Which stores sell most?"}
    assert all(samples[q] == e["id"] for q, e in examples.items())
    bench = {q["question"][0]: q for q in space["benchmarks"]["questions"]}
    assert set(bench) == {"Total revenue", "Customers"}
    assert bench["Total revenue"]["answer"] == [{"format": "SQL",
                                                 "content": ["SELECT SUM(amount) FROM DEMO.SALES_DATA.orders"]}]
    losses = _losses(section)
    assert losses[("example", "Which stores sell most?")] == [("dropped", "name_collision")]
    assert losses[("example", "Broken one")] == [("dropped", "unparseable_for_target")]
    assert losses[("golden", "core:g3")] == [("dropped", "unparseable_for_target")]


def test_a_databricks_model_whose_examples_all_have_sql_still_has_sample_questions(model):
    view, opts = _view(model)
    view = dataclasses.replace(view, examples=_questions("example", 3))
    space = json.loads(G.write_genie_space(view, opts).files["sales.geniespace.json"])
    asked = [q["question"] for q in space["config"]["sample_questions"]]
    assert sorted(asked) == [[f"Question {i:03}?"] for i in range(3)]
    assert [q["id"] for q in space["config"]["sample_questions"]] == [
        e["id"] for e in space["instructions"]["example_question_sqls"]]
    assert validate(space) == []


def test_a_golden_item_without_sql_for_databricks_is_no_benchmark(model):
    view, opts = _view(model)
    view = dataclasses.replace(view, golden=(X.ExportQuestion("golden", "core:x", "Total?", None),))
    out = G.write_genie_space(view, opts)
    assert "benchmarks" not in json.loads(out.files["sales.geniespace.json"])
    assert [(x.name, x.reason) for x in out.losses if x.kind == "golden"] == [("core:x", "target_rule")]


def test_without_a_catalog_nothing_is_listed_and_no_space_is_written(model, tmp_path):
    section, files = _export(model, tmp_path / "n")
    assert not _spaces(files) and not [k for k in files if k.startswith("resources/")]
    losses = _losses(section)
    assert losses[("table", "orders")] == [("dropped", "target_rule")]
    assert ("dropped", "target_rule") in losses[("metric_view", "sales__orders")]
    # Other databricks artifacts report on the subject area too; only this one drops it.
    assert ("dropped", "target_rule") in losses[("subject_area", AREA)]


# ---------------------------------------------------------------------------
# Genie's limits, each dropped from the end of its stable order
# ---------------------------------------------------------------------------


def _questions(kind: str, n: int, sql: str | None = "SELECT 1"):
    return tuple(X.ExportQuestion(kind, f"q{i:03}", f"Question {i:03}?", sql) for i in range(n))


def test_instructions_stop_at_one_hundred_counting_the_text_block(model):
    view, opts = _view(model)
    out = G.write_genie_space(dataclasses.replace(view, examples=_questions("example", 120)), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    assert len(space["instructions"]["example_question_sqls"]) == 99
    # Deliberately changed: sample questions have a limit of their own, so an example whose SQL
    # the instruction limit cut is still offered as a sample question, and loses only its SQL.
    assert len(space["config"]["sample_questions"]) == 120
    limited = sorted((x.name, x.outcome) for x in out.losses if x.reason == "target_limit")
    assert limited == [(f"q{i:03}", "degraded") for i in range(99, 120)]
    detail = next(x.detail for x in out.losses if x.reason == "target_limit")
    assert "at most 100 instructions" in detail and "99" not in detail
    assert validate(space) == []


def test_columns_named_alike_in_any_case_are_configured_once(model):
    view, opts = _view(model)
    from semantic_model.models import Column

    extra = [Column(name="Status", type="string", description="Shadow."),
             Column(name="AMOUNT", type="decimal", description="Shadow too.")]
    tables = tuple(dataclasses.replace(et, table=et.table.model_copy(
        update={"columns": [*et.table.columns, *extra]})) if et.table.name == "orders" else et
        for et in view.tables)
    out = G.write_genie_space(dataclasses.replace(view, tables=tables), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    orders = next(t for t in space["data_sources"]["tables"] if t["identifier"].endswith(".orders"))
    names = [c["column_name"].casefold() for c in orders["column_configs"]]
    assert len(names) == len(set(names)) and validate(space) == []
    # The first in the model's order is kept; the other is reported.
    amount = next(c for c in orders["column_configs"] if c["column_name"].casefold() == "amount")
    assert amount["column_name"] == "amount" and "Shadow" not in "".join(amount["description"])
    got = {(x.name, x.reason) for x in out.losses if x.kind == "column"}
    assert {("orders.Status", "name_collision"), ("orders.AMOUNT", "name_collision")} <= got


def test_synonyms_and_column_configs_past_genies_limits_are_reported(model, monkeypatch):
    monkeypatch.setattr(G, "MAX_ITEMS", 3)
    monkeypatch.setattr(G, "MAX_STRING", 40)
    view, opts = _view(model)
    from semantic_model.models import Entity

    words = ["buyer", "client", "patron", "shopper", "x" * 41]
    entity = Entity(name="customer", other_names=words, review_state="approved",
                    maps_to=[{"table": "customers", "column": "id", "primary": True}])
    out = G.write_genie_space(dataclasses.replace(view, entities=(entity,)), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    customers = next(t for t in space["data_sources"]["tables"]
                     if t["identifier"].endswith(".customers"))
    configs = customers["column_configs"]
    assert len(configs) <= 3
    syn = next(c for c in configs if c["column_name"] == "id")["synonyms"]
    assert syn == ["customer", "buyer", "client"]           # the long one left out, never cut
    losses = {(x.kind, x.name, x.reason) for x in out.losses}
    assert ("entity", "customer", "target_limit") in losses      # the word past the string limit
    assert ("column", "customers.id", "target_limit") in losses  # the list past the item limit


def test_column_configs_past_the_item_limit_are_cut_and_reported(model, monkeypatch):
    monkeypatch.setattr(G, "MAX_ITEMS", 2)
    view, opts = _view(model)
    from semantic_model.models import Column

    extra = [Column(name=f"note_{i}", type="string", description="A note.") for i in range(3)]
    tables = tuple(dataclasses.replace(et, table=et.table.model_copy(
        update={"columns": [*et.table.columns, *extra]})) if et.table.name == "orders" else et
        for et in view.tables)
    out = G.write_genie_space(dataclasses.replace(view, tables=tables), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    orders = next(t for t in space["data_sources"]["tables"] if t["identifier"].endswith(".orders"))
    assert len(orders["column_configs"]) == 2
    assert ("table", "orders", "target_limit") in {(x.kind, x.name, x.reason) for x in out.losses}


def test_two_identical_default_filters_on_one_table_get_two_ids(model):
    # Genie rejects a space whose instruction ids repeat; a filter's id is its positional name.
    view, opts = _view(model)
    f = "{alias}.status != 'void'"
    tables = tuple(dataclasses.replace(et, table=et.table.model_copy(update={"default_filters": [f, f]}))
                   if et.table.name == "orders" else et for et in view.tables)
    out = G.write_genie_space(dataclasses.replace(view, tables=tables), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    ids = [x["id"] for x in space["instructions"]["sql_snippets"]["filters"]]
    assert sorted(ids) == sorted(view.id("default_filter", f"orders#{i}") for i in (1, 2))
    assert validate(space) == []


def test_the_validator_rejects_a_repeated_instruction_id(model):
    view, opts = _view(model)
    space = json.loads(G.write_genie_space(view, opts).files["sales.geniespace.json"])
    (f,) = space["instructions"]["sql_snippets"]["filters"]
    space["instructions"]["sql_snippets"]["filters"] = [f, dict(f)]
    assert "instruction ids are not unique" in validate(space)


def test_benchmarks_stop_at_five_hundred(model):
    view, opts = _view(model)
    out = G.write_genie_space(dataclasses.replace(view, golden=_questions("golden", 501)), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    assert len(space["benchmarks"]["questions"]) == 500
    assert [x.name for x in out.losses if x.reason == "target_limit"] == ["q500"]
    assert validate(space) == []


def test_knowledge_store_snippets_stop_at_two_hundred(model, monkeypatch):
    # Descriptions are spent first, then joins, filters and measures, each in name order.
    monkeypatch.setattr(G, "MAX_SNIPPETS", 4)
    view, opts = _view(model)
    out = G.write_genie_space(view, opts)
    space = json.loads(out.files["sales.geniespace.json"])
    described = [t for t in space["data_sources"]["tables"] if "description" in t]
    assert len(described) == 2 and len(space["instructions"]["join_specs"]) == 2
    assert "sql_snippets" not in space["instructions"]
    limited = {(x.kind, x.name, x.outcome) for x in out.losses if x.reason == "target_limit"}
    assert ("metric", "revenue", "dropped") in limited
    assert ("default_filter", "orders#1", "dropped") in limited
    monkeypatch.setattr(G, "MAX_SNIPPETS", 1)
    out = G.write_genie_space(view, opts)
    assert ("table", "orders", "degraded", "target_limit") in {
        (x.kind, x.name, x.outcome, x.reason) for x in out.losses}


def test_what_hangs_from_a_source_left_out_goes_with_it(model, monkeypatch):
    monkeypatch.setattr(G, "MAX_DATA_SOURCES", 1)  # balances alone, first by name
    monkeypatch.setattr(G, "MAX_ITEMS", 2)          # and a text instruction cut at two strings
    view, opts = _view(model)
    out = G.write_genie_space(view, opts)
    space = json.loads(out.files["sales.geniespace.json"])
    assert [t["identifier"] for t in space["data_sources"]["tables"]] == ["DEMO.SALES_DATA.balances"]
    assert "metric_views" not in space
    assert len(space["instructions"]["text_instructions"][0]["content"]) <= 2
    losses = {(x.kind, x.name): (x.outcome, x.reason) for x in out.losses}
    # What the cut left out is reported past the limit, never as carried in the text.
    cut = {k for k, v in losses.items() if v == ("dropped", "target_limit")
           and k[0] in ("ambiguity", "terminology", "datasource", "subject_area")}
    carried = {(x.kind, x.name) for x in out.losses if x.reason == "carried_as_text"}
    assert cut and not cut & carried
    assert losses[("default_filter", "orders#1")] == ("dropped", "depends_on_excluded")
    assert losses[("metric", "revenue")] == ("dropped", "depends_on_excluded")
    assert losses[("metric_view", "sales__orders")] == ("dropped", "target_limit")
    assert validate(space) == []


def test_a_metric_view_ace170_does_not_create_is_not_listed(model, monkeypatch):
    real = DB.definition_body
    monkeypatch.setattr(DB, "definition_body", lambda d: None if DB.dollar_fields(d) else real(d))
    path = model / f"subject_areas/{AREA}/metrics/closing_balance.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["description"] = "Costs $$ to hold."
    path.write_text(yaml.safe_dump(doc))
    view, opts = _view(model)
    space = json.loads(G.write_genie_space(view, opts).files["sales.geniespace.json"])
    listed = {m["identifier"] for m in space["data_sources"]["metric_views"]}
    assert "DEMO.SALES_DATA.sales__balances" not in listed and "DEMO.SALES_DATA.sales__orders" in listed


def test_long_text_is_split_within_the_string_limit_and_joins_back():
    text = "a" * 60_000 + "\nSELECT 1"
    parts = G.text_field(text)
    assert "".join(parts) == text and max(map(len, parts)) <= 25_000 and len(parts) == 4
    assert parts[2] == "a" * 10_000 + "\n" and parts[3] == "SELECT 1"
    # Sentences become lines, and only a line end splits: a lone carriage return is text.
    assert G.lines_field(["One.", "Two.", "Three."]) == ["One.\n", "Two.\n", "Three."]
    assert G.text_field("a\rb\r\nc\n") == ["a\rb\r\n", "c\n"] and G.text_field("") == []
    assert G.parses("1 +", DB.ENGINE, as_expression=True) is False
    assert G.parses("SELECT 1", DB.ENGINE) is True


# ---------------------------------------------------------------------------
# Carried SQL names each table as the space lists it
# ---------------------------------------------------------------------------
# Every table in example and benchmark SQL is written catalog.schema.table, as the space lists
# it, so the SQL does not depend on a default catalog or schema.


def _dbx_sample(tmp_path: Path) -> Path:
    import shutil

    dst = tmp_path / "sample-dbx"
    shutil.copytree(SAMPLE, dst)
    for p in dst.rglob("storage.yaml"):
        p.write_text(p.read_text().replace("storage_type: SQLite", "storage_type: Databricks"))
    return dst


def _with_questions(model: Path, examples=(), golden=()):
    view, opts = _view(model)
    view = dataclasses.replace(view, examples=tuple(examples), golden=tuple(golden))
    out = G.write_genie_space(view, opts)
    return json.loads(out.files["sales.geniespace.json"]), out


def test_every_table_in_example_and_benchmark_sql_is_a_listed_three_part_name(synthetic, tmp_path):
    checked = 0
    for _, files in (synthetic, _export(_dbx_sample(tmp_path), tmp_path / "d", *LOCATION)):
        for name, space in _spaces(files).items():
            sources = space["data_sources"]
            idents = {s["identifier"] for k in ("tables", "metric_views") for s in sources.get(k, [])}
            for kind, sql in sql_strings(space):
                if kind == "stmt":
                    assert _table_ref_errors(sql, idents) == [], (name, sql)
                    checked += len(list(_parse(sql).find_all(exp.Table)))
    assert checked > 10


def test_a_cte_is_left_alone_and_every_base_table_is_qualified(model):
    q = [("cte", "WITH big AS (SELECT amount FROM orders WHERE amount > 10)\nSELECT COUNT(*) FROM big",
          "WITH big AS (SELECT amount FROM DEMO.SALES_DATA.orders WHERE amount > 10)\n"
          "SELECT COUNT(*) FROM big"),
         ("shadow", "WITH orders AS (SELECT 1 AS amount)\nSELECT SUM(amount) FROM orders",
          "WITH orders AS (SELECT 1 AS amount)\nSELECT SUM(amount) FROM orders"),
         ("join", "SELECT c.id FROM orders AS o JOIN `customers` c ON o.customer_id = c.id",
          "SELECT c.id FROM DEMO.SALES_DATA.orders AS o JOIN DEMO.SALES_DATA.customers c "
          "ON o.customer_id = c.id"),
         ("schema", "SELECT orders.amount FROM SALES_DATA.orders",
          "SELECT orders.amount FROM DEMO.SALES_DATA.orders")]
    space, _ = _with_questions(model, [X.ExportQuestion("example", n, f"{n}?", sql) for n, sql, _ in q],
                               [X.ExportQuestion("golden", f"core:{n}", f"{n}!", sql) for n, sql, _ in q])
    examples = {e["question"][0]: "".join(e["sql"]) for e in space["instructions"]["example_question_sqls"]}
    bench = {b["question"][0]: "".join(b["answer"][0]["content"]) for b in space["benchmarks"]["questions"]}
    assert examples == {f"{n}?": want for n, _, want in q}
    assert bench == {f"{n}!": want for n, _, want in q}
    assert validate(space) == []


def test_sql_that_cannot_be_qualified_is_never_written_unqualified(model):
    # promos is defined by SQL and shipments is another area's: neither is listed. A column
    # qualified by its schema is a shape the rewrite will not guess at.
    bad = {"outside": "SELECT COUNT(*) FROM shipments", "promo": "SELECT COUNT(*) FROM promos",
           "schema column": "SELECT SALES_DATA.orders.amount FROM SALES_DATA.orders"}
    space, out = _with_questions(
        model, [X.ExportQuestion("example", n, f"{n}?", sql) for n, sql in bad.items()],
        [X.ExportQuestion("golden", f"core:{n}", f"{n}!", sql) for n, sql in bad.items()])
    assert "example_question_sqls" not in space["instructions"] and "benchmarks" not in space
    # An example keeps its question, offered as a sample question.
    assert sorted(q["question"][0] for q in space["config"]["sample_questions"]) == sorted(
        f"{n}?" for n in bad)
    assert not [sql for kind, sql in sql_strings(space) if kind == "stmt"]
    losses = {(x.kind, x.name): (x.outcome, x.reason) for x in out.losses}
    for n, reason in (("outside", "depends_on_excluded"), ("promo", "depends_on_excluded"),
                      ("schema column", "target_rule")):
        assert losses[("example", n)] == ("degraded", reason)
        assert losses[("golden", f"core:{n}")] == ("dropped", reason)
    assert validate(space) == []


# ---------------------------------------------------------------------------
# Sensitive columns: counted by default, hidden under --genie-hide-sensitive
# ---------------------------------------------------------------------------


def _sensitive() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for sa in load_datasource(SAMPLE).subject_areas:
        for t in sa.tables_defined:
            out.setdefault(t.name, set()).update(c.name for c in t.columns if c.sensitive)
    return {t: cs for t, cs in out.items() if cs}


def _names_re(names) -> re.Pattern:
    return re.compile(rf"(?<![\w$])({'|'.join(map(re.escape, names))})(?![\w$])", re.I)


def test_by_default_no_sensitive_column_is_named_and_the_report_counts_the_unhidden_ones(sample):
    section, files = sample
    sensitive = _sensitive()
    names = _names_re(sorted(set().union(*sensitive.values())))
    assert sum(map(len, sensitive.values())) == 2
    for rel, data in files.items():
        assert not names.search(data.decode("utf-8")), rel
    space = _spaces(files)["agami_example.geniespace.json"]
    assert not any(c.get("exclude") for t in space["data_sources"]["tables"]
                   for c in t.get("column_configs", []))
    (entry,) = [x for x in section["losses"] if x["reason"] == "sensitive" and x["kind"] == "subject_area"]
    assert entry["outcome"] == "degraded" and entry["name"] == "agami-example"
    assert entry["detail"].startswith("2 sensitive column(s)") and "--genie-hide-sensitive" in entry["detail"]
    assert "column mask" in entry["detail"] and not names.search(entry["detail"])


def test_the_hide_flag_excludes_each_sensitive_column_and_names_it_nowhere_else(tmp_path):
    sensitive = _sensitive()
    names = _names_re(sorted(set().union(*sensitive.values())))
    section, files = _export(SAMPLE, tmp_path / "h", *LOCATION, "--genie-hide-sensitive")
    space = _spaces(files)["agami_example.geniespace.json"]
    assert validate(space) == []
    hidden = {}
    for t in space["data_sources"]["tables"]:
        for c in t.get("column_configs", []):
            if "exclude" in c:
                assert c == {"column_name": c["column_name"], "exclude": True}
                hidden.setdefault(t["identifier"].rsplit(".", 1)[1], set()).add(c["column_name"])
    assert hidden == sensitive
    # The space file is the one place a name is written: not the report, not another file.
    for rel, data in files.items():
        if rel != "agami_example.geniespace.json":
            assert not names.search(data.decode("utf-8")), rel
    report = (tmp_path / "h" / "export-report.json").read_text(encoding="utf-8")
    assert not names.search(report)
    assert json.loads(report)["options"]["target_args"] == {"genie_hide_sensitive": True}
    assert not [x for x in section["losses"] if x["kind"] == "subject_area" and x["reason"] == "sensitive"]
    assert _export(SAMPLE, tmp_path / "h2", *LOCATION, "--genie-hide-sensitive")[1] == files


def test_the_hide_flag_says_what_it_writes_and_that_it_is_not_access_control():
    import argparse

    sp = argparse.ArgumentParser()
    G.add_arguments(sp)
    (action,) = [a for a in sp._actions if a.dest == G.HIDE_SENSITIVE]
    assert "names into the space file" in action.help and "not access control" in action.help
    assert "column mask" in action.help and vars(sp.parse_args([])) == {}
    assert "--genie-hide-sensitive" in G.__doc__ and "not access control" in G.__doc__


# ---------------------------------------------------------------------------
# What else agami holds: fiscal year, time window, caveats, labels
# ---------------------------------------------------------------------------


def test_the_text_instruction_carries_a_fiscal_year_and_a_default_time_window(model, synthetic):
    view, opts = _view(model)
    view = dataclasses.replace(view, fiscal_year_start_month=2, default_time_window="the last 90 days")
    out = G.write_genie_space(view, opts)
    space = json.loads(out.files["sales.geniespace.json"])
    content = "".join(space["instructions"]["text_instructions"][0]["content"])
    assert "Fiscal year: the fiscal year starts in February;" in content
    assert "when a question names no time range, use the last 90 days" in content
    assert content.index("Acme demo sales.") < content.index("Fiscal year") < content.index("GMV")
    losses = {(x.kind, x.name, x.detail) for x in out.losses if x.reason == "carried_as_text"}
    assert ("datasource", "acme", "its fiscal-year start is in the space's text instruction") in losses
    assert ("subject_area", AREA, "its default time window is in the space's text instruction") in losses
    assert run_together(space) == [] and validate(space) == []
    # January is the default, and says nothing.
    plain = "".join(_spaces(synthetic[1])["sales.geniespace.json"]["instructions"]["text_instructions"][0][
        "content"])
    assert "Fiscal year" not in plain and "time window" not in plain


def _coded(view):
    def table(et):
        if et.table.name != "orders":
            return et
        cols = [c.model_copy(update={"choice_field": {"p": "pending", "v": "void", "x": "X", "q": ""},
                                     "caveats": ["Legacy rows are blank."]}) if c.name == "status"
                else c.model_copy(update={"choice_field": {"1": "North"}}) if c.name == "store_id"
                else c for c in et.table.columns]
        return dataclasses.replace(et, table=et.table.model_copy(update={
            "caveats": ["Refunds are not netted."], "columns": cols}))

    metrics = tuple(dataclasses.replace(m, metric=m.metric.model_copy(update={"caveats": ["Excludes tax."]}))
                    if m.metric.name == "revenue" else m for m in view.metrics)
    return dataclasses.replace(view, tables=tuple(map(table, view.tables)), metrics=metrics)


def test_caveats_labels_and_entity_matching_reach_the_space(model):
    view, opts = _view(model)
    out = G.write_genie_space(_coded(view), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    orders = next(t for t in space["data_sources"]["tables"] if t["identifier"] == "DEMO.SALES_DATA.orders")
    assert orders["description"] == ["One row per order.\n", "Caveat: Refunds are not netted."]
    configs = {c["column_name"]: c for c in orders["column_configs"]}
    # A label that repeats its code or is blank is left out; matching needs a string column.
    assert configs["status"] == {"column_name": "status",
                                 "description": ["Labels: p = pending; v = void. Caveat: Legacy rows are blank."],
                                 "enable_entity_matching": True, "enable_format_assistance": True}
    assert configs["store_id"] == {"column_name": "store_id", "description": ["Labels: 1 = North."]}
    # A coded number has no value dictionary in a space: its labels are only text, and say so.
    losses = {(x.kind, x.name): (x.outcome, x.reason) for x in out.losses}
    assert losses[("column", "orders.store_id")] == ("degraded", "carried_as_text")
    assert ("column", "orders.status") not in losses
    measures = {m["display_name"]: m for m in space["instructions"]["sql_snippets"]["measures"]}
    assert measures["revenue"]["instruction"][-1] == "Caveat: Excludes tax."
    assert validate(space) == [] and run_together(space) == []


def test_entity_matching_stops_at_its_column_limit(model, monkeypatch):
    monkeypatch.setattr(G, "MAX_ENTITY_MATCHING", 0)
    view, opts = _view(model)
    out = G.write_genie_space(_coded(view), opts)
    space = json.loads(out.files["sales.geniespace.json"])
    configs = [c for t in space["data_sources"]["tables"] for c in t.get("column_configs", [])]
    assert not any("enable_entity_matching" in c for c in configs)
    assert ("column", "orders.status", "degraded", "target_limit") in {
        (x.kind, x.name, x.outcome, x.reason) for x in out.losses}


def test_the_bundle_resource_says_how_to_run_the_benchmarks(synthetic, sample):
    text = synthetic[1]["resources/sales.genie.yml"].decode()
    assert "POST /api/2.0/genie/spaces/{space_id}/eval-runs" in text
    assert "see Databricks' documentation for this API's current" in text
    assert "/eval-runs/{eval_run_id}/results" in text and "4 significant digits" in text
    assert set(yaml.safe_load(text)) == {"variables", "resources"}
    # The sample ships no golden dataset, so its space has no benchmarks to run.
    assert "eval-runs" not in sample[1]["resources/agami_example.genie.yml"].decode()


# ---------------------------------------------------------------------------
# The validator itself, and the seam
# ---------------------------------------------------------------------------


def _first(space, *path):
    obj = space
    for p in path:
        obj = obj[p]
    return obj


@pytest.mark.parametrize("break_it, needle", [
    (lambda s: s.update(version=1), "version must be 2"),
    (lambda s: s.update(version="2"), "version must be 2"),
    (lambda s: s.update(extra=1), "space key extra"),
    (lambda s: _first(s, "instructions", "join_specs", 0).update(id="ABC"), "id"),
    (lambda s: _first(s, "instructions", "join_specs", 0).update(id="A" * 32), "id"),
    (lambda s: _first(s, "instructions", "join_specs").reverse(), "join is not sorted"),
    (lambda s: _first(s, "data_sources", "tables").reverse(), "tables is not sorted"),
    (lambda s: _first(s, "data_sources", "tables", 1, "column_configs").reverse(), "column_configs is not"),
    (lambda s: _first(s, "config", "sample_questions").append(
        dict(_first(s, "benchmarks", "questions", 0), answer=None)), "question ids"),
    (lambda s: _first(s, "instructions", "sql_snippets", "measures", 0).update(
        id=_first(s, "instructions", "join_specs", 0, "id")), "instruction ids"),
    (lambda s: _first(s, "instructions", "text_instructions").append(
        {"id": "f" * 32, "content": ["x"]}), "more than one text instruction"),
    (lambda s: _first(s, "instructions", "text_instructions", 0).update(content="x"), "list of strings"),
    (lambda s: _first(s, "instructions", "text_instructions", 0).update(content=["x" * 25_001]), "25,000"),
    (lambda s: _first(s, "instructions", "join_specs", 0).update(sql=["`a`.`b` = `c`.`d`"]), "join sql"),
    (lambda s: _first(s, "instructions", "join_specs", 0)["sql"].__setitem__(1, "--rt=MANY--"), "join sql"),
    (lambda s: _first(s, "instructions", "join_specs", 0)["sql"].__setitem__(0, "`x`.`id` = 1"), "besides"),
    (lambda s: _first(s, "benchmarks", "questions", 0)["answer"].append(
        {"format": "SQL", "content": ["SELECT 1"]}), "exactly one SQL answer"),
    (lambda s: _first(s, "benchmarks", "questions", 0)["answer"][0].update(format="TEXT"), "one SQL answer"),
    (lambda s: _first(s, "data_sources", "tables", 0).update(identifier="SALES_DATA.orders"), "catalog"),
    (lambda s: _first(s, "instructions", "sql_snippets", "filters", 0).update(alias="f"), "alias"),
    (lambda s: _first(s, "instructions", "sql_snippets", "measures", 0).update(sql=[" "]), "empty"),
    (lambda s: _first(s, "data_sources", "tables").extend(
        {"identifier": f"a.b.z{i:02}"} for i in range(50)), "more than 50"),
    (lambda s: _first(s, "config", "sample_questions").extend(
        {"id": "f" * 32, "question": ["q"]} for _ in range(10_001)), "10,000 items"),
    (lambda s: _first(s, "instructions", "example_question_sqls", 0).update(
        sql=["SELECT 1 FROM orders"]), "not catalog.schema.table"),
    (lambda s: _first(s, "instructions", "example_question_sqls", 0).update(
        sql=["SELECT 1 FROM DEMO.SALES_DATA.shipments"]), "not a listed data source"),
    (lambda s: _first(s, "benchmarks", "questions", 0)["answer"][0].update(
        content=["SELECT 1 FROM SALES_DATA.orders"]), "not catalog.schema.table"),
    (lambda s: _first(s, "data_sources", "tables", 0).setdefault("column_configs", []).append(
        {"column_name": "zz", "exclude": "yes"}), "not a boolean"),
    (lambda s: _first(s, "data_sources", "tables", 0).setdefault("column_configs", []).append(
        {"column_name": "zz", "enable_entity_matching": True}), "needs format assistance"),
    (lambda s: _first(s, "data_sources", "tables", 0).setdefault("column_configs", []).append(
        {"column_name": "zz", "hidden": True}), "column key hidden"),
])
def test_the_validator_rejects_each_kind_of_bad_space(synthetic, break_it, needle):
    space = _spaces(synthetic[1])["sales.geniespace.json"]
    assert validate(space) == []
    break_it(space)
    assert any(needle in e for e in validate(space)), validate(space)


def test_the_validator_counts_each_documented_limit():
    base = {"version": 2, "data_sources": {"tables": [{"identifier": "a.b.c"}]}}
    many = [{"id": f"{i:032x}", "question": ["q"], "sql": ["SELECT 1"]} for i in range(101)]
    assert "more than 100 instructions" in validate({**base, "instructions": {"example_question_sqls": many}})
    snips = [{"id": f"{i:032x}", "sql": ["1 = 1"]} for i in range(201)]
    assert "more than 200 knowledge-store snippets" in validate(
        {**base, "instructions": {"sql_snippets": {"filters": snips}}})
    bench = [{"id": f"{i:032x}", "question": ["q"], "answer": [{"format": "SQL", "content": ["SELECT 1"]}]}
             for i in range(501)]
    assert "more than 500 benchmark questions" in validate({**base, "benchmarks": {"questions": bench}})
    cols = [{"column_name": f"c{i:03}", "enable_entity_matching": True, "enable_format_assistance": True}
            for i in range(121)]
    assert "entity matching on more than 120 columns" in validate(
        {**base, "data_sources": {"tables": [{"identifier": "a.b.c", "column_configs": cols}]}})
    assert validate([]) == ["space is not an object"]


def test_the_artifact_is_registered_with_databricks_and_the_privacy_scan_covers_it():
    import importlib.util

    assert G.write_genie_space in DB.ARTIFACTS
    spec = importlib.util.spec_from_file_location("privacy", REPO / "tests" / "test_privacy_no_network.py")
    privacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(privacy)
    module = PKG_SRC / "semantic_model" / "export_databricks_genie.py"
    assert module in privacy.SCRIPTS
    assert not any(rx.search(module.read_text(encoding="utf-8")) for rx in privacy._FORBIDDEN_RE)


def test_sql_naming_another_schemas_table_is_never_pointed_at_the_listed_one(model):
    bad = {"archive": "SELECT SUM(amount) FROM archive.orders",
           "other catalog": "SELECT SUM(amount) FROM otherdb.otherschema.orders",
           "dynamic": "SELECT SUM(amount) FROM IDENTIFIER('orders')"}
    space, out = _with_questions(
        model, [X.ExportQuestion("example", n, f"{n}?", sql) for n, sql in bad.items()],
        [X.ExportQuestion("golden", f"core:{n}", f"{n}!", sql) for n, sql in bad.items()])
    assert not [sql for kind, sql in sql_strings(space) if kind == "stmt"]
    losses = {(x.kind, x.name): x.outcome for x in out.losses}
    assert all(losses[("golden", f"core:{n}")] == "dropped" for n in bad)
    assert all(losses[("example", n)] == "degraded" for n in bad)
    assert validate(space) == []


def test_a_join_type_alone_travels_in_the_singular(synthetic):
    section, _ = synthetic
    details = [x["detail"] for x in section["losses"]
               if x["kind"] == "relationship" and x["reason"] == "carried_as_text"]
    assert details and all(d.startswith(("its join type travels as", "its join type and the "
                                         "questions it is for travel as")) for d in details)
    assert any(d.endswith("no field for it") for d in details)


def test_every_writer_names_months_from_one_locale_free_list():
    from semantic_model import export_snowflake as S
    from semantic_model import export_snowflake_agent as A

    for module in (G, S, A):
        assert not hasattr(module, "_MONTHS") and not hasattr(module, "calendar")
    assert X.MONTH_NAMES[3] == "April" and len(X.MONTH_NAMES) == 12
    view = X.ExportView(**{**{f.name: None for f in dataclasses.fields(X.ExportView)},
                           "fiscal_year_start_month": 4})
    assert "starts in April" in A._fiscal_year(view)


def test_the_hide_flag_is_described_as_a_space_setting_not_as_genie_behaviour(sample):
    import argparse

    sp = argparse.ArgumentParser()
    G.add_arguments(sp)
    (action,) = [a for a in sp._actions if a.dest == G.HIDE_SENSITIVE]
    section, _ = sample
    (entry,) = [x for x in section["losses"] if x["reason"] == "sensitive"
                and x["kind"] == "subject_area"]
    for text in (action.help, entry["detail"], G.__doc__):
        flat = " ".join(text.split())
        # What the setting is, what it is not, and what restricts reading a column.
        assert "hidden in the space" in flat, text
        assert "does not prevent a query that names" in flat, text
        assert "Unity Catalog column mask" in flat, text
        # Nothing about what Genie reads, sees or does.
        for claim in ("Genie reads", "Genie sees", "visible to Genie", "visible to it",
                      "still sees", "Genie hides", "Genie through"):
            assert claim not in flat, (claim, text)
