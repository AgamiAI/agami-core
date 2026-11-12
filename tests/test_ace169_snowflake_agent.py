"""`sm export --target snowflake` (ACE-169): the Cortex Agent bound to the view, its evaluation files,
and the Snowflake-managed MCP server.

The validators below are written from Snowflake's documentation, not from what the writer emits:

* `validate_agent_spec`, from CREATE AGENT (sql-reference/sql/create-agent): the specification's
  key tree, the 100,000-byte limit, and the `ToolResource` rule of the Cortex Agents REST API page
  (exactly one of `semantic_model_file` / `semantic_view` for `cortex_analyst_text_to_sql`), with
  `tool_resources` keys matching tool names. `validate_agent_statement` adds the statement's own
  shape: `CREATE OR REPLACE AGENT <name> COPY GRANTS FROM SPECIFICATION`.
* `validate_evaluation`, from Cortex Agent evaluations ("Dataset format", "Tool selection and
  execution metrics ground truth") and SYSTEM$CREATE_EVALUATION_DATASET: a two-column table
  (input query VARCHAR, ground truth VARIANT) filled with a VARIANT built by PARSE_JSON, ground
  truth keys `ground_truth_output` / `ground_truth_invocations`, invocation keys `tool_name`
  (required), `tool_input`, `tool_output`, and a dataset registered with type `Cortex Agent` and
  the column mapping keys `query_text` / `expected_tools`. A row whose expected answer is computed
  at load time adds exactly `ground_truth_output` to that object, as a string (OBJECT_INSERT,
  wrapped back into a VARIANT as the page asks), from a FROM clause; every statement parses.
* `validate_run_config`, from "Agent Evaluation YAML specification": the top-level keys, the
  agent and source keys, system metric names and `v<major>[_<minor>]` versions, and custom
  metrics (`name`, optional `model`, `score_ranges` of three two-element ranges, `prompt`).
* `validate_analyst_config`, from Cortex Analyst evaluations ("Analyst evaluation YAML
  specification"): `analyst_params`, `verified_queries` source, the one `sql_correctness` metric.
* `validate_mcp_spec`, from CREATE MCP SERVER: a `tools` array, each tool's required `name`,
  `type`, `title`, `description`, a supported type, an `identifier` where the type needs one,
  documented annotation keys, and unique names.

Negative tests prove each rule rejects what breaks it. Creating the objects or running an
evaluation against an account is manual and outside CI.
"""

from __future__ import annotations

import argparse
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

REPO = Path(__file__).resolve().parent.parent
PKG_SRC = REPO / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import cli  # noqa: E402
from semantic_model import export as X  # noqa: E402
from semantic_model import export_snowflake as S  # noqa: E402
from semantic_model import export_snowflake_agent as A  # noqa: E402
from semantic_model.golden import GoldenBounds  # noqa: E402
from semantic_model.models import Table  # noqa: E402
from sqlglot import exp  # noqa: E402
from sqlglot.errors import ErrorLevel  # noqa: E402

SAMPLE = REPO / "plugins" / "agami" / "samples" / "store" / "model"
AREA = "sales"
LIMIT = 100_000
OPTS = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"))


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="snowflake", error_level=ErrorLevel.RAISE)


def _statements(sql: str) -> list:
    return [s for s in sqlglot.parse(sql, dialect="snowflake", error_level=ErrorLevel.RAISE) if s]


def _opts(**args) -> X.ExportOptions:
    return X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"),
                           target_args=args)


# ---------------------------------------------------------------------------
# The agent specification validator, from CREATE AGENT
# ---------------------------------------------------------------------------

_SPEC_KEYS = {"models", "orchestration", "instructions", "tools", "tool_resources"}
_MODELS_KEYS = {"orchestration"}
_ORCH_KEYS = {"capabilities", "tool_not_accessible", "budget"}
_BUDGET_KEYS = {"seconds", "tokens"}
# CREATE AGENT lists response, orchestration and sample_questions; the REST API's
# AgentInstructions adds system.
_INSTRUCTION_KEYS = {"response", "orchestration", "sample_questions", "system"}
_TOOL_SPEC_KEYS = {"type", "name", "description", "input_schema"}
_ANALYST_RESOURCE_KEYS = {"semantic_model_file", "semantic_view", "execution_environment"}
# The REST API's ExecutionEnvironment: a warehouse, named, with an optional query timeout.
_EXECUTION_KEYS = {"type", "warehouse", "query_timeout"}


def _unknown(where: str, got: dict, allowed: set, problems: list[str]) -> None:
    if not isinstance(got, dict):
        problems.append(f"{where} is not a mapping")
        return
    problems += [f"{where}: undocumented key {k!r}" for k in sorted(set(got) - allowed)]


def validate_agent_spec(text: str) -> list[str]:
    problems: list[str] = []
    if len(text.encode("utf-8")) > LIMIT:
        problems.append(f"specification is {len(text.encode('utf-8'))} bytes, over {LIMIT}")
    spec = yaml.safe_load(text)
    _unknown("specification", spec, _SPEC_KEYS, problems)
    if not isinstance(spec, dict):
        return problems
    _unknown("models", spec.get("models", {}), _MODELS_KEYS, problems)
    orch = spec.get("orchestration", {})
    _unknown("orchestration", orch, _ORCH_KEYS, problems)
    if isinstance(orch, dict):
        _unknown("orchestration.budget", orch.get("budget", {}), _BUDGET_KEYS, problems)
    ins = spec.get("instructions", {})
    _unknown("instructions", ins, _INSTRUCTION_KEYS, problems)
    if isinstance(ins, dict):
        for k in ("response", "orchestration", "system"):
            if k in ins and not isinstance(ins[k], str):
                problems.append(f"instructions.{k} is not a string")
        for i, q in enumerate(ins.get("sample_questions", [])):
            if not (isinstance(q, dict) and set(q) == {"question"} and isinstance(q["question"], str)):
                problems.append(f"sample_questions[{i}] is not {{question: <text>}}")
    names: dict[str, str] = {}
    for i, tool in enumerate(spec.get("tools", [])):
        if not (isinstance(tool, dict) and set(tool) == {"tool_spec"}):
            problems.append(f"tools[{i}] is not {{tool_spec: ...}}")
            continue
        ts = tool["tool_spec"]
        _unknown(f"tools[{i}].tool_spec", ts, _TOOL_SPEC_KEYS, problems)
        if isinstance(ts, dict) and ts.get("name"):
            names[ts["name"]] = ts.get("type", "")
    resources = spec.get("tool_resources", {})
    if not isinstance(resources, dict):
        return problems + ["tool_resources is not a mapping"]
    for name, res in resources.items():
        if name not in names:
            problems.append(f"tool_resources.{name} matches no tool's name")
        elif names[name] == A.TOOL_TYPE:
            _unknown(f"tool_resources.{name}", res, _ANALYST_RESOURCE_KEYS, problems)
            if isinstance(res, dict) and len({"semantic_model_file", "semantic_view"} & set(res)) != 1:
                problems.append(f"tool_resources.{name}: exactly one of semantic_model_file or "
                                "semantic_view")
            env = res.get("execution_environment") if isinstance(res, dict) else None
            if env is not None:
                where = f"tool_resources.{name}.execution_environment"
                _unknown(where, env, _EXECUTION_KEYS, problems)
                if not (isinstance(env, dict) and env.get("type") == "warehouse"
                        and isinstance(env.get("warehouse"), str) and env["warehouse"]):
                    problems.append(f"{where}: type warehouse with a warehouse name")
    return problems


# CREATE AGENT's syntax: `CREATE [ OR REPLACE ] … AGENT <name> [ COPY GRANTS ] … FROM SPECIFICATION`,
# COPY GRANTS "meaningful only when used with OR REPLACE" and exclusive with IF NOT EXISTS.
_AGENT_STATEMENT = re.compile(
    r"^CREATE OR REPLACE AGENT (?P<name>\S+) COPY GRANTS\n  FROM SPECIFICATION\n  ", re.M)


