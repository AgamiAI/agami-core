"""`sm export` end to end (F19 / ACE-174): the bundled sample, every target, one run.

Each target slice proves its own writer; this file proves the command a person runs. One call
through `cli.main` writes Snowflake, Databricks and Ossie files for the bundled sample, and the
assertions are the ones a reader of `docs/context-export.md` relies on: the file set, every file
passing its target's validator, the report's closed reason codes, the eight sample metrics reaching
every target, no sensitive column's name in any emitted byte, byte-identical re-runs, an untouched
model, and clean refusals. The validators are the slice tests' own, imported rather than copied.

Nothing here touches a vendor account: these are offline checks against the vendors' documented
rules. The optional last test runs one exported measure on open-source Spark when it is installed.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model import export_databricks as DB  # noqa: E402
from semantic_model import export_ossie as O  # noqa: E402
from semantic_model import export_snowflake as S  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
TARGETS = ("snowflake", "databricks", "ossie")
LOCATION = "DEMO.SALES_DATA"
ORG = load_datasource(SAMPLE)
(AREA,) = [sa.name for sa in ORG.subject_areas]
# Each target names its files by the object they create, so the area's name under its naming rule.
SF_STEM, OSSIE_STEM = S.snowflake_name(AREA), O.ossie_name(AREA)
METRICS = sorted(m.name for sa in ORG.subject_areas for m in sa.metrics)
SENSITIVE = sorted(
    c.name for sa in ORG.subject_areas for t in sa.tables_defined for c in t.columns if c.sensitive
)


def _slice(name: str):
    """A slice test module, for its validator. Importing it skips this test when it would skip."""
    return importlib.import_module(name)


def _sm(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", *argv])
    return rc, buf.getvalue()


def _export(out: Path) -> tuple[int, str]:
    targets = [arg for t in TARGETS for arg in ("--target", t)]
    return _sm(str(SAMPLE), *targets, "--location", LOCATION, "--out", str(out))


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> tuple[Path, dict, str]:
    out = tmp_path_factory.mktemp("export")
    rc, text = _export(out)
    assert rc == 0, text
    return out, json.loads(text), text


def _text(out: Path, rel: str) -> str:
    return (out / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# What the run writes
# ---------------------------------------------------------------------------


def test_one_run_writes_each_targets_files_and_the_report_lists_exactly_those(run):
    out, report, text = run
    db_area = DB.databricks_name(AREA)
    facts = ("invoices", "order_items", "orders", "refunds", "subscriptions")
    pages = [
        f"pages/{db_area}__entity__{DB.databricks_name(e.name)}.json"
        for sa in ORG.subject_areas
        for e in sa.entities
    ]
    expected = {
        # No golden dataset ships with the sample, so no `<area>.evaluation.sql`.
        "snowflake": {f"{SF_STEM}.semantic_view.yaml", f"{SF_STEM}.sql", f"{SF_STEM}.agent.sql"},
        "databricks": {
            f"{db_area}.geniespace.json",
            f"resources/{db_area}.genie.yml",
            f"pages/{db_area}__manifest.json",
            *pages,
            *(f"metric_views/{db_area}__{f}.sql" for f in facts),
        },
        "ossie": {f"{OSSIE_STEM}.ossie.yaml"},
    }
    written = _tree(out)
    assert set(written) == {"export-report.json"} | {
        f"{t}/{f}" for t, files in expected.items() for f in files
    }
    for t, files in expected.items():
        assert report["targets"][t]["files"] == sorted(f"{t}/{f}" for f in files)
    # The report on disk is the one printed.
    assert written["export-report.json"].decode("utf-8") == text


def test_the_report_names_every_target_its_losses_by_closed_code_and_what_never_travels(run):
    _, report, _ = run
    assert set(report["targets"]) == set(TARGETS)
    assert report["source"] == {"datasource": ORG.datasource, "engine": "SQLite", "areas": [AREA]}
    assert report["options"]["targets"] == list(TARGETS)
    assert report["options"]["location"] == LOCATION
    assert report["does_not_travel"] == list(X.DOES_NOT_TRAVEL) and report["does_not_travel"]
    for t in TARGETS:
        section = report["targets"][t]
        assert section["losses"], t
        assert section["included_unreviewed"] == []
        for loss in section["losses"]:
            assert set(loss) == {"area", "kind", "name", "outcome", "reason", "detail"}
            assert loss["outcome"] in ("dropped", "degraded") and loss["reason"] in X.REASONS
            assert loss["detail"], loss


def test_all_eight_sample_metrics_reach_every_target(run):
    out, report, _ = run
    assert len(METRICS) == 8
    for t in TARGETS:
        assert report["targets"][t]["carried"]["metric"] == 8, t
    view = yaml.safe_load(_text(out, f"snowflake/{SF_STEM}.semantic_view.yaml"))
    snow = [m["name"] for tb in view["tables"] for m in tb.get("metrics", [])]
    assert sorted(snow) == sorted(S.snowflake_name(m) for m in METRICS)
    defs = [
        _slice("test_ace170_databricks_metric_views")._definition(_text(out, f))
        for f in report["targets"]["databricks"]["files"]
        if "/metric_views/" in f
    ]
    measures = [m["name"] for d in defs for m in d["measures"]]
    assert sorted(measures) == sorted(DB.databricks_name(m) for m in METRICS)
    doc = yaml.safe_load(_text(out, f"ossie/{OSSIE_STEM}.ossie.yaml"))
    ossie = [m["name"] for m in doc["semantic_model"][0]["metrics"]]
    assert sorted(ossie) == sorted(O.ossie_name(m) for m in METRICS)


def test_no_emitted_byte_names_a_sensitive_column(run):
    out, _, _ = run
    assert len(SENSITIVE) == 2
    names = re.compile(rf"(?<![\w$])({'|'.join(map(re.escape, SENSITIVE))})(?![\w$])", re.I)
    for rel, data in _tree(out).items():
        assert not names.search(data.decode("utf-8")), rel


def _yaml_edit(path: Path, change) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc = change(doc) or doc
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _leaky_sample(dst: Path, engine: str) -> Path:
    """The bundled sample, written for `engine` so its non-metric SQL travels, with the sensitive
    columns reached every way a model can reach them: a table defined by a query, a `*`, prose in
    every kind of item, a term, an ambiguity, a metric, examples and golden answers."""
    shutil.copytree(SAMPLE, dst)
    area = dst / "subject_areas" / AREA
    _yaml_edit(dst / "datasources/agami-example_sqlite/storage.yaml",
               lambda d: {**d, "storage_type": engine})

    def datasource(d):
        d["description"] = "A sample store; reach customers by phone."
        d["key_terminology"] = {"email": "the address on file", "MRR": "monthly recurring revenue"}
        d["ambiguities"] = [{"name": "contactability", "when": "asked about reachable customers",
                             "applies_to": ["customers"],
                             "options": [{"label": "by phone", "how": "phone IS NOT NULL",
                                          "default": True}, {"label": "any", "how": "TRUE"}]}]
    _yaml_edit(dst / "datasource.yaml", datasource)

    def customers(d):
        d["description"] = "People who signed up. Contact them by email."
        d["caveats"] = ["The phone column is free text."]
        for c in d["columns"]:
            if c["name"] == "country":
                c["choice_field"]["XX"] = "unknown (see the email domain)"
            if c["name"] == "full_name":
                c["description"] = "Display name; pair with email to de-duplicate."
    _yaml_edit(area / "tables/customers.yaml", customers)
    _yaml_edit(area / "metrics/order_count.yaml",
               lambda d: {**d, "calculation": "Orders, never one per email.",
                          "business_question": "How many orders came by phone?"})
    _yaml_edit(area / "entities/customer.yaml",
               lambda d: {**d, "value_format_hint": "looks like an email address"})

    def rels(d):
        d["relationships"][0]["description"] = "Join on subscription id, never on email."
    _yaml_edit(area / "relationships.yaml", rels)
    reach = yaml.safe_load((area / "metrics/order_count.yaml").read_text(encoding="utf-8"))
    reach.update(name="reachable customers", other_names=[], source_tables=["customers"],
                 primary_table="customers",
                 bindings={e: "COUNT(customers.email)" for e in ("SQLite", "Snowflake", "Databricks")})
    _yaml_edit(area / "subject_area.yaml", lambda d: d["tables"].extend(
        {"storage_connection": "agami-example_sqlite", "schema": "main", "table": t}
        for t in ("customer_contacts", "all_customers", "big_orders")))
    (area / "metrics/reachable_customers.yaml").write_text(yaml.safe_dump(reach), encoding="utf-8")
    for name, sql in (("customer_contacts", "SELECT id, email AS contact FROM customers"),
                      ("all_customers", "SELECT * FROM customers"),
                      ("big_orders", "SELECT id, total_amount FROM orders WHERE total_amount > 100")):
        (area / f"tables/{name}.yaml").write_text(yaml.safe_dump({
            "name": name, "schema": "main", "storage_connection": "agami-example_sqlite",
            "source_type": "sql", "sql": sql, "review_state": "approved",
            "columns": [{"name": "id", "type": "integer"}]}), encoding="utf-8")
    _yaml_edit(dst / f"prompt_examples/{AREA}/examples.yaml", lambda exs: exs + [
        {"question": "Which customers have an email on file?", "status": "confirmed",
         "sql": "SELECT full_name FROM customers WHERE email IS NOT NULL", "tables": ["customers"]},
        {"question": "Show every customer", "status": "confirmed",
         "sql": "SELECT * FROM customers", "tables": ["customers"]},
        {"question": "Who can we reach?", "status": "confirmed",
         "sql": "SELECT contact FROM customer_contacts", "tables": ["customer_contacts"]}])
    (dst / "golden_datasets").mkdir()
    (dst / "golden_datasets/core.yaml").write_text(yaml.safe_dump({"test_cases": [
        {"id": "g1", "query": "How many orders?", "expected": {
            "sql": "SELECT COUNT(*) FROM orders", "sql_confirmed": True, "tables_used": ["orders"]}},
        {"id": "g2", "query": "Every customer", "expected": {
            "sql": "SELECT c.* FROM customers c", "sql_confirmed": True,
            "tables_used": ["customers"]}}]}), encoding="utf-8")
    return dst


@pytest.mark.parametrize("engine", ["Snowflake", "Databricks"])
def test_no_emitted_byte_names_a_sensitive_column_when_the_models_sql_travels(tmp_path, engine):
    """The scan the spec asks for: every byte a real three-target run writes, on a model whose
    engine is the target's so its examples, golden answers and table queries travel."""
    model = _leaky_sample(tmp_path / "model", engine)
    out = tmp_path / "out"
    targets = [arg for t in TARGETS for arg in ("--target", t)]
    rc, text = _sm(str(model), *targets, "--location", LOCATION, "--out", str(out))
    assert rc == 0, text
    names = re.compile(rf"(?<![\w$])({'|'.join(SENSITIVE)}|contact)(?![\w$])", re.I)
    written = _tree(out)
    for rel, data in written.items():
        assert not names.search(data.decode("utf-8")), (rel, names.search(data.decode("utf-8")))
    # The model's own SQL did travel: a clean example, golden answer and table query.
    body = b"\n".join(written.values()).decode("utf-8")
    assert "SELECT COUNT(*) AS order_count FROM" in body   # Snowflake names its logical table
    assert "total_amount > 100" in body
    report = json.loads(text)
    for t in TARGETS:
        losses = {(x["kind"], x["name"]): x["reason"] for x in report["targets"][t]["losses"]}
        for kind, name in (("table", "customer_contacts"), ("table", "all_customers"),
                           ("metric", "reachable customers"), ("example", "Show every customer"),
                           ("example", "Who can we reach?"), ("golden", "core:g2")):
            assert losses.get((kind, name)) == "depends_on_excluded", (t, kind, name)


