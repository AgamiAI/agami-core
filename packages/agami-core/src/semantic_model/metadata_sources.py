"""Enrich a semantic model from STRUCTURED SOURCES that already live in the database —
self-describing metadata tables (a data dictionary) and code→label lookup tables — instead
of guessing column meanings or fetching external docs.

This is GENERAL, not vendor-specific. Many systems store their own schema metadata in tables:
ServiceNow (`sys_dictionary` field defs + `sys_choice` value labels), SAP (`DD03L`/`DD02T`),
Salesforce metadata, most metadata-driven / low-code platforms. And countless ordinary schemas
carry code→label lookup / dimension tables (`order_status_codes`, `region_dim`, …). All of them
can be read the same way: point at a table, give a column mapping, get back curate ops.

`PRESETS` names the table + column mapping for *recognized* platforms (ServiceNow first), so the
common case is one-touch; everything else is driven by an explicit mapping the caller supplies.
Adding a platform = adding a preset row, not new logic.

The functions here are PURE transforms (rows -> curate ops / reference specs) so they test without
a database. Fetching the rows (a live query) and applying the ops (`curate.apply`) is the CLI's
job — this module never touches the DB or disk.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

# A (table, column) pair, lower-cased, used to filter source rows down to columns the model
# actually has — a dictionary table describes the whole platform, most of which isn't modelled.
ColKey = tuple[str, str]


# ---------------------------------------------------------------------------
# Preset registry — recognized in-DB metadata/lookup sources
# ---------------------------------------------------------------------------
# Each preset maps a logical role ("choice" / "dictionary") to the source table name + which of
# its columns carry the (table, column, value/label/comment/type/reference) facts. Declarative
# config; `detect_preset` / the CLI consume it. NOT branching logic in the engine.

PRESETS: dict[str, dict] = {
    "servicenow": {
        "choice": {
            "source": "sys_choice",
            "table_col": "name", "column_col": "element",
            "value_col": "value", "label_col": "label",
        },
        "dictionary": {
            "source": "sys_dictionary",
            "table_col": "name", "column_col": "element",
            "label_col": "column_label", "comment_col": "comments",
            "type_col": "internal_type", "reference_col": "reference",
            "reference_type": "reference",
        },
        # The class table: each class names its parent class by the parent's record id. A child's
        # record is also a row of every ancestor (an incident is also a task), with the same id.
        "hierarchy": {
            "source": "sys_db_object",
            "table_col": "name", "id_col": "sys_id", "parent_col": "super_class",
        },
        # Every record's id column, and how an export names a link column holding one (ACE-181).
        "key": "sys_id",
        "link_suffix": "_sys_id",
        # KNOWN reference graph — purely declarative config (field name → target table) for
        # ServiceNow's standard, published reference fields. This exists because a sparse export's
        # sys_dictionary often DOESN'T declare these (the joins live as sys_id GUIDs with no
        # readable target), so deterministic extraction alone returns a thin graph. The engine
        # applies this map inheritance-aware AND VERIFIES every candidate by value-overlap against
        # the live data before adding — so a standard join that doesn't match THIS export is simply
        # dropped, never blindly trusted. It's config + verification, not engine logic, and not a
        # substitute for the dictionary (instance declarations override these on conflict).
        "reference_graph": {
            "caller_id": "sys_user", "opened_by": "sys_user", "closed_by": "sys_user",
            "resolved_by": "sys_user", "assigned_to": "sys_user", "opened_for": "sys_user",
            "requested_by": "sys_user", "requested_for": "sys_user", "watch_list": "sys_user",
            "assignment_group": "sys_user_group", "group": "sys_user_group",
            "cmdb_ci": "cmdb_ci", "business_service": "cmdb_ci_service",
            "company": "core_company", "location": "cmn_location",
            "department": "cmn_department", "cost_center": "cmn_cost_center",
            "parent": "task", "problem_id": "problem", "rfc": "change_request",
            "request": "sc_request", "request_item": "sc_req_item", "cat_item": "sc_cat_item",
        },
        # Platform SYSTEM-COLUMN convention: ServiceNow prefixes its bookkeeping columns `sys_`
        # (sys_domain, sys_class_name, sys_tags, sys_created_on, …). Their name IS their meaning,
        # so the coverage gate treats them as self-evident and doesn't force a "name → the name"
        # description. A platform convention → config (NOT the universal self-evident regex).
        "self_evident": r"^sys_",
    },
}


def known_reference_graph(preset: Optional[str]) -> dict[str, str]:
    """The preset's declarative `field name → target table` map (empty if none). Config only —
    every entry is overlap-verified by the engine before any join is written."""
    return dict(PRESETS.get(preset or "", {}).get("reference_graph", {}))


def self_evident_pattern(preset: Optional[str]):
    """Compiled regex of ADDITIONAL self-evident column-name conventions for a preset (system /
    bookkeeping columns whose name is its own meaning), or None. Lets the coverage gate skip a
    platform's system columns without forcing anti-pattern descriptions — and without baking the
    platform's names into the universal self-evident regex. Config, not engine logic."""
    pat = PRESETS.get(preset or "", {}).get("self_evident")
    return re.compile(pat, re.IGNORECASE) if pat else None


