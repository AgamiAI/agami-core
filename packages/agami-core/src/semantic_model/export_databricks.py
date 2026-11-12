"""`databricks` export target (F19 / ACE-170): one metric view per fact table.

A Databricks metric view is a YAML definition, created with `CREATE VIEW … WITH METRICS LANGUAGE
YAML`, that names one source table, the tables joined to it, its fields and its measures. Genie and
Databricks' business semantics compute a metric from one, so each subject area's metrics are written
as one metric view per fact table: a table that some eligible metric measures.

The `databricks` target is several artifacts (these metric views, then the Genie space and Pages).
Each is a function `(view, options) -> WriterOutput` in `ARTIFACTS`; a later slice adds its own with
`register_artifact` from its own module, and `write_databricks` merges them. A slice that needs the
metric views, as the Genie space does, reads them from `plan_metric_views` and names them with
`metric_view_name` / `metric_view_identifier`, so the naming rule lives here once.

A second-order metric (`OUTERAGG({base})` at an `inner_grain`) becomes a finer level-of-detail
measure (`partition: {include, outer_aggregate}`), and a metric whose unit is a plain currency
code carries `format: {type: currency}`.

Reads only the `ExportView`: eligibility, the sensitive-column rule and binding resolution already
hold there.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

import yaml

from .derived import SECOND_ORDER_RE
from .export import (
    ExportMetric,
    ExportOptions,
    ExportTable,
    ExportView,
    LossEntry,
    Target,
    Writer,
    WriterOutput,
    degraded,
    dropped,
    parse_statement,
    register,
)
from .models import bare_name
from .units import parse_unit

ENGINE = "Databricks"
DIALECT = "databricks"
METRIC_VIEW_DIR = "metric_views"
# 1.1 is the first version with synonyms and display_name, and the one Databricks documents.
YAML_VERSION = 1.1
MAX_SYNONYMS = 10
MAX_TEXT = 255   # a synonym's or a display_name's length limit
# Databricks' `semiadditive` takes only these; agami's `average`, `min` and `max` have no home.
_SEMIADDITIVE = ("first", "last")
# What a finer level-of-detail measure's `outer_aggregate` takes, from "Use level of detail (LOD)
# expressions in metric views": a single-argument aggregate, and only these.
_OUTER_AGGREGATES = ("SUM", "AVG", "MIN", "MAX", "COUNT", "MEDIAN")
# In the statement of a view holding such a measure, under its first line. YAML `#` comments are
# stripped at 1.1, so the note is SQL's.
_FINER_NOTE = (
    "-- This metric view has a finer level-of-detail measure (a measure with a `partition` block,\n"
    "-- YAML 1.1): an aggregate of an aggregate at a finer grain. Databricks documents it on \"Use\n"
    "-- level of detail (LOD) expressions in metric views\"; see Databricks' documentation for the\n"
    "-- runtime it needs, and compare the measure with agami's answer before relying on it.\n")
# In the statement of a view whose definition holds a `$`. The Unity Catalog file escapes `$` as
# `\$` in its string literals, so a notebook's widget syntax cannot reach it; the body here is
# dollar-quoted, where no escape exists, so the note says where to run it instead.
_DOLLAR_NOTE = (
    "-- The definition below holds a `$`. A notebook cell reads `$name` and `${name}` as widget\n"
    "-- references, and a dollar-quoted body has no escape for them: run this file in the SQL\n"
    "-- editor or on a SQL warehouse, where the text stays as written.\n")
_SIMPLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def databricks_name(text: str) -> str:
    """The fixed rule from an agami name to a Databricks one: lowercase, every run of other
    characters one `_`. Queryable without backticks, e.g. `MEASURE(order_count)`."""
    name = re.sub(r"[^0-9a-z]+", "_", text.casefold()).strip("_") or "unnamed"
    return f"_{name}" if name[0].isdigit() else name


def metric_view_name(area: str, table: str) -> str:
    """The metric view of `table` (a fact table) in subject area `area`: `<area>__<table>`."""
    return f"{databricks_name(area)}__{databricks_name(bare_name(table))}"


def _quote(part: str) -> str:
    return part if _SIMPLE.fullmatch(part) else "`" + part.replace("`", "``") + "`"


def _dotted(*parts: Optional[str]) -> str:
    return ".".join(_quote(p) for p in parts if p)


def metric_view_parts(view: ExportView, options: ExportOptions,
                      table: str) -> tuple[Optional[str], Optional[str], str]:
    """Where the metric view of `table` is created, as (catalog, schema, name): the `--location`
    catalog and schema, else the fact table's own."""
    loc = options.location
    home = next((t.physical for t in view.tables if t.table.name == table), None)
    catalog, schema = (loc.database, loc.schema) if loc else (
        (home.database, home.schema) if home else (None, None))
    return catalog, schema, metric_view_name(view.area, table)