# Every flag that adds content to a target's files. `--snowflake-no-expected-answers` is left out:
# it only removes the computed answers, so the run without it emits more.
OPT_IN = ("--snowflake-warehouse", "AGAMI_WH", "--snowflake-certification-tags",
          "--snowflake-mcp-server", "--genie-hide-sensitive", "--uc-comments", "--uc-constraints",
          "--uc-certify", "--pages-publish", "--pages-metrics", "--ossie-version", "0.1.1")


def _hidden(space: dict) -> tuple[dict, list[str]]:
    """The space without its `exclude: true` column configs, and the columns those configs hide."""
    hidden: list[str] = []
    for table in space.get("data_sources", {}).get("tables", []):
        keep = []
        for c in table.get("column_configs", []):
            (hidden.append(c["column_name"]) if c.get("exclude") else keep.append(c))
        table["column_configs"] = keep
    return space, hidden


@pytest.mark.parametrize("engine", ["Snowflake", "Databricks"])
def test_no_emitted_byte_names_a_sensitive_column_with_every_opt_in_flag(tmp_path, engine):
    """The same scan with every opt-in flag on. The one place a sensitive name may appear is the
    Genie space's `column_configs` entry that hides it under --genie-hide-sensitive."""
    model = _leaky_sample(tmp_path / "model", engine)
    out = tmp_path / "out"
    targets = [arg for t in TARGETS for arg in ("--target", t)]
    rc, text = _sm(str(model), *targets, "--location", LOCATION, *OPT_IN, "--out", str(out))
    assert rc == 0, text
    assert set(json.loads(text)["options"]["target_args"]) == {
        "snowflake_warehouse", "snowflake_certification_tags", "snowflake_mcp_server",
        "genie_hide_sensitive", "uc_comments", "uc_constraints", "uc_certify", "pages_publish",
        "pages_metrics", "ossie_version"}
    names = re.compile(rf"(?<![\w$])({'|'.join(SENSITIVE)}|contact)(?![\w$])", re.I)
    written = _tree(out)
    space_file = f"databricks/{DB.databricks_name(AREA)}.geniespace.json"
    assert {"databricks/unity_catalog/" + DB.databricks_name(AREA) + ".sql",
            f"snowflake/{SF_STEM}.mcp_server.sql", space_file} <= set(written)
    hidden: list[str] = []
    for rel, data in written.items():
        body = data.decode("utf-8")
        if rel == space_file:
            space, hidden = _hidden(json.loads(body))
            body = json.dumps(space)
        assert not names.search(body), (rel, names.search(body))
    # Each sensitive column of the tables the space lists is hidden there, by name, once.
    listed = {bare for t in json.loads(written[space_file])["data_sources"]["tables"]
              for bare in [t["identifier"].rsplit(".", 1)[-1]]}
    want = sorted(c.name for sa in ORG.subject_areas for t in sa.tables_defined if t.name in listed
                  for c in t.columns if c.sensitive)
    assert want and sorted(hidden) == want


