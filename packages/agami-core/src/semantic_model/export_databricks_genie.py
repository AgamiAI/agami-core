"""`databricks` target, Genie space (F19 / ACE-171): one Genie space file per subject area.

A Genie space is a JSON document Databricks calls the `serialized_space`, at version 2: the tables
and metric views Genie may query, column descriptions, joins, reusable filters and measures, one
block of text instructions, example questions with SQL, and benchmark questions. Each subject area
is written as `<area>.geniespace.json` beside the bundle resource `resources/<area>.genie.yml`
that deploys it with the Databricks CLI.

Databricks rejects a space that breaks its documented rules rather than repairing it, so they hold
here, at export: ids 32 lowercase hex characters (all from `ExportView.id`), arrays pre-sorted,
question and instruction ids unique, text as lists of strings within the length limit that join
back to the text with each string but the last ending a line, and the
documented limits on data sources, instructions, knowledge-store snippets and benchmarks. Items past
a limit are dropped from the end of their stable order and reported as `target_limit`.

Every base table in example and benchmark SQL is written by the identifier the space lists it
under, spliced by token position, so the SQL does not depend on a default catalog or schema. SQL
that cannot be rewritten for certain, such as SQL naming another schema's table of the same name,
is never written unqualified or re-pointed.
Filter and measure snippets keep the documented `table.column` form: Genie places them itself.

A sensitive column is left out of the file, but the space lists the whole table that holds it.
`--genie-hide-sensitive` lists each such column with `exclude: true`, which marks it hidden in the
space; that is the one place an export writes a sensitive column's name, and without the flag the
report counts those columns. Hiding is not access control: it does not prevent a query that names
the column, and restricting who can read a column is done with a Unity Catalog column mask.

The metric views come from ACE-170's plan and naming rule, so the space lists each one by the
identifier its `.sql` file creates. Reads only the `ExportView`, like every writer.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import yaml

from . import export_databricks as DB
from .export import (
    DEFAULT_FILTER_NOTE,
    MONTH_NAMES,
    ExportOptions,
    ExportQuestion,
    ExportTable,
    ExportView,
    LossEntry,
    PhysicalName,
    WriterOutput,
    degraded,
    dropped,
    parses,
    rewrite_table_refs,
)
from .models import bare_name

VERSION = 2
RESOURCE_DIR = "resources"
WAREHOUSE_VAR = "genie_warehouse_id"
MAX_STRING = 25_000          # characters in one string of a text field
MAX_ITEMS = 10_000           # items in one array
MAX_DATA_SOURCES = 50        # tables, views and metric views together
MAX_INSTRUCTIONS = 100       # the text block, each example SQL and each SQL function count one
MAX_SNIPPETS = 200           # table descriptions, join specs, filters and measures together
MAX_BENCHMARKS = 500
MAX_ENTITY_MATCHING = 120    # columns with entity matching in one space
HIDE_SENSITIVE = "genie_hide_sensitive"
_CARDINALITY = {"many_to_one": "MANY_TO_ONE", "one_to_many": "ONE_TO_MANY", "one_to_one": "ONE_TO_ONE"}
_BEHAVIOUR = {"ask": "ask the user which they mean before running SQL",
              "answer_and_offer": "answer with the default, say which one was used, and offer the "
                                  "others"}


def space_path(area: str) -> str:
    return f"{DB.databricks_name(area)}.geniespace.json"


def resource_path(area: str) -> str:
    return f"{RESOURCE_DIR}/{DB.databricks_name(area)}.genie.yml"


# ---------------------------------------------------------------------------
# Genie's text and SQL rules
# ---------------------------------------------------------------------------


def text_field(text: str) -> list[str]:
    """`text` as Genie stores it: a list of strings, each within the length limit, that join back
    to `text` exactly. It is split after each line end, so every string but the last ends a line.
    Only a line over the length limit is cut elsewhere."""
    out: list[str] = []
    for line in re.findall(r"[^\n]*\n|[^\n]+", text):
        out += [line[i:i + MAX_STRING] for i in range(0, len(line), MAX_STRING)]
    return out


def lines_field(parts: Iterable[str]) -> list[str]:
    """Separate sentences or notes as one text field, one per line."""
    return text_field("\n".join(parts))


def _tables_read(sql: str) -> set[str]:
    """The casefolded bare names of the tables `sql` reads, CTE names aside; empty when it does
    not parse. It only picks the reason a rewrite failed, so a miss costs a reason code, not a leak."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    try:
        tree = sqlglot.parse_one(sql, dialect=DB.DIALECT, error_level=ErrorLevel.RAISE)
    except Exception:
        return set()
    ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
    return {t.name.casefold() for t in tree.find_all(exp.Table)} - ctes