# A preset value carries the platform's facts as dicts ("choice"/"dictionary"/"reference_graph")
# OR scalars ("self_evident" is a regex string). Only the dict roles name a source TABLE, so
# detection considers dict values only.
def detect_preset(table_names: Iterable[str]) -> Optional[str]:
    """The preset whose source table(s) are present in the model, else None. Matches if ANY of a
    preset's sources exist (a model might carry `sys_dictionary` but not `sys_choice`, or vice
    versa); `sm enrich-metadata` then reads whichever of its roles it finds."""
    have = {t.lower() for t in table_names}
    for key, spec in PRESETS.items():
        if any(isinstance(role, dict) and role.get("source", "").lower() in have
               for role in spec.values()):
            return key
    return None


# ---------------------------------------------------------------------------
# Pure transforms: source rows -> curate ops / reference specs
# ---------------------------------------------------------------------------


def _norm(v) -> str:
    return ("" if v is None else str(v)).strip()


def choice_field_ops(
    rows: Iterable[dict], *, table_col: str, column_col: str, value_col: str, label_col: str,
    valid: Optional[set[ColKey]] = None,
) -> list[dict]:
    """Curate edit ops setting `choice_field` from a self-describing choice/lookup table
    (rows of table, column, value, label). Groups by (table, column); blank labels and blank
    values are skipped; a column with no labelled values yields no op. `valid` (lower-cased
    (table, column) pairs) restricts output to columns the model actually has."""
    by_col: dict[ColKey, dict[str, str]] = {}
    for r in rows:
        tbl, col, lab = _norm(r.get(table_col)), _norm(r.get(column_col)), _norm(r.get(label_col))
        val = r.get(value_col)
        if not (tbl and col and lab) or val is None or _norm(val) == "":
            continue
        if valid is not None and (tbl.lower(), col.lower()) not in valid:
            continue
        by_col.setdefault((tbl, col), {})[_norm(val)] = lab
    return [
        {"op": "edit", "kind": "table", "name": tbl, "column": col,
         "field": "choice_field", "value": mapping}
        for (tbl, col), mapping in sorted(by_col.items())
    ]