def validate_agent_statement(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    if not _AGENT_STATEMENT.match(body):
        return ["the statement is not CREATE OR REPLACE AGENT <name> COPY GRANTS FROM SPECIFICATION"]
    return []


def spec_text(agent_sql: str) -> str:
    """The specification inside `FROM SPECIFICATION`: dollar-quoted, or a single-quoted constant."""
    body = agent_sql.split("FROM SPECIFICATION", 1)[1].strip()
    assert body.endswith(";")
    body = body[:-1]
    if body.startswith("$$"):
        assert body.endswith("$$")
        return body[2:-2].lstrip("\n")
    return _parse(f"SELECT {body}").expressions[0].this


def _bound_view(agent_sql: str) -> str:
    spec = yaml.safe_load(spec_text(agent_sql))
    (tool,) = spec["tools"]
    assert tool["tool_spec"]["type"] == A.TOOL_TYPE
    return spec["tool_resources"][tool["tool_spec"]["name"]]["semantic_view"]


# ---------------------------------------------------------------------------
# The evaluation validator, from Cortex Agent evaluations
# ---------------------------------------------------------------------------

_TRUTH_KEYS = {"ground_truth_output", "ground_truth_invocations"}
_CALL_KEYS = {"tool_name", "tool_input", "tool_output"}
COMPUTED = "<computed when the file runs>"


def _strings(node) -> list[str]:
    return [lit.this for lit in node.find_all(exp.Literal) if lit.is_string]


def _from(select):
    # sqlglot names the FROM clause `from_` in newer releases and `from` in older ones.
    return select.args.get("from_") or select.args.get("from")


def _ground_truth_doc(select, problems: list[str], q: str) -> tuple[dict | None, str | None]:
    """The static ground truth and, for a computed answer, the subquery the answer comes from."""
    truth = select.expressions[1]
    if isinstance(truth, exp.ParseJSON) and isinstance(truth.this, exp.Literal):
        if _from(select):
            problems.append(f"{q!r}: a constant row reads from a table")
        return json.loads(truth.this.this), None
    # OBJECT_INSERT returns an OBJECT: the page asks for TO_VARIANT (or PARSE_JSON) around it.
    ins = truth.this if isinstance(truth, exp.ToVariant) else None
    if not (isinstance(ins, exp.ObjectInsert) and isinstance(ins.this, exp.Anonymous)
            and ins.this.name.upper() == "TO_OBJECT"
            and isinstance(ins.this.expressions[0], exp.ParseJSON)
            and isinstance(ins.this.expressions[0].this, exp.Literal)):
        problems.append(f"{q!r}: ground truth is not PARSE_JSON of a constant, or one with "
                        "ground_truth_output inserted")
        return None, None
    if ins.args["key"].this != "ground_truth_output":
        problems.append(f"{q!r}: computes {ins.args['key'].this!r}, not ground_truth_output")
    doc = json.loads(ins.this.expressions[0].this.this)
    if "ground_truth_output" in doc:
        problems.append(f"{q!r}: ground_truth_output is both constant and computed")
    if not _from(select):
        problems.append(f"{q!r}: a computed answer reads from nothing")
        return doc, None
    golden = next((s for s in select.find_all(exp.Subquery) if s.alias == "agami_golden"), None)
    if golden is None:
        problems.append(f"{q!r}: no golden subquery")
        return doc, None
    return {**doc, "ground_truth_output": COMPUTED}, golden.this


def validate_evaluation(sql: str, tool: str) -> tuple[list[str], list[tuple[str, dict]]]:
    """Problems, and the (input query, ground truth) rows the file inserts; a computed answer
    reads `COMPUTED`. Every statement must parse in Snowflake's dialect."""
    problems: list[str] = []
    create, *inserts, call = _statements(sql)
    cols = [(c.name.casefold(), c.args["kind"].this) for c in create.find_all(exp.ColumnDef)]
    if not isinstance(create, exp.Create) or [k for _, k in cols] != [exp.DataType.Type.VARCHAR,
                                                                      exp.DataType.Type.VARIANT]:
        problems.append("the table is not (input query VARCHAR, ground truth VARIANT)")
    table = create.this.this.sql("snowflake") if isinstance(create, exp.Create) else ""
    rows = []
    for ins in inserts:
        if not isinstance(ins, exp.Insert) or ins.this.this.sql("snowflake") != table:
            problems.append("a statement other than an insert into the table")
            continue
        select = ins.expression
        query = select.expressions[0]
        if not (isinstance(query, exp.Literal) and query.is_string):
            problems.append("an input query that is not a string")
            continue
        doc, _ = _ground_truth_doc(select, problems, query.this)
        if doc is None:
            continue
        problems += [f"{query.this!r}: undocumented ground truth key {k!r}"
                     for k in sorted(set(doc) - _TRUTH_KEYS)]
        if not isinstance(doc.get("ground_truth_output", ""), str):
            problems.append(f"{query.this!r}: ground_truth_output is not a string")
        for c in doc.get("ground_truth_invocations", []):
            problems += [f"{query.this!r}: undocumented invocation key {k!r}"
                         for k in sorted(set(c) - _CALL_KEYS)]
            if c.get("tool_name") != tool:
                problems.append(f"{query.this!r}: tool_name {c.get('tool_name')!r} is not {tool!r}")
            if not isinstance(c.get("tool_output", ""), str):
                problems.append(f"{query.this!r}: tool_output is not a string")
        rows.append((query.this, doc))
    text = call.sql("snowflake")
    if not text.upper().startswith("CALL SYSTEM$CREATE_EVALUATION_DATASET("):
        problems.append("the dataset is not registered with SYSTEM$CREATE_EVALUATION_DATASET")
    args = _strings(_parse(f"SELECT f{text[text.index('('):]}"))
    if len(args) != 7 or args[0].casefold() != "cortex agent" or args[1] != table:
        problems.append("the call does not register this table as a Cortex Agent dataset")
    elif dict(zip(args[3::2], args[4::2])) != {"query_text": cols[0][0].upper(),
                                                "expected_tools": cols[1][0].upper()}:
        problems.append("the column mapping is not {query_text, expected_tools} onto the table")
    return problems, rows


def golden_subqueries(sql: str) -> dict[str, str]:
    """Each computed row's golden subquery, by input query, as the file's text holds it."""
    out = {}
    for ins in _statements(sql)[1:-1]:
        select = ins.expression
        _, sub = _ground_truth_doc(select, [], select.expressions[0].this)
        if sub is not None:
            literal = S.snowflake_literal(select.expressions[0].this)
            m = re.search(re.escape(f"SELECT {literal}"), sql)
            body = sql[m.end():].split("                    FROM (\n", 1)[1]
            out[select.expressions[0].this] = body.split("\n                    ) AS agami_golden", 1)[0]
    return out


# ---------------------------------------------------------------------------
# The run configuration validators, from the evaluation YAML specifications
# ---------------------------------------------------------------------------

_SYSTEM_METRICS = {"answer_correctness", "tool_selection_accuracy", "tool_execution_accuracy",
                   "logical_consistency"}
_VERSION = re.compile(r"v\d+(_\d+)?|auto")
_PLACEHOLDERS = {"input", "output", "ground_truth", "tool_info", "start_timestamp", "duration",
                 "span_id", "span_type", "span_name", "llm_model", "error", "status"}


def _metric_problems(m, builtins: set[str]) -> list[str]:
    if isinstance(m, str):
        return [] if m in builtins else [f"metric {m!r} is no system metric"]
    if not isinstance(m, dict) or not isinstance(m.get("name"), str):
        return [f"metric {m!r} has no name"]
    if m["name"] in builtins:
        out = [f"{m['name']}: undocumented key {k!r}" for k in sorted(set(m) - {"name", "version"})]
        if "version" in m and not _VERSION.fullmatch(str(m["version"])):
            out.append(f"{m['name']}: version {m['version']!r} is not v<major>[_<minor>]")
        return out
    out = [f"{m['name']}: undocumented key {k!r}"
           for k in sorted(set(m) - {"name", "model", "score_ranges", "prompt"})]
    out += [f"{m['name']}: custom metric has no {k}" for k in ("score_ranges", "prompt")
            if k not in m]
    ranges = m.get("score_ranges", {})
    if not (isinstance(ranges, dict) and set(ranges) == {"min_score", "median_score", "max_score"}
            and all(isinstance(r, list) and len(r) == 2 for r in ranges.values())):
        out.append(f"{m['name']}: score_ranges is not min/median/max two-element ranges")
    used = set(re.findall(r"{{(\w+)}}", str(m.get("prompt", ""))))
    out += [f"{m['name']}: undocumented placeholder {p!r}" for p in sorted(used - _PLACEHOLDERS)]
    return out


def validate_run_config(doc: dict) -> list[str]:
    problems: list[str] = []
    _unknown("config", doc, {"dataset", "evaluation", "metrics"}, problems)
    ev = doc.get("evaluation", {}) if isinstance(doc, dict) else {}
    _unknown("evaluation", ev, {"agent_params", "run_params", "source_metadata"}, problems)
    agent = ev.get("agent_params", {})
    _unknown("agent_params", agent, {"agent_name", "agent_type", "agent_version"}, problems)
    if not agent.get("agent_name") or str(agent.get("agent_type", "")).casefold() != "cortex agent":
        problems.append("agent_params needs agent_name and agent_type CORTEX AGENT")
    _unknown("run_params", ev.get("run_params", {}), {"label", "description"}, problems)
    src = ev.get("source_metadata", {})
    _unknown("source_metadata", src, {"type", "dataset_name"}, problems)
    if src.get("type") != "dataset" or not src.get("dataset_name"):
        problems.append("source_metadata is not type dataset with a dataset_name")
    metrics = doc.get("metrics") if isinstance(doc, dict) else None
    if not isinstance(metrics, list) or not metrics:
        return problems + ["metrics is not a non-empty sequence"]
    for m in metrics:
        problems += _metric_problems(m, _SYSTEM_METRICS)
    names = [m if isinstance(m, str) else m.get("name") for m in metrics]
    if len(names) != len(set(names)):
        problems.append("a metric is listed twice")
    return problems


def validate_analyst_config(doc: dict) -> list[str]:
    problems: list[str] = []
    _unknown("config", doc, {"evaluation", "metrics"}, problems)
    ev = doc.get("evaluation", {})
    _unknown("evaluation", ev, {"analyst_params", "source_metadata"}, problems)
    params = ev.get("analyst_params", {})
    _unknown("analyst_params", params, {"analyst_name", "analyst_type"}, problems)
    if not params.get("analyst_name") or params.get("analyst_type") != "SEMANTIC VIEW":
        problems.append("analyst_params needs analyst_name and analyst_type SEMANTIC VIEW")
    src = ev.get("source_metadata", {})
    _unknown("source_metadata", src, {"type", "verified_queries"}, problems)
    if src.get("type") != "verified_queries":
        problems.append("source_metadata type is not verified_queries")
    listed = src.get("verified_queries", [])
    if not (isinstance(listed, list) and all(isinstance(q, str) and q for q in listed)):
        problems.append("verified_queries is not a list of question texts")
    for m in doc.get("metrics", []) or ["<none>"]:
        problems += _metric_problems(m, {"sql_correctness"})
    return problems


# ---------------------------------------------------------------------------
# The MCP server validator, from CREATE MCP SERVER
# ---------------------------------------------------------------------------

_MCP_TYPES = {"CORTEX_SEARCH_SERVICE_QUERY", "CORTEX_ANALYST_MESSAGE", "SYSTEM_EXECUTE_SQL",
              "CORTEX_AGENT_RUN", "CODING_AGENT", "GENERIC"}
_MCP_NEEDS_IDENTIFIER = {"CORTEX_SEARCH_SERVICE_QUERY", "CORTEX_ANALYST_MESSAGE", "CORTEX_AGENT_RUN",
                         "GENERIC"}
_MCP_TOOL_KEYS = {"name", "type", "title", "description", "identifier", "annotations", "config"}
_MCP_ANNOTATIONS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}