# ---------------------------------------------------------------------------
# Each file passes its target's validator
# ---------------------------------------------------------------------------


def test_the_snowflake_files_pass_the_snowflake_validators(run):
    out, _, _ = run
    t168, t169 = _slice("test_ace168_export_snowflake"), _slice("test_ace169_snowflake_agent")
    yml = _text(out, f"snowflake/{SF_STEM}.semantic_view.yaml")
    view = yaml.safe_load(yml)
    assert t168.validate_semantic_view(view) == []
    for sql in t168.emitted_sql(view):
        t168._parse(sql)
    # The call that creates the view carries the YAML file byte for byte.
    assert _text(out, f"snowflake/{SF_STEM}.sql").split("$$\n", 1)[1].rsplit("$$", 1)[0] == yml
    assert t169.validate_agent_spec(t169.spec_text(_text(out, f"snowflake/{SF_STEM}.agent.sql"))) == []


def test_the_databricks_files_pass_the_databricks_validators(run):
    out, report, _ = run
    t170 = _slice("test_ace170_databricks_metric_views")
    t171, t172 = _slice("test_ace171_genie_space"), _slice("test_ace172_databricks_pages")
    files = report["targets"]["databricks"]["files"]
    for f in (f for f in files if "/metric_views/" in f):
        assert t170._validate(t170._definition(_text(out, f))) == [], f
    space = json.loads(_text(out, f"databricks/{DB.databricks_name(AREA)}.geniespace.json"))
    assert t171.validate(space) == []
    manifest = f"databricks/pages/{DB.databricks_name(AREA)}__manifest.json"
    for f in (f for f in files if "/pages/" in f and f != manifest):
        assert t172._validate_page(json.loads(_text(out, f))) == [], f
    for page in json.loads(_text(out, manifest))["pages"]:
        for link in page["related_assets"]:
            assert t172._validate_link(link) == [], link


