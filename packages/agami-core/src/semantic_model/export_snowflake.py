"""Snowflake semantic view (F19 / ACE-168): one subject area as one semantic view YAML file.

`write_snowflake` turns the `ExportView` of one subject area into `<area>.semantic_view.yaml`, in
the shape `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` takes, and `<area>.sql`, the call that applies it
to the `--location` schema. The call is written, never run: applying it is the customer's act.
`export_snowflake_agent` (ACE-169) adds the agent bound to that view and its evaluation data.

Snowflake checks a view when it is created, so the rules it documents are enforced here first: no
self-join, no relationship cycle (transitive included), a relationship points at the other table's
primary or unique key (so many-to-many only through a bridge), a table-level metric aggregates and
reads only tables it relates to, and synonyms are unique across the view. An item that breaks one
is dropped as `target_rule`, naming the rule, so what is written can be created as it stands. Two
shapes the rules forbid are recovered the way Snowflake documents: a self-join joins a second
logical table on the same base table (a "role-playing" table), and a metric that reads tables no
one table relates to becomes a view-level (derived) metric aggregating their dimensions and facts.

Review state: `--snowflake-certification-tags` writes Snowflake's own `CERTIFICATION_STATUS` tag on
the view and its tables, dimensions, facts and metrics. It is opt-in because only a role granted
the `SNOWFLAKE.OOB_TAG_APPLY` application role may apply that tag, and by default only ACCOUNTADMIN
holds it. No sensitivity tag is written: a sensitive column is not in the view at all.

Naming rule (ACE-169 binds its agent by it): every emitted name is the agami name lowercased, with
each run of characters other than a-z and 0-9 turned into one `_`, and a trailing `_` on a name that
is a Snowflake keyword; the view is the subject area's name under that rule. Two items that map to
one name keep the first in order (`name_collision`).

Identifier rule, for a name the model holds (a column, a table, a `--location` part): the model spells
a Snowflake catalog name the way agami's dialect layer reads it (`dialects.Dialect.ident`), so an
all-lower or all-upper plain name is the unquoted one, stored upper-case, and any other spelling is
the stored name exactly. SQL here quotes a keyword in its stored case, and any name an unquoted
spelling cannot reach. Where the YAML holds such a name bare (base table, keys, relationship
columns), Snowflake documents no quoting, so it is written as stored and reported.
"""

from __future__ import annotations

import argparse
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple, Optional

from .export import (
    DEFAULT_FILTER_NOTE,
    MONTH_NAMES,
    ExportOptions,
    ExportQuestion,
    ExportRefused,
    ExportTable,
    ExportView,
    Location,
    LossEntry,
    PhysicalName,
    Reason,
    Target,
    WriterOutput,
    degraded,
    dropped,
    is_select_alias,
    join_sentences,
    names_table,
    parse_statement,
    parses,
    put_nonempty,
    register,
    splice_spans,
    token_span,
)
from .models import bare_name

TARGET = "snowflake"
ENGINE = "Snowflake"
DIALECT = "snowflake"

# Snowflake lists no `data_type` vocabulary; these are the types its documented examples use, or the
# Snowflake type of the same name where the examples show none.
_DATA_TYPES = {"string": "VARCHAR", "integer": "NUMBER", "decimal": "NUMBER", "float": "FLOAT",
               "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP", "time": "TIME",
               "json": "VARIANT", "array": "ARRAY", "uuid": "VARCHAR", "bytes": "BINARY"}
_TIME_TYPES = frozenset({"date", "timestamp"})
_NUMERIC_TYPES = frozenset({"integer", "decimal", "float"})
# Snowflake keeps the value of the last row in the sort order over a non-additive dimension, so
# agami's "period end" is ascending and "period start" descending; nothing else has a counterpart.
_SORT = {"last": "ascending", "first": "descending"}
_PLAIN_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

# Snowflake-provided tags (user-guide/object-tagging/snowflake-provided-tags): CERTIFICATION_STATUS
# takes a fixed set of values that no account can change. agami's "not yet reviewed" is the
# documented TO BE REVIEWED; nobody is reviewing it (IN REVIEW) or still writing it (DRAFT).
CERTIFICATION_TAG = {"database": "SNOWFLAKE", "schema": "TAGS", "tag": "CERTIFICATION_STATUS"}
CERTIFIED, TO_BE_REVIEWED = "CERTIFIED", "TO BE REVIEWED"
# Without onboarding flags Cortex Analyst suggests "up to five" verified questions by similarity;
# flagged ones are all returned whatever was asked (suggested-questions-feature). So the first five
# approved, in the view's question order, keep that count and make it fixed and reviewed.
ONBOARDING_QUESTIONS = 5

# Snowflake's reserved and limited keywords: every keyword in the table of "Reserved & limited
# keywords" (https://docs.snowflake.com/en/sql-reference/reserved-keywords, read 2026-11-11), plus
# ASC and DESC, which its preamble names as reserved. A word limited in one place only (a FROM alias,
# a SHOW command, a scalar expression) is quoted everywhere: quoted in its stored case it is the same
# name, so the wider list costs nothing and needs no table of contexts.
RESERVED_KEYWORDS = frozenset({
    "ACCOUNT", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "BETWEEN", "BY", "CASE", "CAST", "CHECK",
    "COLUMN", "CONNECT", "CONNECTION", "CONSTRAINT", "CREATE", "CROSS", "CURRENT", "CURRENT_DATE",
    "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_USER", "DATABASE", "DELETE", "DESC", "DISTINCT",
    "DROP", "ELSE", "EXISTS", "FALSE", "FOLLOWING", "FOR", "FROM", "FULL", "GRANT", "GROUP",
    "GSCLUSTER", "HAVING", "ILIKE", "IN", "INCREMENT", "INNER", "INSERT", "INTERSECT", "INTO", "IS",
    "ISSUE", "JOIN", "LATERAL", "LEFT", "LIKE", "LOCALTIME", "LOCALTIMESTAMP", "MINUS", "NATURAL",
    "NOT", "NULL", "OF", "ON", "OR", "ORDER", "ORGANIZATION", "QUALIFY", "REGEXP", "REVOKE", "RIGHT",
    "RLIKE", "ROW", "ROWS", "SAMPLE", "SCHEMA", "SELECT", "SET", "SOME", "START", "TABLE",
    "TABLESAMPLE", "THEN", "TO", "TRIGGER", "TRUE", "TRY_CAST", "UNION", "UNIQUE", "UPDATE", "USING",
    "VALUES", "VIEW", "WHEN", "WHENEVER", "WHERE", "WINDOW", "WITH",
})


