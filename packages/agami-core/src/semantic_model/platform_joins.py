"""A metadata-driven platform's reference joins, found on a warehouse that renamed its tables (ACE-181).

An export renames each platform table (`acme_task_v`) and splits each reference into a column of its
own (`assigned_to_sys_id`), so matching the dictionary's table and field names finds almost nothing.
Candidates come instead from four places, and every one is checked on the data before it is kept:

- the dictionary's reference fields, on every table mapped to a class and its parent classes;
- the name a link column carries, against tables' names once the layer's words are stripped;
- a child class's record to its parent class's record (an incident is also a task, same id);
- a link column no signal reached, against every table keyed by the platform's record id.

Only a join the dictionary declares AND the data bears out is written, approved like a declared
foreign key. The rest go to `join_candidates.json` for a person, each with why it is there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import dialects as D
from . import introspect as INTRO
from . import metadata_sources as MS
from . import profile as PF
from .models import Column, Datasource, Table

# A link whose rows find their partner at this rate, short of PF.JOIN_MIN_MATCH, is shown to a
# person as a partial match; below it the pair is dropped.
REVIEW_MIN_MATCH = 0.70
# Checks per run for the planned candidates, and again for the value scan of what they left.
REFERENCE_PROBE_CAP = PF.JOIN_CHECK_CAP


@dataclass
class _Pair:
    t: Table
    c: Column
    u: Table
    found_by: str
    declared: bool
    check: Optional[PF.JoinCheck] = None


@dataclass
class Found:
    trusted: list[dict] = field(default_factory=list)   # specs for `cli._route_references`
    review: list[dict] = field(default_factory=list)    # candidates, as join_candidates.json holds them
    mapping: dict[str, dict] = field(default_factory=dict)
    probes: int = 0
    skipped_target: int = 0
    notes: list[str] = field(default_factory=list)


def _tables(org: Datasource) -> list[Table]:
    return [t for sa in org.subject_areas for t in sa.tables_defined
            if t.review_state not in INTRO._UNSERVED and len(t.grain) == 1]


def _probeable(c: Column) -> bool:
    return (c.review_state not in INTRO._UNSERVED and c.type in INTRO._KEY_TYPES
            and not INTRO._personal_column(c))


def _run(dialect: D.Dialect, runner, pairs: list[_Pair], found: Found) -> None:
    """Check each pair on the data, up to the cap; a pair that can't be checked keeps no check."""
    todo = []
    for p in pairs:
        if PF.checkable(dialect, p.t, p.u, [p.c.name]) is None and len(todo) < REFERENCE_PROBE_CAP:
            p.check = PF.JoinCheck(p.t, [p.c.name], p.u, [p.u.grain[0]])
            todo.append(p.check)
    if len(todo) < len(pairs):
        found.notes.append(f"{len(pairs) - len(todo)} reference candidate(s) not checked on the data "
                           "(a table of unknown size, or the check cap was reached)")
    PF.run_checks(dialect, runner, todo)
    found.probes += len(todo)


def _rate(p: _Pair) -> float:
    return (p.check.rate or 0.0) if p.check is not None else 0.0


def _most_specific(pairs: list[_Pair], mapping: dict[str, dict], parents: dict[str, str]) -> Optional[_Pair]:
    """Of targets that are nested classes (service < item), the deepest; None when any two are not
    nested, or a target is no known class: then which one is meant is a person's call."""
    classes = [mapping.get(p.u.name, {}).get("class") for p in pairs]
    if None in classes or len(set(classes)) != len(classes):
        return None
    for p, cls in zip(pairs, classes):
        if all(o == cls or o in MS.ancestors(cls, parents) for o in classes):
            return p
    return None


