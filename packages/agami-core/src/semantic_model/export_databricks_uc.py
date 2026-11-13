"""`databricks` target, Unity Catalog DDL (F19 / ACE-175): comments, keys and certification.

Genie reads a table's Unity Catalog comment and its primary and foreign keys before anything a
space adds, saves the keys as join relationships, and ranks certified assets first; so do Genie
One, Genie Code and any agent reading the catalog. This artifact writes what agami holds of those
as one SQL script per subject area, `unity_catalog/<area>.sql`, for the customer to review and
run, and `unity_catalog/<area>_undo.sql`, which reverses it statement by statement.

Unlike every other file of the export it changes the customer's catalog metadata, so each of its
three sections is asked for by its own flag, and with none of them no file is written:

* `--uc-comments`: `COMMENT ON TABLE` / `COMMENT ON COLUMN` from descriptions, units and caveats.
* `--uc-constraints`: informational primary and foreign keys, with the NOT NULL a key needs.
* `--uc-certify`: the `system.certification_status` tag on tables approved with every column
  they carry, and on the metric views of this export whose every measure, table, column and join
  is approved and that hold no level-of-detail measure (their file asks for a comparison with
  agami's answer first).

Three flags rather than one list flag: each section carries a different risk and needs different
privileges, so each is its own explicit decision, and each reads plainly in the report's options.

A table two subject areas carry gets its statements once, in the file of the first area by name
(`ExportView.claimed_by`): the second copy of `ADD CONSTRAINT` or `SET TAG` would fail, and either
undo file would then remove what the other created. Each undo file reverses only its own file, and
only the statements whose apply succeeded may be run: every constraint name starts `agami_`, so a
constraint of that name that existed before is implausible. The certification section opens with a
check per schema that stops it when an object it tags already carries the certification tag, and
the tag removals are commented out, since UNSET TAG removes the tag whoever set it.

Reads only the `ExportView`. A table defined by a query has no catalog object and gets nothing;
a sensitive column is not in the view, so nothing here can name it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Optional

from . import export_databricks as DB
from .export import (
    ExportOptions,
    ExportTable,
    ExportView,
    LossEntry,
    WriterOutput,
    degraded,
    join_sentences,
)
from .models import bare_name

UC_DIR = "unity_catalog"
# Section -> the argparse dest of its flag, in the order the script runs them.
SECTIONS: dict[str, str] = {"comments": "uc_comments", "constraints": "uc_constraints",
                            "certify": "uc_certify"}
# The governed system tag and value of "Flag data as certified or deprecated".
CERTIFICATION_TAG = "`system.certification_status`"
CERTIFIED = "`certified`"
MAX_NAME = 255  # an identifier's length limit
# Every constraint this file adds is named `agami_…`, so its undo drops only what agami named.
CONSTRAINT_PREFIX = "agami"

# What the regular-literal escapes of Databricks' STRING type stand for. `$` is escaped too: a
# notebook reads `$name` as a widget, and `\$` is `$` everywhere else.
_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0",
            "\b": "\\b", "\x1a": "\\Z", "$": "\\$"}


def ddl_path(area: str) -> str:
    return f"{UC_DIR}/{DB.databricks_name(area)}.sql"


def undo_path(area: str) -> str:
    return f"{UC_DIR}/{DB.databricks_name(area)}_undo.sql"


def sql_string(text: str) -> str:
    """`text` as a Databricks SQL string literal. Other control characters become a space: a
    comment is read as one line, and the literal has no escape for them."""
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(" ")
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


# ---------------------------------------------------------------------------
# Building the statements
# ---------------------------------------------------------------------------


@dataclass
class _Script:
    """Each section's statements, each paired with the one that reverses it."""

    view: ExportView
    options: ExportOptions
    losses: list[LossEntry] = field(default_factory=list)
    pairs: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.keyed: list[tuple[Optional[str], Optional[str], str]] = []   # given a primary key here
        self.tagged: list[tuple[Optional[str], Optional[str], str]] = []  # certified here
        self.tables: dict[str, ExportTable] = {}   # every catalog table the view carries
        self.owned: dict[str, ExportTable] = {}    # those whose statements this file writes
        self.elsewhere: set[str] = set()           # areas whose files declare a key used here
        for t in self.view.tables:
            if t.table.source_type == "sql":
                if self.asked():
                    self.degrade("table", t.table.name, "no_home_in_target", "defined by a query, "
                                 "so Unity Catalog holds no object to comment, key or certify")
                continue
            key = bare_name(t.table.name).casefold()
            self.tables[key] = t
            first = self.view.claimed_by(t.physical)
            if first is None:
                self.owned[key] = t
            elif self.asked():
                self.degrade("table", t.table.name, "target_rule", "its Unity Catalog statements "
                             f"are in subject area {first}'s file ({ddl_path(first)}): a table two "
                             "subject areas carry is written once, by the first area by name, "
                             "since a second ADD CONSTRAINT or SET TAG would fail")

    def asked(self) -> list[str]:
        return [s for s, dest in SECTIONS.items() if self.options.target_args.get(dest)]

    def degrade(self, kind: str, name: str, reason: Any, detail: str) -> None:
        self.losses.append(degraded(self.view.area, kind, name, reason, detail))

    def add(self, section: str, apply: str, undo: str) -> None:
        self.pairs.setdefault(section, []).append((apply, undo))

    def name(self, t: ExportTable) -> str:
        return DB._dotted(*self.parts(t))

    def parts(self, t: ExportTable) -> tuple[Optional[str], Optional[str], str]:
        p = t.physical
        return p.database, p.schema, p.table

    # -- comments -----------------------------------------------------------

    def comments(self) -> None:
        for t in self.owned.values():
            tname = self.name(t)
            text = join_sentences(t.table.description,
                                  *(f"Caveat: {c}" for c in t.table.caveats))
            if text:
                self.add("comments", f"COMMENT ON TABLE {tname} IS {sql_string(text)};",
                         f"COMMENT ON TABLE {tname} IS NULL;")
            for c in t.table.columns:
                text = join_sentences(c.description, f"Unit: {c.unit}." if c.unit else "",
                                      *(f"Caveat: {x}" for x in c.caveats))
                if text:
                    col = f"{tname}.{DB._quote(c.name)}"
                    self.add("comments", f"COMMENT ON COLUMN {col} IS {sql_string(text)};",
                             f"COMMENT ON COLUMN {col} IS '';")

    # -- constraints --------------------------------------------------------

    def key(self, t: ExportTable) -> Optional[list[str]]:
        """The table's primary key as its columns' names, when its grain names whole columns."""
        cols = {c.name.casefold(): c.name for c in t.table.columns}
        key = [cols.get(g.casefold()) for g in t.table.grain]
        if not key or None in key or len(set(key)) != len(key):
            return None
        return key  # type: ignore[return-value]

    def constraint_name(self, *parts: str) -> Optional[str]:
        name = "__".join([CONSTRAINT_PREFIX, *(DB.databricks_name(p) for p in parts)])
        return name if len(name) <= MAX_NAME else None

    def constraints(self) -> None:
        keys: dict[str, list[str]] = {}
        taken: dict[str, str] = {}
        for k, t in self.tables.items():
            key = self.key(t)
            if key is None:
                continue
            name = self.constraint_name(t.table.name, "pk")
            if name is None or name in taken:
                if k in self.owned:
                    self.degrade("table", t.table.name, "name_collision" if name else "target_limit",
                                 "its primary key is not declared: its constraint name "
                                 + (f"{name} is taken by {taken[name]}" if name else "is too long"))
                continue
            taken[name] = t.table.name
            keys[k] = key
            if k not in self.owned:
                continue   # declared by the area that owns the table; a foreign key may use it
            alter = f"ALTER TABLE {self.name(t)}"
            self.keyed.append(self.parts(t))
            for c in key:  # a primary key column must be NOT NULL
                col = f"{alter} ALTER COLUMN {DB._quote(c)}"
                self.add("constraints", f"{col} SET NOT NULL;", f"{col} DROP NOT NULL;")
            cols = ", ".join(DB._quote(c) for c in key)
            self.add("constraints", f"{alter} ADD CONSTRAINT {name} PRIMARY KEY ({cols});",
                     f"{alter} DROP CONSTRAINT IF EXISTS {name};")
        declared: set[tuple[str, str]] = set()
        for er in self.view.relationships:
            fk = self.foreign_key(er, keys)
            if fk is None:
                continue
            child, column, parent, parent_column = fk
            ct = self.tables[child]
            first = self.view.claimed_by(ct.physical)
            if first is not None:
                self.degrade("relationship", er.name, "target_rule", "its foreign key is not "
                             f"declared here: its table's statements are in subject area {first}'s "
                             "file, which declares it only if that area holds this relationship")
                continue
            owner = self.view.claimed_by(self.tables[parent].physical)
            if owner is not None:
                self.elsewhere.add(owner)
            name = self.constraint_name(ct.table.name, column, "fk")
            if (child, column.casefold()) in declared:
                self.degrade("relationship", er.name, "target_rule", "a foreign key on the same "
                             "column is declared already, and Unity Catalog takes one per column set")
                continue
            if name is None or name in taken:
                self.degrade("relationship", er.name, "name_collision" if name else "target_limit",
                             "its foreign key is not declared: its constraint name "
                             + (f"{name} is taken by {taken[name]}" if name else "is too long"))
                continue
            taken[name] = er.name
            declared.add((child, column.casefold()))
            cname, pname = self.name(ct), self.name(self.tables[parent])
            self.add("constraints",
                     f"ALTER TABLE {cname} ADD CONSTRAINT {name} FOREIGN KEY ({DB._quote(column)}) "
                     f"REFERENCES {pname} ({DB._quote(parent_column)});",
                     f"ALTER TABLE {cname} DROP CONSTRAINT IF EXISTS {name};")

    def foreign_key(self, er: Any, keys: dict[str, list[str]]
                    ) -> Optional[tuple[str, str, str, str]]:
        """(child table, its column, parent table, the parent's key column) for a relationship
        that is a column equality onto a one-column primary key declared above; None, reported,
        for any other."""
        r = er.relationship
        a, b = bare_name(r.from_table).casefold(), bare_name(r.to_table).casefold()
        if r.on or not (r.from_column and r.to_column):
            self.degrade("relationship", er.name, "no_home_in_target", "its join is written as an "
                         "SQL condition, and a Unity Catalog foreign key is an equality of columns")
            return None
        if a not in self.tables or b not in self.tables or a == b:
            self.degrade("relationship", er.name, "no_home_in_target", "a foreign key joins two "
                         "catalog tables, and one end here is defined by a query, or both are one")
            return None
        ways = {"many_to_one": [(a, r.from_column, b, r.to_column)],
                "one_to_many": [(b, r.to_column, a, r.from_column)]}.get(
            r.relationship, [(a, r.from_column, b, r.to_column), (b, r.to_column, a, r.from_column)])
        for child, column, parent, pcol in ways:
            ccol, key = self.column(child, column), keys.get(parent) or []
            if ccol is None or [k.casefold() for k in key] != [pcol.casefold()]:
                continue
            ptype = self.column(parent, pcol).type
            if ccol.type != ptype:
                self.degrade("relationship", er.name, "target_rule", "a foreign key column takes "
                             f"its key's type, and agami types them {ccol.type} and {ptype}")
                return None
            return child, ccol.name, parent, key[0]
        self.degrade("relationship", er.name, "no_home_in_target", "it points at no primary key "
                     "this file declares, and a Unity Catalog foreign key must reference one whole")
        return None

    def column(self, table: str, name: str) -> Any:
        return next((c for c in self.tables[table].table.columns
                     if c.name.casefold() == name.casefold()), None)

    # -- certification -----------------------------------------------------

    def certify(self) -> None:
        for t in self.owned.values():
            # Certified as a whole: a column carried unreviewed is part of what the tag vouches for.
            pending = [c.name for c in t.table.columns
                       if not self.view.approved("column", f"{t.table.name}.{c.name}")]
            if not self.view.approved("table", t.table.name) or pending:
                self.degrade("table", t.table.name, "not_approved", "not marked certified: "
                             + (f"its column(s) {', '.join(pending)} are" if pending and
                                self.view.approved("table", t.table.name) else "it is")
                             + " carried unreviewed")
                continue
            self.tag(self.parts(t))
        # Every unreviewed table, column and relationship is in some metric view's fields or joins.
        unreviewed = sorted(n for k, n in self.view.unreviewed
                            if k in ("table", "column", "relationship"))
        plans, _ = DB.plan_metric_views(self.view)  # their losses are write_metric_views' to report
        for p in plans:
            ident = DB.metric_view_identifier(self.view, self.options, p.table)
            if not DB.writable(self.view, self.options, p):
                continue  # write_metric_views did not write it, so it is not created
            pending = [m for m in p.measures if not self.view.approved("metric", m)]
            if pending or unreviewed:
                self.degrade("metric_view", p.name, "not_approved", "not marked certified: it "
                             "holds " + ", ".join(pending or unreviewed) + ", carried unreviewed")
                continue
            if any("partition" in m for m in p.definition["measures"]):
                # Its own file asks for the measure to be compared with agami's answer before it
                # is relied on; a certified tag would say the opposite.
                self.degrade("metric_view", p.name, "target_rule", "not marked certified: it holds "
                             "a level-of-detail measure, which its file says to compare with "
                             "agami's answer before relying on it")
                continue
            self.tag(DB.metric_view_parts(self.view, self.options, p.table))

    def tag(self, parts: tuple[Optional[str], Optional[str], str]) -> None:
        # The undo is commented out: UNSET TAG removes the tag whoever set it, so only the person
        # who saw this SET TAG succeed knows the removal takes back nothing but agami's.
        name = DB._dotted(*parts)
        self.tagged.append(parts)
        self.add("certify", f"SET TAG ON TABLE {name} {CERTIFICATION_TAG} = {CERTIFIED};",
                 f"-- UNSET TAG ON TABLE {name} {CERTIFICATION_TAG};")


