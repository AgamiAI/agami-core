"""Joins the data dictionary states but the database does not declare (#694).

A dictionary applied to the model (`sm describe-file`, `sm enrich-metadata`) lands as column
descriptions stamped `description_source: metadata`. Some of them say where the column points:
`FK -> parent.pkey`, `FK to parent(pkey)`, `Foreign key referencing parent.pkey`, `Foreign key
reference to parent` (its one-column key), `references parent.pkey`, or `Reference to the parent` /
`pointer to the parent` when exactly one table anchors that entity. Each such statement becomes one
check on the data, the same bounded, read-only aggregate the join-candidate scan runs
(`profile.JoinCheck`): the share of the column's filled rows found in the named key, and whether
that key repeats. What holds is offered as a join candidate, `found_by: dictionary`, beside the
scan's in `join_candidates.json`; nothing is written to the model. What does not hold is a note with
its numbers.

The text is the customer's, and nobody vetted it. A statement is only ever read as identifiers, which
must name a table and a column the model serves; the check builds its SQL from the model's own
spelling through the dialect's quoting, never from the text.

These checks have a budget of their own, beside the scan's `FK_OVERLAP_PROBE_CAP`: a capped scan
must not hide a join the dictionary names, and each statement costs one check, not a scan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import dialects as D
from . import introspect as INTRO
from . import profile as PF
from .models import Column, Datasource, Entity, Table, table_key
from .scratch import LinkedTarget, write_replacing

# Checks per run for stated references; the scan's cap is not touched.
DICTIONARY_PROBE_CAP = PF.JOIN_CHECK_CAP
# The dictionary line kept as evidence is cut to this many characters.
_LINE_MAX = 200

_IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"
# What a statement may name, bare identifiers only: `table.column`, `schema.table.column`,
# `table(column)`, or (after FK / foreign key only) a bare `table`, read as its one-column key. A
# quoted or punctuated name is not read, so text built to reach the SQL (quotes, semicolons) never
# matches, and whatever does match must still name a table and column the model serves.
_DOTTED = re.compile(rf"({_IDENT}(?:\.{_IDENT}){{1,2}})(?!\.?[\w(])")
_PAREN = re.compile(rf"((?:{_IDENT}\.)?{_IDENT})\s*\(\s*({_IDENT})\s*\)")
_BARE = re.compile(rf"({_IDENT})(?=\s|$|[.,;:)](?:\s|$))")
_ENTITY = re.compile(r"(?:the|an?)\s+([A-Za-z][A-Za-z ]{0,60})", re.IGNORECASE)
# `FK -> t.c`, `FK Reference to t.c`, `FK to t(c)`, `Foreign key referencing t.c`, `Foreign-key
# pointer to the seller`: the head, then within the same sentence a word that leads to the target.
_HEAD = re.compile(r"\b(?:FK|foreign[\s-]+key)\b", re.IGNORECASE)
_LEAD = re.compile(r"(?:->|→|=>|\b(?:referenc\w*|to|on)\b)\s*", re.IGNORECASE)
_SENTENCE_END = re.compile(r"\.(?:\s|$)|;|\n")
_REFERENCES = re.compile(r"\breferences\s+", re.IGNORECASE)
_REFERENCE_TO = re.compile(r"\b(?:reference|pointer)\s+to\s+", re.IGNORECASE)
_LEAD_WORDS = {"to", "on", "reference", "references", "referencing", "referenced"}
# After an entity's name, a word that ends the noun phrase. "the buyer session analytics record" is
# not "the buyer": a name followed by more nouns names something else.
_PHRASE_END = {
    "where",
    "that",
    "which",
    "who",
    "whose",
    "this",
    "these",
    "for",
    "of",
    "in",
    "on",
    "at",
    "by",
    "with",
    "being",
    "is",
    "was",
    "from",
    "to",
    "when",
    "if",
    "as",
    "and",
    "or",
    # the record itself, named generically: "the parent record", "the plant id"
    "record",
    "row",
    "entry",
    "id",
    "identifier",
    "key",
    "code",
    "number",
}


@dataclass(frozen=True)
class Statement:
    """One reference a column's dictionary text states: a `target` (`(table, column)`,
    `(schema, table, column)`, or `(table,)` for a bare table), or an `entity` name."""

    form: str
    line: str
    target: Optional[tuple[str, ...]] = None
    entity: Optional[str] = None


def _line_of(text: str, start: int) -> str:
    """The line of `text` the match starts on, trimmed and cut, for the evidence."""
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", start)
    line = " ".join(text[a : b if b >= 0 else len(text)].split())
    return line if len(line) <= _LINE_MAX else line[: _LINE_MAX - 1] + "…"


def _target_at(text: str, i: int, *, bare: bool, entity: bool) -> Optional[tuple[str, object]]:
    """The target that starts at `text[i]`: ("target", parts) or ("entity", words), else None."""
    if i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_.$\"'`"):
        return None
    if entity and (m := _ENTITY.match(text, i)):
        words = m.group(1).split()
        return ("entity", " ".join(words[:4])) if words else None
    if m := _PAREN.match(text, i):
        return "target", (*m.group(1).split("."), m.group(2))
    if m := _DOTTED.match(text, i):
        return "target", tuple(m.group(1).split("."))
    if bare and (m := _BARE.match(text, i)) and m.group(1).lower() not in _LEAD_WORDS:
        return "target", (m.group(1),)
    return None


def _heads(text: str) -> Iterator[tuple[int, str, tuple[str, object]]]:
    """(position, form, target) of each `FK …` / `foreign key …` statement."""
    for h in _HEAD.finditer(text):
        end = _SENTENCE_END.search(text, h.end())
        window = end.start() if end else len(text)
        for lead in _LEAD.finditer(text, h.end(), window):
            hit = _target_at(text[:window], lead.end(), bare=True, entity=True)
            if hit:
                yield h.start(), "fk" if h.group(0).lower() == "fk" else "foreign_key", hit
                break


def parse(text: str) -> list[Statement]:
    """Every reference `text` states, in order, one per target (`FK -> a.b` and `references a.b` in
    one description are one statement)."""
    text = text or ""
    hits = list(_heads(text))
    for m in _REFERENCES.finditer(text):
        hit = _target_at(text, m.end(), bare=False, entity=False)
        if hit:
            hits.append((m.start(), "references", hit))
    for m in _REFERENCE_TO.finditer(text):
        hit = _target_at(text, m.end(), bare=False, entity=True)
        if hit:
            hits.append((m.start(), "reference_to", hit))
    out: list[Statement] = []
    seen: set = set()
    for start, form, (kind, value) in sorted(hits, key=lambda h: h[0]):
        key = (kind, value if kind == "target" else str(value).lower().split()[0])
        if key in seen:
            continue
        seen.add(key)
        line = _line_of(text, start)
        out.append(
            Statement(form, line, target=value)
            if kind == "target"
            else Statement("entity", line, entity=str(value))
        )
    # A named table says more than an entity: read the entity only when nothing names a table.
    named = [s for s in out if s.target]
    return named or out


def _served(t: Table) -> bool:
    return t.review_state not in INTRO._UNSERVED


def _tables(org: Datasource) -> list[Table]:
    return [t for sa in org.subject_areas for t in sa.tables_defined if _served(t)]


def _entities(org: Datasource) -> list[Entity]:
    return [e for sa in org.subject_areas for e in sa.entities] + list(
        org.cross_subject_area_entities
    )


def _column(t: Table, name: str) -> Optional[Column]:
    exact = [c for c in t.columns if c.name == name]
    cols = exact or [c for c in t.columns if c.name.lower() == name.lower()]
    return cols[0] if len(cols) == 1 and cols[0].review_state not in INTRO._UNSERVED else None


def _resolve_table(tables: list[Table], parts: tuple[str, ...], near: Table) -> Optional[Table]:
    """The served table a statement names, case-insensitively: `schema.table` reads that schema's
    only; a bare name two schemas share reads the source table's own schema, else neither."""
    schema, name = (None, parts[0]) if len(parts) <= 2 else (parts[0], parts[1])
    cands = [t for t in tables if t.name.lower() == name.lower()]
    if schema is not None:
        cands = [t for t in cands if (t.schema_name or "").lower() == schema.lower()]
    elif len(cands) > 1:
        cands = [
            t for t in cands if (t.schema_name or "").lower() == (near.schema_name or "").lower()
        ]
    return cands[0] if len(cands) == 1 else None