def test_the_ossie_file_passes_the_published_schema(run):
    out, _, _ = run
    t173 = _slice("test_ace173_ossie_export")
    doc = yaml.safe_load(_text(out, f"ossie/{OSSIE_STEM}.ossie.yaml"))
    assert doc["version"] == "0.1.1"
    t173._validate(doc, "0.1.1")
    t173._check_references(t173._model(doc))


# ---------------------------------------------------------------------------
# Re-runs, the model, refusals
# ---------------------------------------------------------------------------


def test_a_second_run_is_byte_identical_and_leaves_the_model_alone(tmp_path):
    before = _tree(SAMPLE)
    out = tmp_path / "out"
    first = (_export(out), _tree(out))
    second = (_export(out), _tree(out))
    assert first == second and first[0][0] == 0
    assert _tree(SAMPLE) == before


def test_each_writer_helper_has_one_definition_the_writers_import():
    """The helpers several writers need (parsing, splicing, joining text, leaving out an empty
    field, a Snowflake string constant) are defined once and imported, never copied."""
    from semantic_model import export_databricks_genie as G
    from semantic_model import export_databricks_uc as U
    from semantic_model import export_snowflake_agent as A

    copies = ("_parse", "_parses", "_parses_as", "_text", "_sentences", "_put", "_span",
              "_token_span", "_splice_spans", "_literal")
    for module in (X, S, A, DB, G, U, O):
        assert not [c for c in copies if hasattr(module, c)], module.__name__
    assert G.parses is X.parses and A.parses is X.parses and S.parses is X.parses
    assert DB.parse_statement is X.parse_statement is S.parse_statement
    assert A.join_sentences is X.join_sentences is U.join_sentences is S.join_sentences
    assert O.put_nonempty is X.put_nonempty is S.put_nonempty
    assert S.token_span is X.token_span and S.splice_spans is X.splice_spans
    assert A.snowflake_literal is S.snowflake_literal
    assert S.snowflake_literal("it's a \\ path") == "'it''s a \\\\ path'"
    assert X.splice_spans("abcdef", {(0, 2): "X", (1, 3): "Y"}) is None
    assert X.parses("SELECT 1; SELECT 2", "Snowflake")
    assert not X.parses("SELECT 1; SELECT 2", "Snowflake", one_statement=True)
    assert not X.parses("1 +", "Databricks", as_expression=True)


