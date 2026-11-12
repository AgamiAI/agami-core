"""Ossie export (F19 / ACE-173): each subject area as one Apache Ossie semantic model.

Apache Ossie (formerly OSI) is an open interchange format for semantic models. Version 0.1.1 is
the default; 0.3.0.dev0, a development version whose shape may still move, is written with
`--ossie-version 0.3`. Both versions share
every mapping and differ only in the document's wrapper, the extension block and how an expression
is spelt, so one writer serves both.

Ossie has no home for default filters or sign-off; those travel in one agami extension block per
file, outside Ossie's portable semantics, and the report says so. Like every writer, this one reads
only the `ExportView`: eligibility and binding resolution were decided upstream.

`ai_context` is a string or an object in both schemas; 0.1.1 writes each one as a single text of
labelled lines and 0.3 keeps the object.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Optional, Sequence

import yaml

from .export import (
    DEFAULT_FILTER_NOTE,
    ExportOptions,
    ExportRefused,
    ExportTable,
    ExportView,
    LossEntry,
    Target,
    WriterOutput,
    degraded,
    dropped,
    put_nonempty,
    register,
)
from .models import bare_name

V011 = "0.1.1"
V03 = "0.3.0.dev0"
# `0.3` is what a person types; the document must still carry the schema's exact version string.
VERSIONS = {"0.1.1": V011, "0.3": V03, "0.3.0.dev0": V03}

# agami engine -> Ossie dialect, from Ossie 0.1.1's closed dialect list. 0.3 accepts any string, but
# both versions use this one map so a version switch never changes which metrics travel.
DIALECTS = {"Databricks": "DATABRICKS", "Snowflake": "SNOWFLAKE"}

# 0.1.1 closes `vendor_name` to six vendors, none of them agami, so its block goes under COMMON
# with the payload keyed `agami`; 0.3 takes a free-form vendor name.
_EXT_KEY = {V011: "custom_extensions", V03: "nonportable_extensions"}
_VENDOR = {V011: "COMMON", V03: "AGAMI"}

# Ossie 0.3's portable data types. 0.1.1 has no `datatype` on a field at all.
_DATATYPE = {
    "string": "String",
    "uuid": "String",
    "integer": "Integer",
    "decimal": "Decimal",
    "float": "Float",
    "boolean": "Boolean",
    "date": "Date",
    "time": "Time",
    "timestamp": "DateTime",
    "json": "Opaque",
    "array": "Opaque",
    "bytes": "Opaque",
}
_TEMPORAL = frozenset({"date", "time", "timestamp"})
_PLAIN_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Report details, kept here so each loss reads as one line where it is decided.
_SQL_TABLE = "defined by SQL written for {engine}; SQL is never translated"
_SQL_JOIN = "joins on an SQL condition; an Ossie relationship takes key columns only"
_MISSING_END = "an end is a dataset or field this file leaves out"
_COLLISION = "another {kind} is already named {name}"
_REL_DETAIL = "cardinality {card} and join type {join} have no Ossie field"
_NON_ADDITIVE = "its non-additive dimensions have no Ossie field; they travel in its AI context"
_EXAMPLE_SQL = "carried as its question only; Ossie examples hold no SQL"
_GOLDEN = "Ossie has no construct for an evaluation question"
_ENTITY = "carried as {where}; its mappings and key have no Ossie construct"
_FILTER = "in the agami extension block; Ossie has no filter construct and does not apply it"
_SIGN_OFF = (
    "review state and sign-off time travel in the agami extension block, outside "
    "Ossie's portable semantics; the signer does not"
)
_EMPTY = "an Ossie semantic model needs at least one dataset, and none carried over"
_AI_TEXT = (
    "every AI context in this file is one text of labelled lines (instructions, synonyms, example "
    "questions), not lists"
)


def ossie_version(options: ExportOptions) -> str:
    asked = options.target_args.get("ossie_version") or V011
    if asked not in VERSIONS:
        raise ExportRefused(f"--ossie-version takes one of {', '.join(VERSIONS)}, got {asked!r}")
    return VERSIONS[asked]


def ossie_name(name: str) -> str:
    """The fixed rule for names Ossie gets from free text: lower case, runs of anything but a
    letter or digit become one underscore. Metric and relationship names hold spaces and arrows."""
    folded = name.replace("->", " to ").casefold()
    return re.sub(r"[^0-9a-z]+", "_", folded).strip("_") or "unnamed"


def column_ref(column: str) -> str:
    """A column as an ANSI SQL expression: bare when it is a plain identifier, else double-quoted."""
    if _PLAIN_IDENT.fullmatch(column):
        return column
    return '"' + column.replace('"', '""') + '"'


def _context(
    version: str,
    instructions: Sequence[str] = (),
    synonyms: Sequence[str] = (),
    examples: Sequence[str] = (),
) -> Any:
    out: dict[str, Any] = {}
    put_nonempty(out, "instructions", "\n".join(x for x in instructions if x))
    put_nonempty(out, "synonyms", list(dict.fromkeys(s for s in synonyms if s)))
    put_nonempty(out, "examples", list(examples))
    if not out:
        return None
    return _context_text(out) if version == V011 else out


def _context_text(context: dict) -> str:
    """The object form as one text, for 0.1.1. One synonym or question per line, so a comma inside
    one (a terminology entry often has one) never reads as two."""
    lines = [context["instructions"]] if "instructions" in context else []
    for key, label in (("synonyms", "Synonyms:"), ("examples", "Example questions:")):
        if key in context:
            lines += [label, *(f"- {x}" for x in context[key])]
    return "\n".join(lines)


def _has_context(node: Any) -> bool:
    if isinstance(node, dict):
        return "ai_context" in node or any(_has_context(v) for v in node.values())
    return isinstance(node, list) and any(_has_context(v) for v in node)


# ---------------------------------------------------------------------------
# Expressions: the one place the two versions spell SQL differently
# ---------------------------------------------------------------------------


def _column_expression(version: str, column: str) -> dict:
    # 0.3 has a portable `sql` slot for exactly this; 0.1.1 needs a dialect, and a field's
    # expression is written under ANSI_SQL.
    if version == V03:
        return {"sql": column_ref(column)}
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": column_ref(column)}]}


def _metric_expression(sql: dict[str, str]) -> dict:
    # Each engine's own SQL under its own dialect; no portable `sql`, since nothing is translated.
    pairs = sorted((DIALECTS[e], s) for e, s in sql.items() if e in DIALECTS)
    return {"dialects": [{"dialect": d, "expression": s} for d, s in pairs]}


def _extension(version: str, payload: dict) -> list[dict]:
    if version == V03:
        return [{"vendor_name": _VENDOR[version], "data": payload}]
    text = json.dumps({"agami": payload}, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    return [{"vendor_name": _VENDOR[version], "data": text}]


# ---------------------------------------------------------------------------
# Datasets and fields
# ---------------------------------------------------------------------------


def _field(version: str, column: Any) -> dict:
    out: dict[str, Any] = {
        "name": column.name,
        "expression": _column_expression(version, column.name),
    }
    temporal = column.type in _TEMPORAL or bool(column.date_format)
    if column.aggregation == "dimension" or temporal:
        out["dimension"] = {"is_time": temporal}
    put_nonempty(out, "description", column.description)
    if version == V03:
        out["datatype"] = _DATATYPE[column.type]
    codes = column.choice_field or {}
    coded = "Coded values: " + ", ".join(f"{k} = {v}" for k, v in codes.items()) if codes else ""
    put_nonempty(out, "ai_context", _context(version, [*column.caveats, coded]))
    return out


def _source(view: ExportView, et: ExportTable) -> Optional[str]:
    """Where the dataset reads from, or None when only SQL for another engine could say."""
    t = et.table
    if t.source_type == "sql" and t.sql:
        return t.sql if view.sql_engine else None
    return ".".join(p for p in et.physical if p)


def _entity_home(e: Any) -> str:
    """The table an entity lives in, or '' when it maps to none."""
    home = (
        e.primary_table
        or next((m.table for m in e.maps_to if m.primary), None)
        or (e.maps_to[0].table if e.maps_to else "")
    )
    return bare_name(home) if home else ""


def _entity_synonyms(view: ExportView) -> dict[str, list[str]]:
    """Entity names and their other names, keyed by the table the entity lives in."""
    out: dict[str, list[str]] = {}
    for e in view.entities:
        out.setdefault(_entity_home(e), []).extend([e.name, *e.other_names])
    return out


def _dataset(
    version: str, view: ExportView, et: ExportTable, source: str, synonyms: list[str]
) -> dict:
    t = et.table
    out: dict[str, Any] = {"name": bare_name(t.name), "source": source}
    keys = [c.name for c in t.columns if c.primary_key] or list(t.grain)
    put_nonempty(out, "primary_key", keys)
    put_nonempty(out, "unique_keys", [list(k) for k in t.unique_keys])
    put_nonempty(out, "description", t.description)
    put_nonempty(out, "ai_context", _context(version, list(t.caveats), synonyms))
    out["fields"] = [_field(version, c) for c in t.columns]
    return out


def _datasets(
    version: str,
    view: ExportView,
    synonyms: dict[str, list[str]],
    losses: list[LossEntry],
    kept: set[tuple[str, str]],
) -> list[dict]:
    out = []
    for et in view.tables:
        source = _source(view, et)
        if source is None:
            detail = _SQL_TABLE.format(engine=view.source_engine)
            losses.append(dropped(view.area, "table", et.table.name, "sql_not_for_target", detail))
            continue
        kept.add(("table", et.table.name))
        out.append(_dataset(version, view, et, source, synonyms.get(bare_name(et.table.name), [])))
    return out


# ---------------------------------------------------------------------------
# Relationships and metrics
# ---------------------------------------------------------------------------


def _relationship(
    r: Any, name: str, fields: dict[str, set[str]]
) -> tuple[Optional[dict], Any, str]:
    """The Ossie relationship, many side first; or None, the reason code and why."""
    if r.from_column is None or r.to_column is None:
        return None, "no_home_in_target", _SQL_JOIN
    ends = [(bare_name(r.from_table), r.from_column), (bare_name(r.to_table), r.to_column)]
    if r.relationship == "one_to_many":
        ends.reverse()  # Ossie's `from` is the many side
    if any(col not in fields.get(ds, ()) for ds, col in ends):
        return None, "depends_on_excluded", _MISSING_END
    (many, many_col), (one, one_col) = ends
    rel = {
        "name": name,
        "from": many,
        "to": one,
        "from_columns": [many_col],
        "to_columns": [one_col],
    }
    return rel, None, ""


def _relationships(
    view: ExportView, datasets: list[dict], losses: list[LossEntry], kept: set[tuple[str, str]]
) -> list[dict]:
    fields = {d["name"]: {f["name"] for f in d["fields"]} for d in datasets}
    out, taken = [], set()
    for er in view.relationships:
        r, name = er.relationship, ossie_name(er.name)
        rel, reason, why = _relationship(r, name, fields)
        if rel is None:
            losses.append(dropped(view.area, "relationship", er.name, reason, why))
        elif name in taken:
            why = _COLLISION.format(kind="relationship", name=name)
            losses.append(dropped(view.area, "relationship", er.name, "name_collision", why))
        else:
            taken.add(name)
            kept.add(("relationship", er.name))
            out.append(rel)
            why = _REL_DETAIL.format(card=r.relationship, join=r.join_type)
            losses.append(degraded(view.area, "relationship", er.name, "detail_lost", why))
    return out


def _metric_notes(m: Any) -> list[str]:
    notes = [m.calculation if m.calculation != m.description else "", *m.caveats]
    if m.non_additive_dimensions:
        how = f", take the {m.semi_additive_agg} value" if m.semi_additive_agg else ""
        notes.append(f"Do not sum across {', '.join(m.non_additive_dimensions)}{how}.")
    return notes


def _metrics(
    version: str, view: ExportView, losses: list[LossEntry], kept: set[tuple[str, str]]
) -> list[dict]:
    out, taken = [], set()
    for em in view.metrics:
        m, name = em.metric, ossie_name(em.metric.name)
        if name in taken:
            why = _COLLISION.format(kind="metric", name=name)
            losses.append(dropped(view.area, "metric", m.name, "name_collision", why))
            continue
        taken.add(name)
        kept.add(("metric", m.name))
        # `em.sql` holds only the engines the view resolved; an engine the target lacks was never
        # Ossie's to carry, and one it lacks a binding for the core has reported already.
        if m.non_additive_dimensions:
            losses.append(degraded(view.area, "metric", m.name, "carried_as_text", _NON_ADDITIVE))
        metric: dict[str, Any] = {"name": name, "expression": _metric_expression(em.sql)}
        put_nonempty(metric, "description", m.description or m.calculation)
        synonyms = [*([m.name] if name != m.name else []), *m.other_names]
        put_nonempty(metric, "ai_context", _context(version, _metric_notes(m), synonyms))
        out.append(metric)
    return out


# ---------------------------------------------------------------------------
# What has no Ossie construct
# ---------------------------------------------------------------------------


def _ambiguity_text(a: Any) -> str:
    act = (
        "ask which is meant"
        if a.behaviour == "ask"
        else "answer with the default and offer the others"
    )
    options = "; ".join(f"{o.label}{' (default)' if o.default else ''}: {o.how}" for o in a.options)
    return f"{a.name}: when {a.when}, {act}. Options: {options}."


def _questions(view: ExportView, losses: list[LossEntry]) -> list[str]:
    for q in view.examples:
        if q.sql:
            losses.append(degraded(view.area, "example", q.name, "no_home_in_target", _EXAMPLE_SQL))
    for q in view.golden:
        losses.append(dropped(view.area, "golden", q.name, "no_home_in_target", _GOLDEN))
    return [q.question for q in view.examples]


def _entity_losses(view: ExportView, homes: set[str]) -> list[LossEntry]:
    out = []
    for e in view.entities:
        where = "its dataset's synonyms" if _entity_home(e) in homes else "the model's synonyms"
        why = _ENTITY.format(where=where)
        out.append(degraded(view.area, "entity", e.name, "detail_lost", why))
    return out


def _agami_block(view: ExportView, kept: set[tuple[str, str]], losses: list[LossEntry]) -> dict:
    """Default filters and sign-off, which Ossie has no construct for."""
    filters: dict[str, list[str]] = {}
    for et in view.tables:
        ds = bare_name(et.table.name)
        if ("table", et.table.name) not in kept:
            continue
        for i, f in enumerate(et.table.default_filters, 1):
            filters.setdefault(ds, []).append(f.replace("{alias}", ds))
            name = f"{et.table.name}#{i}"
            losses.append(degraded(view.area, "default_filter", name, "carried_as_text", _FILTER))
    sign_off = _sign_off(view, kept)
    if sign_off:
        losses.append(degraded(view.area, "sign_off", view.area, "carried_as_text", _SIGN_OFF))
    block: dict[str, Any] = {"datasource": view.datasource, "subject_area": view.area}
    if filters:
        block["default_filters"] = {"engine": view.sql_engine, "usage": DEFAULT_FILTER_NOTE,
                                    "by_dataset": filters}
    put_nonempty(block, "sign_off", sign_off)
    return block


def _sign_off(view: ExportView, kept: set[tuple[str, str]]) -> list[dict]:
    items = [("table", et.table.name, bare_name(et.table.name), et.table) for et in view.tables]
    items += [
        ("metric", em.metric.name, ossie_name(em.metric.name), em.metric) for em in view.metrics
    ]
    items += [
        ("relationship", er.name, ossie_name(er.name), er.relationship) for er in view.relationships
    ]
    out = []
    for kind, name, emitted, item in sorted(
        (x for x in items if x[:2] in kept), key=lambda x: x[:2]
    ):
        entry = {
            "kind": kind,
            "name": name,
            "ossie_name": emitted,
            "id": view.id(kind, name),
            "review_state": item.review_state,
        }
        put_nonempty(entry, "signed_off_at", item.signed_off_at)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def build_model(view: ExportView, version: str) -> tuple[Optional[dict], list[LossEntry]]:
    """The Ossie semantic model for one subject area, or None when Ossie cannot hold it."""
    losses: list[LossEntry] = []
    kept: set[tuple[str, str]] = set()  # (kind, agami name) of what this file carries
    synonyms = _entity_synonyms(view)
    datasets = _datasets(version, view, synonyms, losses, kept)
    if not datasets:
        losses.append(dropped(view.area, "subject_area", view.area, "target_rule", _EMPTY))
        return None, losses
    model: dict[str, Any] = {"name": ossie_name(view.area)}
    put_nonempty(model, "description", view.area_description or view.description)
    homes = {d["name"] for d in datasets}
    loose = [s for home, names in synonyms.items() if home not in homes for s in names]
    terms = [f"{term}: {meaning}" for term, meaning in view.terminology]
    instructions = [_ambiguity_text(a) for a in view.ambiguities]
    questions = _questions(view, losses)
    put_nonempty(model, "ai_context", _context(version, instructions, [*terms, *loose], questions))
    model["datasets"] = datasets
    put_nonempty(model, "relationships", _relationships(view, datasets, losses, kept))
    put_nonempty(model, "metrics", _metrics(version, view, losses, kept))
    losses += _entity_losses(view, homes)
    if version == V011 and _has_context(model):
        # Once per file: the same loss on every item would bury the report's other entries.
        losses.append(degraded(view.area, "ai_context", view.area, "carried_as_text", _AI_TEXT))
    model[_EXT_KEY[version]] = _extension(version, _agami_block(view, kept, losses))
    return model, losses


def document(model: dict, version: str) -> dict:
    # 0.1.1 wraps models in a list; 0.3 puts the one model at the root.
    if version == V03:
        return {"version": V03, **model}
    return {"version": V011, "semantic_model": [model]}


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # A multi-line string (instructions, the 0.1.1 extension's JSON) reads and diffs line by line
    # as a literal block rather than as one escaped, quoted line.
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dump(doc: dict) -> bytes:
    text = yaml.dump(
        doc,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return text.encode("utf-8")


def file_name(view: ExportView) -> str:
    """`<model name>.ossie.yaml`: the model's own name, so the file and the `name` inside it agree.
    The naming rule leaves only `[0-9a-z_]`, so no area name reaches a directory or leaves `ossie/`.
    Two areas that fold to one name meet the core's check that no path is written twice."""
    return f"{ossie_name(view.area)}.ossie.yaml"


def write_ossie(view: ExportView, options: ExportOptions) -> WriterOutput:
    version = ossie_version(options)
    model, losses = build_model(view, version)
    files = {} if model is None else {file_name(view): dump(document(model, version))}
    return WriterOutput(files, tuple(losses))


def add_arguments(sp: argparse.ArgumentParser) -> None:
    # Suppressed unless given: `sm export` hands every target's flags to every run's report, and an
    # export without the ossie target should not record an Ossie version.
    sp.add_argument(
        "--ossie-version",
        dest="ossie_version",
        choices=list(VERSIONS),
        default=argparse.SUPPRESS,
        help="Ossie version for --target ossie: 0.1.1 (default) or "
        "0.3 (0.3.0.dev0, a development version)",
    )


register(Target("ossie", tuple(sorted(DIALECTS)), write_ossie, add_arguments))