def _labels(choices: Optional[dict[str, str]]) -> list[str]:
    # A label that only repeats its code, or travels blank, tells Genie nothing.
    return [f"{k} = {v}" for k, v in (choices or {}).items()
            if v.strip() and v.casefold() != k.casefold()]


def _identifier(database: Optional[str], schema: Optional[str], table: str) -> Optional[str]:
    # Genie names every data source catalog.schema.table.
    return DB._dotted(database, schema, table) if database and schema else None


def _unique(names: list[str]) -> list[str]:
    seen, out = set(), []
    for n in names:
        if n.strip() and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Building one space
# ---------------------------------------------------------------------------


@dataclass
class _Space:
    view: ExportView
    options: ExportOptions
    losses: list[LossEntry] = field(default_factory=list)
    in_space: set[str] = field(default_factory=set)    # casefolded bare names of tables listed
    # Bare table name -> the identifier the space lists it by, for qualifying carried SQL.
    refs: dict[str, str] = field(default_factory=dict)
    # Bare table name -> where it lives: a schema or catalog that carried SQL names must be this.
    homes: dict[str, PhysicalName] = field(default_factory=dict)
    listed: list[ExportTable] = field(default_factory=list)
    snippets_left: int = field(init=False, default=0)
    matching_left: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.snippets_left = MAX_SNIPPETS
        self.matching_left = MAX_ENTITY_MATCHING

    @property
    def hide_sensitive(self) -> bool:
        return bool(self.options.target_args.get(HIDE_SENSITIVE))

    def drop(self, kind: str, name: str, reason: Any, detail: str) -> None:
        self.losses.append(dropped(self.view.area, kind, name, reason, detail))

    def degrade(self, kind: str, name: str, reason: Any, detail: str) -> None:
        self.losses.append(degraded(self.view.area, kind, name, reason, detail))

    def take_snippet(self) -> bool:
        """One knowledge-store snippet from the space's budget; False once it is spent."""
        if self.snippets_left <= 0:
            return False
        self.snippets_left -= 1
        return True

    def capped(self, items: list[tuple[str, str, Any]], limit: int, what: str) -> list[Any]:
        """The first `limit` of (kind, name, item), in their stable order; the rest reported."""
        for kind, name, _ in items[limit:]:
            self.drop(kind, name, "target_limit", f"a Genie space holds at most {limit} {what}")
        return [item for _, _, item in items[:limit]]

    # --- data sources -------------------------------------------------------

    def data_sources(self) -> dict[str, list[dict[str, Any]]]:
        """Tables first, in name order, then ACE-170's metric views: the 50-source limit falls on
        the end of that order. Descriptions are added after the cut, so a table left out spends
        none of the snippet budget."""
        kept = self.capped([*self._tables(), *self._metric_views()], MAX_DATA_SOURCES,
                           "tables, views and metric views")
        synonyms = self._entity_synonyms()
        out: dict[str, list[dict[str, Any]]] = {}
        for et, item in kept:
            if et is not None:
                self.in_space.add(bare_name(et.table.name).casefold())
                self.refs[bare_name(et.table.name)] = item["identifier"]
                self.homes[bare_name(et.table.name)] = et.physical
                self.listed.append(et)
                item = self._table(et, item["identifier"], synonyms)
            out.setdefault("tables" if et is not None else "metric_views", []).append(item)
        return {key: sorted(items, key=lambda d: d["identifier"]) for key, items in out.items()}

    def _tables(self) -> list[tuple[str, str, tuple[Any, dict[str, Any]]]]:
        out = []
        for et in self.view.tables:
            t, p = et.table, et.physical
            if t.source_type == "sql":
                self.drop("table", t.name, "no_home_in_target", "a table defined by SQL is no Unity "
                          "Catalog table; create it as a view to list it in the space")
                continue
            ident = _identifier(p.database, p.schema, p.table)
            if ident is None:
                self.drop("table", t.name, "target_rule", "Genie names a table catalog.schema.table "
                          "and this one has no catalog; pass --location <catalog>.<schema>")
                continue
            out.append(("table", t.name, (et, {"identifier": ident})))
        return out

    def _table(self, et: Any, ident: str, synonyms: dict[tuple[str, str], list[str]]) -> dict[str, Any]:
        t = et.table
        item: dict[str, Any] = {"identifier": ident}
        about = [x for x in (t.description, *(f"Caveat: {c}" for c in t.caveats)) if x]
        if about:
            # A table description is a knowledge-store snippet, so it shares that budget.
            if self.take_snippet():
                item["description"] = lines_field(about)
            else:
                self.degrade("table", t.name, "target_limit", "its description is left out: a "
                             f"Genie space holds at most {MAX_SNIPPETS} knowledge-store snippets")
        configs = [self._column(t, c, synonyms) for c in sorted(t.columns, key=lambda c: c.name)]
        configs = [c for c in configs if len(c) > 1]
        if self.hide_sensitive:
            # The flag's whole purpose: the space lists the whole table, so a column left out of
            # this file is marked hidden in the space only by naming it here.
            configs += [{"column_name": n, "exclude": True} for n in et.withheld_columns]
        if configs:
            item["column_configs"] = sorted(configs, key=lambda c: c["column_name"])[:MAX_ITEMS]
        return item

    def _column(self, t: Any, c: Any, synonyms: dict[tuple[str, str], list[str]]) -> dict[str, Any]:
        config: dict[str, Any] = {"column_name": c.name}
        labels = _labels(c.choice_field)
        text = " ".join(x for x in (c.description, f"Unit: {c.unit}." if c.unit else "",
                                    f"Labels: {'; '.join(labels)}." if labels else "",
                                    *(f"Caveat: {x}" for x in c.caveats)) if x)
        if text:
            config["description"] = text_field(text)
        names = synonyms.get((bare_name(t.name).casefold(), c.name.casefold()), [])
        if names:
            config["synonyms"] = names
        if c.choice_field and c.type == "string":
            # A coded column is what entity matching is for: Genie keeps its stored values and
            # matches a question's wording to them. It takes string columns only, and needs
            # format assistance on.
            if self.matching_left > 0:
                self.matching_left -= 1
                config["enable_entity_matching"] = True
                config["enable_format_assistance"] = True
            else:
                self.degrade("column", f"{t.name}.{c.name}", "target_limit", "entity matching is "
                             f"left off: a Genie space has it on at most {MAX_ENTITY_MATCHING} "
                             "columns; its labels are in its description")
        return config

    def _entity_synonyms(self) -> dict[tuple[str, str], list[str]]:
        """The words people use for a column: the names of each entity mapped to it. Entities
        themselves travel to Pages (ACE-172); here they only name columns."""
        out: dict[tuple[str, str], list[str]] = {}
        for e in self.view.entities:
            words = [e.name, *([e.plural] if e.plural else []), *e.other_names]
            for m in e.maps_to:
                key = (bare_name(m.table).casefold(), m.column.casefold())
                out[key] = _unique([*out.get(key, []), *words])
        return {k: [w[:MAX_STRING] for w in v][:MAX_ITEMS] for k, v in out.items()}

    def _metric_views(self) -> list[tuple[str, str, tuple[Any, dict[str, Any]]]]:
        plans, _ = DB.plan_metric_views(self.view)  # their losses are write_metric_views' to report
        out = []
        for p in plans:
            ident = DB.metric_view_identifier(self.view, self.options, p.table)
            if not DB.writable(self.view, self.options, p):
                continue  # write_metric_views did not write it, so it is not created
            if not self._catalog_known(p.table):
                self.drop("metric_view", p.name, "target_rule", "Genie names a metric view "
                          "catalog.schema.name and this one has no catalog; pass --location")
                continue
            out.append(("metric_view", p.name, (None, {"identifier": ident})))
        return out

    def _catalog_known(self, table: str) -> bool:
        if self.options.location is not None:
            return True
        home = next(et.physical for et in self.view.tables if et.table.name == table)
        return bool(home.database and home.schema)

    # --- instructions -------------------------------------------------------

    def text_instruction(self) -> list[dict[str, Any]]:
        """Ambiguities, the datasource description and terminology, in that order, as the one text
        instruction Genie allows. Each is guidance there, not a rule, so each is reported."""
        v = self.view
        parts: list[str] = []
        for a in v.ambiguities:
            options = "; ".join(f"{o.label}{' (default)' if o.default else ''}: {o.how}"
                                for o in a.options)
            parts.append(f"{a.name}: when {a.when}, {_BEHAVIOUR[a.behaviour]}. Options: {options}.\n")
            self.degrade("ambiguity", a.name, "carried_as_text", "in the space's text instruction")
        if v.description:
            parts.append(f"About {v.datasource}: {v.description}\n")
            self.degrade("datasource", v.datasource, "carried_as_text",
                         "its description is in the space's text instruction")
        if v.fiscal_year_start_month != 1:
            month = MONTH_NAMES[v.fiscal_year_start_month - 1]
            parts.append(f"Fiscal year: the fiscal year starts in {month}; count fiscal years, "
                         "fiscal quarters and fiscal year to date from then.\n")
            self.degrade("datasource", v.datasource, "carried_as_text",
                         "its fiscal-year start is in the space's text instruction")
        if v.default_time_window:
            parts.append("Default time window: when a question names no time range, use "
                         f"{v.default_time_window}, and say so in the answer.\n")
            self.degrade("subject_area", v.area, "carried_as_text",
                         "its default time window is in the space's text instruction")
        for term, meaning in v.terminology:
            parts.append(f"{term}: {meaning}\n")
            self.degrade("terminology", term, "carried_as_text", "in the space's text instruction")
        content = text_field("".join(parts))
        if not content:
            return []
        if len(content) > MAX_ITEMS:
            self.degrade("datasource", v.datasource, "target_limit", "the text instruction is cut "
                         f"at {MAX_ITEMS} strings")
        return [{"id": v.id("text_instruction", v.area), "content": content[:MAX_ITEMS]}]

    def example_sqls(self, examples: list[ExportQuestion], budget: int) -> list[dict[str, Any]]:
        items = [("example", q.name, {"id": self.view.id("example", q.name),
                                      "question": text_field(q.question), "sql": text_field(q.sql)})
                 for q in examples if q.sql is not None]
        return self.capped(items, budget, "instructions")

    def qualify_examples(self, examples: list[ExportQuestion]) -> list[ExportQuestion]:
        """`examples` with their SQL qualified; one whose SQL cannot be keeps its question only,
        so it is still offered as a sample question."""
        out = []
        for q in examples:
            sql = self.qualified(q)
            if q.sql is not None and sql is None:
                reason, why = self._unqualified(q.sql)
                self.degrade("example", q.name, reason, f"carried as its question only: {why}")
            out.append(dataclasses.replace(q, sql=sql))
        return out

    def qualified(self, q: ExportQuestion) -> Optional[str]:
        """`q`'s SQL with each base table written as the space lists it, so it does not depend on
        a default catalog or schema; None when it has no SQL or that cannot be done for certain."""
        if q.sql is None:
            return None
        sql = rewrite_table_refs(q.sql, self.refs, DB.ENGINE, self.homes)
        return sql if sql is not None and parses(sql, DB.ENGINE) else None

    def _unqualified(self, sql: str) -> tuple[str, str]:
        if _tables_read(sql) - self.in_space:
            return "depends_on_excluded", "its SQL reads a table the space does not list"
        return "target_rule", ("each table in Genie SQL is written catalog.schema.table, as the "
                               "space lists it, and this SQL could not be rewritten for certain")

    def sample_questions(self, examples: list[ExportQuestion],
                         with_sql: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Every example question the space carries, with or without its SQL: the Genie UI offers
        only these, so a space whose examples all have SQL would offer none. An example and its
        sample question share an id; sample questions and benchmarks are the id space that must
        not repeat. An example cut at the instruction limit stays out of both."""
        kept = {e["id"] for e in with_sql}
        items = []
        for q in examples:
            qid = self.view.id("example", q.name)
            if q.sql is None or qid in kept:
                items.append(("example", q.name, {"id": qid, "question": text_field(q.question)}))
        return self.capped(items, MAX_ITEMS, "sample questions")

    def benchmarks(self) -> list[dict[str, Any]]:
        items = []
        for q in self.questions(self.view.golden):
            if q.sql is None:
                self.drop("golden", q.name, "target_rule", "a Genie benchmark needs exactly one SQL "
                          "answer, and this item has none for Databricks")
                continue
            sql = self.qualified(q)
            if sql is None:
                reason, why = self._unqualified(q.sql)
                self.drop("golden", q.name, reason, why)
                continue
            items.append(("golden", q.name, {
                "id": self.view.id("golden", q.name), "question": text_field(q.question),
                "answer": [{"format": "SQL", "content": text_field(sql)}]}))
        return self.capped(items, MAX_BENCHMARKS, "benchmark questions")

    def questions(self, qs: tuple[ExportQuestion, ...]) -> list[ExportQuestion]:
        """`qs`, each name once and its SQL parsing. An example is a sample question or an example
        SQL by the same id, so one name is one item across both."""
        out, seen = [], set()
        for q in qs:
            if q.name in seen:
                self.drop(q.kind, q.name, "name_collision", "an earlier item has the same question, "
                          "so the same id")
                continue
            seen.add(q.name)
            if q.sql is not None and not parses(q.sql, DB.ENGINE):
                self.drop(q.kind, q.name, "unparseable_for_target", "its SQL does not parse as "
                          "Databricks SQL")
                continue
            out.append(q)
        return out

    # --- knowledge-store snippets -------------------------------------------

    def join_specs(self) -> list[dict[str, Any]]:
        out = []
        for er in self.view.relationships:
            spec = self._join_spec(er)
            if spec is not None and self._fits("relationship", er.name):
                out.append(spec)
                both = bool(er.relationship.for_questions_about)
                what = ("its join type and the questions it is for travel" if both
                        else "its join type travels")
                self.degrade("relationship", er.name, "carried_as_text", f"{what} as the join "
                             "spec's instruction text; a join spec has no field for "
                             f"{'them' if both else 'it'}")
        return out

    def _join_spec(self, er: Any) -> Optional[dict[str, Any]]:
        r = er.relationship
        a, b = bare_name(r.from_table), bare_name(r.to_table)
        if a.casefold() == b.casefold():
            self.drop("relationship", er.name, "target_rule", "a Genie join spec joins two tables")
            return None
        if not {a.casefold(), b.casefold()} <= self.in_space:
            self.drop("relationship", er.name, "depends_on_excluded", "joins a table the space "
                      "does not list")
            return None
        left, right = _alias(a), _alias(b)
        if r.from_column and r.to_column:
            cond = f"{left}.{_alias(r.from_column)} = {right}.{_alias(r.to_column)}"
        else:
            cond = DB._requalify(r.on or "", {a.casefold(): left, b.casefold(): right})
        if not cond or not parses(cond, DB.ENGINE, as_expression=True):
            self.drop("relationship", er.name, "target_rule", "a join spec's condition reads its "
                      "two tables only, and this on: names another or does not parse")
            return None
        spec: dict[str, Any] = {
            "id": self.view.id("relationship", er.name),
            "left": {"identifier": self._ident(a), "alias": a},
            "right": {"identifier": self._ident(b), "alias": b},
            "sql": [cond, f"--rt=FROM_RELATIONSHIP_TYPE_{_CARDINALITY[r.relationship]}--"]}
        if r.description:
            spec["comment"] = text_field(r.description)
        # Genie's join spec has no join type, so it travels as the spec's own guidance.
        guidance = [f"Join as {r.join_type} JOIN."] + [f"Use for questions about {x}."
                                                      for x in r.for_questions_about]
        spec["instruction"] = lines_field(guidance)
        return spec

    def _ident(self, table: str) -> str:
        p = next(et.physical for et in self.view.tables
                 if bare_name(et.table.name).casefold() == table.casefold())
        return _identifier(p.database, p.schema, p.table) or ""

    def filters(self) -> list[dict[str, Any]]:
        out = []
        for et in self.view.tables:
            t = et.table
            for i, f in enumerate(t.default_filters, 1):
                name = f"{t.name}#{i}"
                sql = f.replace("{alias}", _alias(bare_name(t.name)))
                if bare_name(t.name).casefold() not in self.in_space:
                    self.drop("default_filter", name, "depends_on_excluded", "filters a table the "
                              "space does not list")
                elif not parses(sql, DB.ENGINE, as_expression=True):
                    self.drop("default_filter", name, "unparseable_for_target", "does not parse as "
                              "Databricks SQL")
                elif self._fits("default_filter", name):
                    # The positional name the core reports it by: two identical filters on one
                    # table are two items, and Genie rejects a repeated id.
                    out.append({"id": self.view.id("default_filter", name), "sql": text_field(sql),
                                "display_name": f"{t.name} optional filter {i}",
                                "instruction": text_field(DEFAULT_FILTER_NOTE)})
        return out

    def measures(self) -> list[dict[str, Any]]:
        out, aliases = [], set()
        for em in self.view.metrics:
            m, sql = em.metric, em.sql[DB.ENGINE]
            alias = DB.databricks_name(m.name)
            tables = {bare_name(x).casefold() for x in m.source_tables} | DB._qualifiers(sql)
            if m.inner_grain:
                self.drop("metric", m.name, "target_rule", "an aggregate of an aggregate at a finer "
                          "grain; a measure snippet aggregates rows once")
            elif not tables <= self.in_space:
                self.drop("metric", m.name, "depends_on_excluded", "reads a table the space does "
                          "not list")
            elif len(alias) > DB.MAX_NAME:
                self.drop("metric", m.name, "target_limit", f"its alias is {len(alias)} characters, "
                          f"past the {DB.MAX_NAME}-character identifier limit")
            elif alias in aliases:
                self.drop("metric", m.name, "name_collision", f"its alias {alias} is already taken")
            elif self._fits("metric", m.name):
                aliases.add(alias)
                out.append(self._measure(em, alias, sql))
        return out

    def _measure(self, em: Any, alias: str, sql: str) -> dict[str, Any]:
        m = em.metric
        notes = [x for x in (m.description, m.calculation, f"Unit: {m.unit}." if m.unit else "",
                             *(f"Caveat: {c}" for c in m.caveats)) if x]
        if m.non_additive_dimensions:
            notes.append(f"Semi-additive: across {', '.join(m.non_additive_dimensions)} take the "
                         f"{m.semi_additive_agg or 'single'} value; do not sum across them.")
            self.degrade("metric", m.name, "carried_as_text", "a measure snippet has no "
                         "semi-additive rule; it is in the snippet's instruction")
        item: dict[str, Any] = {"id": self.view.id("metric", m.name), "alias": alias,
                                "sql": text_field(sql), "display_name": m.name}
        synonyms = _unique([s[:MAX_STRING] for s in m.other_names])[:MAX_ITEMS]
        if synonyms:
            item["synonyms"] = synonyms
        item["instruction"] = lines_field(notes)
        return item

    def _fits(self, kind: str, name: str) -> bool:
        if self.take_snippet():
            return True
        self.drop(kind, name, "target_limit", f"a Genie space holds at most {MAX_SNIPPETS} "
                  "knowledge-store snippets")
        return False

    def report_unhidden_sensitive(self) -> None:
        """Without the flag, how many sensitive columns the listed tables hold that the space
        does not mark hidden: a count, never a name."""
        n = sum(et.withheld_count for et in self.listed)
        if n and not self.hide_sensitive:
            self.degrade("subject_area", self.view.area, "sensitive", f"{n} sensitive column(s) of "
                         "the tables this space lists are left out of the file and not marked "
                         "hidden in the space; --genie-hide-sensitive marks them hidden in the "
                         "space. Hiding is not access control: it does not prevent a query that "
                         "names the column; a Unity Catalog column mask restricts who can read it")

    # --- the whole space ----------------------------------------------------

    def build(self) -> Optional[dict[str, Any]]:
        sources = self.data_sources()
        if not sources:
            self.drop("subject_area", self.view.area, "target_rule", "a Genie space needs at least "
                      "one table or metric view, and none could be listed")
            return None
        self.report_unhidden_sensitive()
        text = self.text_instruction()
        questions = self.qualify_examples(self.questions(self.view.examples))
        examples = self.example_sqls(questions, MAX_INSTRUCTIONS - len(text))
        joins, filters, measures = self.join_specs(), self.filters(), self.measures()
        instructions = _sections(("text_instructions", text), ("example_question_sqls", examples),
                                 ("join_specs", joins))
        snippets = _sections(("filters", filters), ("measures", measures))
        if snippets:
            instructions["sql_snippets"] = snippets
        space: dict[str, Any] = {"version": VERSION}
        samples = self.sample_questions(questions, examples)
        if samples:
            space["config"] = {"sample_questions": _by_id(samples)}
        space["data_sources"] = sources
        if instructions:
            space["instructions"] = instructions
        bench = self.benchmarks()
        if bench:
            space["benchmarks"] = {"questions": _by_id(bench)}
        return space


def _alias(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda d: d["id"])


def _sections(*pairs: tuple[str, list[dict[str, Any]]]) -> dict[str, Any]:
    # Genie requires every list sorted by id, whatever order the items were budgeted in.
    return {key: _by_id(items) for key, items in pairs if items}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


# Written, never called: the export makes no request. Databricks' benchmark-run API is
# `genie_create_eval_run` and its readers in the Databricks SDK.
_BENCHMARK_RUN = (
    "# The space carries benchmark questions with SQL answers. To score Genie against them once\n"
    "# it is deployed, start a benchmark run (see Databricks' documentation for this API's current\n"
    "# status):\n"
    "#   POST /api/2.0/genie/spaces/{space_id}/eval-runs\n"
    "#        body {} runs every benchmark; {\"benchmark_question_ids\": [...]} runs some\n"
    "#   GET  /api/2.0/genie/spaces/{space_id}/eval-runs/{eval_run_id}          status, num_correct\n"
    "#   GET  /api/2.0/genie/spaces/{space_id}/eval-runs/{eval_run_id}/results  one per question\n"
    "# An answer is rated good when its result set matches the SQL answer's (in any row order,\n"
    "# numbers to 4 significant digits). Results stay visible for one week.\n")


def bundle_resource(view: ExportView, benchmarks: bool = False) -> str:
    """The `genie_spaces` resource deploying the area's space. The warehouse belongs to the
    customer's workspace, so it is a bundle variable they set, never a value in the file. When the
    space has benchmarks, the header says how to run them."""
    key = DB.databricks_name(view.area)
    title = view.area if view.area == view.datasource else f"{view.datasource}: {view.area}"
    space: dict[str, Any] = {"title": title}
    if view.area_description:
        space["description"] = view.area_description
    space["warehouse_id"] = "${var." + WAREHOUSE_VAR + "}"
    # Relative to this file, which is where the CLI resolves a resource's paths from.
    space["file_path"] = f"../{space_path(view.area)}"
    doc = {"variables": {WAREHOUSE_VAR: {"description": "The id of the SQL warehouse the Genie "
                                                        "space runs on, in your workspace."}},
           "resources": {"genie_spaces": {key: space}}}
    head = (f"# Genie space for subject area {view.area}, written by `sm export`.\n"
            f"# Include it from databricks.yml (include: [{RESOURCE_DIR}/*.yml]) and set "
            f"{WAREHOUSE_VAR},\n# e.g. `databricks bundle deploy --var {WAREHOUSE_VAR}=<id>`.\n"
            "# A genie_spaces resource deploys only with the direct deployment engine (see "
            "Databricks'\n# documentation for the CLI version that has it); databricks.yml "
            "selects it with:\n"
            "#   bundle:\n#     engine: direct\n")
    if benchmarks:
        head += _BENCHMARK_RUN
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=10_000)


def write_genie_space(view: ExportView, options: ExportOptions) -> WriterOutput:
    builder = _Space(view, options)
    space = builder.build()
    if space is None:
        return WriterOutput({}, tuple(builder.losses))
    text = json.dumps(space, indent=2, ensure_ascii=False) + "\n"
    resource = bundle_resource(view, "benchmarks" in space)
    files = {space_path(view.area): text.encode("utf-8"),
             resource_path(view.area): resource.encode("utf-8")}
    return WriterOutput(files, tuple(builder.losses))


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--genie-hide-sensitive", dest=HIDE_SENSITIVE, action="store_true", default=argparse.SUPPRESS,
        help="in each Genie space, mark the sensitive columns of the tables it lists hidden in the "
             "space (column_configs exclude: true). This writes those columns' names into the "
             "space file, the only place an export names one. Hiding is not access control: it does not "
             "prevent a query that names the column; restricting who can read a column is done "
             "with a Unity Catalog column mask")


DB.register_artifact(write_genie_space, add_arguments=add_arguments)
