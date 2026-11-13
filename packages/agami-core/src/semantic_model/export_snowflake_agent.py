"""Snowflake agent, its evaluation files and an MCP server (F19 / ACE-169), beside the semantic view
ACE-168 writes.

`<area>.agent.sql` is a `CREATE OR REPLACE AGENT … COPY GRANTS FROM SPECIFICATION` statement (docs:
sql-reference/sql/create-agent; COPY GRANTS keeps the grants on the agent it replaces). Its one tool
is `cortex_analyst_text_to_sql`, bound through `tool_resources.<tool>.semantic_view` to the view
`<area>.sql` creates. The specification carries the datasource and subject area descriptions, the
fiscal year when it does not start in January and the optional-filter rule as orchestration
instructions, the ambiguities and where results come from as response instructions, and every
eligible example question as a sample question. It sets no orchestration model or budget, which
have Snowflake defaults. The warehouse is the customer's own to name: `--snowflake-warehouse` writes
it as the tool's `execution_environment`. Without it the agent runs on the caller's default
warehouse, so the file's header and the report both say a caller needs one.

When the area has golden items, the agent's evaluation files (user-guide/snowflake-cortex/
cortex-agents-evaluations):

* `<area>.evaluation.sql`: a table of `input_query VARCHAR` and `ground_truth VARIANT`, registered
  with `SYSTEM$CREATE_EVALUATION_DATASET`. Each row names the tool the agent should call and, when
  the golden SQL travels, that SQL as the expected tool output, rewritten onto the view's logical
  names the way its verified queries are (as written, and reported, where that rewrite is
  refused). agami holds no answer text, so the expected answer (`ground_truth_output`, a
  plain-language rubric) is computed inside Snowflake when the file runs: the row's INSERT runs
  the golden SQL as a subquery on the physical tables and writes its result into the rubric. Only
  SQL that reads columns the view carries is run that way, never a `*`, since its result rows land
  in the table. The answer never passes through agami, and it is as old as the load. Each result
  row is quoted as JSON with its keys in order and every number rounded to `ANSWER_DECIMALS`
  places in positional notation. A golden item's `bounds` become one more rubric sentence.
* `<area>.evaluation_config.yaml`: the run configuration EXECUTE_AI_EVALUATION reads, with the four
  system metrics pinned to `v3` and two custom metrics for what agami knows (its ambiguity rules and
  its optional filters), each naming its judge model.

`<area>.analyst_evaluation_config.yaml` when the semantic view holds verified queries, golden items
or not: a Cortex Analyst evaluation of the view, named in full, which runs the verified queries it
lists by question and compares RESULTS. And `<area>.evaluation_run.sql` when either configuration
is written: the statements that stage it, start the run and read its results. Written, never run.

`--snowflake-mcp-server` adds `<area>.mcp_server.sql`: a Snowflake-managed MCP server
(sql-reference/sql/create-mcp-server) with a Cortex Analyst tool on the view and the agent as a
tool. It is off by default because it opens an endpoint on the account.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any, NamedTuple, Optional

from .export import (
    DEFAULT_FILTER_NOTE,
    MONTH_NAMES,
    ExportOptions,
    ExportQuestion,
    ExportView,
    LossEntry,
    PhysicalName,
    WriterOutput,
    degraded,
    dropped,
    join_sentences,
    parses,
    rewrite_table_refs,
    token_span,
    undeclared_reads,
)
from .export_snowflake import (
    ENGINE,
    SemanticView,
    _sql_string,
    ambiguity_handling,
    base_database,
    execution_warehouse,
    file_stem,
    needs_quotes,
    render_yaml,
    semantic_view,
    semantic_view_fqn,
    semantic_view_name,
    snowflake_ident,
    snowflake_literal,
    view_location,
)
from .models import bare_name

# "The maximum length of the specification object is 100,000 bytes." (CREATE AGENT)
SPEC_LIMIT = 100_000
TOOL_TYPE = "cortex_analyst_text_to_sql"
# The column names Snowflake's own example uses; the dataset maps them by name, so any would do.
QUERY_COLUMN, TRUTH_COLUMN = "input_query", "ground_truth"
# How much of a golden SQL's result an expected answer quotes. The rubric is read by an LLM judge
# beside the agent's reply, so it stays a few rows, not a table dump.
ANSWER_ROWS = 20
ANSWER_CHARS = 4_000
# A non-integer number in an expected answer is rounded to this many decimal places and written as
# a plain decimal: a FLOAT's own text is in scientific notation (Snowflake's default number formats,
# sql-format-models) and carries binary-fraction digits no reader needs.
ANSWER_DECIMALS = 6
# "v3": judged by claude-sonnet-4-6, or openai-gpt-5.4 where an account does not allow Anthropic
# models (cortex-agents-evaluations, "System metric versions").
METRIC_VERSION = "v3"
# A custom metric names its own judge. Without `model` Snowflake picks one for the account, and
# the docs say to "set `model` explicitly", so each custom metric names v3's default judge;
# JUDGE_ALTERNATIVE is v3's other one.
JUDGE_MODEL, JUDGE_ALTERNATIVE = "claude-sonnet-4-6", "openai-gpt-5.4"
SYSTEM_METRICS = ("answer_correctness", "tool_selection_accuracy", "tool_execution_accuracy",
                  "logical_consistency")
# The documented range semantics: min [low, high), median [low, high], max (low, high].
SCORE_RANGES = {"min_score": [1, 4], "median_score": [4, 7], "max_score": [7, 10]}
# A run name is used once per object evaluated: Snowflake refuses a second START under it
# ("Ingestion already executed for Run"). Each is the prefix and a digest of the files the run
# scores, so a changed export gets a new name, and an unchanged one the same: the export stays
# byte-identical from run to run, which a clock or a counter would break.
AGENT_RUN, ANALYST_RUN = "agami-run", "agami-analyst-run"
RUN_DIGEST = 12


def run_name(prefix: str, *texts: str) -> str:
    """`<prefix>-<12 hex>` from the texts the run scores, the same for the same texts."""
    h = hashlib.sha256()
    for text in texts:
        h.update(text.encode("utf-8") + b"\0")
    return f"{prefix}-{h.hexdigest()[:RUN_DIGEST]}"


def tool_name(view: ExportView) -> str:
    # The view's own name: an evaluation's `tool_name` matches a Cortex Analyst tool by its tool
    # name or its semantic view name, so one name serves both.
    return semantic_view_name(view)


def _parts(view: ExportView, options: ExportOptions, suffix: str) -> tuple[str, str, str]:
    loc = view_location(options)
    return loc.database, loc.schema, f"{semantic_view_name(view)}{suffix}"


def _fqn(view: ExportView, options: ExportOptions, suffix: str) -> str:
    return ".".join(snowflake_ident(p) for p in _parts(view, options, suffix))


def _stored(name: str) -> str:
    """A name as Snowflake stores it, for a string argument (`GET_AI_EVALUATION_DATA('DEMO', …)`)."""
    return name if needs_quotes(name) else name.upper()


def _stored_ident(name: str) -> str:
    """A name in a YAML field that holds an identifier: upper case when unquoted, else quoted."""
    return snowflake_ident(name) if needs_quotes(name) else name.upper()


def agent_fqn(view: ExportView, options: ExportOptions) -> str:
    # Suffixed: an agent sits in the same schema as the view it is bound to.
    return _fqn(view, options, "_agent")


def evaluation_table_fqn(view: ExportView, options: ExportOptions) -> str:
    return _fqn(view, options, "_evaluation_data")


def evaluation_dataset_fqn(view: ExportView, options: ExportOptions) -> str:
    # A dataset's name must be unique among every schema-level object, the table included.
    return _fqn(view, options, "_evaluation")


def mcp_server_fqn(view: ExportView, options: ExportOptions) -> str:
    return _fqn(view, options, "_mcp")


def add_agent_arguments(sp: argparse.ArgumentParser) -> None:
    """This module's flags; `export_snowflake.add_arguments` calls it. Suppressed unless given, as
    every target flag is, so the report records only what was asked."""
    sp.add_argument(
        "--snowflake-no-expected-answers", dest="snowflake_no_expected_answers",
        action="store_true", default=argparse.SUPPRESS,
        help="write the --target snowflake evaluation data without expected answers; by default "
        "each row's answer is computed from the golden SQL when the file runs")
    sp.add_argument(
        "--snowflake-mcp-server", dest="snowflake_mcp_server", action="store_true",
        default=argparse.SUPPRESS,
        help="also write <area>.mcp_server.sql, a Snowflake-managed MCP server exposing the "
        "semantic view and the agent as MCP tools")


# ---------------------------------------------------------------------------
# The agent specification
# ---------------------------------------------------------------------------


def _fiscal_year(view: ExportView) -> str:
    month = view.fiscal_year_start_month
    if month == 1:
        return ""
    return (f"The fiscal year starts in {MONTH_NAMES[month - 1]}: a fiscal year, quarter or "
            "half counts from that month, and a calendar one from January.")


def _filter_rule(view: ExportView) -> str:
    if not any(et.table.default_filters for et in view.tables):
        return ""
    return f"About the semantic view's default filters: {DEFAULT_FILTER_NOTE}"


def agent_spec(view: ExportView, options: ExportOptions, questions: list[str]) -> dict:
    """The specification, in CREATE AGENT's key order, with `questions` as its sample questions."""
    tool, fqn = tool_name(view), semantic_view_fqn(view, options)
    instructions: dict[str, Any] = {}
    response = "\n".join([*(ambiguity_handling(a) for a in view.ambiguities),
                          f"When results come from the {tool} tool, name the semantic view {fqn} "
                          "as their source."])
    orchestration = join_sentences(view.description, view.area_description, _fiscal_year(view),
                          _filter_rule(view))
    instructions["response"] = response
    if orchestration:
        instructions["orchestration"] = orchestration
    if questions:
        instructions["sample_questions"] = [{"question": q} for q in questions]
    spec: dict[str, Any] = {"instructions": instructions}
    spec["tools"] = [{"tool_spec": {
        "type": TOOL_TYPE, "name": tool,
        "description": join_sentences(f"Answers questions about {view.area} with SQL over the "
                                      f"semantic view {fqn}.", view.area_description)}}]
    resource: dict[str, Any] = {"semantic_view": fqn}
    warehouse = execution_warehouse(options)
    if warehouse:
        resource["execution_environment"] = {"type": "warehouse", "warehouse": warehouse}
    spec["tool_resources"] = {tool: resource}
    return spec