def map_model(org: Datasource, preset: str, dictionary_rows: list[dict], dictionary_cfg: dict,
              parents: dict[str, str]) -> dict[str, dict]:
    """`table -> {"class", "by", ...}` for each modelled table that is one of the platform's classes."""
    spec, cfg = MS.PRESETS.get(preset, {}), dictionary_cfg
    if not dictionary_rows or "table_col" not in cfg:
        return {}
    fields = MS.fields_by_class(dictionary_rows, table_col=cfg["table_col"], column_col=cfg["column_col"])
    return MS.map_tables({t.name: {c.name for c in t.columns} for t in _tables(org)}, fields, parents,
                         key=spec.get("key", ""), link_suffix=spec.get("link_suffix", ""))


def find_reference_joins(org: Datasource, runner, dialect: D.Dialect, preset: str, *,
                         dictionary_rows: list[dict], dictionary_cfg: dict,
                         parents: dict[str, str]) -> Found:
    spec = MS.PRESETS.get(preset, {})
    key, suffix = spec.get("key", ""), spec.get("link_suffix", "")
    cfg = dictionary_cfg
    tables = _tables(org)
    found = Found()
    refs = by_field = {}
    if dictionary_rows and "reference_col" in cfg:
        kw = dict(column_col=cfg["column_col"], type_col=cfg["type_col"], reference_col=cfg["reference_col"],
                  reference_type=cfg.get("reference_type", "reference"))
        refs = MS.references_by_class(dictionary_rows, table_col=cfg["table_col"], **kw)
        by_field = {f: t.lower() for f, t in MS.reference_declarations(dictionary_rows, **kw).items()}
    graph = {f: t.lower() for f, t in MS.known_reference_graph(preset).items()}
    found.mapping = map_model(org, preset, dictionary_rows, cfg, parents)
    of_class: dict[str, list[Table]] = {}
    for t in tables:
        if t.name in found.mapping:
            of_class.setdefault(found.mapping[t.name]["class"], []).append(t)
    pre, suf = MS.common_affixes([t.name for t in tables], min_share=0.25)
    of_stem: dict[str, list[Table]] = {}
    for u in tables:
        for s in MS.stems(u.name, pre, suf):
            of_stem.setdefault(s, []).append(u)

    planned: dict[tuple, _Pair] = {}

    def propose(t: Table, c: Column, u: Table, how: str, declared: bool) -> None:
        k = (INTRO._qualified(t), c.name.lower(), INTRO._qualified(u))
        if k not in planned:
            planned[k] = _Pair(t, c, u, how, declared)
        elif declared and not planned[k].declared:
            planned[k].found_by, planned[k].declared = how, True

    links: list[tuple[Table, Column]] = []
    for t in tables:
        cls = found.mapping.get(t.name, {}).get("class")
        chain = [cls, *MS.ancestors(cls, parents)] if cls else []
        for c in t.columns:
            if not _probeable(c):
                continue
            f = MS.column_field(c.name, key, suffix) if key else c.name.lower()
            if f is None:
                # the record's own id: a child class's record is also its parent class's (an
                # explicit exception to never pairing two tables' own keys)
                for a in chain[1:]:
                    for u in of_class.get(a, []):
                        propose(t, c, u, "parent_class", False)
                continue
            link = bool(suffix) and c.name.lower().endswith(suffix.lower())
            target, how, declared = next(((refs[(a, f)], "dictionary", True) for a in chain if (a, f) in refs),
                                         (None, "", False))
            if target is None and f in by_field:
                target, how = by_field[f], "dictionary_field_name"
            elif target is None and f in graph:
                target, how = graph[f], "preset"
            if target is not None:
                for u in of_class.get(target, []) or [u for u in tables if u.name.lower() == target]:
                    propose(t, c, u, how, declared)
                if not of_class.get(target) and not any(u.name.lower() == target for u in tables):
                    found.skipped_target += 1
            if link:
                links.append((t, c))
                for u in of_stem.get(f, []):
                    propose(t, c, u, "name", False)
    _run(dialect, runner, list(planned.values()), found)

    # A link column nothing above bore out: try every table keyed by the platform's record id. Ids
    # are unique across the platform, so the table its values are found in is the one it points at.
    settled = {(INTRO._qualified(p.t), p.c.name.lower()) for p in planned.values() if _rate(p) >= REVIEW_MIN_MATCH}
    keyed = [u for u in tables if key and u.grain[0].lower() == key.lower()]
    scan = [_Pair(t, c, u, "values", False) for t, c in links
            if (INTRO._qualified(t), c.name.lower()) not in settled for u in keyed
            if (INTRO._qualified(t), c.name.lower(), INTRO._qualified(u)) not in planned]
    _run(dialect, runner, scan, found)
    _settle([*planned.values(), *scan], found, parents)
    return found