def _names(e: Entity) -> set[str]:
    return {n.lower() for n in [e.name, e.plural or "", *e.other_names] if n}


def _resolve_entity(
    org: Datasource, tables: list[Table], words: str
) -> Optional[tuple[Optional[Table], str]]:
    """The one table, and its column, that anchors the entity the words name: the longest leading
    run of words that is an entity's name. (None, "") when no table or several carry it; None when
    the words name no entity at all ("Link to the PDF"), which is prose, not a reference."""
    ws = words.lower().split()
    ents = _entities(org)
    for n in range(len(ws), 0, -1):
        phrase = " ".join(ws[:n])
        hits = [e for e in ents if phrase in _names(e)]
        after = ws[n] if n < len(ws) else ""
        if not hits or (after and after not in _PHRASE_END and not after.endswith("ed")):
            continue
        anchors = {(m.table.lower(), m.column) for e in hits for m in e.maps_to if m.primary}
        anchors = anchors or {(m.table.lower(), m.column) for e in hits for m in e.maps_to}
        if len({a[0] for a in anchors}) != 1 or len(anchors) != 1:
            return None, ""
        tname, col = next(iter(anchors))
        want_schema, want = table_key(tname, None)
        ts = [
            t
            for t in tables
            if t.name.lower() == want
            and (not want_schema or (t.schema_name or "").lower() == want_schema)
        ]
        return (ts[0], col) if len(ts) == 1 else (None, "")
    return None