def validate_mcp_spec(text: str) -> list[str]:
    problems: list[str] = []
    spec = yaml.safe_load(text)
    _unknown("specification", spec, {"tools"}, problems)
    tools = spec.get("tools") if isinstance(spec, dict) else None
    if not isinstance(tools, list) or not tools:
        return problems + ["tools is not a non-empty array"]
    for i, t in enumerate(tools):
        _unknown(f"tools[{i}]", t, _MCP_TOOL_KEYS, problems)
        if not isinstance(t, dict):
            continue
        problems += [f"tools[{i}] has no {k}" for k in ("name", "type", "title", "description")
                     if not t.get(k)]
        if t.get("type") not in _MCP_TYPES:
            problems.append(f"tools[{i}]: unsupported type {t.get('type')!r}")
        if t.get("type") in _MCP_NEEDS_IDENTIFIER and not t.get("identifier"):
            problems.append(f"tools[{i}]: {t.get('type')} needs an identifier")
        if "annotations" in t:
            if t.get("type") not in {"GENERIC", "CORTEX_AGENT_RUN"}:
                problems.append(f"tools[{i}]: annotations only on GENERIC and CORTEX_AGENT_RUN")
            _unknown(f"tools[{i}].annotations", t["annotations"], _MCP_ANNOTATIONS, problems)
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    if len(names) != len(set(names)):
        problems.append("tool names are not unique")
    return problems


def mcp_spec_text(sql: str) -> str:
    body = sql.split("FROM SPECIFICATION ", 1)[1].strip()
    assert body.startswith("$$") and body.endswith("$$;")
    return body[2:-3].lstrip("\n")


# ---------------------------------------------------------------------------
# Running the export
# ---------------------------------------------------------------------------


def _run(root: Path, out: Path, *flags: str) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["export", str(root), "--target", "snowflake", "--location", "DEMO.SALES_DATA",
                       "--out", str(out), *flags])
    return rc, json.loads(buf.getvalue())


def _export(root: Path, out: Path, *flags: str) -> dict:
    rc, report = _run(root, out, *flags)
    assert rc == 0, report
    return report


def _losses(report: dict) -> dict[tuple[str, str], list[tuple[str, str]]]:
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for x in report["targets"]["snowflake"]["losses"]:
        out.setdefault((x["kind"], x["name"]), []).append((x["outcome"], x["reason"]))
    return out


def _read(out: Path, name: str) -> str:
    return (out / "snowflake" / name).read_text("utf-8")


def _view_fqn_in_create_call(sql: str) -> str:
    """Where `<area>.sql` creates the view: its schema argument plus the view's own name."""
    schema = sql.split("SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(\n  '", 1)[1].split("'", 1)[0]
    name = yaml.safe_load(sql.split("$$\n", 1)[1].rsplit("$$", 1)[0])["name"]
    return f"{schema}.{name}"


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("sample")
    return out, _export(SAMPLE, out)


def _write_model(root: Path, fiscal: int = 1, filters: tuple[str, ...] = ()) -> Path:
    def put(rel, doc):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    opts = [{"label": "net", "how": "use orders.amount", "default": True}, {"label": "gross", "how": "add tax"}]
    put("datasource.yaml", {
        "datasource": "acme", "description": "Acme demo sales.", "fiscal_year_start_month": fiscal,
        "storage_connections": [{"name": "acme_db", "storage_type": "Snowflake", "storage_config": {}}],
        "subject_areas": [f"subject_areas/{AREA}"],
        "ambiguities": [
            {"name": "which amount", "when": "a question says amount", "applies_to": ["orders"],
             "options": opts},
            {"name": "which period", "when": "a question says recent", "applies_to": ["orders"],
             "behaviour": "answer_and_offer", "options": [{"label": "30 days", "how": "last 30 days", "default": True},
                                                          {"label": "quarter", "how": "this quarter"}]}]})
    put(f"subject_areas/{AREA}/subject_area.yaml", {"name": AREA, "description": "Orders and customers."})
    put(f"subject_areas/{AREA}/tables/orders.yaml", {
        "name": "orders", "schema": "SALES_DATA", "grain": ["id"], "default_filters": list(filters),
        "columns": [{"name": "id", "type": "integer"}, {"name": "customer_id", "type": "integer"},
                    {"name": "amount", "type": "decimal", "aggregation": "additive"}]})
    put(f"subject_areas/{AREA}/tables/customers.yaml", {
        "name": "customers", "schema": "SALES_DATA", "grain": ["id"],
        "columns": [{"name": "id", "type": "integer"}, {"name": "region", "type": "string"},
                    {"name": "ssn", "type": "string", "sensitive": True}]})
    put(f"subject_areas/{AREA}/relationships.yaml", [
        {"from_table": "orders", "to_table": "customers", "relationship": "many_to_one",
         "from_column": "customer_id", "to_column": "id", "review_state": "approved"}])
    put(f"subject_areas/{AREA}/metrics/revenue.yaml", {
        "name": "revenue", "calculation": "Sum of amount.", "bindings": {"Snowflake": "SUM(orders.amount)"},
        "source_tables": ["orders"], "review_state": "approved"})
    put(f"prompt_examples/{AREA}/examples.yaml", [
        {"question": "Revenue by region?", "status": "confirmed",
         "sql": "SELECT c.region, SUM(o.amount) FROM SALES_DATA.orders AS o JOIN customers AS c "
                "ON o.customer_id = c.id GROUP BY c.region"},
        {"question": "What changed?", "status": "confirmed"},
        {"question": "Audit rows?", "status": "confirmed", "sql": "SELECT COUNT(*) FROM audit_log"},
        {"question": "Draft question?", "status": "proposed"},
    ])
    put("golden_datasets/core.yaml", {"test_cases": [
        {"id": "g1", "query": "Total revenue", "expected": {
            "sql": "SELECT SUM(amount) FROM SALES_DATA.orders", "sql_confirmed": True}},
        {"id": "g2", "query": "Draft count", "expected": {
            "sql": "SELECT COUNT(*) FROM orders", "sql_confirmed": False}},
        {"id": "g3", "query": "Customers by ssn", "expected": {
            "sql": "SELECT ssn FROM customers", "sql_confirmed": True}},
        {"id": "g4", "query": "Revenue for the 'north' region, per \\ customer", "expected": {
            "sql": "SELECT o.customer_id, SUM(o.amount) FROM orders AS o JOIN customers AS c "
                   "ON o.customer_id = c.id WHERE c.region = 'north' GROUP BY 1",
            "sql_confirmed": True}},
        {"id": "g5", "query": "Average order value", "match": "bounded",
         "bounds": {"min_rows": 1, "max_rows": 1, "min_value": 1570.31, "max_value": 1570.32},
         "expected": {"sql": "SELECT AVG(amount) AS aov FROM orders;", "sql_confirmed": True}},
    ]})
    return root


@pytest.fixture(scope="module")
def acme(tmp_path_factory) -> tuple[Path, dict]:
    root = _write_model(tmp_path_factory.mktemp("art") / "acme")
    out = tmp_path_factory.mktemp("acme_out")
    return out, _export(root, out)


def _fake_view(**kw) -> X.ExportView:
    base = dict(target="snowflake", engines=("Snowflake",), source_engine="Snowflake",
                sql_engine="Snowflake", datasource="acme", description="Acme demo.", area=AREA,
                area_description="Orders.", tables=(), metrics=(), relationships=(), entities=(),
                examples=(), golden=(), ambiguities=(), terminology=())
    return X.ExportView(**{**base, **kw})


# ---------------------------------------------------------------------------
# Success criteria: the agent
# ---------------------------------------------------------------------------


def test_the_sample_writes_one_agent_file_bound_to_the_view_ace168_names(sample):
    out, report = sample
    agents = sorted(p.name for p in (out / "snowflake").glob("*.agent.sql"))
    assert agents == ["agami_example.agent.sql"]
    assert "snowflake/agami_example.agent.sql" in report["targets"]["snowflake"]["files"]
    bound = _bound_view(_read(out, "agami_example.agent.sql"))
    assert bound == _view_fqn_in_create_call(_read(out, "agami_example.sql")) == \
        S.semantic_view_fqn(_fake_view(area="agami-example"), OPTS) == "DEMO.SALES_DATA.agami_example"


def test_the_sample_has_no_golden_items_so_writes_no_evaluation_files(sample):
    out, _ = sample
    assert not list((out / "snowflake").glob("*evaluation*"))
    assert not list((out / "snowflake").glob("*.mcp_server.sql"))     # opt-in only


@pytest.mark.parametrize("which", ["sample", "acme"])
def test_the_specification_passes_the_documented_rules(which, request):
    out, _ = request.getfixturevalue(which)
    (path,) = (out / "snowflake").glob("*.agent.sql")
    sql = path.read_text("utf-8")
    assert validate_agent_statement(sql) == []
    text = spec_text(sql)
    assert validate_agent_spec(text) == []
    spec = yaml.safe_load(text)
    assert [t["tool_spec"]["type"] for t in spec["tools"]] == [A.TOOL_TYPE]
    assert "models" not in spec and "orchestration" not in spec   # left to Snowflake's defaults
    assert "execution_environment" not in json.dumps(spec)         # the warehouse is the customer's


def test_a_replace_keeps_the_agents_grants():
    # CREATE AGENT: "When COPY GRANTS is not specified, the replacement agent loses all explicit
    # access privileges that were granted on the original agent."
    sql = A.write_agent(_fake_view(), OPTS).files[f"{AREA}.agent.sql"].decode()
    assert "CREATE OR REPLACE AGENT DEMO.SALES_DATA.sales_agent COPY GRANTS\n  FROM SPECIFICATION" in sql
    assert "keeping the grants" in sql.split("CREATE OR REPLACE AGENT", 1)[0]