def metric_view_identifier(view: ExportView, options: ExportOptions, table: str) -> str:
    """`metric_view_parts` as one identifier."""
    return _dotted(*metric_view_parts(view, options, table))


# ---------------------------------------------------------------------------
# Reading SQL: qualifiers rewritten, never translated
# ---------------------------------------------------------------------------


def _qualifiers(sql: str) -> set[str]:
    from sqlglot import exp

    tree = parse_statement(sql, ENGINE)
    return {c.table.casefold() for c in tree.find_all(exp.Column) if c.table}


def _dotted_chains(tokens: list) -> list[list]:
    """Each `a.b[.c…]` name chain that is not a function call, as its name tokens."""
    from sqlglot.tokens import TokenType

    def is_name(t: Any) -> bool:  # a back-ticked name, or a bare word, keywords such as `date` too
        return t.token_type == TokenType.IDENTIFIER or (
            t.token_type not in (TokenType.STRING, TokenType.NUMBER) and bool(re.fullmatch(r"\w+", t.text)))

    chains, i = [], 0
    while i < len(tokens):
        chain = [tokens[i]] if is_name(tokens[i]) else []
        while chain and i + 2 < len(tokens) and tokens[i + 1].token_type == TokenType.DOT \
                and is_name(tokens[i + 2]):
            i += 2
            chain.append(tokens[i])
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if len(chain) > 1 and not (nxt and nxt.token_type == TokenType.L_PAREN):
            chains.append(chain)
        i += 1
    return chains


def _requalify(sql: str, refs: Mapping[str, str]) -> Optional[str]:
    """`sql` with each `[schema.]table.column` written as the metric view reaches it
    (`source.column`, `<join path>.column`); None when it names a table the view does not hold.
    Only the qualifier's text changes, so the rest stays exactly the SQL a person wrote."""
    from sqlglot.dialects.dialect import Dialect

    out, last = [], 0
    for chain in _dotted_chains(Dialect.get_or_raise(DIALECT).tokenize(sql)):
        ref = refs.get(chain[-2].text.casefold())
        if ref is None:
            return None
        out += [sql[last:chain[0].start], ref]
        last = chain[-2].end + 1
    return "".join(out) + sql[last:]


# ---------------------------------------------------------------------------
# Joins: the many-to-one tree under a fact table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Edge:
    parent: str          # casefolded bare table names
    child: str
    rel: Any             # ExportRelationship
    parent_col: Optional[str]
    child_col: Optional[str]


def _on_reads_its_ends(r: Any, a: str, b: str) -> bool:
    try:
        return _requalify(r.on, {a: "p", b: "c"}) is not None
    except Exception:
        return False


def _edges(view: ExportView) -> tuple[dict[str, list[_Edge]], list[Any]]:
    """Every join a metric view can make: from a table to one holding at most one row for each of
    its rows. A one-to-one relationship goes both ways. Also the relationships whose `on:` names a
    third table, which a join between two tables cannot hold."""
    out: dict[str, list[_Edge]] = {}
    unusable = []
    for er in view.relationships:
        r = er.relationship
        a, b = bare_name(r.from_table).casefold(), bare_name(r.to_table).casefold()
        if r.on and not _on_reads_its_ends(r, a, b):
            unusable.append(er)
            continue
        ways = {"many_to_one": [(a, b, r.from_column, r.to_column)],
                "one_to_many": [(b, a, r.to_column, r.from_column)]}.get(
            r.relationship, [(a, b, r.from_column, r.to_column), (b, a, r.to_column, r.from_column)])
        for parent, child, pcol, ccol in ways:
            if parent != child:
                out.setdefault(parent, []).append(_Edge(parent, child, er, pcol, ccol))
    return out, unusable