def _size(spec: dict) -> int:
    return len(render_yaml(spec).encode("utf-8"))


def fit_questions(view: ExportView, options: ExportOptions) -> Optional[int]:
    """How many sample questions, kept from the front of the stable order, fit the size limit; None
    when the specification is over it with none. The size only grows with each question kept, so a
    binary search finds the count in a few renders even for thousands of questions."""
    questions = [q.question for q in view.examples]
    if _size(agent_spec(view, options, [])) > SPEC_LIMIT:
        return None
    lo, hi = 0, len(questions)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(agent_spec(view, options, questions[:mid])) <= SPEC_LIMIT:
            lo = mid
        else:
            hi = mid - 1
    return lo


_NO_ANSWERS = (
    "no expected answer (ground_truth_output) is written, as --snowflake-no-expected-answers asks, "
    "except a golden item's bounds; an evaluation's answer-correctness metric cannot score the "
    "other rows, while tool selection, tool execution and logical consistency can"
)

_NO_WAREHOUSE = (
    "no warehouse is set, so the agent runs its SQL on the caller's default warehouse and does not "
    "run for a caller with none; pass --snowflake-warehouse NAME, or add the execution_environment "
    "block the file's header shows"
)


def _warehouse_note(view: ExportView, options: ExportOptions) -> str:
    warehouse = execution_warehouse(options)
    if warehouse:
        return f"-- Its SQL runs on the warehouse {warehouse}, named by --snowflake-warehouse.\n"
    # Spelt out in full, since nothing earlier in applying the files points here.
    return ("-- No warehouse is set: its SQL runs on the caller's default warehouse, and the agent\n"
            "-- does NOT run for a caller with no default warehouse. To name one, export again\n"
            "-- with --snowflake-warehouse NAME, or add these lines below the semantic_view line\n"
            f"-- of tool_resources.{tool_name(view)}, at its indentation:\n"
            "--     execution_environment:\n"
            "--       type: warehouse\n"
            "--       warehouse: <YOUR_WAREHOUSE>\n")