@dataclass
class Found:
    candidates: list[dict] = field(default_factory=list)
    # stated but not proven, with the numbers
    notes: list[dict] = field(default_factory=list)
    # names nothing the model serves: not checked
    unresolved: list[dict] = field(default_factory=list)
    already_joined: list[str] = field(default_factory=list)
    # (column, key) pairs, as tested_pairs keys them
    tested: set[tuple] = field(default_factory=set)
    probes: int = 0


@dataclass
class _Ref:
    t: Table
    c: Column
    s: Statement
    u: Optional[Table] = None
    k: Optional[str] = None
    check: Optional[PF.JoinCheck] = None


def _where(t: Table, c: str) -> str:
    return INTRO._qualified_column(t, c)


def _covered(org: Datasource, sg: Optional[str]) -> dict[tuple, set[str]]:
    """(table, column) of each column a served join reads from -> the tables it joins it to. A
    column in a composite key to one table can still reference another on its own."""
    rels = [r for sa in org.subject_areas for r in sa.relationships] + list(
        org.cross_subject_area_relationships
    )
    out: dict[tuple, set[str]] = {}
    for r in rels:
        if r.review_state in INTRO._UNSERVED:
            continue
        for fc, _tc in INTRO._column_pairs(r, sg) or []:
            key = (table_key(r.from_table, r.from_schema)[1], fc.lower())
            out.setdefault(key, set()).add(table_key(r.to_table, r.to_schema)[1])
    return out


def _joined(covered: dict[tuple, set[str]], t: Table, c: Column, u: Optional[Table]) -> bool:
    """A join already reads `c`: to `u` when the statement resolved, else to any table."""
    to = covered.get((table_key(t.name, t.schema_name)[1], c.name.lower()))
    return bool(to) and (u is None or table_key(u.name, u.schema_name)[1] in to)


@dataclass
class _Resolved:
    u: Optional[Table]
    kc: Optional[Column]
    named: str
    why: str = ""


def _resolve(org: Datasource, tables: list[Table], t: Table, s: Statement) -> Optional[_Resolved]:
    """The table and key column a statement names, with why when it names none the model serves;
    None when the words are prose rather than a reference (a bare word that is no table, an entity
    the model doesn't have)."""
    if s.target is not None and len(s.target) == 1:
        u = _resolve_table(tables, s.target, t)
        if u is None:
            return None
        keys = INTRO._single_keys(u)
        kc = _column(u, keys[0]) if keys else None
        return _Resolved(u, kc, s.target[0], "" if kc else "that table has no one-column key")
    if s.target is not None:
        u = _resolve_table(tables, s.target, t)
        kc = _column(u, s.target[-1]) if u is not None else None
        why = (
            ""
            if kc
            else f"the model serves no {'column' if u is not None else 'table'} by that name"
        )
        return _Resolved(u, kc, ".".join(s.target), why)
    hit = _resolve_entity(org, tables, s.entity or "")
    if hit is None:
        return None
    u, k = hit
    kc = _column(u, k) if u is not None and k else None
    return _Resolved(
        u if kc else None, kc, f"the {s.entity}", "" if kc else "no one table carries that entity"
    )


def stated(org: Datasource) -> Iterator[tuple[Table, Column, Statement]]:
    """Every reference the dictionary text on the model's columns states."""
    for t in _tables(org):
        for c in t.columns:
            if c.description_source == "metadata" and (c.description or "").strip():
                for s in parse(c.description):
                    yield t, c, s