@dataclass(frozen=True)
class _Tree:
    fact: str
    order: tuple[str, ...]          # joined tables, breadth first
    via: Mapping[str, _Edge]        # joined table -> the join that reaches it
    skipped: tuple[_Edge, ...]      # joins to a table already reached another way

    def path(self, table: str) -> list[str]:
        out = []
        while table != self.fact:
            out.insert(0, table)
            table = self.via[table].parent
        return out

    def refs(self, aliases: Mapping[str, str]) -> dict[str, str]:
        """Table -> how a field or measure expression reaches it."""
        refs = {self.fact: "source"}
        refs.update({t: ".".join(aliases[p] for p in self.path(t)) for t in self.order})
        return refs


def _join_tree(fact: str, edges: Mapping[str, list[_Edge]], placeable: set[str]) -> _Tree:
    """Breadth first, so each table is reached by its shortest path; a metric view joins a table
    once, and a binding names a table, not a path, so a second path could not be chosen anyway."""
    via: dict[str, _Edge] = {}
    order, skipped, queue = [], [], [fact]
    while queue:
        for e in edges.get(queue.pop(0), []):
            if e.child not in placeable:
                continue
            if e.child == fact or e.child in via:
                skipped.append(e)
                continue
            via[e.child] = e
            order.append(e.child)
            queue.append(e.child)
    return _Tree(fact, tuple(order), via, tuple(skipped))


def _condition(e: _Edge, tree: _Tree, aliases: Mapping[str, str]) -> str:
    # A join's `on` reaches the table it hangs from by that table's join name, `source` at the top.
    near = {e.parent: "source" if e.parent == tree.fact else aliases[e.parent],
            e.child: aliases[e.child]}
    if e.parent_col and e.child_col:
        return f"{near[e.parent]}.{_quote(e.parent_col)} = {near[e.child]}.{_quote(e.child_col)}"
    return _requalify(e.rel.relationship.on, near) or ""  # `_edges` kept only `on:` that reads


def measure_format(unit: Optional[str]) -> Optional[dict[str, str]]:
    """A measure's `format` from agami's `unit`, where one of Databricks' format types says the
    same: a plain ISO currency code is `type: currency`. A scaled or per-period currency
    (`USD_millions`, `USD/day`) is not, since the format would print 12.5 as $12.50; nor is
    `percent`, which agami writes as 0-100 while the `percentage` type is for ratios. Those keep
    their unit in the comment only."""
    cu = parse_unit(unit)
    if cu is None or cu.scale or cu.period:
        return None
    return {"type": "currency", "currency_code": cu.code}


# ---------------------------------------------------------------------------
# Planning the metric views of one subject area
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricViewPlan:
    """One metric view: what ACE-171 lists in the Genie space and this module writes as SQL."""

    table: str                       # the fact table's agami name
    name: str                        # metric_view_name(area, table)
    definition: Mapping[str, Any]    # the YAML document, in emitted key order
    measures: tuple[str, ...]        # agami metric names, in emitted order