def test_every_eligible_sample_example_is_a_sample_question_in_stable_order(sample):
    out, report = sample
    raw = yaml.safe_load((SAMPLE / "prompt_examples" / "agami-example" / "examples.yaml").read_text())
    want = sorted(e["question"] for e in raw if e.get("status") == "confirmed")
    spec = yaml.safe_load(spec_text(_read(out, "agami_example.agent.sql")))
    assert [q["question"] for q in spec["instructions"]["sample_questions"]] == want
    assert not [x for x in report["targets"]["snowflake"]["losses"]
                if x["kind"] == "example" and x["outcome"] == "dropped"]


def test_the_acme_agent_carries_descriptions_ambiguities_and_eligible_questions(acme):
    out, report = acme
    spec = yaml.safe_load(spec_text(_read(out, f"{AREA}.agent.sql")))
    ins = spec["instructions"]
    assert [q["question"] for q in ins["sample_questions"]] == [
        "Audit rows?", "Revenue by region?", "What changed?"]       # the proposed one stays home
    assert ins["orchestration"] == "Acme demo sales. Orders and customers."
    # Deliberately changed: the last line asks the agent to name the view as the source of the
    # tool's results, and says nothing about how the agent behaves.
    assert ins["response"].splitlines() == [
        "When a question says amount and the question does not settle it, ask which is meant: "
        "net, gross.",
        "When a question says recent, answer with 30 days, say so, and offer 30 days, quarter.",
        "When results come from the sales tool, name the semantic view DEMO.SALES_DATA.sales as "
        "their source."]
    losses = _losses(report)
    assert ("example", "What changed?") not in losses              # a sample question carries it
    assert losses[("example", "Audit rows?")] == [("degraded", "no_home_in_target")]


def test_a_fiscal_year_and_default_filters_become_orchestration_instructions(tmp_path):
    root = _write_model(tmp_path / "acme", fiscal=4, filters=("{alias}.amount > 0",))
    _export(root, tmp_path / "out")
    text = spec_text(_read(tmp_path / "out", f"{AREA}.agent.sql"))
    assert validate_agent_spec(text) == []
    orch = yaml.safe_load(text)["instructions"]["orchestration"]
    assert "The fiscal year starts in April: a fiscal year, quarter or half counts from that " \
           "month, and a calendar one from January." in orch
    # The optional-filter rule in the words every target uses.
    assert orch.endswith(f"About the semantic view's default filters: {X.DEFAULT_FILTER_NOTE}")


def test_a_january_fiscal_year_and_no_filters_add_nothing(acme):
    out, _ = acme
    orch = yaml.safe_load(spec_text(_read(out, f"{AREA}.agent.sql")))["instructions"]["orchestration"]
    assert "fiscal" not in orch and "default filter" not in orch


def _many_examples(n: int, size: int) -> tuple[X.ExportQuestion, ...]:
    return tuple(X.ExportQuestion("example", f"q{i:04d}", f"Question {i:04d} " + "x" * size, None)
                 for i in range(n))


def test_an_oversized_specification_drops_sample_questions_from_the_end():
    view = _fake_view(examples=_many_examples(400, 400))
    out = A.write_agent(view, OPTS)
    text = spec_text(out.files[f"{AREA}.agent.sql"].decode("utf-8"))
    assert len(text.encode("utf-8")) <= LIMIT and validate_agent_spec(text) == []
    kept = [q["question"] for q in yaml.safe_load(text)["instructions"]["sample_questions"]]
    assert kept == [q.question for q in view.examples[:len(kept)]] and 0 < len(kept) < 400
    dropped = [(x.kind, x.name, x.outcome, x.reason) for x in out.losses]
    assert dropped == [("example", q.name, "dropped", "target_limit") for q in view.examples[len(kept):]] + [
        ("agent", AREA, "degraded", "target_rule")]   # no --snowflake-warehouse
    # Maximal: one more question would not have fitted.
    one_more = A.agent_spec(view, OPTS, [q.question for q in view.examples[:len(kept) + 1]])
    assert len(S.render_yaml(one_more).encode("utf-8")) > LIMIT


def test_instructions_alone_over_the_limit_write_no_agent_and_say_so():
    view = _fake_view(area_description="y" * (LIMIT + 1), examples=_many_examples(2, 5))
    out = A.write_agent(view, OPTS)
    assert out.files == {}
    assert [(x.kind, x.reason) for x in out.losses] == [
        ("example", "target_limit"), ("example", "target_limit"), ("agent", "target_limit")]


# ---------------------------------------------------------------------------
# Success criteria: the evaluation data, with answers computed at load time
# ---------------------------------------------------------------------------


def test_the_evaluation_data_holds_exactly_the_eligible_golden_items(acme):
    out, report = acme
    sql = _read(out, f"{AREA}.evaluation.sql")
    problems, rows = validate_evaluation(sql, "sales")
    assert problems == []
    assert [q for q, _ in rows] == ["Total revenue", "Revenue for the 'north' region, per \\ customer",
                                    "Average order value"]
    losses = _losses(report)
    assert losses[("golden", "core:g2")] == [("dropped", "not_approved")]
    assert losses[("golden", "core:g3")] == [("dropped", "depends_on_excluded")]
    # Deliberately changed: the expected tool output is on the view's logical names, as the SQL
    # the Cortex Analyst tool writes is.
    assert rows[0][1] == {"ground_truth_invocations": [
        {"tool_name": "sales", "tool_input": "Total revenue",
         "tool_output": "SELECT SUM(amount) FROM __orders"}],
        "ground_truth_output": COMPUTED}
    # Deliberately replaced (wave 2): every row's expected answer is computed, so the old
    # evaluation-level `detail_lost` entry is gone, and the header says what the answer is.
    assert ("evaluation", AREA) not in losses
    head = sql.split("CREATE OR REPLACE TABLE", 1)[0]
    assert "3 of 3 rows compute theirs from the golden SQL" in head
    assert "goes stale when the data changes" in head and "under a new dataset name" in head
    assert "documents no statement that drops a dataset" in head


def test_the_golden_sql_runs_on_the_physical_tables_inside_the_insert(acme):
    out, _ = acme
    subs = golden_subqueries(_read(out, f"{AREA}.evaluation.sql"))
    assert subs == {
        "Total revenue": "SELECT SUM(amount) FROM DEMO.SALES_DATA.orders",
        "Revenue for the 'north' region, per \\ customer":
            "SELECT o.customer_id, SUM(o.amount) FROM DEMO.SALES_DATA.orders AS o JOIN "
            "DEMO.SALES_DATA.customers AS c ON o.customer_id = c.id WHERE c.region = 'north' GROUP BY 1",
        "Average order value": "SELECT AVG(amount) AS aov FROM DEMO.SALES_DATA.orders",   # `;` gone
    }
    for sub in subs.values():
        assert _parse(sub) is not None


def test_the_computed_answer_is_capped_and_ordered_by_text(acme):
    out, _ = acme
    sql = _read(out, f"{AREA}.evaluation.sql")
    ins = _statements(sql)[1]
    windows = [w for w in ins.find_all(exp.Window) if isinstance(w.this, exp.RowNumber)]
    assert len(windows) == 1 and windows[0].args["order"].expressions[0].this.name == "j"
    qualify = ins.find(exp.Qualify)
    assert qualify.this.expression.this == str(A.ANSWER_ROWS)
    assert f"LEFT(agami_result.r, {A.ANSWER_CHARS})" in sql
    assert "ARRAY_AGG(j) WITHIN GROUP (ORDER BY j)" in sql


def test_bounds_become_a_rubric_sentence_and_are_declared_carried(acme):
    out, report = acme
    sql = _read(out, f"{AREA}.evaluation.sql")
    avg = sql.split("SELECT 'Average order value'", 1)[1].split(";\n", 1)[0]
    assert "The value must be between 1570.31 and 1570.32. The number of result rows must be 1." \
        in avg
    view = _fake_view(golden=(X.ExportQuestion(
        "golden", "core:b", "Floor?", None, bounds=GoldenBounds(min_rows=1),
        detail_fields=("bounds",)),))
    w = A.write_agent(view, OPTS)
    assert w.carried_golden_fields == {"core:b": frozenset({"bounds"})}
    _, rows = validate_evaluation(w.files[f"{AREA}.evaluation.sql"].decode(), "sales")
    # No SQL to compute from, so the bounds stand alone as the rubric.
    assert rows[0][1]["ground_truth_output"] == "The number of result rows must be at least 1."


@pytest.mark.parametrize("sql,why", [
    ("SELECT 1; SELECT 2", "more than one statement"),
    ("UPDATE orders SET amount = 0", "not a query"),
    ("SELECT amount FROM orders WHERE id = :id", "parameters"),
    ("SELECT amount FROM orders WHERE id = ?", "parameters"),
    ("SELECT * FROM orders", "reads *"),
    ("SELECT o.* FROM orders AS o", "reads *"),
    ("SELECT id AS x, amount AS X FROM orders", "share a name"),
    ("SELECT amount FROM audit_log", "cannot be placed"),
    ("SELECT COUNT(*) AS n FROM big_orders", "cannot be placed"),    # defined by a query
    ("SELECT FROM ((", "does not parse"),
])
def test_golden_sql_that_cannot_run_as_a_subquery_gets_no_answer(sql, why):
    view = _acme_view()
    sub, reason = A.embeddable_sql(view, OPTS, X.ExportQuestion("golden", "core:x", "Q?", sql))
    assert sub is None and why in reason