# ---------------------------------------------------------------------------
# Reading information_schema: the certification check and the primary key query
# ---------------------------------------------------------------------------

_GUARD_MESSAGE = ("agami: no tag was set. These objects already carry system.certification_status; "
                  "delete each one's SET TAG line and its name in this check, then run the "
                  "section again: ")


def _by_schema(objects: list[tuple[Optional[str], Optional[str], str]]
               ) -> dict[tuple[Optional[str], Optional[str]], list[str]]:
    groups: dict[tuple[Optional[str], Optional[str]], list[str]] = {}
    for catalog, schema, name in objects:
        groups.setdefault((catalog, schema), []).append(name)
    return groups


def _where(catalog: Optional[str], schema: Optional[str], schema_column: str,
           names: list[str]) -> tuple[str, str, str]:
    """The catalog's information_schema, the filter on one schema, and the filter on its objects
    by name. Unity Catalog names are case-insensitive, so both sides are compared in lower case; a
    name with no catalog or schema reads the session's current one, as the statements do."""
    home = f"{DB._quote(catalog)}.information_schema" if catalog else "information_schema"
    on_schema = (f"lower({schema_column}) = "
                 + (sql_string(schema.lower()) if schema else "lower(current_schema())"))
    listed = ", ".join(sql_string(n.lower()) for n in dict.fromkeys(names))
    return home, on_schema, f"lower(table_name) IN ({listed})"