def description_ops(
    rows: Iterable[dict], *, table_col: str, column_col: str,
    label_col: Optional[str] = None, comment_col: Optional[str] = None,
    valid: Optional[set[ColKey]] = None,
) -> list[dict]:
    """Curate edit ops setting column `description` from a metadata/dictionary table. Prefers the
    longer `comment` text, falls back to the short `label`. Stamped `source:"metadata"` so the
    description records its authoritative provenance (NOT an LLM guess, NOT validated-through-use).
    First non-empty row per (table, column) wins; `valid` restricts to modelled columns."""
    seen: set[ColKey] = set()
    ops: list[dict] = []
    for r in rows:
        tbl, col = _norm(r.get(table_col)), _norm(r.get(column_col))
        if not (tbl and col) or (tbl, col) in seen:
            continue
        if valid is not None and (tbl.lower(), col.lower()) not in valid:
            continue
        desc = (_norm(r.get(comment_col)) if comment_col else "") or \
               (_norm(r.get(label_col)) if label_col else "")
        if not desc:
            continue
        seen.add((tbl, col))
        ops.append({"op": "edit", "kind": "table", "name": tbl, "column": col,
                    "field": "description", "value": desc, "source": "metadata"})
    return ops


def descriptions_by_element(
    rows: Iterable[dict], *, column_col: str,
    label_col: Optional[str] = None, comment_col: Optional[str] = None,
) -> dict[str, str]:
    """Map a column NAME (element) → its dictionary description, keyed by element NOT the declaring
    table — so a description declared once under a PARENT table (ServiceNow declares shared fields
    under `sys_dictionary.name='task'`) PROPAGATES to the inheriting children that physically have
    the column (`incident`/`problem`/`change`), which the table-keyed `description_ops` misses.
    Prefers the longer comment, falls back to the short label. Elements whose description CONFLICTS
    across declarations are dropped as ambiguous (same guard as `reference_declarations`), so an
    unrelated `state`/`name` collision never propagates the wrong meaning."""
    desc: dict[str, str] = {}
    conflict: set[str] = set()
    for r in rows:
        el = _norm(r.get(column_col)).lower()
        d = (_norm(r.get(comment_col)) if comment_col else "") or \
            (_norm(r.get(label_col)) if label_col else "")
        if not (el and d):
            continue
        if el in desc and desc[el] != d:
            conflict.add(el)
        else:
            desc[el] = d
    for el in conflict:
        desc.pop(el, None)
    return desc


def reference_declarations(
    rows: Iterable[dict], *, column_col: str, type_col: str, reference_col: str,
    reference_type: str = "reference",
) -> dict[str, str]:
    """Map a reference FIELD NAME → its target table, from a metadata table's reference rows.

    Keyed by the ELEMENT (column), deliberately NOT by the declaring table. ServiceNow (and any
    table-inheritance platform) declares a shared reference field ONCE on the base table —
    `assignment_group → sys_user_group` lives under `sys_dictionary.name='task'` — but the column
    physically exists on every child (`incident`/`problem`/`change`). Keying on the field name lets
    the caller resolve the join for whichever table actually HAS that column, which is exactly how
    inheritance surfaces in the data. (Matching the declaring table instead — the old behaviour —
    returned 0 joins for the children.) Elements whose target CONFLICTS across declarations are
    dropped as ambiguous."""
    target: dict[str, str] = {}
    conflict: set[str] = set()
    for r in rows:
        if _norm(r.get(type_col)).lower() != reference_type.lower():
            continue
        el, tgt = _norm(r.get(column_col)).lower(), _norm(r.get(reference_col))
        if not (el and tgt):
            continue
        if el in target and target[el] != tgt:
            conflict.add(el)
        else:
            target[el] = tgt
    for el in conflict:
        target.pop(el, None)
    return target


# ---------------------------------------------------------------------------
# Renamed layers: a platform's tables reach the warehouse under other names (ACE-181)
# ---------------------------------------------------------------------------
# An export renames every table the same way (`acme_task_v`), splits a reference into its own
# `<field>_sys_id` column, and parks the platform's metadata beside it (`sys_dictionary_raw`). The
# affixes are read off the names themselves: a first or last word many names share is a layer's.