@pytest.mark.parametrize(
    "argv",
    [
        ("--target", "snowflake", "--target", "ossie", "--target", "snowflake"),
        ("--target", "snowflake", "--target", "tableau"),
    ],
    ids=["repeated", "unknown"],
)
def test_a_repeated_or_unknown_target_refuses_before_writing(tmp_path, argv):
    out = tmp_path / "out"
    rc, text = _sm(str(SAMPLE), *argv, "--location", LOCATION, "--out", str(out))
    assert rc == 2 and json.loads(text)["error"] == "export_refused"
    assert not out.exists()


# ---------------------------------------------------------------------------
# Optional: one exported metric view, computed on open-source Spark
# ---------------------------------------------------------------------------


def _local(expr: str) -> str:
    # Open-source Spark resolves a metric view's columns bare; Databricks also accepts `source.`.
    tree = sqlglot.parse_one(expr, read="databricks")
    for col in tree.find_all(sqlglot.exp.Column):
        if col.table == "source":
            col.set("table", None)
    return tree.sql(dialect="spark")


def test_an_exported_measure_computes_on_open_source_spark(run, tmp_path):
    """Runs only where pyspark and a JVM are installed; neither is a dependency of this repo.

    Reduces the orders metric view to what open-source Spark's metric views read (version 0.1, one
    source, bare columns, no joins) and checks each measure over a four-row table against the same
    aggregate written as plain SQL. It proves the measure expressions compute, not that Databricks
    accepts the file; docs/context-export.md says what is checked where."""
    pyspark = pytest.importorskip("pyspark", reason="pyspark is not installed")
    if not (shutil.which("java") or os.environ.get("JAVA_HOME")):
        pytest.skip("no JVM for pyspark")
    from pyspark.errors import ParseException
    from pyspark.sql import SparkSession

    t170 = _slice("test_ace170_databricks_metric_views")
    out, _, _ = run
    doc = t170._definition(
        _text(out, f"databricks/metric_views/{DB.databricks_name(AREA)}__orders.sql")
    )
    measures = {m["name"]: _local(m["expr"]) for m in doc["measures"]}
    columns = sorted(
        {
            c.name
            for e in measures.values()
            for c in sqlglot.parse_one(e).find_all(sqlglot.exp.Column)
        }
    )
    rows = ", ".join("(" + ", ".join(str(i) for _ in columns) + ")" for i in range(1, 5))
    spark = (
        SparkSession.builder.master("local[1]")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", str(tmp_path))
        .getOrCreate()
    )
    try:
        spark.sql(
            f"CREATE TABLE src USING parquet AS SELECT * FROM VALUES {rows} AS t({', '.join(columns)})"
        )
        body = yaml.safe_dump(
            {
                "version": 0.1,
                "source": "src",
                "measures": [{"name": n, "expr": e} for n, e in measures.items()],
            }
        )
        try:
            spark.sql(f"CREATE VIEW mv WITH METRICS LANGUAGE YAML AS $$\n{body}$$")
        except ParseException as exc:
            # Only a Spark that predates metric views fails to parse it; anything else fails.
            if "METRICS" not in str(exc):
                raise
            pytest.skip(f"Spark {pyspark.__version__} has no metric views")
        for name, expr in measures.items():
            got = spark.sql(f"SELECT MEASURE({name}) FROM mv").collect()[0][0]
            assert got == spark.sql(f"SELECT {expr} FROM src").collect()[0][0], name
    finally:
        spark.stop()