def snowflake_name(text: str) -> str:
    """The naming rule: lowercase, each run of other characters one `_`, never a leading digit, and
    never a keyword. Emitted names stand bare (YAML `name:` fields, the agent's view reference,
    verified query columns), where Snowflake documents no quoting, so a keyword gets a trailing `_`."""
    name = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_") or "unnamed"
    name = f"_{name}" if name[0].isdigit() else name
    return f"{name}_" if name.upper() in RESERVED_KEYWORDS else name


def snowflake_ident(name: str) -> str:
    """A name the model holds, as SQL reaches it under the identifier rule: bare where an unquoted
    spelling folds to it and it is no keyword, else double-quoted in the case it is stored in."""
    if _PLAIN_IDENT.fullmatch(name) and name in (name.lower(), name.upper()):
        stored = name.upper()
        return f'"{stored}"' if stored in RESERVED_KEYWORDS else name
    return '"' + name.replace('"', '""') + '"'


def needs_quotes(name: str) -> bool:
    return snowflake_ident(name) != name


def semantic_view_name(view: ExportView) -> str:
    return snowflake_name(view.area)


def file_stem(view: ExportView) -> str:
    """The view's own name: `<stem>.semantic_view.yaml`, `<stem>.sql`. Two areas that create one view
    (`a-b`, `a_b`) then write one path, which the core refuses, instead of the second view silently
    replacing the first when both are applied. The naming rule also keeps it a plain file name."""
    return semantic_view_name(view)


def view_location(options: ExportOptions) -> Location:
    # The view is created in a schema, and the base tables need a database the model never names.
    if options.location is None:
        raise ExportRefused("the snowflake target creates its semantic view in a schema; pass "
                            "--location <database>.<schema>")
    return options.location


def execution_warehouse(options: ExportOptions) -> Optional[str]:
    """The warehouse `--snowflake-warehouse` names for the agent's SQL, as Snowflake stores it; None
    when not given, since the export never chooses the customer's warehouse. The specification holds
    the name bare and Snowflake documents no quoting there, so only a name an unquoted spelling
    reaches is taken, and it is written upper-case, as stored."""
    name = options.target_args.get("snowflake_warehouse")
    if name is None:
        return None
    if not name or needs_quotes(name):
        raise ExportRefused("--snowflake-warehouse takes a name an unquoted identifier reaches: "
                            "letters, digits, _ and $, not starting with a digit, all one case, "
                            f"and not a keyword; got {name!r}")
    return name.upper()


def semantic_view_fqn(view: ExportView, options: ExportOptions) -> str:
    """`database.schema.view`, as an agent's `tool_resources.<tool>.semantic_view` names it."""
    loc = view_location(options)
    return ".".join(snowflake_ident(p) for p in (loc.database, loc.schema, semantic_view_name(view)))


_VQ_LOST = "carried as an agent sample question without its SQL, not as a verified query:"


def ambiguity_handling(a: Any) -> str:
    """How to answer when an ambiguity's `when` holds: one sentence, for the view and the agent."""
    default = next((o.label for o in a.options if o.default), None)
    labels = ", ".join(o.label for o in a.options)
    if a.behaviour == "ask":
        return f"When {a.when} and the question does not settle it, ask which is meant: {labels}."
    return f"When {a.when}, answer with {default or a.options[0].label}, say so, and offer {labels}."