def common_affixes(names: Iterable[str], *, min_share: float = 0.0) -> tuple[set[str], set[str]]:
    """(prefix words, suffix words) that start or end at least two of `names` and at least
    `min_share` of them: the words a renamed layer puts on every table."""
    split = [n.lower().split("_") for n in names]
    split = [w for w in split if len(w) > 1]
    need = max(2, int(min_share * len(split) + 0.999))
    firsts: dict[str, int] = {}
    lasts: dict[str, int] = {}
    for w in split:
        firsts[w[0]] = firsts.get(w[0], 0) + 1
        lasts[w[-1]] = lasts.get(w[-1], 0) + 1
    return ({w for w, n in firsts.items() if n >= need}, {w for w, n in lasts.items() if n >= need})


def stems(name: str, prefixes: set[str], suffixes: set[str]) -> list[str]:
    """`name` with layer words stripped from either end, the least stripped first:
    `acme_task_v` -> acme_task_v, acme_task, task_v, task."""
    words = name.lower().split("_")
    out: list[tuple[int, str]] = []
    for i in range(len(words)):
        if i and words[i - 1] not in prefixes:
            break
        for j in range(len(words) - i):
            if j and words[len(words) - j] not in suffixes:
                break
            out.append((i + j, "_".join(words[i:len(words) - j])))
    return [s for _, s in sorted(out, key=lambda x: x[0])]


def locate(source: str, catalog: Iterable[tuple[Optional[str], str]],
           prefer_schemas: Iterable[Optional[str]] = (),
           fits: Optional[Callable[[Optional[str], str], bool]] = None) -> Optional[tuple[Optional[str], str]]:
    """The catalog object a preset's source table is, in any schema: its own name, else the name a
    renamed layer gave it, and only one that `fits` (has the columns the role reads), so a sibling
    such as `sys_choice_set` is never taken for `sys_choice`. The least-renamed match wins; a tie
    between schemas goes to one the model already reads, and is otherwise left unresolved."""
    catalog = list(catalog)
    pre, suf = common_affixes(t for _, t in catalog)
    best = [(0 if t.lower() == source.lower() else 1, (s, t)) for s, t in catalog
            if source.lower() in stems(t, pre, suf) and (fits is None or fits(s, t))]
    if not best:
        return None
    top = [x for c, x in best if c == min(c for c, _ in best)]
    preferred = {(p or "").lower() for p in prefer_schemas}
    if len(top) > 1:
        top = [x for x in top if (x[0] or "").lower() in preferred]
    return top[0] if len(top) == 1 else None


def class_parents(rows: Iterable[dict], *, table_col: str, id_col: str, parent_col: str) -> dict[str, str]:
    """`class -> parent class` from a class table whose rows name their parent by its record id."""
    rows = list(rows)
    by_id = {_norm(r.get(id_col)): _norm(r.get(table_col)).lower() for r in rows}
    out = {}
    for r in rows:
        parent = by_id.get(_norm(r.get(parent_col)))
        if parent and _norm(r.get(table_col)):
            out[_norm(r.get(table_col)).lower()] = parent
    return out


def ancestors(cls: str, parents: dict[str, str]) -> list[str]:
    """`cls`'s parent classes, nearest first (a loop in the class table stops the walk)."""
    out: list[str] = []
    start = cls
    while (cls := parents.get(cls, "")) and cls not in out and cls != start:
        out.append(cls)
    return out


def column_field(column: str, key: str, link_suffix: str) -> Optional[str]:
    """The platform field a column carries: `assigned_to_sys_id` -> `assigned_to`; the record's own id
    is no field."""
    c = column.lower()
    if c == key.lower():
        return None
    return c[: -len(link_suffix)] if link_suffix and c.endswith(link_suffix.lower()) and len(c) > len(link_suffix) else c