def certification_guards(objects: list[tuple[Optional[str], Optional[str], str]]) -> list[str]:
    """One statement per catalog and schema that raises an error, naming them, when any of
    `objects` already carries the certification tag; it returns no row otherwise."""
    out = []
    for (catalog, schema), names in _by_schema(objects).items():
        home, on_schema, on_names = _where(catalog, schema, "schema_name", names)
        out.append(f"SELECT raise_error(concat({sql_string(_GUARD_MESSAGE)}, "
                   "array_join(sort_array(collect_set(table_name)), ', '))) "
                   f"FROM {home}.table_tags WHERE {on_schema} "
                   f"AND tag_name = 'system.certification_status' AND {on_names} "
                   "HAVING count(*) > 0;")
    return out


def primary_key_queries(objects: list[tuple[Optional[str], Optional[str], str]]) -> list[str]:
    """Commented-out queries, one per catalog and schema, listing the primary keys `objects`
    already have."""
    out = []
    for (catalog, schema), names in _by_schema(objects).items():
        home, on_schema, on_names = _where(catalog, schema, "table_schema", names)
        out.append("-- SELECT table_catalog, table_schema, table_name, constraint_name FROM "
                   f"{home}.table_constraints WHERE constraint_type = 'PRIMARY KEY' "
                   f"AND {on_schema} AND {on_names};")
    return out