def agent_sql(view: ExportView, options: ExportOptions, spec_text: str) -> str:
    # COPY GRANTS: without it a replace drops every grant on the agent (CREATE AGENT, "COPY GRANTS").
    return (f"-- Snowflake Cortex Agent {agent_fqn(view, options)}, written by `sm export`. "
            "Running this creates\n-- or replaces it, keeping the grants on the agent it "
            "replaces. It is bound to the semantic view\n"
            f"-- {semantic_view_fqn(view, options)}, which {file_stem(view)}.sql creates: run "
            f"that first.\n{_warehouse_note(view, options)}-- The orchestration model and budget "
            f"are Snowflake's defaults.\n{_agent_grants(view, options)}"
            f"CREATE OR REPLACE AGENT {agent_fqn(view, options)} "
            f"COPY GRANTS\n  FROM SPECIFICATION\n  {_sql_string(spec_text)};\n")


def _agent_grants(view: ExportView, options: ExportOptions) -> str:
    """What creating and calling the agent take, as Snowflake documents them (CREATE AGENT, and
    Cortex Agents' access control)."""
    schema = ".".join(snowflake_ident(p) for p in view_location(options))
    return ("-- Privileges. The role running this needs USAGE on the database and schema and "
            f"CREATE AGENT\n-- on {schema} (and, to replace an agent another role owns, "
            "OWNERSHIP or MODIFY on it).\n"
            "-- A role that calls the agent needs the SNOWFLAKE.CORTEX_USER or "
            "SNOWFLAKE.CORTEX_AGENT_USER\n-- database role, USAGE on the agent, SELECT on the "
            f"semantic view {semantic_view_fqn(view, options)},\n-- SELECT on each table the "
            "view reads (the SQL the agent writes reads them), and USAGE on\n"
            "-- the warehouse that SQL runs on.\n")


# ---------------------------------------------------------------------------
# Expected answers
# ---------------------------------------------------------------------------