@dataclass
class _Planner:
    view: ExportView
    losses: list[LossEntry]

    def __post_init__(self) -> None:
        self.tables = {bare_name(t.table.name).casefold(): t for t in self.view.tables}
        self.sources = {k: s for k, t in self.tables.items() if (s := self._source(t)) is not None}
        self.aliases = {k: databricks_name(k) for k in self.tables}
        self.edges, unusable = _edges(self.view)
        self.trees: dict[str, _Tree] = {}
        # A second-order metric's name -> (its base metric, its outer aggregate), once placed.
        self.finer: dict[str, tuple[ExportMetric, str]] = {}
        for er in unusable:
            self.degrade("relationship", er.name, "target_rule", "left out of every metric view: "
                         "its on: names a table besides the two it joins")

    def _source(self, t: ExportTable) -> Optional[str]:
        # A table defined by SQL is joined as that query, which only Databricks SQL can be.
        if t.table.source_type == "sql":
            return t.table.sql if self.view.sql_engine == ENGINE and t.table.sql else None
        p = t.physical
        return _dotted(p.database, p.schema, p.table)

    def tree(self, fact: str) -> _Tree:
        if fact not in self.trees:
            self.trees[fact] = _join_tree(fact, self.edges, set(self.sources))
        return self.trees[fact]

    def drop(self, kind: str, name: str, reason: Any, detail: str) -> None:
        self.losses.append(dropped(self.view.area, kind, name, reason, detail))

    def degrade(self, kind: str, name: str, reason: Any, detail: str) -> None:
        self.losses.append(degraded(self.view.area, kind, name, reason, detail))

    def home(self, em: ExportMetric) -> Optional[str]:
        """The fact table a metric is measured from: the table every table it reads hangs from.
        Only there, since in a view over a finer table its rows would be counted more than once."""
        m = em.metric
        # The core parsed this SQL in the Databricks dialect already, so it parses here.
        needed = {bare_name(t).casefold() for t in m.source_tables} | _qualifiers(em.sql[ENGINE])
        unknown = sorted(needed - set(self.sources))
        if not needed or unknown:
            self.drop("metric", m.name, "target_rule", "a metric view reads tables by name, and it "
                      f"reads {', '.join(unknown) or 'none'} that this subject area's views cannot")
            return None
        first = [bare_name(m.primary_table).casefold()] if m.primary_table else []
        for fact in [*first, *sorted(needed)]:
            if fact in needed and needed <= {fact, *self.tree(fact).order}:
                return fact
        self.drop("metric", m.name, "target_rule", "a metric view has one source table, and no one "
                  f"of {', '.join(sorted(needed))} reaches the others by many-to-one joins")
        return None

    def finer_home(self, em: ExportMetric) -> Optional[str]:
        """The fact table of a second-order metric (`OUTERAGG({base})` at `inner_grain`), stated
        as a finer level-of-detail measure: the base's aggregate at the query's grain plus the
        inner grain, rolled up by the outer aggregate. None, reported, where that would not be
        exactly agami's metric."""
        m = em.metric
        shape = SECOND_ORDER_RE.match(em.template.get(ENGINE) or "")
        base = next((b for b in self.view.metrics
                     if shape and b.metric.name == shape.group(2).strip()), None)
        tables: set[str] = set()
        if base is not None:
            tables = {bare_name(t).casefold() for t in base.metric.source_tables} | _qualifiers(
                base.sql[ENGINE])
        why = None
        if shape is None:
            why = "a second-order metric bound other than as OUTERAGG({base metric})"
        elif shape.group(1).upper() not in _OUTER_AGGREGATES:
            why = (f"its outer aggregate {shape.group(1)} is not one a level-of-detail measure rolls "
                   f"up with ({', '.join(_OUTER_AGGREGATES)})")
        elif m.non_additive_dimensions:
            why = "semi-additive and second-order at once; a level-of-detail measure has no window"
        elif base is None:
            why = (f"its base metric {shape.group(2).strip()} is not in this export of the subject "
                   "area, and the measure states the base's aggregate itself")
        elif base.metric.inner_grain or base.metric.non_additive_dimensions:
            why = ("its base metric is second-order or semi-additive itself, which a "
                   "level-of-detail measure cannot build on")
        elif len(tables) != 1 or not tables <= set(self.sources):
            # agami joins a base over several tables INNER before grouping; a metric view joins
            # LEFT OUTER, so a row with no match would count here and not there.
            why = ("its base metric reads several tables, or one this subject area's views cannot; "
                   "agami inner-joins them before grouping, and a metric view joins left outer")
        if why is not None:
            self.drop("metric", m.name, "target_rule", why)
            return None
        assert base is not None and shape is not None
        self.finer[m.name] = (base, shape.group(1).upper())
        return tables.pop()

    def plans(self) -> list[MetricViewPlan]:
        by_fact: dict[str, list[ExportMetric]] = {}
        for em in self.view.metrics:
            fact = self.finer_home(em) if em.metric.inner_grain else self.home(em)
            if fact is not None:
                by_fact.setdefault(fact, []).append(em)
        return [p for fact in sorted(by_fact) if (p := self.plan(fact, by_fact[fact])) is not None]

    def plan(self, fact: str, metrics: list[ExportMetric]) -> Optional[MetricViewPlan]:
        tree = self.tree(fact)
        refs = tree.refs(self.aliases)
        taken: set[str] = set()
        # Measures claim their names before fields do: a metric has no other home in this view.
        named = [(em, n) for em in metrics if (n := self._claim(taken, em.metric.name))]
        fields, by_column = self._fields(tree, refs, taken)
        measures = [m for em, n in named if (m := self._measure(em, n, refs, by_column, tree))]
        if not measures:
            return None
        et = self.tables[fact]
        doc: dict[str, Any] = {"version": YAML_VERSION}
        if et.table.description:
            doc["comment"] = et.table.description
        doc["source"] = self.sources[fact]
        joins = self._joins(fact, tree)
        if joins:
            doc["joins"] = joins
        if fields:
            doc["fields"] = fields
        doc["measures"] = [m for _, m in measures]
        self._note_skipped(tree, fact)
        return MetricViewPlan(et.table.name, metric_view_name(self.view.area, et.table.name), doc,
                              tuple(name for name, _ in measures))

    def _claim(self, taken: set[str], agami_name: str) -> Optional[str]:
        name = databricks_name(agami_name)
        if name in taken:
            self.drop("metric", agami_name, "name_collision", f"its metric view name {name} is "
                      "already taken by an earlier measure")
            return None
        taken.add(name)
        return name

    def _fields(self, tree: _Tree, refs: Mapping[str, str], taken: set[str]
                ) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
        """Every column of the source and joined tables. A joined table's columns carry its join
        name as a prefix, since `id` and `name` recur across tables."""
        fields, by_column = [], {}
        for t in (tree.fact, *tree.order):
            et = self.tables[t]
            for c in et.table.columns:
                base = c.name if t == tree.fact else f"{self.aliases[t]}_{c.name}"
                name = databricks_name(base)
                if name in taken:
                    self.degrade("column", f"{et.table.name}.{c.name}", "name_collision",
                                 f"left out of metric view {metric_view_name(self.view.area, tree.fact)}"
                                 f": its field name {name} is taken")
                    continue
                taken.add(name)
                by_column[(t, c.name.casefold())] = name
                field: dict[str, Any] = {"name": name, "expr": f"{refs[t]}.{_quote(c.name)}"}
                if c.description:
                    field["comment"] = c.description
                fields.append(field)
        return fields, by_column

    def _measure(self, em: ExportMetric, name: str, refs: Mapping[str, str],
                 by_column: Mapping[tuple[str, str], str], tree: _Tree
                 ) -> Optional[tuple[str, dict[str, Any]]]:
        m = em.metric
        # A second-order metric states its base's aggregate, which `partition` regroups.
        sql = self.finer[m.name][0].sql[ENGINE] if m.inner_grain else em.sql[ENGINE]
        # `home` placed the metric where every table it names is reachable, so this rewrite holds.
        expr = _requalify(sql, refs) or sql
        window = self._window(m, by_column, tree)
        partition = self._partition(m, by_column, tree) if m.inner_grain else {}
        if window is None or partition is None:
            return None
        measure: dict[str, Any] = {"name": name, "expr": expr}
        comment = " ".join(x for x in (m.description, m.calculation,
                                       f"Unit: {m.unit}." if m.unit else "") if x)
        if comment:
            measure["comment"] = comment
        if name != m.name and len(m.name) <= MAX_TEXT:
            measure["display_name"] = m.name
        fmt = measure_format(m.unit)
        if fmt:
            measure["format"] = fmt
        synonyms = self._synonyms(m)
        if synonyms:
            measure["synonyms"] = synonyms
        if window:
            measure["window"] = window
        if partition:
            measure["partition"] = partition
        return m.name, measure

    def _partition(self, m: Any, by_column: Mapping[tuple[str, str], str], tree: _Tree
                   ) -> Optional[dict[str, Any]]:
        """A second-order metric's `inner_grain` as the fields its base is grouped by first. Each
        must be a column of the fact table, which is where agami groups the base."""
        include: list[str] = []
        for g in m.inner_grain:
            table, _, column = g.rpartition(".")
            name = None if table and bare_name(table).casefold() != tree.fact else by_column.get(
                (tree.fact, column.casefold()))
            if name is None:
                self.drop("metric", m.name, "target_rule", f"its inner grain {g} is no field of "
                          f"its metric view's source table {tree.fact}, which a partition includes")
                return None
            if name not in include:
                include.append(name)
        return {"include": include, "outer_aggregate": self.finer[m.name][1]}

    def _window(self, m: Any, by_column: Mapping[tuple[str, str], str], tree: _Tree
                ) -> Optional[list[dict[str, str]]]:
        """A semi-additive metric's `non_additive_dimensions` as `semiadditive` windows: summed
        within one value of each, and collapsed to its first or last value across them. [] for an
        additive metric; None, reported, when Databricks cannot say what agami says."""
        if not m.non_additive_dimensions:
            return []
        if m.semi_additive_agg not in _SEMIADDITIVE:
            self.drop("metric", m.name, "target_rule", "Databricks collapses a semi-additive measure "
                      f"to its first or last value only, not {m.semi_additive_agg or 'unspecified'}")
            return None
        out = []
        for dim in m.non_additive_dimensions:
            order = self._order_field(dim, by_column, tree)
            if order is None:
                self.drop("metric", m.name, "target_rule", f"its non-additive dimension {dim} is "
                          "no one field of its metric view, which a window must order by")
                return None
            out.append({"order": order, "range": "current", "semiadditive": m.semi_additive_agg})
        return out

    def _order_field(self, dim: str, by_column: Mapping[tuple[str, str], str], tree: _Tree
                     ) -> Optional[str]:
        if dim == "time":  # agami's shorthand for any date grain: the fact table's one date column
            dates = [c.name for c in self.tables[tree.fact].table.columns
                     if c.type in ("date", "timestamp")]
            return by_column.get((tree.fact, dates[0].casefold())) if len(dates) == 1 else None
        table, _, column = dim.rpartition(".")
        for t in ([bare_name(table).casefold()] if table else [tree.fact, *tree.order]):
            if (t, column.casefold()) in by_column:
                return by_column[(t, column.casefold())]
        return None

    def _synonyms(self, m: Any) -> list[str]:
        out: list[str] = []
        for s in m.other_names:
            if len(s) > MAX_TEXT:
                self.degrade("metric", m.name, "target_limit",
                             f"a synonym longer than {MAX_TEXT} characters is left out")
            elif s.strip() and s.casefold() not in {o.casefold() for o in out}:
                out.append(s)
        if len(out) > MAX_SYNONYMS:
            self.degrade("metric", m.name, "target_limit", f"carries its first {MAX_SYNONYMS} "
                         f"synonyms of {len(out)}")
        return out[:MAX_SYNONYMS]

    def _joins(self, parent: str, tree: _Tree) -> list[dict[str, Any]]:
        out = []
        for child in tree.order:
            e = tree.via[child]
            if e.parent != parent:
                continue
            join: dict[str, Any] = {"name": self.aliases[child], "source": self.sources[child],
                                    "on": _condition(e, tree, self.aliases)}
            nested = self._joins(child, tree)
            if nested:
                join["joins"] = nested
            if e.rel.relationship.join_type != "LEFT":
                self.degrade("relationship", e.rel.name, "detail_lost", "a metric view joins LEFT "
                             f"OUTER, so its {e.rel.relationship.join_type} join type does not carry")
            out.append(join)
        return out

    def _note_skipped(self, tree: _Tree, fact: str) -> None:
        for e in tree.skipped:
            self.degrade("relationship", e.rel.name, "target_rule",
                         f"metric view {metric_view_name(self.view.area, fact)} joins "
                         f"{e.child} once, by a shorter or earlier join")