@pytest.mark.parametrize("sql,want", [
    ("SELECT SUM(amount) FROM orders;  ", "SELECT SUM(amount) FROM DEMO.SALES_DATA.orders"),
    ("WITH big AS (SELECT id, amount FROM orders WHERE amount > 10) SELECT COUNT(*) AS n FROM big",
     "WITH big AS (SELECT id, amount FROM DEMO.SALES_DATA.orders WHERE amount > 10) "
     "SELECT COUNT(*) AS n FROM big"),
])
def test_a_trailing_semicolon_and_ctes_still_embed(sql, want):
    sub, _ = A.embeddable_sql(_acme_view(), OPTS, X.ExportQuestion("golden", "core:x", "Q?", sql))
    assert sub == want


def _table(name: str, sql: str | None = None, *more: str) -> X.ExportTable:
    doc = {"name": name, "schema": "SALES_DATA", "columns": [{"name": c, "type": "integer"}
                                                              for c in ("id", *more)]}
    if sql:
        doc.update(source_type="sql", sql=sql)
    return X.ExportTable(Table.model_validate(doc), X.PhysicalName(None, "SALES_DATA", name))


def _acme_view() -> X.ExportView:
    return _fake_view(tables=(_table("orders", None, "amount"), _table("customers"),
                              _table("big_orders", "SELECT id FROM orders WHERE amount > 100")))


def test_golden_sql_not_carried_or_not_parseable_gets_no_answer_and_says_so():
    view = _fake_view(golden=(X.ExportQuestion("golden", "core:a", "Other engine?", None),
                              X.ExportQuestion("golden", "core:b", "Broken?", "SELECT FROM ((")))
    out = A.write_agent(view, OPTS)
    problems, rows = validate_evaluation(out.files[f"{AREA}.evaluation.sql"].decode(), "sales")
    assert problems == []
    assert [d for _, d in rows] == [
        {"ground_truth_invocations": [{"tool_name": "sales", "tool_input": "Other engine?"}]},
        {"ground_truth_invocations": [{"tool_name": "sales", "tool_input": "Broken?"}]}]
    # Deliberately replaced (wave 2): one entry per item without an answer, saying why.
    assert [(x.name, x.outcome, x.reason) for x in out.losses] == [
        (AREA, "degraded", "target_rule"),
        ("core:a", "degraded", "detail_lost"),
        ("core:b", "degraded", "unparseable_for_target"), ("core:b", "degraded", "detail_lost")]
    assert "written for another engine" in out.losses[1].detail
    assert "does not parse" in out.losses[3].detail


def test_the_flag_turns_computed_answers_off_and_reports_it():
    q = X.ExportQuestion("golden", "core:a", "Total?", "SELECT 1 AS one")
    out = A.write_agent(_fake_view(golden=(q,)), _opts(snowflake_no_expected_answers=True))
    sql = out.files[f"{AREA}.evaluation.sql"].decode()
    problems, rows = validate_evaluation(sql, "sales")
    assert problems == [] and "ground_truth_output" not in rows[0][1]
    assert "0 of 1 rows compute theirs" in sql
    assert [(x.kind, x.reason) for x in out.losses][-1] == ("evaluation", "detail_lost")
    config = yaml.safe_load(out.files[f"{AREA}.evaluation_config.yaml"])
    # With no expected answer anywhere, answer correctness would score zero on every row.
    assert "answer_correctness" not in [m["name"] for m in config["metrics"]]


def test_the_flags_parse_as_target_arguments():
    sp = argparse.ArgumentParser()
    A.add_agent_arguments(sp)
    assert vars(sp.parse_args([])) == {}
    assert vars(sp.parse_args(["--snowflake-no-expected-answers", "--snowflake-mcp-server"])) == {
        "snowflake_no_expected_answers": True, "snowflake_mcp_server": True}


def test_every_sql_string_in_the_evaluation_data_parses_in_snowflake(acme):
    out, _ = acme
    _, rows = validate_evaluation(_read(out, f"{AREA}.evaluation.sql"), "sales")
    sqls = [c["tool_output"] for _, d in rows for c in d["ground_truth_invocations"] if "tool_output" in c]
    assert len(sqls) == 3
    for sql in sqls:
        assert _parse(sql) is not None


# ---------------------------------------------------------------------------
# Success criteria: run configurations and statements
# ---------------------------------------------------------------------------


def test_the_run_configuration_passes_the_documented_rules(acme):
    out, _ = acme
    doc = yaml.safe_load(_read(out, f"{AREA}.evaluation_config.yaml"))
    assert validate_run_config(doc) == []
    assert doc["evaluation"]["agent_params"] == {"agent_name": "DEMO.SALES_DATA.SALES_AGENT",
                                                 "agent_type": "CORTEX AGENT"}
    assert doc["evaluation"]["source_metadata"] == {"type": "dataset",
                                                    "dataset_name": "SALES_EVALUATION"}
    assert "dataset" not in doc                   # the evaluation file registers it, once
    system = [m for m in doc["metrics"] if m["name"] in _SYSTEM_METRICS]
    assert system == [{"name": n, "version": "v3"} for n in (
        "answer_correctness", "tool_selection_accuracy", "tool_execution_accuracy",
        "logical_consistency")]
    (amb,) = [m for m in doc["metrics"] if m["name"] == "agami_ambiguity_handling"]
    assert "ask which is meant: net, gross" in amb["prompt"] and "{{input}}" in amb["prompt"]
    # Snowflake's documentation says to set `model`, so each custom metric names v3's default judge.
    assert amb["model"] == "claude-sonnet-4-6"
    assert not [m for m in doc["metrics"] if m["name"] == "agami_optional_filters"]


def test_default_filters_add_the_optional_filter_metric(tmp_path):
    root = _write_model(tmp_path / "acme", filters=("{alias}.amount > 0",))
    _export(root, tmp_path / "out")
    doc = yaml.safe_load(_read(tmp_path / "out", f"{AREA}.evaluation_config.yaml"))
    assert validate_run_config(doc) == []
    (m,) = [m for m in doc["metrics"] if m["name"] == "agami_optional_filters"]
    assert "- orders: orders.amount > 0" in m["prompt"] and X.DEFAULT_FILTER_NOTE in m["prompt"]
    assert "{{tool_info}}" in m["prompt"]


def test_the_analyst_configuration_scores_verified_queries_by_result(acme):
    out, _ = acme
    doc = yaml.safe_load(_read(out, f"{AREA}.analyst_evaluation_config.yaml"))
    assert validate_analyst_config(doc) == []
    # Deliberately changed: the view is named in full, and its verified queries are listed.
    assert doc == {"evaluation": {"analyst_params": {"analyst_name": "DEMO.SALES_DATA.SALES",
                                                     "analyst_type": "SEMANTIC VIEW"},
                                  "source_metadata": {"type": "verified_queries",
                                                      "verified_queries": ["Revenue by region?"]}},
                   "metrics": [{"name": "sql_correctness", "version": "v3"}]}


def test_no_example_sql_means_no_analyst_configuration():
    view = _fake_view(golden=(X.ExportQuestion("golden", "core:a", "Total?", None),))
    files = A.write_agent(view, OPTS).files
    assert f"{AREA}.analyst_evaluation_config.yaml" not in files
    assert "GET_ANALYST_AI_EVALUATION_DATA" not in files[f"{AREA}.evaluation_run.sql"].decode()


def test_the_run_statements_parse_and_stage_the_files_byte_for_byte(acme):
    out, _ = acme
    sql = _read(out, f"{AREA}.evaluation_run.sql")
    statements = _statements(sql)
    assert isinstance(statements[0], exp.Use)
    staged = {}
    for s in statements:
        if isinstance(s, exp.Copy):
            path = s.this.name if hasattr(s.this, "name") else ""
            body = s.find(exp.RawString) or s.find(exp.Literal)
            staged[path or len(staged)] = body.this
    assert sorted(staged.values()) == sorted(
        "\n" + _read(out, f) for f in (f"{AREA}.evaluation_config.yaml",
                                       f"{AREA}.analyst_evaluation_config.yaml"))
    for call in ("CALL EXECUTE_AI_EVALUATION('START', OBJECT_CONSTRUCT('run_name', 'agami-run-1'), "
                 "'@DEMO.SALES_DATA.sales_evaluation_config/sales.evaluation_config.yaml');",
                 "SNOWFLAKE.LOCAL.GET_AI_EVALUATION_DATA(\n  'DEMO', 'SALES_DATA', 'SALES_AGENT', "
                 "'CORTEX AGENT', 'agami-run-1')",
                 "SNOWFLAKE.LOCAL.GET_ANALYST_AI_EVALUATION_DATA(\n  'DEMO', 'SALES_DATA', 'SALES', "
                 "'SEMANTIC VIEW', 'agami-analyst-run-1')"):
        assert call in sql
    assert "written, never run" in sql.splitlines()[0]


def test_two_runs_are_byte_identical(tmp_path):
    root = _write_model(tmp_path / "art" / "acme")
    a, b = tmp_path / "a", tmp_path / "b"
    _export(root, a), _export(root, b)
    names = sorted(p.name for p in (a / "snowflake").iterdir())
    assert names == ["sales.agent.sql", "sales.analyst_evaluation_config.yaml", "sales.evaluation.sql",
                     "sales.evaluation_config.yaml", "sales.evaluation_run.sql",
                     "sales.semantic_view.yaml", "sales.sql"]
    for name in names:
        assert (a / "snowflake" / name).read_bytes() == (b / "snowflake" / name).read_bytes()


# ---------------------------------------------------------------------------
# Success criteria: the MCP server
# ---------------------------------------------------------------------------