def fields_by_class(rows: Iterable[dict], *, table_col: str, column_col: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in rows:
        cls, el = _norm(r.get(table_col)).lower(), _norm(r.get(column_col)).lower()
        if cls and el:
            out.setdefault(cls, set()).add(el)
    return out


def references_by_class(rows: Iterable[dict], *, table_col: str, column_col: str, type_col: str,
                        reference_col: str, reference_type: str = "reference") -> dict[tuple[str, str], str]:
    """`(class, field) -> target class` for every reference field the dictionary declares. Keyed by
    the declaring class as well, so a field name two classes point at different targets (`parent`)
    keeps both, where `reference_declarations` has to drop it."""
    out = {}
    for r in rows:
        if _norm(r.get(type_col)).lower() == reference_type.lower() and _norm(r.get(reference_col)):
            out[(_norm(r.get(table_col)).lower(), _norm(r.get(column_col)).lower())] = _norm(r.get(reference_col)).lower()
    return out


# A table maps by its fields only when the best class shares this many of them and leads the
# runner-up by this much of the overlap score: otherwise the guess is left unmade.
MAP_MIN_SHARED = 3
MAP_MARGIN = 0.1


def map_tables(tables: dict[str, set[str]], fields: dict[str, set[str]], parents: dict[str, str],
               *, key: str, link_suffix: str) -> dict[str, dict]:
    """`table -> {"class", "by", ...evidence}` for each modelled table that is one of the platform's
    classes: by name once the layer's words are stripped, else by the fields its columns carry (a
    class's own and its parents'), when one class clearly leads."""
    pre, suf = common_affixes(tables, min_share=0.25)
    # A field most classes declare is bookkeeping (created on, updated by): sharing it says nothing
    # about which class a table is, and let a custom table map to a class that has little else.
    declared_by: dict[str, int] = {}
    for f in fields.values():
        for name in f:
            declared_by[name] = declared_by.get(name, 0) + 1
    common = {n for n, k in declared_by.items() if len(fields) >= 3 and k * 2 > len(fields)}
    full = {c: set(f).union(*(fields.get(a, set()) for a in ancestors(c, parents))) - common
            for c, f in fields.items()}
    out: dict[str, dict] = {}
    for t, cols in tables.items():
        named = next((s for s in stems(t, pre, suf) if s in fields), None)
        if named:
            out[t] = {"class": named, "by": "name"}
            continue
        have = {f for f in (column_field(c, key, link_suffix) for c in cols) if f} - common
        scored = sorted(((len(have & f) / len(have | f), len(have & f), c) for c, f in full.items() if have & f),
                        reverse=True)
        if not scored or scored[0][1] < MAP_MIN_SHARED:
            continue
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if scored[0][0] - runner_up >= MAP_MARGIN:
            out[t] = {"class": scored[0][2], "by": "fields", "shared_fields": scored[0][1],
                      "score": round(scored[0][0], 3), "runner_up_score": round(runner_up, 3)}
    return out


def view_choice_ops(rows: Iterable[dict], *, table_col: str, column_col: str, value_col: str, label_col: str,
                    held: dict[str, dict[str, list[str]]], chains: dict[str, list[str]], key: str,
                    link_suffix: str) -> list[dict]:
    """`choice_field` ops for renamed tables: the labels the choice table gives a class's field (or a
    parent class's), keyed by the values each column actually holds (`held`: table -> column ->
    values). A held value matches a choice's value or its label ignoring case, so an export that
    wrote `NEW` where the platform stores `new` and shows `New` still gets its label."""
    labels: dict[tuple[str, str], dict[str, str]] = {}
    for r in rows:
        cls, el, lab, val = (_norm(r.get(table_col)).lower(), _norm(r.get(column_col)).lower(),
                             _norm(r.get(label_col)), _norm(r.get(value_col)))
        if cls and el and lab and val:
            m = labels.setdefault((cls, el), {})
            m.setdefault(val.lower(), lab)
            m.setdefault(lab.lower(), lab)
    ops = []
    for t, cols in sorted(held.items()):
        for col, values in sorted(cols.items()):
            f = column_field(col, key, link_suffix)
            m = next((labels[(a, f)] for a in chains.get(t, []) if (a, f) in labels), None)
            mapping = {v: m[v.lower()] for v in values if m and v.lower() in m}
            if mapping:
                ops.append({"op": "edit", "kind": "table", "name": t, "column": col,
                            "field": "choice_field", "value": mapping})
    return ops