def plan_metric_views(view: ExportView) -> tuple[list[MetricViewPlan], list[LossEntry]]:
    """The metric views of one subject area, ordered by fact table, and what planning them lost."""
    losses: list[LossEntry] = []
    plans = _Planner(view, losses).plans()
    return plans, losses


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def create_statement(identifier: str, definition: Mapping[str, Any]) -> str:
    """`CREATE OR REPLACE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$`."""
    body = yaml.safe_dump(dict(definition), sort_keys=False, allow_unicode=True, width=10_000)
    return (f"CREATE OR REPLACE VIEW {identifier}\nWITH METRICS\nLANGUAGE YAML\nAS $$\n"
            f"{body}$$;\n")


def write_metric_views(view: ExportView, options: ExportOptions) -> WriterOutput:
    plans, losses = plan_metric_views(view)
    files = {}
    for p in plans:
        sql = create_statement(metric_view_identifier(view, options, p.table), p.definition)
        body = sql.split("$$")[1] if sql.count("$$") == 2 else ""
        notes = "".join(note for note, holds in (
            (_FINER_NOTE, any("partition" in m for m in p.definition["measures"])),
            (_DOLLAR_NOTE, "$" in body)) if holds)
        if notes:
            # Below the first line, which readers take as the statement's head and view name.
            head, _, rest = sql.partition("\n")
            sql = f"{head}\n{notes}{rest}"
        if sql.count("$$") != 2:
            # `$$` inside the definition would end it early.
            losses += [dropped(view.area, "metric", m, "target_rule", "its metric view's text holds "
                               "$$, which would end the definition early") for m in p.measures]
            continue
        files[f"{METRIC_VIEW_DIR}/{p.name}.sql"] = sql.encode("utf-8")
    return WriterOutput(files, tuple(losses))