def test_the_mcp_server_exposes_the_view_and_the_agent():
    view = _fake_view()
    sql = A.write_agent(view, _opts(snowflake_mcp_server=True)).files[f"{AREA}.mcp_server.sql"].decode()
    text = mcp_spec_text(sql)
    assert validate_mcp_spec(text) == []
    tools = yaml.safe_load(text)["tools"]
    assert [(t["name"], t["type"], t["identifier"]) for t in tools] == [
        ("sales-analyst", "CORTEX_ANALYST_MESSAGE", "DEMO.SALES_DATA.sales"),
        ("sales-agent", "CORTEX_AGENT_RUN", "DEMO.SALES_DATA.sales_agent")]
    head = sql.split("CREATE OR REPLACE MCP SERVER", 1)[0]
    assert ("https://<account_url>/api/v2/databases/DEMO/schemas/SALES_DATA/mcp-servers/SALES_MCP"
            in head)
    assert "GRANT USAGE ON MCP SERVER DEMO.SALES_DATA.sales_mcp" in head
    assert "CREATE OR REPLACE MCP SERVER DEMO.SALES_DATA.sales_mcp\n  FROM SPECIFICATION $$" in sql


def test_without_an_agent_the_mcp_server_has_only_the_analyst_tool():
    view = _fake_view(area_description="y" * (LIMIT + 1))
    files = A.write_agent(view, _opts(snowflake_mcp_server=True)).files
    assert f"{AREA}.agent.sql" not in files
    tools = yaml.safe_load(mcp_spec_text(files[f"{AREA}.mcp_server.sql"].decode()))["tools"]
    assert [t["type"] for t in tools] == ["CORTEX_ANALYST_MESSAGE"]


# ---------------------------------------------------------------------------
# The warehouse: without --snowflake-warehouse the agent runs on the caller's default warehouse,
# so the header and the report say a caller needs one.
# ---------------------------------------------------------------------------

_BLOCK = ("--     execution_environment:\n--       type: warehouse\n"
          "--       warehouse: <YOUR_WAREHOUSE>\n")


def test_without_a_warehouse_the_header_shows_the_block_and_the_report_says_it_will_not_run(sample):
    out, report = sample
    sql = _read(out, "agami_example.agent.sql")
    header = sql.split("CREATE OR REPLACE AGENT", 1)[0]
    assert "does NOT run for a caller with no default warehouse" in header
    assert "--snowflake-warehouse NAME" in header and _BLOCK in header
    assert "tool_resources.agami_example" in header
    (entry,) = [x for x in report["targets"]["snowflake"]["losses"] if x["kind"] == "agent"]
    assert (entry["name"], entry["outcome"], entry["reason"]) == \
        ("agami-example", "degraded", "target_rule")
    assert "caller with none" in entry["detail"] and "--snowflake-warehouse" in entry["detail"]
    assert "snowflake_warehouse" not in report["options"]["target_args"]


def test_the_header_block_is_the_one_the_flag_writes():
    # What the comment tells a person to add is, uncommented, what --snowflake-warehouse emits.
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "SALES_DATA"),
                           target_args={"snowflake_warehouse": "MY_WH"})
    block = "".join(line[3:] + "\n" for line in _BLOCK.splitlines())
    spec = S.render_yaml(A.agent_spec(_fake_view(), opts, []))
    assert block.replace("<YOUR_WAREHOUSE>", "MY_WH") in spec


def test_the_warehouse_flag_writes_the_execution_environment(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    report = _export(SAMPLE, a, "--snowflake-warehouse", "agami_smoke_wh")
    _export(SAMPLE, b, "--snowflake-warehouse", "agami_smoke_wh")
    sql = _read(a, "agami_example.agent.sql")
    text = spec_text(sql)
    assert validate_agent_spec(text) == []
    assert yaml.safe_load(text)["tool_resources"] == {"agami_example": {
        "semantic_view": "DEMO.SALES_DATA.agami_example",
        "execution_environment": {"type": "warehouse", "warehouse": "AGAMI_SMOKE_WH"}}}
    header = sql.split("CREATE OR REPLACE AGENT", 1)[0]
    assert "AGAMI_SMOKE_WH" in header and "does NOT run" not in header
    assert not [x for x in report["targets"]["snowflake"]["losses"] if x["kind"] == "agent"]
    assert report["options"]["target_args"] == {"snowflake_warehouse": "agami_smoke_wh"}
    for p in sorted((a / "snowflake").iterdir()):
        assert p.read_bytes() == (b / "snowflake" / p.name).read_bytes()


@pytest.mark.parametrize("name", ["My_Wh", "select", "wh name", '"WH"', "1wh", "db.wh", ""])
def test_a_warehouse_name_snowflake_would_need_quoted_is_refused(tmp_path, name):
    rc, report = _run(SAMPLE, tmp_path / "out", "--snowflake-warehouse", name)
    assert rc == 2 and report["error"] == "export_refused"
    assert "--snowflake-warehouse" in report["detail"]
    assert not (tmp_path / "out" / "snowflake").exists()


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_a_specification_holding_dollar_quotes_falls_back_to_an_escaped_literal():
    view = _fake_view(area_description="Costs in $$ and 'quotes' \\ too.")
    sql = A.write_agent(view, OPTS).files[f"{AREA}.agent.sql"].decode()
    text = spec_text(sql)
    assert "$$\n" not in sql.split("FROM SPECIFICATION", 1)[1]
    assert yaml.safe_load(text)["instructions"]["orchestration"] == \
        "Acme demo. Costs in $$ and 'quotes' \\ too."


def test_names_follow_the_view_and_quote_where_snowflake_needs_it():
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("DEMO", "my schema"))
    view = _fake_view(area="Sales Ops")
    assert A.agent_fqn(view, opts) == 'DEMO."my schema".sales_ops_agent'
    assert A.evaluation_table_fqn(view, opts) == 'DEMO."my schema".sales_ops_evaluation_data'
    assert A.evaluation_dataset_fqn(view, opts) == 'DEMO."my schema".sales_ops_evaluation'
    assert A.mcp_server_fqn(view, opts) == 'DEMO."my schema".sales_ops_mcp'
    assert A.tool_name(view) == S.semantic_view_name(view) == "sales_ops"
    config = A.run_config(view, opts, [])
    assert config["evaluation"]["agent_params"]["agent_name"] == 'DEMO."my schema".SALES_OPS_AGENT'


def test_a_keyword_location_and_area_still_name_objects_snowflake_can_parse():
    # A keyword is quoted in the case Snowflake stores it; a name minted from one is never one.
    opts = X.ExportOptions(targets=("snowflake",), location=X.Location("demo", "order"))
    view = _fake_view(area="Select")
    assert A.agent_fqn(view, opts) == 'demo."ORDER".select__agent'
    assert A.evaluation_table_fqn(view, opts) == 'demo."ORDER".select__evaluation_data'
    assert A.tool_name(view) == "select_"
    spec = A.agent_spec(view, opts, [])
    assert spec["tool_resources"] == {"select_": {"semantic_view": 'demo."ORDER".select_'}}
    for fqn in (A.agent_fqn(view, opts), A.evaluation_dataset_fqn(view, opts),
                A.mcp_server_fqn(view, opts)):
        assert sqlglot.parse_one(f"SELECT 1 FROM {fqn}", dialect="snowflake",
                                 error_level=ErrorLevel.RAISE).find(exp.Table) is not None


# ---------------------------------------------------------------------------
# The validators reject what breaks each rule
# ---------------------------------------------------------------------------


def _good_spec() -> dict:
    return {"instructions": {"response": "r", "sample_questions": [{"question": "q?"}]},
            "tools": [{"tool_spec": {"type": A.TOOL_TYPE, "name": "t"}}],
            "tool_resources": {"t": {"semantic_view": "D.S.v"}}}


def test_the_spec_validator_passes_a_documented_specification():
    assert validate_agent_spec(yaml.safe_dump(_good_spec())) == []
    spec = _good_spec()
    spec["tool_resources"]["t"]["execution_environment"] = {"type": "warehouse", "warehouse": "W"}
    assert validate_agent_spec(yaml.safe_dump(spec)) == []


@pytest.mark.parametrize("fn,needle", [
    (lambda s: s.update(extra=1), "undocumented key 'extra'"),
    (lambda s: s["instructions"].update(tone="x"), "undocumented key 'tone'"),
    (lambda s: s["instructions"]["sample_questions"].append("bare"), "sample_questions[1]"),
    (lambda s: s.update(models={"planner": "x"}), "models: undocumented"),
    (lambda s: s.update(orchestration={"budget": {"dollars": 1}}), "budget: undocumented"),
    (lambda s: s["tools"].append({"spec": {}}), "tools[1]"),
    (lambda s: s["tools"][0]["tool_spec"].update(warehouse="W"), "undocumented key 'warehouse'"),
    (lambda s: s["tool_resources"].update(other={"semantic_view": "x"}), "matches no tool"),
    (lambda s: s["tool_resources"]["t"].pop("semantic_view"), "exactly one of"),
    (lambda s: s["tool_resources"]["t"].update(semantic_model_file="@s/f.yaml"), "exactly one of"),
    (lambda s: s["tool_resources"]["t"].update(execution_environment={"type": "warehouse"}),
     "type warehouse with a warehouse name"),
    (lambda s: s["tool_resources"]["t"].update(execution_environment={
        "type": "warehouse", "warehouse": "W", "size": "XL"}), "undocumented key 'size'"),
    (lambda s: s["instructions"].update(response="z" * LIMIT), "over 100000"),
    (lambda s: s["instructions"].update(system=["x"]), "instructions.system is not a string"),
])
def test_the_spec_validator_rejects_each_broken_rule(fn, needle):
    spec = _good_spec()
    fn(spec)
    assert any(needle in p for p in validate_agent_spec(yaml.safe_dump(spec, width=10**6))), needle


@pytest.mark.parametrize("bad", [
    "CREATE OR REPLACE AGENT D.S.a\n  FROM SPECIFICATION\n  $$x$$;",           # grants dropped
    "CREATE AGENT IF NOT EXISTS D.S.a COPY GRANTS\n  FROM SPECIFICATION\n  $$x$$;",
    "CREATE OR REPLACE AGENT D.S.a FROM SPECIFICATION COPY GRANTS\n  $$x$$;",
])
def test_the_statement_validator_rejects_a_replace_without_copy_grants(bad):
    assert validate_agent_statement(bad) != []