# ---------------------------------------------------------------------------
# The two files
# ---------------------------------------------------------------------------

_HEADER = """\
-- Unity Catalog metadata for subject area {area}, written by agami `sm export`.
--
-- Nothing in this file has been applied: agami writes files and never connects to Databricks.
-- Review it, then run it on a SQL warehouse or a Databricks Runtime that supports COMMENT ON
-- COLUMN, SET TAG and raise_error (see Databricks' documentation). {undo} reverses it,
-- statement by statement: note which statements here succeed, since only those may be reversed.
-- Names without a catalog resolve against the session's current catalog and schema.
-- A table another subject area also carries gets its statements in that area's file only.
{order}"""

_SECTION_NOTES = {
    "comments": """\
-- Comments (--uc-comments). Each statement REPLACES the object's existing comment, including
-- one a person or Databricks' AI-generated comments wrote. Save the current ones first
-- (DESCRIBE TABLE EXTENDED). Needs MODIFY on a table, or ownership of a view.
-- Undo sets each table comment to NULL, which removes it, and each column comment to an empty
-- string; neither brings back an earlier comment.""",
    "constraints": """\
-- Keys (--uc-constraints). Primary and foreign keys in Databricks are informational: they are
-- NOT ENFORCED, and nothing checks the data against them. They are declared without RELY on
-- purpose: RELY tells the optimizer it may trust the key to rewrite queries, and a key the data
-- breaks then returns wrong results. agami reviewed these keys but never checked the data; add
-- RELY yourself once you have. SET NOT NULL, which a primary key column needs, validates every
-- existing row and fails if one is NULL. Keys need Delta tables in Unity Catalog, ownership of
-- each table (MODIFY for SET NOT NULL), and SELECT on each referenced table. Adding a constraint
-- may upgrade a Delta table's writer protocol. ADD CONSTRAINT fails if the name exists; every
-- name here starts agami_, so one that existed before is implausible, and a failed ADD left it
-- as it was. Undo drops each key by that name and runs DROP NOT NULL, which also relaxes a
-- column that was NOT NULL before this script ran: skip those lines for such a column, and the
-- line of any key whose ADD CONSTRAINT failed.
-- A table can have at most one primary key (Databricks' constraint documentation). Where a table
-- already has one, its ADD CONSTRAINT ... PRIMARY KEY here fails and can be skipped; a foreign key
-- here that references it still applies when the existing key is on the same columns; and skip
-- that table's lines in the undo file. In the apply file, the commented-out query that opens
-- this section lists the primary keys these tables already have. This section cannot run
-- twice: once applied, its constraints exist and each ADD CONSTRAINT fails. To apply it again,
-- run the undo file's Keys section first.""",
    "certify": """\
-- Certification (--uc-certify). Sets the governed tag system.certification_status to certified
-- on tables approved in agami and on this export's metric views whose every measure is
-- approved. Needs ASSIGN on the system.certification_status governed tag, and APPLY TAG on the
-- object with USE SCHEMA and USE CATALOG (or ownership). In the apply file the section opens
-- with a check, one per schema, that reads information_schema.table_tags: if any object listed
-- in it already carries system.certification_status, it stops the section with an error naming
-- those objects, before any SET TAG runs. To skip an object, delete its SET TAG line and its
-- name in the check.
-- Undo: UNSET TAG removes the tag whoever set it, and Databricks SQL has no form that unsets a
-- tag only when it holds a given value, so the undo lines are commented out. Remove the `-- `
-- from the line of each object whose SET TAG succeeded, and leave the others.""",
}