def unjoined(org: Datasource, sg: Optional[str] = None) -> list[dict]:
    """Columns the dictionary calls references that no join of the model reads from yet."""
    covered = _covered(org, sg)
    tables = _tables(org)
    out, seen = [], set()
    for t, c, s in stated(org):
        key = (t.schema_name, t.name, c.name)
        r = _resolve(org, tables, t, s)
        if key in seen or r is None or _joined(covered, t, c, r.u):
            continue
        seen.add(key)
        out.append(
            {"table": t.name, "schema": t.schema_name, "column": c.name, "dictionary": s.line}
        )
    return out


def _unique(check: PF.JoinCheck, u: Table, k: str) -> bool:
    """The key the column points at holds each value once: declared so, or so on the rows read."""
    if u.unique_on([k]):
        return True
    if check.target_read == "whole":
        return check.to_rows == check.to_keys
    return check.to_rows_matched == check.values_matched


def _numbers(check: PF.JoinCheck) -> dict:
    return {k: v for k, v in check.evidence().items() if k != "fans_out"}


def find(org: Datasource, runner, dialect: D.Dialect, sg: Optional[str] = None) -> Found:
    """Check each stated reference on the data, once, and sort it into a candidate or a note."""
    found = Found()
    tables = _tables(org)
    covered = _covered(org, sg)
    refs: list[_Ref] = []
    seen: set[tuple] = set()
    for t, c, s in stated(org):
        where = _where(t, c.name)
        r = _resolve(org, tables, t, s)
        if r is None:
            continue
        if _joined(covered, t, c, r.u if r.kc else None):
            if where not in found.already_joined:
                found.already_joined.append(where)
            continue
        u, kc = r.u, r.kc
        if u is None or kc is None:
            found.unresolved.append(
                {"column": where, "stated": r.named, "reason": r.why, "dictionary": s.line}
            )
            continue
        if u is t and kc.name == c.name:
            continue
        pair = ((t.schema_name, t.name, c.name), (u.schema_name, u.name, kc.name))
        if pair in seen:
            continue
        seen.add(pair)
        if INTRO._personal_column(c) or INTRO._personal_column(kc):
            found.notes.append(
                {
                    "column": where,
                    "stated": _where(u, kc.name),
                    "dictionary": s.line,
                    "reason": "not checked: a column at either end is personal",
                }
            )
            continue
        why = PF.checkable(dialect, t, u, [c.name])
        if why:
            found.notes.append(
                {
                    "column": where,
                    "stated": _where(u, kc.name),
                    "dictionary": s.line,
                    "reason": f"not checked: {why}",
                }
            )
            continue
        refs.append(_Ref(t, c, s, u, kc.name))
    todo = refs[:DICTIONARY_PROBE_CAP]
    for r in refs[DICTIONARY_PROBE_CAP:]:
        found.notes.append(
            {
                "column": _where(r.t, r.c.name),
                "stated": _where(r.u, r.k),
                "dictionary": r.s.line,
                "reason": f"not checked: the cap of {DICTIONARY_PROBE_CAP} dictionary checks was reached",
            }
        )
    for r in todo:
        r.check = PF.JoinCheck(r.t, [r.c.name], r.u, [r.k])
    PF.run_checks(dialect, runner, [r.check for r in todo])
    found.probes = len(todo)
    taken: set[tuple] = set()
    for r in todo:
        check = r.check
        col, key = (r.t.schema_name, r.t.name, r.c.name), (r.u.schema_name, r.u.name, r.k)
        if check.ran:
            found.tested.add((col, key))
        note = {"column": _where(r.t, r.c.name), "stated": _where(r.u, r.k), "dictionary": r.s.line}
        if not check.ran:
            found.notes.append({**note, "reason": "the check could not compare the two columns"})
        elif not check.borne_out:
            found.notes.append(
                {**note, "reason": "stated but not proven: " + check.summary(), **_numbers(check)}
            )
        elif not _unique(check, r.u, r.k):
            found.notes.append(
                {
                    **note,
                    "reason": f"stated but not proven: {r.u.name}.{r.k} repeats, so each "
                    "row would join to several",
                    **_numbers(check),
                }
            )
        elif col in taken:
            found.notes.append(
                {
                    **note,
                    "reason": "the dictionary states another reference for this column first",
                    **_numbers(check),
                }
            )
        else:
            taken.add(col)
            cand = INTRO._candidate(r.t, r.c, r.u, r.k, check, "dictionary")
            cand["op"]["description"] = (
                f"stated in the data dictionary; its values match {r.u.name}.{r.k}"
            )
            cand["evidence"]["dictionary"] = r.s.line
            found.candidates.append(cand)
    return found