_GOOD_EVAL = """CREATE OR REPLACE TABLE D.S.e (input_query VARCHAR, ground_truth VARIANT);
INSERT INTO D.S.e (input_query, ground_truth) SELECT 'q?', PARSE_JSON('{"ground_truth_invocations": [{"tool_name": "t"}]}');
INSERT INTO D.S.e (input_query, ground_truth) SELECT 'r?', TO_VARIANT(OBJECT_INSERT(TO_OBJECT(PARSE_JSON('{"ground_truth_invocations": [{"tool_name": "t"}]}')), 'ground_truth_output', 'n=' || agami_result.n)) FROM (SELECT COUNT(*) AS n FROM (SELECT 1 AS one) AS agami_golden) AS agami_result;
CALL SYSTEM$CREATE_EVALUATION_DATASET('Cortex Agent', 'D.S.e', 'D.S.d', OBJECT_CONSTRUCT('query_text', 'INPUT_QUERY', 'expected_tools', 'GROUND_TRUTH'));
"""


def test_the_evaluation_validator_passes_the_documented_shape():
    assert validate_evaluation(_GOOD_EVAL, "t") == ([], [
        ("q?", {"ground_truth_invocations": [{"tool_name": "t"}]}),
        ("r?", {"ground_truth_invocations": [{"tool_name": "t"}], "ground_truth_output": COMPUTED})])


@pytest.mark.parametrize("old,new,needle", [
    ("ground_truth VARIANT", "ground_truth VARCHAR", "VARIANT"),
    ("PARSE_JSON('{", "('{", "PARSE_JSON"),
    ('"ground_truth_invocations"', '"expected"', "undocumented ground truth key"),
    ('"tool_name": "t"', '"tool_name": "t", "sql": "x"', "undocumented invocation key"),
    ('"tool_name": "t"', '"tool_name": "u"', "tool_name 'u'"),
    ("'Cortex Agent'", "'Other'", "Cortex Agent dataset"),
    ("'expected_tools'", "'ground_truth'", "column mapping"),
    ("CALL SYSTEM$CREATE_EVALUATION_DATASET", "CALL OTHER", "SYSTEM$CREATE_EVALUATION_DATASET"),
    ("INSERT INTO D.S.e", "INSERT INTO D.S.x", "insert into the table"),
    # The computed row: an OBJECT is not a VARIANT, the key is the documented one, it reads a row.
    ("TO_VARIANT(OBJECT_INSERT(", "(OBJECT_INSERT(", "ground_truth_output inserted"),
    ("'ground_truth_output', 'n='", "'answer', 'n='", "computes 'answer'"),
    ("FROM (SELECT COUNT(*) AS n FROM (SELECT 1 AS one) AS agami_golden) AS agami_result", "",
     "reads from nothing"),
    ("AS agami_golden", "AS other", "no golden subquery"),
    ('[{"tool_name": "t"}]}\')), \'ground', '[{"tool_name": "t"}], "ground_truth_output": "x"}\')), \'ground',
     "both constant and computed"),
])
def test_the_evaluation_validator_rejects_each_broken_rule(old, new, needle):
    bad = _GOOD_EVAL.replace(old, new, 1) if old != "ground_truth VARIANT" else \
        _GOOD_EVAL.replace(old, new)
    assert bad != _GOOD_EVAL
    problems, _ = validate_evaluation(bad, "t")
    assert any(needle in p for p in problems), problems


def test_the_evaluation_validator_rejects_an_insert_that_does_not_parse():
    with pytest.raises(sqlglot.errors.ParseError):
        validate_evaluation(_GOOD_EVAL.replace("FROM (SELECT COUNT(*)", "FROM ((SELECT COUNT(*)"), "t")


def _good_config() -> dict:
    return {"evaluation": {"agent_params": {"agent_name": "D.S.A", "agent_type": "CORTEX AGENT"},
                           "source_metadata": {"type": "dataset", "dataset_name": "E"}},
            "metrics": ["logical_consistency", {"name": "answer_correctness", "version": "v3_0"},
                        {"name": "c", "score_ranges": dict(A.SCORE_RANGES), "prompt": "{{input}}"}]}


def test_the_config_validators_pass_documented_configurations():
    assert validate_run_config(_good_config()) == []
    assert validate_analyst_config(A.analyst_config(_fake_view(), OPTS, ["Q?"])) == []


@pytest.mark.parametrize("fn,needle", [
    (lambda c: c.update(extra=1), "undocumented key 'extra'"),
    (lambda c: c["evaluation"]["agent_params"].update(agent_type="AGENT"), "agent_type CORTEX AGENT"),
    (lambda c: c["evaluation"]["agent_params"].update(model="x"), "undocumented key 'model'"),
    (lambda c: c["evaluation"]["source_metadata"].update(type="table"), "type dataset"),
    (lambda c: c.update(metrics=[]), "non-empty"),
    (lambda c: c["metrics"].append("accuracy"), "no system metric"),
    (lambda c: c["metrics"][1].update(version="3"), "not v<major>"),
    (lambda c: c["metrics"][1].update(model="m"), "undocumented key 'model'"),
    (lambda c: c["metrics"][2].pop("prompt"), "has no prompt"),
    (lambda c: c["metrics"][2].update(score_ranges={"min_score": [1, 2]}), "score_ranges"),
    (lambda c: c["metrics"][2].update(prompt="{{answer}}"), "undocumented placeholder 'answer'"),
    (lambda c: c["metrics"].append("logical_consistency"), "listed twice"),
])
def test_the_run_config_validator_rejects_each_broken_rule(fn, needle):
    c = _good_config()
    fn(c)
    assert any(needle in p for p in validate_run_config(c)), validate_run_config(c)


@pytest.mark.parametrize("fn,needle", [
    (lambda c: c["evaluation"]["analyst_params"].update(analyst_type="VIEW"), "SEMANTIC VIEW"),
    (lambda c: c["evaluation"]["source_metadata"].update(type="dataset"), "verified_queries"),
    (lambda c: c.update(metrics=["answer_correctness"]), "no system metric"),
    (lambda c: c["evaluation"].update(agent_params={}), "undocumented key 'agent_params'"),
    (lambda c: c["evaluation"]["source_metadata"].update(verified_queries=[{"name": "q"}]),
     "list of question texts"),
])
def test_the_analyst_config_validator_rejects_each_broken_rule(fn, needle):
    c = A.analyst_config(_fake_view(), OPTS, ["Q?"])
    fn(c)
    assert any(needle in p for p in validate_analyst_config(c)), validate_analyst_config(c)


def _good_mcp() -> dict:
    return A.mcp_spec(_fake_view(), OPTS, True)


@pytest.mark.parametrize("fn,needle", [
    (lambda s: s.update(servers=[]), "undocumented key 'servers'"),
    (lambda s: s.update(tools=[]), "non-empty"),
    (lambda s: s["tools"][0].pop("title"), "has no title"),
    (lambda s: s["tools"][0].update(type="CORTEX_ANALYST"), "unsupported type"),
    (lambda s: s["tools"][0].pop("identifier"), "needs an identifier"),
    (lambda s: s["tools"][0].update(annotations={"readOnlyHint": True}), "annotations only"),
    (lambda s: s["tools"][1]["annotations"].update(safe=True), "undocumented key 'safe'"),
    (lambda s: s["tools"][1].update(name=s["tools"][0]["name"]), "not unique"),
    (lambda s: s["tools"][0].update(warehouse="W"), "undocumented key 'warehouse'"),
])
def test_the_mcp_validator_rejects_each_broken_rule(fn, needle):
    assert validate_mcp_spec(yaml.safe_dump(_good_mcp())) == []
    spec = _good_mcp()
    fn(spec)
    assert any(needle in p for p in validate_mcp_spec(yaml.safe_dump(spec))), needle


# ---------------------------------------------------------------------------
# Review-wave fixes: computed answers only from declared columns; the analyst run's own gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql,why", [
    ("SELECT id, salary FROM orders", "does not carry"),             # not a declared column
    ("SELECT OBJECT_CONSTRUCT(*) AS o FROM orders", "does not carry"),  # a star inside a call
    ("SELECT a.id FROM (SELECT * FROM orders) a", "does not carry"),    # a star in a subquery
    ("SELECT TO_JSON(o) AS j FROM orders o", "does not carry"),         # a whole row
    ("SELECT amount FROM archive.orders", "another schema"),
    ("SELECT SUM(amount) AS s FROM otherdb.otherschema.orders", "another schema"),
    ('SELECT amount FROM "orders"', "another schema"),
    ("SELECT amount FROM IDENTIFIER('orders')", "another schema"),
    ("SELECT COUNT(*), COUNT(*) FROM orders", "share a name, or one has none"),
    ("SELECT COUNT(*), id FROM orders GROUP BY id", "share a name, or one has none"),
])
def test_golden_sql_that_could_copy_undeclared_values_gets_no_computed_answer(sql, why):
    sub, reason = A.embeddable_sql(_acme_view(), OPTS, X.ExportQuestion("golden", "core:x", "Q?", sql))
    assert sub is None and why in reason, reason


def test_a_sole_unnamed_result_column_still_embeds():
    sub, _ = A.embeddable_sql(_acme_view(), OPTS,
                              X.ExportQuestion("golden", "core:x", "Q?", "SELECT COUNT(*) FROM orders"))
    assert sub == "SELECT COUNT(*) FROM DEMO.SALES_DATA.orders"


def test_a_refused_computed_answer_is_reported_with_its_reason():
    view = dataclasses.replace(_acme_view(), golden=(X.ExportQuestion(
        "golden", "core:s", "Salaries?", "SELECT id, salary FROM orders"),))
    w = A.write_agent(view, OPTS)
    _, rows = validate_evaluation(w.files[f"{AREA}.evaluation.sql"].decode(), "sales")
    assert "ground_truth_output" not in rows[0][1]
    # The expected tool output keeps its physical names too, reported as its own entry.
    (loss,) = [x for x in w.losses if x.name == "core:s" and "tool_output" not in x.detail]
    assert (loss.outcome, loss.reason) == ("degraded", "detail_lost")
    assert "does not carry" in loss.detail