def _number(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(float(x))


def _band(low: Optional[float], high: Optional[float], what: str) -> Optional[str]:
    if low is not None and low == high:
        return f"{what} must be {_number(low)}"
    if low is not None and high is not None:
        return f"{what} must be between {_number(low)} and {_number(high)}"
    if low is not None:
        return f"{what} must be at least {_number(low)}"
    if high is not None:
        return f"{what} must be at most {_number(high)}"
    return None


def bounds_rubric(q: ExportQuestion) -> str:
    """The item's `bounds` as rubric sentences, or "" when it has none."""
    b = q.bounds
    if b is None:
        return ""
    parts = [p for p in (_band(b.min_value, b.max_value, "The value"),
                         _band(b.min_rows, b.max_rows, "The number of result rows")) if p]
    return " ".join(f"{p}." for p in parts)


class Embedded(NamedTuple):
    """The golden SQL as a subquery on the physical tables, and how its rows are listed."""

    sql: str
    order: list[str]   # ORDER BY items over its output columns; empty: by the rows' text
    order_words: str   # a key of `_ORDER_WORDS`


def embeddable_sql(view: ExportView, options: ExportOptions,
                   q: ExportQuestion) -> tuple[Optional[Embedded], str]:
    """The golden SQL as a subquery that runs on the physical tables, or None and why not.

    Its result rows are written into the evaluation table, so it is embedded only when every
    column it reads is one the view carries (`undeclared_reads`): leaving a column out of the
    model is how an author keeps its values out of agami, and a `*` reads columns it never names.
    Its result columns must have distinct names, since each row becomes one JSON object."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    text = (q.sql or "").strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    try:
        trees = [t for t in sqlglot.parse(text, dialect="snowflake", error_level=ErrorLevel.RAISE)
                 if t is not None]
    except Exception:
        return None, "it does not parse in Snowflake's dialect"
    if len(trees) != 1:
        return None, "it holds more than one statement"
    tree = trees[0]
    if not isinstance(tree, exp.Query):
        return None, "it is not a query"
    if tree.find(exp.Placeholder, exp.Parameter):
        return None, "it takes parameters"
    loc = view_location(options)
    tables = [et for et in view.tables if et.table.source_type != "sql"]
    homes = {bare_name(et.table.name): PhysicalName(base_database(et, options),
                                                    et.physical.schema or loc.schema,
                                                    et.physical.table) for et in tables}
    columns = {bare_name(et.table.name): [c.name for c in et.table.columns] for et in tables}
    mapping = {k: ".".join(snowflake_ident(p) for p in h) for k, h in homes.items()}
    rewritten = rewrite_table_refs(text, mapping, ENGINE, homes)
    if rewritten is None:
        return None, ("a table it reads cannot be placed on a physical table for certain (one "
                      "defined by a query, one this view lacks, or one in another schema)")
    why = undeclared_reads(text, columns, ENGINE, homes)
    if why is not None:
        return None, f"its result would hold values the semantic model does not carry: {why}"
    names = [n.casefold() for n in tree.named_selects]
    # Snowflake names an unaliased expression after its text, so two of them can share a name:
    # only a sole result column may go unnamed.
    if len(names) != len(set(names)) or (len(names) > 1 and not all(names)):
        return None, "two of its result columns share a name, or one has none: alias each"
    order, words = result_order(tree, text)
    return Embedded(rewritten, order, words), ""


_LEAD = ("The correct answer is the result of the SQL confirmed for this question in agami, "
         "computed from this account's data when this evaluation table was loaded.")
_HOW = ("Values that match after rounding are correct, and the row order matters only when the "
        "question asks for one.")


# What the rubric says the rows it quotes are, by how `result_order` lists them.
_ORDER_WORDS = {"query": "the first {n} in the order its ORDER BY gives",
                "columns": "the first {n} ordered by its columns, left to right",
                "text": "the first {n} by their text"}


def answer_expression(tail: str, order: str = "text") -> str:
    """The rubric, as a Snowflake expression over `agami_result`: `n`, the golden SQL's row count,
    and `r`, the JSON text of at most `ANSWER_ROWS` of its rows, listed as `order` says."""
    which = _ORDER_WORDS[order].format(n=ANSWER_ROWS)
    return (f"{snowflake_literal(_LEAD + ' ')}\n    || CASE WHEN agami_result.n = 0 THEN 'It "
            "returns no "
            "rows, so a correct response says that no data matches.'\n"
            f"       ELSE 'It returns ' || agami_result.n || IFF(agami_result.n > {ANSWER_ROWS}, "
            f"' rows; {which}, are: ', ' row(s): ')\n         || LEFT(agami_result.r, "
            f"{ANSWER_CHARS}) || "
            f"IFF(LEN(agami_result.r) > {ANSWER_CHARS}, ' (cut at {ANSWER_CHARS:,} characters)', "
            f"'') || '.' END\n    || {snowflake_literal(' ' + join_sentences(_HOW, tail))}")


def _output_ident(item: Any) -> Any:
    """The identifier naming one output column of a query, or None when it has none to reach."""
    from sqlglot import exp

    if isinstance(item, exp.Alias):
        return item.args.get("alias")
    if isinstance(item, exp.Column) and isinstance(item.this, exp.Identifier):
        return item.this
    return None


def _order_key(ordered: Any, items: list, refs: list[str]) -> Optional[str]:
    """One ORDER BY item of the golden SQL as a key over its output columns, or None."""
    from sqlglot import exp

    key, pos = ordered.this, None
    if isinstance(key, exp.Literal) and not key.is_string and key.this.isdigit():
        pos = int(key.this) - 1
    else:
        for i, item in enumerate(items):
            inner = item.this if isinstance(item, exp.Alias) else item
            named = isinstance(key, exp.Column) and not key.table and \
                _output_ident(item) is not None and key.name.casefold() == item.alias_or_name.casefold()
            if key == inner or named:
                pos = i
                break
    if pos is None or not 0 <= pos < len(refs):
        return None
    return refs[pos] + (" DESC" if ordered.args.get("desc") else "")


def result_order(tree: Any, sql: str) -> tuple[list[str], str]:
    """How to list the golden SQL's result rows from outside it: ORDER BY items over its output
    columns, and which order that is (`_ORDER_WORDS`). Its own ORDER BY when every key is an
    output column (by name, expression or position), then each output column in turn, so a
    number or date sorts as a value (2 before 10), not as text; none when an output column has no
    name to reach it by, and the rows are then listed by their text."""

    items = list(tree.selects)
    idents = [_output_ident(i) for i in items]
    if not items or any(i is None for i in idents):
        return [], "text"
    # Each name as the golden SQL spells it, quotes included, cut from its text by token position:
    # SQL is never regenerated from a parsed tree (test_ace093_byte_identity).
    refs = [f"agami_golden.{sql[slice(*token_span(i))]}" for i in idents]
    order = tree.args.get("order")
    keys = [_order_key(o, items, refs) for o in (order.expressions if order else [])]
    own = [k for k in keys if k] if keys and all(keys) else []
    rest = [r for r in refs if not any(k.split(" DESC")[0] == r for k in own)]
    return own + rest, "query" if own else "columns"


# ---------------------------------------------------------------------------
# The evaluation data
# ---------------------------------------------------------------------------


def ground_truth(view: ExportView, q: ExportQuestion,
                 built: SemanticView) -> tuple[dict, Optional[LossEntry]]:
    """One row's static ground truth: the expected Cortex Analyst call, with the golden SQL as its
    expected output when that SQL travels and parses. `tool_input` is the question the call should
    answer. The tool writes SQL on the view's logical names, so the expected output is the golden
    SQL rewritten onto them the way the view's verified queries are; where that rewrite is refused,
    the golden SQL as written, reported. When the view holds a verified query asking the same
    question, its SQL is the expected output, so the view and the evaluation agree."""
    call = {"tool_name": tool_name(view), "tool_input": q.question}
    loss = None
    verified = built.verified_sql(q.question)
    if q.sql is not None and verified is not None:
        # The view already says what SQL answers this question; the row expects the same.
        call["tool_output"] = verified
    elif q.sql is not None and parses(q.sql, ENGINE):
        sql, reason, why = built.logical_sql(q.sql)
        call["tool_output"] = q.sql if sql is None else sql
        if sql is None:
            loss = degraded(view.area, q.kind, q.name, reason or "target_rule", "its expected tool "
                            "output (tool_output) keeps the golden SQL's physical table and column "
                            f"names, not the semantic view's logical ones: {why}")
    elif q.sql is not None:
        loss = degraded(view.area, q.kind, q.name, "unparseable_for_target", "carried without its "
                        "SQL: it does not parse in Snowflake's dialect")
    return {"ground_truth_invocations": [call]}, loss


# One value of a result row (`f.value`, a VARIANT) as JSON text. A number becomes NUMBER(38,
# ANSWER_DECIMALS), rounded, printed in positional notation with that many decimals (the default
# format of a fixed-point number, sql-format-models) and its trailing zeros and point dropped; any
# other value, and a number whose integer part that type cannot hold, keeps its TO_JSON text. The
# number test and its magnitude use only functions that return NULL rather than fail (IS_DOUBLE,
# AS_DOUBLE, AS_DECIMAL), and TO_DECIMAL sees only a number in range or NULL, whatever order the
# engine evaluates the branches in.
# NaN and the infinities are FLOAT values JSON has no number for, so each is quoted as a string
# ("NaN", "inf", "-inf"); left to TO_JSON they would make the row's text invalid JSON.
_FIT = f"1e{38 - ANSWER_DECIMALS}"
_VALUE_TEXT = (
    "CASE WHEN IS_DOUBLE(f.value) AND (AS_DOUBLE(f.value) = 'NaN'::FLOAT\n"
    "                                  OR ABS(AS_DOUBLE(f.value)) = 'inf'::FLOAT)\n"
    "                               THEN TO_JSON(TO_VARIANT(TO_VARCHAR(AS_DOUBLE(f.value))))\n"
    "                               ELSE "
    "COALESCE(RTRIM(RTRIM(TO_VARCHAR(TO_DECIMAL(IFF(IS_DOUBLE(f.value)\n"
    "                               AND ABS(COALESCE(AS_DOUBLE(f.value),"
    f" AS_DECIMAL(f.value, 38, 0))) < {_FIT},\n"
    f"                               f.value, NULL), 38, {ANSWER_DECIMALS})), '0'), '.'),\n"
    "                               TO_JSON(f.value)) END")


class _Row:
    """One evaluation row: its question, static ground truth, and, when its answer is computed at
    load time, the subquery that computes it."""

    def __init__(self, question: str, truth: dict, embedded: Optional[Embedded], tail: str) -> None:
        self.question, self.truth, self.embedded, self.tail = question, truth, embedded, tail

    @property
    def subquery(self) -> Optional[str]:
        return self.embedded.sql if self.embedded else None

    @property
    def has_answer(self) -> bool:
        return self.subquery is not None or "ground_truth_output" in self.truth


def _insert(table: str, row: _Row) -> str:
    head = (f"INSERT INTO {table} ({QUERY_COLUMN}, {TRUTH_COLUMN})\n"
            f"  SELECT {snowflake_literal(row.question)}, ")
    text = snowflake_literal(json.dumps(row.truth, ensure_ascii=False))
    if row.embedded is None:
        return f"{head}PARSE_JSON({text});"
    sub = row.embedded
    # Each row's place in the golden SQL's own order (or its columns' order), numbered before
    # the row becomes text, since text sorts 10 before 2. Ties fall back to the text, so the same
    # data always gives the same answer text.
    rank = (f"ROW_NUMBER() OVER (ORDER BY {', '.join(sub.order)})" if sub.order else "0")
    # The golden SQL stands on lines of its own, so a trailing line comment ends before the `)`.
    # Each result row becomes one JSON text, its keys in order (TO_JSON leaves an object's key
    # order unspecified); only the first rows are kept before aggregating, so a large result
    # builds no oversized array.
    return (f"{head}TO_VARIANT(OBJECT_INSERT(TO_OBJECT(PARSE_JSON({text})), 'ground_truth_output',\n"
            f"    {answer_expression(row.tail, sub.order_words)}))\n"
            "  FROM (SELECT COALESCE(MAX(n), 0) AS n,\n"
            "          '[' || ARRAY_TO_STRING(ARRAY_AGG(j) WITHIN GROUP (ORDER BY k, j), ', ') || ']'"
            " AS r\n"
            "        FROM (SELECT j, k, COUNT(*) OVER () AS n\n"
            "              FROM (SELECT '{' || LISTAGG(TO_JSON(TO_VARIANT(f.key)) || ':' ||\n"
            f"                             {_VALUE_TEXT}, ',')\n"
            "                             WITHIN GROUP (ORDER BY f.key) || '}' AS j,\n"
            "                           MIN(agami_object.k) AS k\n"
            f"                    FROM (SELECT OBJECT_CONSTRUCT_KEEP_NULL(*) AS o, {rank} AS k\n"
            f"                    FROM (\n{sub.sql}\n"
            "                    ) AS agami_golden) AS agami_object,\n"
            "                    LATERAL FLATTEN(INPUT => agami_object.o) AS f\n"
            "                    GROUP BY f.seq) AS agami_json\n"
            f"              QUALIFY ROW_NUMBER() OVER (ORDER BY k, j) <= {ANSWER_ROWS}) AS agami_rows"
            ") AS agami_result;")


def evaluation_sql(view: ExportView, options: ExportOptions, rows: list[_Row]) -> str:
    table, dataset = evaluation_table_fqn(view, options), evaluation_dataset_fqn(view, options)
    computed = sum(r.subquery is not None for r in rows)
    lines = [
        f"-- Cortex Agent evaluation data for {agent_fqn(view, options)}, written by `sm export` "
        "from the\n-- golden items confirmed in agami. Running this replaces the table and "
        f"registers it as the dataset\n-- {dataset}, which "
        f"{file_stem(view)}.evaluation_config.yaml names.\n"
        "-- A dataset name registers once, and Snowflake documents no statement that drops a "
        "dataset:\n-- to register again, give the dataset a new name in three places: here, in "
        f"that file, and in\n-- the copy of that file {file_stem(view)}.evaluation_run.sql "
        "stages (its dataset_name line).\n"
        f"-- Expected answers: {computed} of {len(rows)} rows compute theirs from the golden SQL "
        "when this file\n-- runs, from this account's data. The answer goes stale when the data "
        "changes, and the dataset\n-- keeps the answer it was registered with: run this again, "
        "under a new dataset name, to refresh it.\n"
        "-- Privileges. The role running this needs USAGE on the database and schema, CREATE "
        f"TABLE and\n-- CREATE DATASET on {'.'.join(snowflake_ident(p) for p in view_location(options))}"
        ", and SELECT on each table a golden query reads.",
        f"CREATE OR REPLACE TABLE {table} (\n  {QUERY_COLUMN} VARCHAR,\n  {TRUTH_COLUMN} VARIANT\n);",
    ]
    lines += [_insert(table, r) for r in rows]
    lines.append("CALL SYSTEM$CREATE_EVALUATION_DATASET(\n  'Cortex Agent',\n"
                 f"  {snowflake_literal(table)},\n  {snowflake_literal(dataset)},\n"
                 f"  OBJECT_CONSTRUCT('query_text', '{QUERY_COLUMN.upper()}', "
                 f"'expected_tools', '{TRUTH_COLUMN.upper()}')\n);")
    return "\n".join(lines) + "\n"


def _rows(view: ExportView, options: ExportOptions,
          built: SemanticView) -> tuple[list[_Row], list[LossEntry], dict]:
    compute = not options.target_args.get("snowflake_no_expected_answers")
    rows, losses, carried = [], [], {}
    for q in view.golden:
        truth, loss = ground_truth(view, q, built)
        losses += [loss] if loss else []
        tail = bounds_rubric(q)
        if tail:
            carried[q.name] = frozenset({"bounds"})
        sub: Optional[Embedded] = None
        why = ""
        if compute and "tool_output" in truth["ground_truth_invocations"][0]:
            sub, why = embeddable_sql(view, options, q)
        elif compute:
            why = ("its SQL was written for another engine, or never written" if q.sql is None
                   else "its SQL does not parse in Snowflake's dialect")
        if sub is None and tail:
            truth = {"ground_truth_output": tail, **truth}
        if sub is None and compute:
            losses.append(degraded(view.area, q.kind, q.name, "detail_lost", "no expected answer "
                                   f"(ground_truth_output) is computed{' beyond its bounds' if tail else ''}:"
                                   f" {why}"))
        rows.append(_Row(q.question, truth, sub, tail))
    if not compute:
        losses.append(degraded(view.area, "evaluation", view.area, "detail_lost", _NO_ANSWERS))
    return rows, losses, carried


# ---------------------------------------------------------------------------
# Evaluation run configurations
# ---------------------------------------------------------------------------


def inert(text: str) -> str:
    """Model text as it may stand in a custom metric's prompt: a judge's prompt reads `{{name}}` as
    a placeholder it fills in, so each `{{` and `}}` of the model's own is spaced apart (`{ {`)."""
    return re.sub(r"\}(?=\})", "} ", re.sub(r"\{(?=\{)", "{ ", text))


def _ambiguity_metric(view: ExportView) -> Optional[dict]:
    if not view.ambiguities:
        return None
    rules = "\n".join(f"- {inert(ambiguity_handling(a))}" for a in view.ambiguities)
    return {"name": "agami_ambiguity_handling", "model": JUDGE_MODEL, "score_ranges": SCORE_RANGES,
            "prompt": (
        "Score from 1 to 10 how the agent handled a question that can mean more than one thing, "
        "by these rules, declared in agami:\n"
        f"{rules}\n\nThe user's question: {{{{input}}}}\nThe agent's answer: {{{{output}}}}\n\n"
        "10 = no rule applies and the agent answered directly, or a rule applies and the agent did "
        "what it says: asked which option is meant before running any SQL, or answered with the "
        "named option, said so and offered the others.\n"
        "5 = a rule applies and the agent did only part of what it says.\n"
        "1 = a rule applies and the agent ignored it, or no rule applies and the agent asked a "
        "question it did not need to.\n")}


def _filter_metric(view: ExportView) -> Optional[dict]:
    filters = [inert(f"- {bare_name(et.table.name)}: "
                     f"{f.replace('{alias}', bare_name(et.table.name))}")
               for et in view.tables for f in et.table.default_filters]
    if not filters:
        return None
    return {"name": "agami_optional_filters", "model": JUDGE_MODEL, "score_ranges": SCORE_RANGES,
            "prompt": (
        "Score from 1 to 10 whether the agent kept to the rule for these optional filters, declared "
        "in agami as default filters:\n" + "\n".join(filters) + f"\n\n{DEFAULT_FILTER_NOTE}\n\n"
        "The user's question: {{input}}\nThe tools the agent called, with their SQL: "
        "{{tool_info}}\n\n"
        "10 = the SQL applies none of these filters, or only ones the question asks for.\n"
        "1 = the SQL applies one of these filters that the question does not ask for.\n")}


def run_config(view: ExportView, options: ExportOptions, rows: list[_Row]) -> dict:
    """The Agent Evaluation YAML ("Agent Evaluation YAML specification"): no `dataset:` block, since
    the evaluation file registers it, and no `agent_version`, so the run scores the agent as it stands when the run starts."""
    db, schema, name = _parts(view, options, "_agent")
    metrics: list[Any] = [{"name": m, "version": METRIC_VERSION} for m in SYSTEM_METRICS
                          if m != "answer_correctness" or any(r.has_answer for r in rows)]
    metrics += [m for m in (_ambiguity_metric(view), _filter_metric(view)) if m]
    return {
        "evaluation": {
            "agent_params": {"agent_name": ".".join(_stored_ident(p) for p in (db, schema, name)),
                             "agent_type": "CORTEX AGENT"},
            "run_params": {"label": f"agami golden items: {view.area}",
                           "description": f"{len(rows)} golden items exported by sm export"},
            "source_metadata": {"type": "dataset", "dataset_name": _stored_ident(
                _parts(view, options, "_evaluation")[2])}},
        "metrics": metrics}


def analyst_config(view: ExportView, options: ExportOptions, questions: list[str]) -> dict:
    """A Cortex Analyst evaluation (user-guide/snowflake-cortex/cortex-analyst-evaluations): it
    runs the semantic view's verified queries and compares the RESULTS of the SQL generated for
    them. The view is named in full, as the agent's configuration names the agent. The source
    lists the verified queries by their `question` text, the form the documented
    `verified_queries` list takes, so the file says what it scores and a person can trim it."""
    db, schema, name = _parts(view, options, "")
    return {"evaluation": {"analyst_params": {"analyst_name": ".".join(
                               _stored_ident(p) for p in (db, schema, name)),
                                              "analyst_type": "SEMANTIC VIEW"},
                           "source_metadata": {"type": "verified_queries",
                                               "verified_queries": list(dict.fromkeys(questions))}},
            "metrics": [{"name": "sql_correctness", "version": METRIC_VERSION}]}


def _config_yaml(head: str, doc: dict) -> str:
    return head + render_yaml(doc)


def _agent_config_text(view: ExportView, options: ExportOptions, rows: list[_Row]) -> str:
    stem = file_stem(view)
    return _config_yaml(
        f"# Cortex Agent evaluation run configuration for {agent_fqn(view, options)}, written by\n"
        f"# `sm export`. {stem}.evaluation_run.sql stages it and starts a run. Run it after "
        f"{stem}.evaluation.sql,\n# which registers the dataset it names. System metrics are "
        f"pinned to {METRIC_VERSION}. Each custom metric names its judge:\n# {JUDGE_MODEL}, the "
        f"default judge of {METRIC_VERSION}. An account that does not allow Anthropic models "
        f"changes\n# each `model:` line to {JUDGE_ALTERNATIVE}, {METRIC_VERSION}'s other judge; a "
        "model the account cannot use\n# fails the run when it starts.\n",
        run_config(view, options, rows))


def _analyst_config_text(view: ExportView, options: ExportOptions, questions: list[str]) -> str:
    return _config_yaml(
        f"# Cortex Analyst evaluation run configuration for {semantic_view_fqn(view, options)}, "
        "written by\n# `sm export`. It scores the view's verified queries (agami's confirmed "
        "examples with SQL) by\n# running the SQL Cortex Analyst generates and comparing results. "
        "verified_queries lists them\n# by question; remove a line to leave that query out of the "
        "run.\n", analyst_config(view, options, questions))


def _stage_statements(stage: str, path: str, text: str) -> str:
    return (f"COPY INTO @{stage}/{path}\n  FROM (SELECT {_sql_string(text)})\n"
            "  FILE_FORMAT = (TYPE = 'CSV' COMPRESSION = NONE FIELD_DELIMITER = NONE "
            "FIELD_OPTIONALLY_ENCLOSED_BY = NONE ESCAPE_UNENCLOSED_FIELD = NONE)\n"
            "  SINGLE = TRUE HEADER = FALSE OVERWRITE = TRUE;")


def _run_calls(stage: str, path: str, run: str, read: str) -> list[str]:
    target = snowflake_literal(f"@{stage}/{path}")
    call = "CALL EXECUTE_AI_EVALUATION('{}', OBJECT_CONSTRUCT('run_name', {}), {});"
    return [call.format("START", snowflake_literal(run), target),
            "-- Repeat until STATUS reads COMPLETED:",
            call.format("STATUS", snowflake_literal(run), target), read]


def run_sql(view: ExportView, options: ExportOptions, agent_text: Optional[str],
            analyst_text: Optional[str], runs: tuple[str, str] = (AGENT_RUN, ANALYST_RUN)) -> str:
    """The statements that stage each run configuration written and start its run: the agent's
    when the area has golden items, the semantic view's when the view holds verified queries.
    `runs` names the agent's run and the view's (`run_name`)."""
    agent_run, analyst_run = runs
    stem = file_stem(view)
    loc = view_location(options)
    schema = ".".join(snowflake_ident(p) for p in loc)
    stage = _fqn(view, options, "_evaluation_config")
    db, sch = snowflake_literal(_stored(loc.database)), snowflake_literal(_stored(loc.schema))
    first = [f"{stem}.sql"] + ([f"{stem}.agent.sql", f"{stem}.evaluation.sql"] if agent_text else [])
    out = [
        f"-- Evaluation runs for {view.area}, written by `sm export`: written, never run.\n"
        f"-- Run them after {', '.join(first)}. Each section names the privileges its run needs.\n"
        "-- Every name here is fully qualified. The USE SCHEMA statement is there because an\n"
        "-- evaluation started with SQL runs in the session's database and schema (Cortex Agent\n"
        "-- evaluations), so it sets that to the view's schema.\n"
        "-- Privileges for every run: USAGE on the database and schema, CREATE STAGE, CREATE TASK "
        "and\n-- CREATE FILE FORMAT on the schema, EXECUTE TASK ON ACCOUNT, and the "
        "SNOWFLAKE.CORTEX_USER\n-- database role. A run executes as a task, and a task ignores "
        "secondary roles: every privilege\n-- an evaluation needs must be held by one role, the "
        "session's primary role (USE ROLE).\n"
        "-- A run name can be used once per object evaluated; Snowflake refuses a second START "
        "under\n-- it. Each name below ends in a digest of the files the run scores, so an export "
        "that changed\n-- gets new names. To run the same files again, change the run name in "
        "its START, STATUS and\n-- results statements, all three alike. The COPY INTO statements "
        "stage the files beside this\n-- one; PUT them instead from a client if you edit them.",
        f"USE SCHEMA {schema};",
        f"CREATE STAGE IF NOT EXISTS {stage}\n  FILE_FORMAT = (TYPE = 'CSV' FIELD_DELIMITER = NONE "
        "RECORD_DELIMITER = '\\n' SKIP_HEADER = 0\n  FIELD_OPTIONALLY_ENCLOSED_BY = NONE "
        "ESCAPE_UNENCLOSED_FIELD = NONE);",
    ]
    if agent_text is not None:
        agent = snowflake_literal(_stored(_parts(view, options, "_agent")[2]))
        out += [
            f"-- The agent {agent_fqn(view, options)}: {stem}.evaluation_config.yaml. Needs a\n"
            "-- SNOWFLAKE.CORTEX_USER or CORTEX_AGENT_USER database role, USAGE on the agent, and "
            "MONITOR on\n-- the agent to read its results (without MONITOR the results query "
            "returns no rows). The run\n-- reads the dataset the configuration names; "
            f"{stem}.evaluation.sql says how to rename it.",
            _stage_statements(stage, f"{stem}.evaluation_config.yaml", agent_text),
            *_run_calls(stage, f"{stem}.evaluation_config.yaml", agent_run,
                        f"SELECT * FROM TABLE(SNOWFLAKE.LOCAL.GET_AI_EVALUATION_DATA(\n  {db}, {sch}, "
                        f"{agent}, 'CORTEX AGENT', {snowflake_literal(agent_run)}));"),
        ]
    if analyst_text is not None:
        view_name = snowflake_literal(_stored(semantic_view_name(view)))
        out += [
            f"-- The semantic view, scored by result: {stem}.analyst_evaluation_config.yaml. "
            "Needs SELECT on\n-- the view and the tables it reads, CREATE DATASET on this schema, "
            "and MONITOR on the view\n-- to read its results.",
            _stage_statements(stage, f"{stem}.analyst_evaluation_config.yaml", analyst_text),
            *_run_calls(stage, f"{stem}.analyst_evaluation_config.yaml", analyst_run,
                        "SELECT * FROM TABLE(SNOWFLAKE.LOCAL.GET_ANALYST_AI_EVALUATION_DATA(\n"
                        f"  {db}, {sch}, {view_name}, 'SEMANTIC VIEW', "
                        f"{snowflake_literal(analyst_run)}));"),
        ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# The MCP server
# ---------------------------------------------------------------------------


def mcp_spec(view: ExportView, options: ExportOptions, with_agent: bool) -> dict:
    """The server's specification (CREATE MCP SERVER): a Cortex Analyst tool on the view and, when
    the agent file is written, the agent as a tool. Both only read data, which the annotations say;
    they are hints to a client, and the grants are what control access."""
    name = semantic_view_name(view).replace("_", "-").strip("-")
    tools: list[dict] = [{
        "name": f"{name}-analyst", "type": "CORTEX_ANALYST_MESSAGE",
        "identifier": semantic_view_fqn(view, options),
        "title": f"{view.area} semantic view",
        "description": join_sentences(f"Generates SQL over the semantic view for {view.area}.",
                                      view.area_description)}]
    if with_agent:
        tools.append({
            "name": f"{name}-agent", "type": "CORTEX_AGENT_RUN",
            "identifier": agent_fqn(view, options), "title": f"{view.area} agent",
            "description": join_sentences(
                f"Answers questions about {view.area} from the semantic view.",
                view.area_description),
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "openWorldHint": False}})
    return {"tools": tools}


def mcp_sql(view: ExportView, options: ExportOptions, with_agent: bool) -> str:
    loc = view_location(options)
    server = mcp_server_fqn(view, options)
    path = "/".join(("databases", _stored(loc.database), "schemas", _stored(loc.schema),
                     "mcp-servers", _stored(_parts(view, options, "_mcp")[2])))
    agent = (f"\n-- GRANT USAGE ON AGENT {agent_fqn(view, options)} TO ROLE <mcp_access_role>;"
             if with_agent else "")
    schema = ".".join(snowflake_ident(p) for p in loc)
    return (f"-- Snowflake-managed MCP server {server}, written by `sm export`. Running this "
            "creates or\n-- replaces it; CREATE MCP SERVER keeps no grants, so grant USAGE on it "
            "again after a replace.\n-- Run "
            f"{file_stem(view)}.sql{' and the agent file' if with_agent else ''} first.\n"
            "-- Privileges. The role running this needs USAGE on the database and schema and "
            f"CREATE MCP SERVER\n-- on {schema} (and OWNERSHIP of the server to replace one "
            "that exists).\n"
            f"-- An MCP client connects to https://<account_url>/api/v2/{path}\n"
            "-- and signs in with Snowflake OAuth (or External OAuth, or a programmatic access "
            "token);\n-- setting that up is yours. The role it connects with needs:\n"
            f"-- GRANT USAGE ON MCP SERVER {server} TO ROLE <mcp_access_role>;\n"
            f"-- GRANT SELECT ON SEMANTIC VIEW {semantic_view_fqn(view, options)} TO ROLE "
            f"<mcp_access_role>;{agent}\n"
            f"CREATE OR REPLACE MCP SERVER {server}\n  FROM SPECIFICATION "
            f"{_sql_string(render_yaml(mcp_spec(view, options, with_agent)))};\n")


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------


def _agent_file(view: ExportView, options: ExportOptions) -> tuple[Optional[str], list[LossEntry]]:
    kept = fit_questions(view, options)
    over = view.examples[kept or 0:]
    losses = [dropped(view.area, q.kind, q.name, "target_limit", "a sample question past the "
                      f"agent specification's {SPEC_LIMIT:,}-byte limit") for q in over]
    if kept is None:
        losses.append(dropped(view.area, "agent", view.area, "target_limit", "its instructions "
                              f"alone pass the {SPEC_LIMIT:,}-byte specification limit"))
        return None, losses
    spec = agent_spec(view, options, [q.question for q in view.examples[:kept]])
    if execution_warehouse(options) is None:
        losses.append(degraded(view.area, "agent", view.area, "target_rule", _NO_WAREHOUSE))
    return agent_sql(view, options, render_yaml(spec)), losses


def write_agent(view: ExportView, options: ExportOptions,
                built: Optional[SemanticView] = None) -> WriterOutput:
    """The agent file; the agent's evaluation files when the view has eligible golden items; the
    semantic view's evaluation configuration when the view it is bound to (`built`, built here when
    not given) holds verified queries, which that evaluation scores; the run file when either is
    written; the MCP server file when `--snowflake-mcp-server` asks for it."""
    built = semantic_view(view, options) if built is None else built
    stem = file_stem(view)
    files: dict[str, bytes] = {}
    text, losses = _agent_file(view, options)
    if text is not None:
        files[f"{stem}.agent.sql"] = text.encode("utf-8")
    rows, row_losses, carried = _rows(view, options, built)
    losses += row_losses
    agent_cfg = _agent_config_text(view, options, rows) if rows else None
    questions = built.verified_questions()
    analyst_cfg = _analyst_config_text(view, options, questions) if questions else None
    if agent_cfg is not None:
        files[f"{stem}.evaluation.sql"] = evaluation_sql(view, options, rows).encode("utf-8")
        files[f"{stem}.evaluation_config.yaml"] = agent_cfg.encode("utf-8")
    if analyst_cfg is not None:
        files[f"{stem}.analyst_evaluation_config.yaml"] = analyst_cfg.encode("utf-8")
    if agent_cfg is not None or analyst_cfg is not None:
        # Each run is named for what it scores: the agent, its data and its configuration; the
        # view and its configuration.
        runs = (run_name(AGENT_RUN, text or "", files.get(f"{stem}.evaluation.sql", b"").decode(),
                         agent_cfg or ""),
                run_name(ANALYST_RUN, render_yaml(built.doc or {}), analyst_cfg or ""))
        files[f"{stem}.evaluation_run.sql"] = run_sql(view, options, agent_cfg, analyst_cfg,
                                                      runs).encode("utf-8")
    if options.target_args.get("snowflake_mcp_server"):
        files[f"{stem}.mcp_server.sql"] = mcp_sql(view, options, text is not None).encode("utf-8")
    return WriterOutput(files, tuple(losses), carried)
