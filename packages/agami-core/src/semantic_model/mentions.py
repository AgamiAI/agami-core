"""Every prose line in the semantic model that mentions a table or column a statement reads.

Retrieved and quoted, never graded. The ledger grades the structural half of the semantic model
(relationships, declared filters, metric bindings, scope, value lists); the prose half shapes the
AI's SQL just as much and cannot be measured against the warehouse, so it is put beside the grade
for a person to read. Two caveats that disagree about what a status word means are the case this
exists for: the number was graded, and neither caveat was ever shown.

One narrow, deterministic flag: two mentions about one column that each name quoted string values,
with different sets. No edit distance, no meaning. Anything wider is judgment, and judgment lives in
the skill's prose, not here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlglot import expressions as exp

from . import loader as L
from . import runtime as RT
from .models import Datasource

_TEXT_CAP = 300
_MAX_PER_SUBJECT = 20
_UNREADABLE = "the statement could not be read"
_QUOTED = re.compile(r"'([^']*)'")


def _clip(text: Any) -> str:
    flat = re.sub(r"\s+", " ", str(text)).strip()
    return flat if len(flat) <= _TEXT_CAP else flat[:_TEXT_CAP - 3] + "..."


def _word(text: str, word: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None


def statement_subjects(org: Datasource, sql: str, grammar: Optional[str]) -> "tuple[set[str], set[str], str | None]":
    """The tables and `table.column`s the statement reads, as the semantic model keys them; or the
    reason it could not be read."""
    tree, why = RT._parse_reporting(sql, grammar)
    if tree is None:
        return set(), set(), why or _UNREADABLE
    tidx = RT._model_table_index(org)
    tables: set[str] = set()
    columns: set[str] = set()
    for ref in RT._table_references(tree):
        key = RT._tkey(ref.bare)
        if key in tidx:
            tables.add(key)
    for sel in tree.find_all(exp.Select):
        scope = RT._own_alias_map(sel)
        for col in sel.find_all(exp.Column):
            owner = RT._resolve_col_table(col, scope)
            if owner and col.name:
                key = RT._tkey(RT._bare(owner))
                if key in tidx:
                    columns.add(f"{key}.{col.name.lower()}")
    return tables, columns, None


def _prose_lines(org: Datasource, root: Path) -> Iterator[tuple[str, str, str, Optional[str]]]:
    """`(source, where, text, about)`; `about` is set when the line belongs to a table or column of
    its own (its description or caveats) and None when the line is free text matched by words."""
    for sa in org.subject_areas:
        if sa.description:
            yield "subject_area.description", sa.name, sa.description, None
        for table in sa.tables_defined:
            tkey = RT._tkey(table.name)
            if table.description:
                yield "table.description", table.name, table.description, tkey
            for caveat in table.caveats or []:
                yield "table.caveat", table.name, caveat, tkey
            for column in table.columns:
                about = f"{tkey}.{column.name.lower()}"
                where = f"{table.name}.{column.name}"
                if column.description:
                    yield "column.description", where, column.description, about
                for caveat in column.caveats or []:
                    yield "column.caveat", where, caveat, about
        for metric in getattr(sa, "metrics", None) or []:
            for source, text in (("metric.description", getattr(metric, "description", "")),
                                 ("metric.calculation", getattr(metric, "calculation", ""))):
                if text:
                    yield source, metric.name, text, None
    for term, definition in (org.key_terminology or {}).items():
        yield "glossary", term, f"{term}: {definition}", None
    narrative = root / "datasource.md"
    if narrative.exists():
        for paragraph in re.split(r"\n\s*\n", narrative.read_text(encoding="utf-8")):
            if paragraph.strip():
                yield "datasource.md", "datasource.md", paragraph, None
    for sa in org.subject_areas:
        for example in L.list_prompt_examples(root, sa.name):
            example = example or {}
            notes = example.get("notes") or []
            if isinstance(notes, list):
                notes = " ".join(str(n) for n in notes)
            text = " | ".join(str(part) for part in (example.get("question"), notes, example.get("sql")) if part)
            if text:
                yield "example", str(example.get("question") or "?"), text, None


def _about(text: str, tables: set[str], columns: set[str], origin: Optional[str]) -> list[str]:
    """Which subjects a line is about: its own table or column when it has one, else the most
    specific subject its words name. A column is named by `table.column`, or by its bare name
    beside its table's name; a table by its name alone."""
    if origin is not None:
        if origin in columns:
            return [origin]
        if origin not in tables:
            return []
        # A table's own caveat that names one of the statement's columns of that table is about the
        # column too: "open orders are status NOT LIKE 'closed%'" is a sentence about `orders.status`,
        # and it is exactly the line a reader of that column's grade needs beside the column's own.
        named = [subject for subject in sorted(columns)
                 if subject.startswith(origin + ".") and _word(text, subject.split(".", 1)[1])]
        return [origin] + named
    hits: list[str] = []
    for subject in sorted(columns):
        table, _dot, column = subject.partition(".")
        if _word(text, subject) or (_word(text, column) and _word(text, table)):
            hits.append(subject)
    named_tables = {h.split(".")[0] for h in hits}
    for table in sorted(tables):
        if table not in named_tables and _word(text, table):
            hits.append(table)
    return hits


def _values_flags(mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two mentions about one column that each name quoted values, with different sets."""
    by_about: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for m in mentions:
        if "." not in m["about"]:
            continue
        values = frozenset(v.strip().lower() for v in _QUOTED.findall(m["text"]) if v.strip())
        if values:
            by_about.setdefault(m["about"], []).append((m["where"], values))
    flags = []
    for about, named in sorted(by_about.items()):
        if len(named) >= 2 and len({values for _where, values in named}) > 1:
            flags.append({"about": about, "kind": "values_named_differ",
                          "sources": [where for where, _values in named],
                          "values": [sorted(values) for _where, values in named]})
    return flags


def prose_mentions(org: Datasource, root: Path, sql: str, grammar: Optional[str]) -> dict[str, Any]:
    tables, columns, why = statement_subjects(org, sql, grammar)
    if why:
        return {"mentions": [], "subjects": [], "flags": [], "dropped": 0, "unreadable": why, "dialect": grammar}
    mentions: list[dict[str, Any]] = []
    per_subject: dict[str, int] = {}
    dropped = 0
    for source, where, text, origin in _prose_lines(org, root):
        clipped = _clip(text)
        for about in _about(clipped, tables, columns, origin):
            if per_subject.get(about, 0) >= _MAX_PER_SUBJECT:
                dropped += 1
                continue
            per_subject[about] = per_subject.get(about, 0) + 1
            mentions.append({"about": about, "source": source, "where": _clip(where), "text": clipped})
    return {"mentions": mentions, "subjects": sorted(tables | columns), "flags": _values_flags(mentions),
            "dropped": dropped, "unreadable": None, "dialect": grammar}