def _from(c: dict) -> Optional[tuple]:
    op = c.get("op") if isinstance(c, dict) else None
    if not isinstance(op, dict):
        return None
    return (
        (op.get("from_schema") or "").lower(),
        str(op.get("from_table") or "").lower(),
        str(op.get("from_column") or "").lower(),
    )


def _fullness(c: dict) -> tuple:
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    return (
        ev.get("found_by") == "dictionary",
        ev.get("match_rate") or 0,
        ev.get("values_matched") or 0,
        ev.get("to_keys") or 0,
    )


def one_per_column(cands: list[dict]) -> tuple[list[dict], list[dict]]:
    """A column references one table: of several candidates from one column, the one the dictionary
    names, else the fullest match. The rest come back as notes, with their numbers."""
    by_col: dict = {}
    order: list = []
    for c in cands:
        k = _from(c) or ("", id(c), "")
        if k not in by_col:
            order.append(k)
        by_col.setdefault(k, []).append(c)
    kept, notes = [], []
    for k in order:
        group = sorted(by_col[k], key=_fullness, reverse=True)
        kept.append(group[0])
        for other in group[1:]:
            ev = other.get("evidence") if isinstance(other.get("evidence"), dict) else {}
            notes.append(
                {
                    "column": str(other["id"]).split("->")[0],
                    "stated": str(other["id"]).split("->")[-1],
                    "reason": f"also matches; one join per column, so {group[0]['id']} is offered "
                    f"({'the table the dictionary names' if _fullness(group[0])[0] else 'the fuller match'})",
                    "match_rate": ev.get("match_rate"),
                    "summary": ev.get("summary"),
                }
            )
    return kept, notes


def _pair(c: dict) -> Optional[tuple]:
    """A candidate's (column, key) as `Found.tested` keys it, lower-cased."""
    op = c.get("op") if isinstance(c.get("op"), dict) else {}
    to = (op.get("to_schema"), op.get("to_table"), op.get("to_column"))
    frm = _from(c)
    if frm is None or not to[1] or not to[2]:
        return None
    return frm, tuple((x or "").lower() for x in to)


def _retested(c: dict, tested: set[tuple]) -> bool:
    """A dictionary candidate from an earlier run whose pair this run checked: had it held it would
    be among this run's candidates, so it no longer does. A scan's candidate is never this run's."""
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if ev.get("found_by") != "dictionary":
        return False
    low = {tuple(tuple((x or "").lower() for x in end) for end in pair) for pair in tested}
    return _pair(c) in low


def store(root: Path, found: Found) -> Optional[Path]:
    """Fold the dictionary's candidates into `<model>/.introspect/join_candidates.json`: one per
    column, notes beside them, and each tested pair in `tested_pairs` while the scan is unfinished, so
    an append that finishes it leaves these pairs and their result alone."""
    doc = INTRO._read_introspect_file(root, INTRO.JOIN_CANDIDATES_FILE)
    ids = {c["id"] for c in found.candidates}
    prior = [
        c
        for c in INTRO._listed(doc, "candidates")
        if isinstance(c, dict) and c.get("id") not in ids and not _retested(c, found.tested)
    ]
    kept, siblings = one_per_column(found.candidates + prior)
    offered = {_from(c) for c in kept}
    doc["candidates"] = kept
    doc["low_overlap"] = [
        c
        for c in INTRO._listed(doc, "low_overlap")
        if isinstance(c, dict) and c.get("id") not in ids
    ]
    doc["dictionary_notes"] = found.notes + siblings
    doc["dictionary_unresolved"] = found.unresolved
    # Written afresh: a command carried from the file would be whatever the file said.
    doc["how_to_accept"] = INTRO._accept_command(str(root))
    if INTRO._listed(doc, "unchecked_tables"):
        tested = INTRO._tested_pairs(doc) | found.tested
        doc["tested_pairs"] = sorted(
            ([*c, *k] for c, k in tested), key=lambda p: [x or "" for x in p]
        )
    found.candidates = [c for c in found.candidates if _from(c) in offered and c in kept]
    path = root / ".introspect" / INTRO.JOIN_CANDIDATES_FILE
    try:
        if not path.parent.is_symlink():
            path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()  # replaced, never written through
        write_replacing(path, json.dumps(doc, indent=2) + "\n")
    except (OSError, LinkedTarget):
        return None
    return path