def _review(p: _Pair, why: str) -> dict:
    cand = INTRO._candidate(p.t, p.c, p.u, p.u.grain[0], p.check or PF.JoinCheck(p.t, [p.c.name], p.u, p.u.grain),
                            p.found_by)
    cand["review_reason"] = why
    return cand


def _settle(pairs: list[_Pair], found: Found, parents: dict[str, str]) -> None:
    """Sort the checked pairs into written, for review, and dropped."""
    by_col: dict[tuple, list[_Pair]] = {}
    for p in pairs:
        by_col.setdefault((INTRO._qualified(p.t), p.c.name.lower()), []).append(p)
    kept: list[tuple[_Pair, Optional[str]]] = []   # (pair, review reason or None when written)
    for col_pairs in by_col.values():
        passing = [p for p in col_pairs if p.check is not None and p.check.borne_out]
        declared = [p for p in passing if p.declared]
        if len(declared) == 1 or (len(passing) == 1 and not declared):
            win = declared[0] if declared else passing[0]
        else:
            win = _most_specific(passing, found.mapping, parents) if passing else None
        if win is not None:
            kept.append((win, None if win.declared else f"found by {win.found_by}; the dictionary does not declare it"))
        elif passing:
            kept += [(p, "points at several tables that are not one class inside another") for p in passing]
        else:
            kept += [(p, f"partial match ({PF.percent(_rate(p))} of rows find a partner)") for p in col_pairs
                     if p.check is not None and _rate(p) >= REVIEW_MIN_MATCH]
            kept += [(p, "declared by the dictionary, but could not be checked on the data") for p in col_pairs
                     if p.declared and (p.check is None or not p.check.ran)]
    links_between: dict[tuple, int] = {}
    for p, _ in kept:
        k = (INTRO._qualified(p.t), INTRO._qualified(p.u))
        links_between[k] = links_between.get(k, 0) + 1
    for p, why in kept:
        if why is None:
            found.trusted.append({
                "from_table": p.t.name, "from_column": p.c.name, "to_table": p.u.name,
                "confidence": "confirmed", "review_state": "approved", "signed_off_by": "agami_enrich",
                "signed_off_role": "system", "signed_off_at": INTRO._NOW,
                "description": f"reference: {p.c.name} → {p.u.name}.{p.u.grain[0]} "
                               "(declared by the platform's dictionary, confirmed on the data)"})
            continue
        if links_between[(INTRO._qualified(p.t), INTRO._qualified(p.u))] > 1:
            why += "; another link joins the same two tables"
        found.review.append(_review(p, why))


def store_for_review(root: Path, review: list[dict], written: set[tuple[str, str]]) -> Optional[Path]:
    """Add the candidates to `<model>/.introspect/join_candidates.json` beside introspection's, by id,
    and drop any stored candidate whose column a written join now covers."""
    import json

    doc = INTRO._read_introspect_file(root, INTRO.JOIN_CANDIDATES_FILE)
    ids = {c["id"] for c in review}

    def keep(c) -> bool:
        op = c.get("op") if isinstance(c, dict) else None
        return (isinstance(op, dict) and c.get("id") not in ids
                and ((op.get("from_table") or "").lower(), (op.get("from_column") or "").lower()) not in written)

    doc["candidates"] = [c for c in doc.get("candidates") or [] if keep(c)] + review
    doc["low_overlap"] = [c for c in doc.get("low_overlap") or [] if keep(c)]
    doc.setdefault("how_to_accept", INTRO._accept_command(str(root)))
    path = root / ".introspect" / INTRO.JOIN_CANDIDATES_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path