# Every artifact of the `databricks` target, in the order their files and losses are merged.
ARTIFACTS: list[Writer] = [write_metric_views]
# The flags each artifact adds to `sm export`, by artifact. They reach the artifact, as every
# target's flags do, through `options.target_args`.
ARTIFACT_ARGUMENTS: dict[Writer, Callable[[argparse.ArgumentParser], None]] = {}


def register_artifact(writer: Writer,
                      add_arguments: Optional[Callable[[argparse.ArgumentParser], None]] = None) -> None:
    """Add one more artifact (the Genie space, Pages) to the `databricks` target, with the flags
    it adds to `sm export`, declared as a target's are (`default=argparse.SUPPRESS`)."""
    if writer not in ARTIFACTS:
        ARTIFACTS.append(writer)
    if add_arguments is not None:
        ARTIFACT_ARGUMENTS[writer] = add_arguments


def add_arguments(sp: argparse.ArgumentParser) -> None:
    # `sm export` asks each target for its flags after importing every writer module, so every
    # artifact has registered by the time this runs.
    for artifact in ARTIFACTS:
        if artifact in ARTIFACT_ARGUMENTS:
            ARTIFACT_ARGUMENTS[artifact](sp)


def write_databricks(view: ExportView, options: ExportOptions) -> WriterOutput:
    files: dict[str, bytes] = {}
    losses: list[LossEntry] = []
    golden: dict[str, frozenset[str]] = {}
    for artifact in ARTIFACTS:
        out = artifact(view, options)
        clash = sorted(set(files) & set(out.files))
        if clash:
            raise ValueError(f"two databricks artifacts write {', '.join(clash)}")
        files.update(out.files)
        losses += out.losses
        # A golden field one artifact carries is in the target's files.
        for name, fields in out.carried_golden_fields.items():
            golden[name] = golden.get(name, frozenset()) | frozenset(fields)
    return WriterOutput(files, tuple(losses), golden)


register(Target("databricks", (ENGINE,), write_databricks, add_arguments))