# A release state or a date it began is the vendor's to state and goes stale; the export's files and
# docs name what a statement does and what it needs, never when a feature became available.
_RELEASE_CLAIM = re.compile(
    r"(?i)\b(?:generally available|general availability|public preview|private preview|"
    r"in preview|beta)\b|\b(?:available|released|launched)\s+(?:since|on|as of)\s+\d{4}")


def test_no_exported_file_or_doc_claims_a_release_state_or_date(tmp_path):
    out = tmp_path / "out"
    rc, text = _sm(str(SAMPLE), "--target", "snowflake", "--target", "databricks", "--target",
                   "ossie", "--location", "DEMO.SALES_DATA", "--out", str(out),
                   "--snowflake-mcp-server", "--uc-comments", "--uc-constraints", "--uc-certify",
                   "--pages-publish", "--pages-metrics", "--snowflake-certification-tags")
    assert rc == 0, text
    texts = {str(p.relative_to(out)): p.read_text(encoding="utf-8")
             for p in sorted(out.rglob("*")) if p.is_file()}
    texts["docs/context-export.md"] = (REPO / "docs" / "context-export.md").read_text("utf-8")
    texts["SKILL.md"] = (REPO / "plugins/agami/skills/agami-export/SKILL.md").read_text("utf-8")
    for path in sorted((REPO / "packages/agami-core/src/semantic_model").glob("export*.py")):
        texts[path.name] = path.read_text(encoding="utf-8")
    hits = {k: m.group(0) for k, v in texts.items() if (m := _RELEASE_CLAIM.search(v))}
    assert hits == {}
    assert _RELEASE_CLAIM.search("written by `sm export` (generally available since 2025-11-04)")