def test_the_analyst_evaluation_follows_the_views_verified_queries_not_the_golden_items():
    # Verified queries and no golden item: the analyst run alone.
    examples = (X.ExportQuestion("example", "e", "e?", "SELECT COUNT(*) AS n FROM orders"),)
    w = A.write_agent(dataclasses.replace(_acme_view(), examples=examples), OPTS)
    assert {f"{AREA}.analyst_evaluation_config.yaml", f"{AREA}.evaluation_run.sql"} <= set(w.files)
    assert f"{AREA}.evaluation.sql" not in w.files
    run = w.files[f"{AREA}.evaluation_run.sql"].decode()
    assert "GET_ANALYST_AI_EVALUATION_DATA" in run and "GET_AI_EVALUATION_DATA(" not in run
    # Golden items and no verified query (the example reads a table the view lacks): the agent
    # run alone.
    golden = (X.ExportQuestion("golden", "core:g", "Count?", "SELECT COUNT(*) AS n FROM orders"),)
    examples = (X.ExportQuestion("example", "e", "e?", "SELECT COUNT(*) AS n FROM audit_log"),)
    w = A.write_agent(dataclasses.replace(_acme_view(), golden=golden, examples=examples), OPTS)
    assert f"{AREA}.analyst_evaluation_config.yaml" not in w.files
    run = w.files[f"{AREA}.evaluation_run.sql"].decode()
    assert "GET_ANALYST_AI_EVALUATION_DATA" not in run and "GET_AI_EVALUATION_DATA(" in run
    # Neither: no run file.
    assert f"{AREA}.evaluation_run.sql" not in A.write_agent(_acme_view(), OPTS).files


def test_custom_metrics_name_the_v3_judge_and_say_how_to_change_it(acme):
    out, _ = acme
    text = _read(out, f"{AREA}.evaluation_config.yaml")
    doc = yaml.safe_load(text)
    custom = [m for m in doc["metrics"] if m["name"].startswith("agami_")]
    assert custom and all(m["model"] == A.JUDGE_MODEL == "claude-sonnet-4-6" for m in custom)
    header = text.split("\nevaluation:", 1)[0]
    assert "openai-gpt-5.4" in header and "name no" not in header
    # The ambiguity rules are declared in agami, which records no review of them.
    assert "confirmed in agami" not in text


def test_renaming_the_dataset_names_every_place_it_is_written(acme):
    out, _ = acme
    head = _read(out, f"{AREA}.evaluation.sql").split("CREATE OR REPLACE TABLE", 1)[0]
    assert f"{AREA}.evaluation_run.sql" in head and "three places" in head


# ---------------------------------------------------------------------------
# The analyst evaluation names its view in full and lists its verified queries
# ---------------------------------------------------------------------------


def test_the_analyst_configuration_names_the_view_the_way_the_agent_is_named(acme):
    out, _ = acme
    analyst = yaml.safe_load(_read(out, f"{AREA}.analyst_evaluation_config.yaml"))
    agent = yaml.safe_load(_read(out, f"{AREA}.evaluation_config.yaml"))
    name = analyst["evaluation"]["analyst_params"]["analyst_name"]
    assert name == "DEMO.SALES_DATA.SALES"
    # Built the way `run_config` builds the agent's: the same location, then the object's name.
    assert agent["evaluation"]["agent_params"]["agent_name"] == name + "_AGENT"


def test_the_analyst_configuration_lists_the_verified_queries_the_view_holds(acme):
    out, _ = acme
    doc = yaml.safe_load(_read(out, f"{AREA}.analyst_evaluation_config.yaml"))
    view = yaml.safe_load(_read(out, f"{AREA}.semantic_view.yaml"))
    listed = doc["evaluation"]["source_metadata"]["verified_queries"]
    # The documented list holds each verified query's `question` text, in the view's order.
    assert listed == [q["question"] for q in view["verified_queries"]] == ["Revenue by region?"]
    assert validate_analyst_config(doc) == []


def test_the_analyst_list_names_a_question_once():
    view = _fake_view(tables=(_table("orders", None, "amount"),), examples=(
        X.ExportQuestion("example", "a", "Count?", "SELECT COUNT(*) AS n FROM orders"),
        X.ExportQuestion("example", "b", "Count?", "SELECT COUNT(id) AS n FROM orders")))
    doc = A.analyst_config(view, OPTS, ["Count?", "Count?"])
    assert doc["evaluation"]["source_metadata"]["verified_queries"] == ["Count?"]


# ---------------------------------------------------------------------------
# The expected tool output is written the way the view's verified queries are
# ---------------------------------------------------------------------------


def test_the_expected_tool_output_uses_the_views_logical_names(acme):
    out, report = acme
    _, rows = validate_evaluation(_read(out, f"{AREA}.evaluation.sql"), "sales")
    outputs = {q: d["ground_truth_invocations"][0]["tool_output"] for q, d in rows}
    assert outputs == {
        "Total revenue": "SELECT SUM(amount) FROM __orders",
        "Revenue for the 'north' region, per \\ customer":
            "SELECT o.customer_id, SUM(o.amount) FROM __orders AS o JOIN __customers AS c "
            "ON o.customer_id = c.id WHERE c.region = 'north' GROUP BY 1",
        "Average order value": "SELECT AVG(amount) AS aov FROM __orders;",
    }
    # The same rewrite the view's verified queries go through.
    view = yaml.safe_load(_read(out, f"{AREA}.semantic_view.yaml"))
    (vq,) = view["verified_queries"]
    assert vq["sql"].startswith("SELECT c.region, SUM(o.amount) FROM __orders AS o JOIN "
                                "__customers")
    details = [x["detail"] for x in report["targets"]["snowflake"]["losses"]]
    assert not [d for d in details if "tool_output" in d]


def test_a_tool_output_the_rewrite_refuses_keeps_the_physical_sql_and_says_so():
    golden = (X.ExportQuestion("golden", "core:s", "Everything?", "SELECT * FROM orders"),)
    out = A.write_agent(dataclasses.replace(_acme_view(), golden=golden), OPTS)
    _, rows = validate_evaluation(out.files[f"{AREA}.evaluation.sql"].decode(), "sales")
    assert rows[0][1]["ground_truth_invocations"][0]["tool_output"] == "SELECT * FROM orders"
    (loss,) = [x for x in out.losses if x.name == "core:s" and "tool_output" in x.detail]
    assert loss.outcome == "degraded" and loss.reason == "target_rule"
    assert "physical" in loss.detail and "selects *" in loss.detail


# ---------------------------------------------------------------------------
# The computed answer's numbers
# ---------------------------------------------------------------------------


def _insert_of(sql: str, question: str):
    for ins in _statements(sql)[1:-1]:
        if ins.expression.expressions[0].this == question:
            return ins
    raise AssertionError(question)


def test_the_computed_answer_rounds_non_integer_numbers_to_plain_decimals(acme):
    out, _ = acme
    sql = _read(out, f"{AREA}.evaluation.sql")
    ins = _insert_of(sql, "Average order value")       # parses in Snowflake's dialect
    text = sql.split("SELECT 'Average order value'", 1)[1].split(";\n", 1)[0]
    # Each value of each result row is read on its own (FLATTEN of the row object, grouped back by
    # the row's SEQ), so no column type needs to be known offline.
    lateral = ins.find(exp.Lateral)
    assert isinstance(lateral.this, exp.Explode) and lateral.alias == "f"
    assert "LATERAL FLATTEN(INPUT => agami_object.o) AS f" in text
    assert "GROUP BY f.seq" in text and "OBJECT_CONSTRUCT_KEEP_NULL(*)" in text
    # A non-integer number becomes NUMBER(38, 6) text with its trailing zeros dropped; anything
    # else, and a number too large for that type, keeps its JSON text.
    assert A.ANSWER_DECIMALS == 6
    assert "TO_DECIMAL(IFF(IS_DOUBLE(f.value)" in text and ", 38, 6)" in text
    assert "ABS(COALESCE(AS_DOUBLE(f.value), AS_DECIMAL(f.value, 38, 0))) < 1e32" in text
    assert "RTRIM(RTRIM(TO_VARCHAR(" in text and "'0'), '.')" in text
    assert "TO_JSON(f.value)" in text and "TO_JSON(TO_VARIANT(f.key))" in text
    # The row's text no longer depends on TO_JSON's key order, which Snowflake does not fix.
    assert "LISTAGG(" in text and "WITHIN GROUP (ORDER BY f.key)" in text
    assert "TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(*))" not in text
    assert golden_subqueries(sql)["Average order value"] == \
        "SELECT AVG(amount) AS aov FROM DEMO.SALES_DATA.orders"


# ---------------------------------------------------------------------------
# Session statements and the agent's response instruction
# ---------------------------------------------------------------------------


def test_only_the_run_file_sets_a_schema_and_its_header_says_why(acme, sample):
    out, _ = acme
    run = _read(out, f"{AREA}.evaluation_run.sql")
    head = run.split("\nUSE SCHEMA", 1)[0]
    assert "session's database and schema" in head and "fully qualified" in head
    for root in (acme[0], sample[0]):
        for path in (root / "snowflake").glob("*.sql"):
            if path.name.endswith(".evaluation_run.sql"):
                continue
            assert not [s for s in _statements(path.read_text("utf-8"))
                        if isinstance(s, exp.Use)], path.name


def test_the_response_instruction_only_asks_for_attribution(acme):
    out, _ = acme
    spec = yaml.safe_load(spec_text(_read(out, f"{AREA}.agent.sql")))
    assert spec["instructions"]["response"].splitlines()[-1] == (
        "When results come from the sales tool, name the semantic view DEMO.SALES_DATA.sales as "
        "their source.")
