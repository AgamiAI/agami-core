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
from typing import Iterable, Optional

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
# OR scalars ("self_evident" is a regex string). Only the dict roles name a source TABLE, so the
# detection/usability helpers consider dict values only.
def detect_preset(table_names: Iterable[str]) -> Optional[str]:
    """The preset whose source table(s) are present in the model, else None. Matches if ANY of a
    preset's sources exist (a model might carry `sys_dictionary` but not `sys_choice`, or vice
    versa) — the caller then uses whichever roles are actually available via `usable_sources`."""
    have = {t.lower() for t in table_names}
    for key, spec in PRESETS.items():
        if any(isinstance(role, dict) and role.get("source", "").lower() in have
               for role in spec.values()):
            return key
    return None


def usable_sources(preset: str, table_names: Iterable[str]) -> dict[str, dict]:
    """The preset's roles whose source table is actually present in the model."""
    have = {t.lower() for t in table_names}
    return {role: cfg for role, cfg in PRESETS.get(preset, {}).items()
            if isinstance(cfg, dict) and cfg.get("source", "").lower() in have}


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