_SECTION_TITLES = {"comments": "Comments", "constraints": "Keys", "certify": "Certification"}


def _file(header: str, sections: list[str], body: dict[str, list[str]]) -> bytes:
    out = [header]
    for s in sections:
        out.append(f"\n-- ===== {_SECTION_TITLES[s]} =====\n{_SECTION_NOTES[s]}\n")
        stmts = body.get(s, [])
        out.append("\n".join(stmts) + "\n" if stmts else "-- No statements: agami holds none.\n")
    return "".join(out).encode("utf-8")


def write_uc_ddl(view: ExportView, options: ExportOptions) -> WriterOutput:
    script = _Script(view, options)
    sections = script.asked()
    if not sections:
        flags = ", ".join("--" + d.replace("_", "-") for d in SECTIONS.values())
        return WriterOutput({}, (degraded(
            view.area, "unity_catalog", ddl_path(view.area), "detail_lost",
            "Unity Catalog comments, keys and certification are not written: they change the "
            f"catalog's metadata, so each is asked for by its own flag ({flags})"),))
    for s in sections:
        getattr(script, s)()
    # The certification check runs before the tags it guards; the key query is commented out.
    lead = {"constraints": primary_key_queries(script.keyed),
            "certify": certification_guards(script.tagged)}
    apply = {s: lead.get(s, []) + [a for a, _ in script.pairs.get(s, [])] for s in sections}
    # The undo file runs the sections, and the statements in each, in reverse: a foreign key goes
    # before the primary key it references, and a key before its NOT NULL.
    undo = {s: [u for _, u in reversed(script.pairs.get(s, []))] for s in sections}
    path, back = ddl_path(view.area), undo_path(view.area)
    order = "".join(f"-- Run {ddl_path(a).removeprefix(f'{UC_DIR}/')} first: a foreign key here "
                    "references a primary key it declares.\n" for a in sorted(script.elsewhere))
    files = {
        path: _file(_HEADER.format(area=view.area, undo=back.removeprefix(f"{UC_DIR}/"),
                                   order=order), sections, apply),
        back: _file(f"-- Reverses {path.removeprefix(f'{UC_DIR}/')} for subject area {view.area}, "
                    "written by agami `sm export`.\n-- Nothing in this file has been applied. "
                    "Run only the lines whose statement in that file\n-- succeeded: a line for one "
                    "that failed would remove what was there before. Read each\n-- section's note "
                    "before running it.\n",
                    list(reversed(sections)), undo),
    }
    return WriterOutput(files, tuple(script.losses))


def add_arguments(sp: argparse.ArgumentParser) -> None:
    # Off unless given, and suppressed so an export without them records nothing in its report.
    helps = {
        "comments": "with --target databricks, write unity_catalog/<area>.sql with COMMENT ON "
                    "statements that replace the catalog's table and column comments",
        "constraints": "with --target databricks, add informational primary and foreign keys "
                       "(and the NOT NULL a key needs) to unity_catalog/<area>.sql",
        "certify": "with --target databricks, add SET TAG system.certification_status = "
                   "certified to unity_catalog/<area>.sql for each table approved with every "
                   "column it carries, and each metric view whose every item is approved and "
                   "that holds no level-of-detail measure",
    }
    for section, dest in SECTIONS.items():
        sp.add_argument("--" + dest.replace("_", "-"), dest=dest, action="store_true",
                        default=argparse.SUPPRESS, help=helps[section])


DB.register_artifact(write_uc_ddl, add_arguments=add_arguments)