class _Builder:
    """Builds one semantic view document; every loss it takes is recorded as it is taken."""

    def __init__(self, view: ExportView, options: ExportOptions) -> None:
        self.view, self.loc = view, view_location(options)
        self.certify = certification_tags(options)
        self.losses: list[LossEntry] = []
        self.tables: dict[str, dict] = {}       # logical name -> table document
        self.members: dict[str, dict[str, str]] = {}   # logical -> member name -> section
        self.columns: dict[str, dict[str, str]] = {}   # logical -> casefolded column -> column
        self.column_members: dict[str, dict[str, str]] = {}  # logical -> casefolded column -> member
        self.keys: dict[str, list[set[str]]] = {}      # logical -> keys, columns casefolded
        self.edges: dict[str, set[str]] = {}            # left table -> right tables
        self.relationships: list[dict] = []
        self.view_metrics: list[dict] = []
        self.synonyms: set[str] = set()
        self.agami: dict[str, str] = {}                  # logical -> agami table name
        # Each member that stands for one agami item: (item, section, kind, name), for its tag.
        self.items: list[tuple[dict, str, str, str]] = []

    def drop(self, kind: str, name: str, reason: Reason, detail: str) -> None:
        self.losses.append(dropped(self.view.area, kind, name, reason, detail))

    def degrade(self, kind: str, name: str, reason: Reason, detail: str) -> None:
        self.losses.append(degraded(self.view.area, kind, name, reason, detail))

    def logical(self, agami_table: str) -> Optional[str]:
        name = snowflake_name(bare_name(agami_table))
        return name if name in self.tables else None

    def names_base_table(self, ref: Any, logical: str) -> bool:
        """Whether the parsed reference `ref` (a table, or a column's qualifier as one) is the
        base table under `logical`: by the engine's case rules, and with any schema or database it
        names being the table's own. `archive.orders` is not the view's `orders`, and on Snowflake
        neither is `"orders"`. A table defined by a query has no schema to name."""
        base = self.tables[logical]["base_table"]
        home = (PhysicalName(base["database"], base["schema"], base["table"])
                if "table" in base else None)
        return names_table(ref, bare_name(self.agami[logical]), home, ENGINE)

    def qualifier(self, col: Any) -> Optional[str]:
        """The logical table a column's qualifier names, or None when it names none for certain."""
        from sqlglot import exp

        logical = self.logical(col.table) if col.table else None
        ref = exp.Table(this=col.args.get("table"), db=col.args.get("db"),
                        catalog=col.args.get("catalog"))
        return logical if logical is not None and self.names_base_table(ref, logical) else None

    def add_member(self, table: str, section: str, item: dict, kind: str, agami: str) -> bool:
        if item["name"] in self.members[table]:
            self.drop(kind, agami, "name_collision",
                      f"maps to {table}.{item['name']}, which an earlier item already has")
            return False
        self.members[table][item["name"]] = section
        self.tables[table].setdefault(section, []).append(item)
        return True

    def add_synonyms(self, item: dict, words: list[str], kind: str, agami: str) -> None:
        own = {item["name"].casefold(), agami.casefold()}
        kept: list[str] = []
        for w in dict.fromkeys(w.strip() for w in words if w and w.strip()):
            if w.casefold() in own:
                continue
            if w.casefold() in self.synonyms:
                self.degrade(kind, agami, "target_rule", f"synonym {w!r} left out: synonyms must "
                             "be unique across the view, and an earlier item has it")
                continue
            self.synonyms.add(w.casefold())
            kept.append(w)
        put_nonempty(item, "synonyms", kept)

    def bare_names(self, kind: str, agami: str, field: str, names: list[str]) -> None:
        """Report physical names the YAML holds bare that an unquoted spelling cannot reach."""
        odd = [n for n in dict.fromkeys(names) if needs_quotes(n)]
        if odd:
            self.degrade(kind, agami, "target_rule", f"its {field} {', '.join(odd)} is written as "
                         "stored, but needs quoting in SQL and Snowflake documents no quoting there; "
                         "check it with verify_only")

    # --- tables ------------------------------------------------------------------------------

    def table(self, et: ExportTable, entities: dict[str, list[Any]]) -> None:
        t = et.table
        name = snowflake_name(bare_name(t.name))
        if t.source_type == "sql" and self.view.sql_engine != ENGINE:
            self.drop("table", t.name, "sql_not_for_target",
                      f"defined by a query written for {self.view.source_engine}; SQL is never "
                      "translated")
            return
        if name in self.tables:
            self.drop("table", t.name, "name_collision", f"maps to {name}, as an earlier table does")
            return
        ents = entities.get(bare_name(t.name).casefold(), [])
        doc: dict[str, Any] = {"name": name}
        put_nonempty(doc, "description", join_sentences(
            t.description, *(f"Caveat: {c}" for c in t.caveats),
            *(join_sentences(f"Entity {e.name}:", e.description) for e in ents)))
        if t.source_type == "sql":
            doc["base_table"] = {"definition": t.sql}
        else:
            doc["base_table"] = {"database": et.physical.database or self.loc.database,
                                 "schema": et.physical.schema or self.loc.schema,
                                 "table": et.physical.table}
            self.bare_names("table", t.name, "base table name", list(doc["base_table"].values()))
        self.tables[name], self.members[name], self.agami[name] = doc, {}, t.name
        self.items.append((doc, "table", "table", t.name))
        self.columns[name] = {c.name.casefold(): c.name for c in t.columns}
        self.column_members[name] = {}
        keys = [k for k in (t.grain, *t.unique_keys) if k]
        self.bare_names("table", t.name, "key column", [c for k in keys for c in k])
        self.keys[name] = [{c.casefold() for c in k} for k in keys]
        if t.grain:
            doc["primary_key"] = {"columns": list(t.grain)}
        put_nonempty(doc, "unique_keys", [{"columns": list(k)} for k in t.unique_keys])
        self.add_synonyms(doc, [w for e in ents for w in (e.name, e.plural or "", *e.other_names)],
                          "table", t.name)
        for c in t.columns:
            self.column(name, t.name, c)
        for i, f in enumerate(t.default_filters, 1):
            self.default_filter(name, t.name, i, f)

    def column(self, table: str, agami_table: str, c: Any) -> None:
        qual = f"{agami_table}.{c.name}"
        if c.type in _TIME_TYPES:
            section = "time_dimensions"
        elif c.type in _NUMERIC_TYPES and c.aggregation in ("additive", "averageable"):
            section = "facts"
        else:
            section = "dimensions"
        labels = [f"{k} = {v}" for k, v in (c.choice_field or {}).items()
                  if v.casefold() != k.casefold()]
        item: dict[str, Any] = {"name": snowflake_name(c.name)}
        put_nonempty(item, "description", join_sentences(
            c.description, f"Unit: {c.unit}." if c.unit else "",
            f"Labels: {'; '.join(labels)}." if labels else "",
            *(f"Caveat: {x}" for x in c.caveats)))
        item["expr"] = snowflake_ident(c.name)
        item["data_type"] = _DATA_TYPES[c.type]
        if c.choice_field and section == "dimensions":
            # Stored codes, not labels: a filter compares against what is stored, and `is_enum`
            # tells Snowflake to choose only from these.
            item["sample_values"] = [str(k) for k in c.choice_field]
            item["is_enum"] = True
        if not self.add_member(table, section, item, "column", qual):
            return
        self.items.append((item, section, "column", qual))
        self.column_members[table][c.name.casefold()] = item["name"]
        if c.value_transform:
            self.degrade("column", qual, "detail_lost",
                         "its value_transform is not carried; the view reads the stored value")

    def default_filter(self, table: str, agami_table: str, i: int, sql: str) -> None:
        label = f"{agami_table}#{i}"
        expr = sql.replace("{alias}", table)
        if not parses(expr, ENGINE):
            self.drop("default_filter", label, "unparseable_for_target",
                      "does not parse in Snowflake's dialect")
            return
        # The documented home for a reusable filter is a BOOLEAN dimension labelled `filter`;
        # agami's default filters are descriptive, so it stays one a question chooses to apply.
        item = {"name": f"default_filter_{i}", "description": DEFAULT_FILTER_NOTE, "expr": expr, "data_type": "BOOLEAN", "labels": ["filter"]}
        self.add_member(table, "dimensions", item, "default_filter", label)

    # --- relationships -----------------------------------------------------------------------

    def relationship(self, name: str, r: Any) -> None:
        pairs = self._pairs(r)
        if pairs is None:
            self.drop("relationship", name, "no_home_in_target",
                      "its on: is not an equality of columns, the only join a relationship holds")
            return
        left, right = self.logical(r.from_table), self.logical(r.to_table)
        if r.relationship == "one_to_many":
            left, right, pairs = right, left, [(b, a) for a, b in pairs]
        if left is None or right is None:
            self.drop("relationship", name, "no_home_in_target", "joins a table this view lacks")
            return
        role = left == right
        if r.relationship == "one_to_one" and not self._is_key(right, [b for _, b in pairs]):
            left, right, pairs = right, left, [(b, a) for a, b in pairs]
        missing = [c for t, c in ([(left, a) for a, _ in pairs] + [(right, b) for _, b in pairs])
                   if c.casefold() not in self.columns[t]]
        if missing:
            self.drop("relationship", name, "no_home_in_target",
                      f"joins on a column this view lacks: {', '.join(missing)}")
            return
        if not self._is_key(right, [b for _, b in pairs]):
            self.drop("relationship", name, "target_rule", "a relationship references the other "
                      "table's primary or unique key, so many-to-many joins go through a bridge")
            return
        if not role and self._reaches(right, left):
            self.drop("relationship", name, "target_rule",
                      "no circular relationships, transitive ones included")
            return
        rel_name = snowflake_name(name.replace("->", "_to_"))
        if any(x["name"] == rel_name for x in self.relationships):
            self.drop("relationship", name, "name_collision", f"maps to {rel_name}, already used")
            return
        if role:
            right = self._role_table(name, left, [a for a, _ in pairs])
            if right is None:
                return
        self.bare_names("relationship", name, "join column", [c for p in pairs for c in p])
        # A relationship holds tables and key columns only; what else it says does not travel.
        lost = [f"its {r.join_type} join type"] + (["its description"] if r.description else []) + (
            ["the questions it is for"] if r.for_questions_about else [])
        self.degrade("relationship", name, "detail_lost",
                     f"{', '.join(lost)}: a semantic view relationship has no field for it")
        self.edges.setdefault(left, set()).add(right)
        self.relationships.append({
            "name": rel_name, "left_table": left, "right_table": right,
            "relationship_columns": [{"left_column": a, "right_column": b} for a, b in pairs]})

    def _role_table(self, rel: str, table: str, fk: list[str]) -> Optional[str]:
        """A second logical table on `table`'s base table, for a self-join to reference: "a single
        physical table can appear multiple times under different logical table names" (YAML
        specification, role-playing tables). It holds the same keys and column members, without
        synonyms, which must be unique across the view. Named for the joining columns, `_id` off:
        `employees` joined on `manager_id` gives `employees_manager`."""
        role = re.sub(r"(?<=.)_id$", "", snowflake_name("_".join(fk)))
        name = snowflake_name(f"{table}_{role}")
        if name in self.tables:
            self.drop("relationship", rel, "name_collision", f"its second logical table on "
                      f"{table} maps to {name}, as a table already does")
            return None
        src = self.tables[table]
        doc: dict[str, Any] = {"name": name, "description": (
            f"The {table} row that {table}.{', '.join(fk)} references: {table} again, under a "
            "second name, since a table cannot reference itself.")}
        # Copies, never shared objects: the YAML dump would write a shared one as an anchor and
        # alias, which Snowflake documents nowhere.
        doc.update({k: deepcopy(src[k]) for k in ("base_table", "primary_key", "unique_keys")
                    if k in src})
        self.tables[name], self.members[name], self.agami[name] = doc, {}, self.agami[table]
        self.columns[name], self.keys[name] = self.columns[table], self.keys[table]
        self.items.append((doc, "table", "table", self.agami[table]))
        for item, section, kind, agami in list(self.items):
            if kind == "column" and any(x is item for x in src.get(section, [])):
                copy = {k: deepcopy(v) for k, v in item.items() if k != "synonyms"}
                self.members[name][copy["name"]] = section
                doc.setdefault(section, []).append(copy)
                self.items.append((copy, section, kind, agami))
        self.column_members[name] = {c: m for c, m in self.column_members[table].items()
                                     if m in self.members[name]}
        self.degrade("relationship", rel, "target_rule", f"joins {table} to {name}, a second "
                     "logical table on the same base table: a table cannot reference itself")
        return name

    def _pairs(self, r: Any) -> Optional[list[tuple[str, str]]]:
        if not r.on:
            return [(r.from_column, r.to_column)]
        from sqlglot import exp

        try:
            tree = parse_statement(r.on, ENGINE)
        except Exception:
            return None
        ends = (bare_name(r.from_table).casefold(), bare_name(r.to_table).casefold())
        out = []
        for eq in (tree.flatten() if isinstance(tree, exp.And) else [tree]):
            if not (isinstance(eq, exp.EQ) and isinstance(eq.left, exp.Column)
                    and isinstance(eq.right, exp.Column)):
                return None
            sides = {eq.left.table.casefold(): eq.left.name, eq.right.table.casefold(): eq.right.name}
            if set(sides) != set(ends):
                return None
            out.append((sides[ends[0]], sides[ends[1]]))
        return out

    def _is_key(self, table: str, cols: list[str]) -> bool:
        return {c.casefold() for c in cols} in self.keys[table]

    def _reaches(self, start: str, goal: str) -> bool:
        seen, todo = set(), [start]
        while todo:
            t = todo.pop()
            if t == goal:
                return True
            if t not in seen:
                seen.add(t)
                todo.extend(sorted(self.edges.get(t, ())))
        return False

    # --- metrics -----------------------------------------------------------------------------

    def metric(self, m: Any, sql: str) -> None:
        from sqlglot import exp

        if m.inner_grain:
            self.drop("metric", m.name, "no_home_in_target", "an aggregate of an aggregate at an "
                      "inner grain needs level-of-detail expressions, which this export omits")
            return
        tree = parse_statement(sql, ENGINE)   # the core parsed it in this dialect already
        if not any(True for _ in tree.find_all(exp.AggFunc)):
            self.drop("metric", m.name, "target_rule", "a metric that is not derived must aggregate")
            return
        cols = list(tree.find_all(exp.Column))
        foreign = sorted({c.table for c in cols if c.table and self.logical(c.table)
                          and self.qualifier(c) is None})
        if foreign:
            self.drop("metric", m.name, "target_rule", f"references resolve: it names "
                      f"{', '.join(foreign)}, which is no table of the view (another schema's "
                      "table, or a name the view's table does not answer to)")
            return
        refs = [(c.table, c.name) for c in cols]
        tables = sorted({t for t in (self.logical(q) for q, _ in refs if q) if t})
        home = self._home(m, tables)
        if home is None:
            if not self._derived(m, sql, tree):
                self.drop("metric", m.name, "target_rule", "expressions read only related tables: "
                          "no table it names relates to all the others")
            return
        unresolved = sorted({f"{q}.{n}" if q else n for q, n in refs
                             if not self._resolves(self.logical(q) if q else home, n, home)})
        if unresolved:
            self.drop("metric", m.name, "target_rule", f"references resolve: it names "
                      f"{', '.join(unresolved)}, which the view does not declare")
            return
        item = _metric_item(m)
        item["expr"] = self._logical_qualifiers(sql, tree)
        put_nonempty(item, "non_additive_dimensions", self._non_additive(m, home))
        if self.add_member(home, "metrics", item, "metric", m.name):
            self.items.append((item, "metrics", "metric", m.name))
            self.add_synonyms(item, list(m.other_names), "metric", m.name)

    def _derived(self, m: Any, sql: str, tree: Any) -> bool:
        """A metric no one table can hold, as a view-level (derived) metric, whose expression "can
        use aggregations of dimensions and facts defined in any logical table" (Defining derived
        metrics). Taken only when every column is a qualified member of the view read inside an
        aggregate; each becomes `<logical table>.<member>`. False: it is no such metric."""
        from sqlglot import exp

        # "Aggregations of dimensions and facts": each aggregate reads one logical table, and
        # only the view-level expression combines them. `SUM(stores.size * regions.area)` would
        # pair rows of two tables no relationship relates.
        for agg in tree.find_all(exp.AggFunc):
            if len({self.logical(c.table) for c in agg.find_all(exp.Column)}) > 1:
                return False
        spans = {}
        for c in tree.find_all(exp.Column):
            table = self.logical(c.table) if c.table else None
            member = self.column_members[table].get(c.name.casefold()) if table else None
            if member is None or c.find_ancestor(exp.AggFunc) is None:
                return False
            parts = (c.args.get("catalog"), c.args.get("db"), c.args["table"], c.this)
            spans[token_span(*parts)] = f"{table}.{member}"
        item = _metric_item(m)
        taken = {n for t in self.members.values() for n, s in t.items() if s == "metrics"}
        if item["name"] in taken | {x["name"] for x in self.view_metrics}:
            # "You cannot use the same name for a derived metric and a regular metric."
            self.drop("metric", m.name, "name_collision", f"maps to {item['name']}, which an "
                      "earlier metric already has")
            return True
        item["expr"] = _splice(sql, spans)
        if m.non_additive_dimensions:
            self.degrade("metric", m.name, "detail_lost", "its non-additive dimensions: a "
                         "view-level metric has no non_additive_dimensions field")
        self.view_metrics.append(item)
        self.items.append((item, "metrics", "metric", m.name))
        self.add_synonyms(item, list(m.other_names), "metric", m.name)
        return True

    def _logical_qualifiers(self, sql: str, tree: Any) -> str:
        """`sql` with each table qualifier spelt as the logical table it resolved to, spliced by
        token position. A binding names the base table (`"Sales Orders".amount`, `"ORDER".id`), and
        a view expression qualifies by logical table name, which the naming rule can change."""
        from sqlglot import exp

        spans = {}
        for c in tree.find_all(exp.Column):
            ident = c.args.get("table")
            if ident is None:
                continue
            logical = self.logical(c.table)
            if c.args.get("db") or ident.quoted or c.table != logical:
                spans[token_span(c.args.get("catalog"), c.args.get("db"), ident)] = logical
        return _splice(sql, spans)

    def _home(self, m: Any, tables: list[str]) -> Optional[str]:
        # The metric sits on a table every other table it reads is reachable from, i.e. the finest
        # grain, since a single aggregate may read only equal or coarser grains.
        named = [self.logical(x) for x in (m.primary_table or "", *m.source_tables) if x]
        for t in dict.fromkeys([*filter(None, named), *tables]):
            if all(self._reaches(t, o) for o in tables):
                return t
        return None

    def _resolves(self, table: Optional[str], column: str, home: str) -> bool:
        # Another table is read through its semantic expressions; the home table also by column.
        if table is None:
            return False
        own = table == home and column.casefold() in self.columns[table]
        return own or column.casefold() in {n.casefold() for n in self.members[table]}

    def _non_additive(self, m: Any, home: str) -> list[dict]:
        out = []
        for dim in m.non_additive_dimensions:
            found = self._dimension(dim, home)
            if found is None:
                self.degrade("metric", m.name, "detail_lost",
                             f"its non-additive dimension {dim!r} has no one dimension in the view")
                continue
            out.append({"table": found[0], "dimension": found[1],
                        "sort_direction": _SORT.get(m.semi_additive_agg or "", "ascending")})
        if out and (m.semi_additive_agg or "") not in _SORT:
            self.degrade("metric", m.name, "detail_lost", "Snowflake keeps the latest value over a "
                         f"non-additive dimension; agami says {m.semi_additive_agg or 'refuse'}")
        return out

    def _dimension(self, dim: str, home: str) -> Optional[tuple[str, str]]:
        for t in sorted(self.tables, key=lambda x: x != home):
            if not self._reaches(home, t):
                continue
            if dim == "time":
                times = [n for n, s in self.members[t].items() if s == "time_dimensions"]
                if t == home and len(times) == 1:
                    return t, times[0]
                continue
            name = snowflake_name(dim)
            if self.members[t].get(name) in ("dimensions", "time_dimensions"):
                return t, name
        return None

    # --- questions and instructions ----------------------------------------------------------

    def logical_sql(self, sql: str) -> LogicalSql:
        """`sql` on the view's logical table and column names, as a verified query holds it, or
        None with the reason and why not. Called once the view is built, by its verified queries
        and by the agent's expected tool output (ACE-169)."""
        try:
            tree = parse_statement(sql, ENGINE)
        except Exception:
            return LogicalSql(None, "unparseable_for_target",
                              "it does not parse in Snowflake's dialect")
        try:
            return LogicalSql(_LogicalSql(self, sql).rewrite(tree), None, "")
        except _NoRewrite as why:
            return LogicalSql(None, why.reason, why.detail)
        except Exception:   # a shape the scope analysis cannot place: never emit a guess
            return LogicalSql(None, "target_rule", "its names could not all be placed on the "
                              "view's logical tables and columns")

    def verified_query(self, q: ExportQuestion, names: set[str]) -> Optional[dict]:
        # Every example's question travels as an agent sample question (ACE-169), so one that
        # cannot be a verified query loses only its SQL: degraded, and nothing when it had none.
        if q.sql is None:
            return None
        sql, reason, why = self.logical_sql(q.sql)
        if sql is None:
            self.degrade(q.kind, q.name, reason, f"{_VQ_LOST} {why}")
            return None
        name = snowflake_name(q.name)
        if name in names:
            self.degrade(q.kind, q.name, "name_collision", f"{_VQ_LOST} it maps to {name}, "
                         "already used")
            return None
        names.add(name)
        out: dict[str, Any] = {"name": name, "question": q.question}
        # When it was signed off, never who: the signer stays in agami (DOES_NOT_TRAVEL).
        put_nonempty(out, "verified_at", self._verified_at(q))
        out["sql"] = sql
        return out

    def _verified_at(self, q: ExportQuestion) -> Optional[int]:
        at = self.view.signed_off_at(q.kind, q.name)
        if at is None:
            return None
        text = at.strip()
        try:
            when = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            self.degrade(q.kind, q.name, "detail_lost", f"its sign-off time {at!r} is not an ISO "
                         "8601 time, so the verified query has no verified_at")
            return None
        # The model writes times in UTC; one with no offset is read as UTC too.
        return int((when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp())

    def instructions(self) -> dict[str, str]:
        gen, ask = [], []
        for a in self.view.ambiguities:
            default = next((o.label for o in a.options if o.default), None)
            opts = "; ".join(f"{o.label}: {o.how}" for o in a.options)
            gen.append(join_sentences(
                f"{a.name}: when {a.when}, the options are {opts}.",
                f"Use {default} unless the question says otherwise." if default else ""))
            ask.append(ambiguity_handling(a))
            self.degrade("ambiguity", a.name, "carried_as_text",
                         "carried as Cortex Analyst instruction text; the view has no field for it")
        gen += self._conventions()
        out: dict[str, str] = {}
        put_nonempty(out, "sql_generation", "\n".join(gen))
        put_nonempty(out, "question_categorization", "\n".join(ask))
        return out

    def _conventions(self) -> list[str]:
        """The datasource's fiscal year and the area's default time window, in the words of
        Snowflake's own examples ("Always use fiscal year (April-March) ...", "If no date filter is
        provided, apply a filter for the last year."). Key terms and caveats are not repeated
        here: the view's and each item's description already carry them."""
        v, out = self.view, []
        if v.fiscal_year_start_month != 1:
            start = MONTH_NAMES[v.fiscal_year_start_month - 1]
            end = MONTH_NAMES[v.fiscal_year_start_month - 2]
            out.append(f"Use fiscal year ({start}-{end}) when a question asks for a fiscal year "
                       f"or fiscal quarter: the fiscal year starts in {start}.")
            self.degrade("datasource", v.datasource, "carried_as_text", "its fiscal year start is "
                         "carried as Cortex Analyst instruction text; the view has no field for it")
        if v.default_time_window:
            out.append("If no date filter is provided, apply this default time window: "
                       f"{v.default_time_window.strip()}.")
            self.degrade("subject_area", v.area, "carried_as_text", "its default time window is "
                         "carried as Cortex Analyst instruction text; the view has no field for it")
        return out

    # --- the document ------------------------------------------------------------------------

    def build(self) -> Optional[dict]:
        v = self.view
        entities = self._entity_homes()
        for et in v.tables:
            self.table(et, entities)
        for er in v.relationships:
            self.relationship(er.name, er.relationship)
        for em in v.metrics:
            self.metric(em.metric, em.sql[ENGINE])
        names: set[str] = set()
        queries = [(q, x) for q in v.examples if (x := self.verified_query(q, names))]
        for _, x in [p for p in queries if v.approved(p[0].kind, p[0].name)][:ONBOARDING_QUESTIONS]:
            sql = x.pop("sql")   # the specification's order: the flag, then the SQL
            x.update(use_as_onboarding_question=True, sql=sql)
        sections = ("dimensions", "time_dimensions", "metrics")
        if not self.view_metrics and not any(
                t.get(s) for t in self.tables.values() for s in sections):
            self.drop("subject_area", v.area, "target_rule",
                      "a semantic view needs at least one dimension or metric")
            return None
        doc: dict[str, Any] = {"name": semantic_view_name(v)}
        terms = "; ".join(f"{t}: {d}" for t, d in v.terminology)
        put_nonempty(doc, "description", join_sentences(v.description, v.area_description,
                                                        f"Key terms: {terms}." if terms else ""))
        doc["tables"] = list(self.tables.values())
        put_nonempty(doc, "relationships", self.relationships)
        put_nonempty(doc, "metrics", self.view_metrics)
        put_nonempty(doc, "verified_queries", [x for _, x in queries])
        put_nonempty(doc, "module_custom_instructions", self.instructions())
        self._certification(doc)
        return doc

    def _certification(self, doc: dict) -> None:
        """Snowflake's CERTIFICATION_STATUS tag from agami's review state, under the flag: CERTIFIED
        for an approved item, TO BE REVIEWED for one --include-unreviewed carried. A table is
        CERTIFIED only when it and every column it carries are approved, and the view only when
        every item carried for it is approved and it carries no key term or ambiguity, which agami
        holds with no review state. A time dimension has no `tags` field in the YAML specification,
        so it goes untagged and the report says so."""
        v = self.view
        if not self.certify:
            self.degrade("subject_area", v.area, "detail_lost", "review state is not written as "
                         "SNOWFLAKE.TAGS.CERTIFICATION_STATUS tags; --snowflake-certification-tags "
                         f"writes {CERTIFIED} on approved items and {TO_BE_REVIEWED} on ones "
                         "--include-unreviewed carried, and applying them needs the "
                         "SNOWFLAKE.OOB_TAG_APPLY application role (only ACCOUNTADMIN by default)")
            return
        untagged = 0
        # A table is certified only with every column it carries: CERTIFIED on a table holding a
        # column nobody reviewed would vouch for that column too.
        whole = {et.table.name: v.approved("table", et.table.name) and all(
            v.approved("column", f"{et.table.name}.{c.name}") for c in et.table.columns)
            for et in v.tables}
        for item, section, kind, name in self.items:
            if section == "time_dimensions":
                untagged += 1
                continue
            ok = whole.get(name, False) if kind == "table" else v.approved(kind, name)
            if kind == "table" and v.approved(kind, name) and not ok:
                self.degrade("table", name, "not_approved", f"tagged {TO_BE_REVIEWED}, not "
                             f"{CERTIFIED}: it carries a column --include-unreviewed carried")
            item["tags"] = [_certification_tag(ok)]
        # The view's description and instructions also carry key terms and ambiguities, which
        # agami holds with no review state: nobody recorded a review of them to certify.
        unrecorded = [what for what, kind, names in (
            ("key terms", "terminology", [t for t, _ in v.terminology]),
            ("ambiguities", "ambiguity", [a.name for a in v.ambiguities]))
            if any(not v.reviewed(kind, n) for n in names)]
        doc["tags"] = [_certification_tag(not v.unreviewed and not unrecorded)]
        if unrecorded and not v.unreviewed:
            self.degrade("subject_area", v.area, "not_approved", f"the view is tagged "
                         f"{TO_BE_REVIEWED}, not {CERTIFIED}: it carries {' and '.join(unrecorded)}"
                         ", which agami holds with no review state, so no review of them is "
                         "recorded")
        if untagged:
            self.degrade("subject_area", v.area, "detail_lost", f"its {untagged} time dimension(s) "
                         "carry no certification tag: a time dimension has no tags field")

    def _entity_homes(self) -> dict[str, list[Any]]:
        """Each entity goes to the table it primarily maps to, as synonyms and description."""
        held = {bare_name(et.table.name).casefold() for et in self.view.tables}
        out: dict[str, list[Any]] = {}
        for e in self.view.entities:
            maps = sorted(e.maps_to, key=lambda mp: not mp.primary)
            home = next((bare_name(mp.table).casefold() for mp in maps
                         if bare_name(mp.table).casefold() in held), None)
            if home is None:
                self.drop("entity", e.name, "no_home_in_target", "maps to no table this view has")
            else:
                out.setdefault(home, []).append(e)
        return out


def _metric_item(m: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"name": snowflake_name(m.name)}
    put_nonempty(item, "description", join_sentences(
        m.description, f"Calculation: {m.calculation}", f"Unit: {m.unit}." if m.unit else "",
        *(f"Caveat: {c}" for c in m.caveats)))
    return item


def _certification_tag(approved: bool) -> dict:
    return {"name": dict(CERTIFICATION_TAG), "value": CERTIFIED if approved else TO_BE_REVIEWED}


def certification_tags(options: ExportOptions) -> bool:
    return bool(options.target_args.get("snowflake_certification_tags"))


class LogicalSql(NamedTuple):
    """SQL rewritten onto a semantic view's logical names; or `sql` None, with the loss reason and
    why the rewrite was refused."""

    sql: Optional[str]
    reason: Optional[Reason]
    why: str


class _NoRewrite(Exception):
    """A verified query that cannot be put onto the view's logical names with certainty."""

    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def _splice(text: str, spans: dict[tuple[int, int], str]) -> str:
    """`splice_spans`, refusing the rewrite when two spans overlap."""
    out = splice_spans(text, spans)
    if out is None:
        raise _NoRewrite("target_rule", "two of its names overlap in the text")
    return out


class _LogicalSql:
    """One verified query rewritten onto the view's logical names. Snowflake's verified query
    repository page: "Verified SQL queries must use the names of the logical tables and columns
    defined in the semantic model, not those in the underlying dataset", a logical table written
    `__<name>`. So a base table becomes `__<logical>` (and so does a column qualifier that was its
    name), and a base column becomes the member the view holds it as.

    Each name is placed through sqlglot's scopes, so a CTE or subquery that shadows a table, an alias
    and a correlated reference resolve as Snowflake would resolve them. The new names are spliced
    into the text by token position, never regenerated from the tree, so every other byte is as a
    person wrote it. Anything not placed with certainty raises `_NoRewrite`."""

    def __init__(self, b: _Builder, sql: str) -> None:
        self.b, self.sql = b, sql
        self.spans: dict[tuple[int, int], str] = {}
        self.physical: dict[int, str] = {}     # id of a base table reference -> its logical table
        self.seen: set[int] = set()            # ids of the column references placed
        self.renamed: set[int] = set()         # ids of the column references given a new name

    def rewrite(self, tree: Any) -> str:
        from sqlglot.optimizer.scope import traverse_scope

        self._no_star(tree)
        scopes = traverse_scope(tree)
        for scope in scopes:
            self._tables(scope)
        self._every_table_placed(tree)
        for scope in scopes:
            for col in scope.columns:
                if id(col) not in self.seen:
                    self.seen.add(id(col))
                    self._column(scope, col)
        self._every_column_placed(tree)
        self._outputs_keep_their_names(scopes)
        self._using(tree)
        return _splice(self.sql, self.spans)

    def _no_star(self, tree: Any) -> None:
        from sqlglot import exp

        for select in tree.find_all(exp.Select):
            for p in select.selects:
                if isinstance(p, exp.Star) or (isinstance(p, exp.Column)
                                               and isinstance(p.this, exp.Star)):
                    raise _NoRewrite("target_rule", "it selects *, and the view's columns are not "
                                     "the base table's")

    def _tables(self, scope: Any) -> None:
        from sqlglot import exp

        for src in scope.sources.values():
            if not isinstance(src, exp.Table) or id(src) in self.physical:
                continue
            logical = self.b.logical(src.name)
            if logical is None or not self.b.names_base_table(src, logical):
                # `archive.orders` or `"orders"` is another table than the view's `orders`.
                raise _NoRewrite("no_home_in_target", f"it reads {src.name}, a table this view "
                                 "lacks (another schema's, or a name its table does not answer to)")
            self.physical[id(src)] = logical
            parts = (src.args.get("catalog"), src.args.get("db"), src.this)
            self.spans[token_span(*parts)] = f"__{logical}"

    def _every_table_placed(self, tree: Any) -> None:
        from sqlglot import exp

        ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if id(t) not in self.physical and not (t.name.casefold() in ctes and not t.db):
                raise _NoRewrite("target_rule", f"it reads {t.name} where no scope places it")

    def _column(self, scope: Any, col: Any) -> None:
        from sqlglot import exp

        src = self._qualified(scope, col) if col.table else self._unqualified(scope, col)
        if not isinstance(src, exp.Table):
            return  # a CTE's or subquery's output, or a select alias: its name is the query's own
        logical = self.physical[id(src)]
        member = self.b.column_members[logical].get(col.name.casefold())
        if member is None:
            raise _NoRewrite("no_home_in_target", f"it reads {src.name}.{col.name}, a column the "
                             "view does not carry")
        # A quoted name keeps its case in Snowflake, and logical names are unquoted.
        if member != col.name or col.this.quoted:
            self.spans[token_span(col.this)] = member
            self.renamed.add(id(col))
        if col.table and not src.alias:
            # Qualified by the table's own name, which the rewrite replaced.
            parts = (col.args.get("catalog"), col.args.get("db"), col.args["table"])
            self.spans[token_span(*parts)] = f"__{logical}"

    def _qualified(self, scope: Any, col: Any) -> Any:
        s = scope
        while s is not None:
            hits = [v for k, v in s.sources.items() if k.casefold() == col.table.casefold()]
            if len(hits) > 1:
                raise _NoRewrite("target_rule", f"its qualifier {col.table} names two sources")
            if hits:
                return hits[0]
            s = s.parent
        raise _NoRewrite("target_rule", f"its qualifier {col.table} is no table or alias in scope")

    def _unqualified(self, scope: Any, col: Any) -> Any:
        from sqlglot import exp

        name = col.name.casefold()
        hits = []
        for src in scope.sources.values():
            if isinstance(src, exp.Table):
                if name in self.b.columns[self.physical[id(src)]]:
                    hits.append(src)
            elif name in {n.casefold() for n in getattr(src.expression, "named_selects", [])}:
                hits.append(src)
        if len(hits) == 1:
            return hits[0]
        if not hits and not col.find_ancestor(exp.Where) and is_select_alias(col, scope.expression):
            # Snowflake lets a select alias stand in a later clause. Not in WHERE, where a column
            # of the same name comes first; and a bare output (`SELECT region, salary`) is no alias.
            return None
        raise _NoRewrite("target_rule", f"its unqualified column {col.name} could come from "
                         f"{'several sources' if hits else 'no source in its scope'}")

    def _every_column_placed(self, tree: Any) -> None:
        # Scopes leave out an ORDER BY or QUALIFY reference to a select alias; anything else they
        # leave out is a column this rewrite has not placed.
        from sqlglot import exp

        for col in tree.find_all(exp.Column):
            if id(col) in self.seen or isinstance(col.this, exp.Star):
                continue
            select = col.find_ancestor(exp.Select)
            aliases = {n.casefold() for n in select.named_selects} if select else set()
            if col.table or col.name.casefold() not in aliases:
                raise _NoRewrite("target_rule", f"its column {col.name} is in no scope")

    def _outputs_keep_their_names(self, scopes: list) -> None:
        # A CTE or subquery that selects a renamed column unaliased would rename its own output
        # under every reader of it.
        from sqlglot import exp

        for scope in scopes:
            if not (scope.is_cte or scope.is_derived_table):
                continue
            for p in getattr(scope.expression, "selects", []):
                if isinstance(p, exp.Column) and id(p) in self.renamed:
                    raise _NoRewrite("target_rule", f"a subquery's output {p.name} would change "
                                     "name; alias it to keep the name")

    def _using(self, tree: Any) -> None:
        from sqlglot import exp

        tables = set(self.physical.values())
        for join in tree.find_all(exp.Join):
            for ident in join.args.get("using") or []:
                names = {self.b.column_members[t].get(ident.name.casefold()) for t in tables}
                if any(n is not None and (n != ident.name or ident.quoted) for n in names):
                    raise _NoRewrite("target_rule", f"its USING column {ident.name} would change "
                                     "name")


class SemanticView(NamedTuple):
    """One built semantic view: its document (None when nothing in the area can make one), the
    losses the mapping took, and its rewrite onto the view's logical names."""

    doc: Optional[dict]
    losses: list[LossEntry]
    logical_sql: Callable[[str], LogicalSql]

    def verified_questions(self) -> list[str]:
        """The `question` of each verified query the view holds, in its order, each once."""
        queries = (self.doc or {}).get("verified_queries", [])
        return list(dict.fromkeys(q["question"] for q in queries))


def semantic_view(view: ExportView, options: ExportOptions) -> SemanticView:
    b = _Builder(view, options)
    return SemanticView(b.build(), b.losses, b.logical_sql)


def build_semantic_view(view: ExportView,
                        options: ExportOptions) -> tuple[Optional[dict], list[LossEntry]]:
    """The semantic view document for `view` (None when nothing in it can make one), and the losses
    the mapping took."""
    built = semantic_view(view, options)
    return built.doc, built.losses


def render_yaml(doc: dict) -> str:
    import yaml

    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def snowflake_literal(text: str) -> str:
    """`text` as a single-quoted Snowflake string constant. A backslash starts an escape sequence
    inside one, so it is doubled, and so is a single quote."""
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _sql_string(text: str) -> str:
    # A dollar-quoted constant keeps the YAML readable; it cannot hold `$$`, so such text falls
    # back to an escaped single-quoted constant.
    if "$$" not in text:
        return f"$$\n{text}$$"
    return snowflake_literal(text)


def create_call(view: ExportView, options: ExportOptions, yaml_text: str) -> str:
    loc = view_location(options)
    schema = ".".join(snowflake_ident(p) for p in loc)
    tags = ("-- It tags the view with SNOWFLAKE.TAGS.CERTIFICATION_STATUS: the role running it needs "
            "the\n-- SNOWFLAKE.OOB_TAG_APPLY application role. CREATE OR ALTER semantics (TRUE as a "
            "fourth\n-- argument) do not add or change tags; existing ones are kept.\n"
            if certification_tags(options) else "")
    return (f"-- Snowflake semantic view {semantic_view_fqn(view, options)}, written by "
            "`sm export`. Running this\n-- creates or replaces it. To check it without creating "
            f"it, add TRUE as a third argument\n-- (verify_only).\n"
            "-- Privileges. The role running this needs USAGE on the database and schema, CREATE "
            f"SEMANTIC VIEW\n-- on {schema}, and SELECT on each table the view reads. A role "
            f"that queries the view needs\n-- SELECT on the view.\n{tags}"
            "CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(\n"
            f"  '{schema.replace(chr(39), chr(39) * 2)}',\n  {_sql_string(yaml_text)}\n);\n")


def write_snowflake(view: ExportView, options: ExportOptions) -> WriterOutput:
    execution_warehouse(options)   # refused before any file, whether or not an agent is written
    built = semantic_view(view, options)
    doc, losses = built.doc, built.losses
    if doc is None:
        return WriterOutput({}, tuple(losses))
    text = render_yaml(doc)
    stem = file_stem(view)
    # Imported here: the agent module builds on this one's names, and binds to the view only
    # once there is one to bind to.
    from .export_snowflake_agent import write_agent

    agent = write_agent(view, options, built)
    return WriterOutput({f"{stem}.semantic_view.yaml": text.encode("utf-8"),
                         f"{stem}.sql": create_call(view, options, text).encode("utf-8"),
                         **agent.files}, (*losses, *agent.losses),
                        agent.carried_golden_fields)


def add_arguments(sp: argparse.ArgumentParser) -> None:
    # Suppressed unless given, as every target flag is: the report records only what was asked.
    sp.add_argument(
        "--snowflake-warehouse",
        dest="snowflake_warehouse",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="warehouse the --target snowflake agent runs its SQL on; without it the agent uses "
        "the caller's default warehouse, and does not run for a caller with none",
    )
    sp.add_argument(
        "--snowflake-certification-tags",
        dest="snowflake_certification_tags",
        action="store_true",
        default=argparse.SUPPRESS,
        help="tag the semantic view and its tables, dimensions, facts and metrics with "
        "SNOWFLAKE.TAGS.CERTIFICATION_STATUS from agami's review state (CERTIFIED, or TO BE "
        "REVIEWED under --include-unreviewed); the role creating the view needs the "
        "SNOWFLAKE.OOB_TAG_APPLY application role",
    )
    # The agent, evaluation and MCP files declare their own flags beside the code that reads them.
    from .export_snowflake_agent import add_agent_arguments

    add_agent_arguments(sp)


register(Target(TARGET, (ENGINE,), write_snowflake, add_arguments))
