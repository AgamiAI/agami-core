"""Curation engine for the semantic model — the model-native replacement for the
legacy compute_confidence.py + apply_model_exclusions.py + the review/explorer
item-building logic.

Two read views + one write path, all over the on-disk model tree:

  review_queue(org)   -> the trust-review items: entries needing sign-off
                         (review_state != approved) or low confidence
                         (proposed / inferred), partitioned Rule 1 (metrics) vs
                         Rule 2 (relationships / entities). Feeds the Review tab of /agami-model.
  model_tree(org)     -> the browsable area→table→column tree with each node's
                         review_state. Feeds /agami-model (the explorer).
  apply(root, ops)    -> flip review_state (exclude/include/approve/reject), record
                         sign-off, or edit a field — on the canonical YAMLs, gated
                         by the validator, best-effort git-committed, with revert on
                         validation failure. The single write path both skills use.

Locators address an entry uniquely: {kind, area, name, [column]} where kind ∈
{table, column, entity, metric, relationship}. relationships use name = "from->to".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import validator as V
from .loader import _read_yaml as _load
from .loader import load_datasource
from .models import CrossSubjectAreaRelationship, Datasource, Entity, Metric, Relationship

# Process-lived incremental-validation cache (ACE-046). Every curation write re-validates the whole
# model; keyed by (area content + table registry) so an enrichment run — which edits one area at a
# time — only re-runs the changed area's rules instead of the whole model on every edit. Safe to
# share process-wide: entries are content-addressed, so a hit is always a byte-identical result;
# `validate` self-bounds its size.
_VALIDATION_CACHE: V.ValidationCache = {}

# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------


def _needs_review(obj) -> bool:
    """review_state is the gate: an entry needs a human iff it's unreviewed or has
    drifted stale. Approval/rejection clears it regardless of the confidence label
    (a human signing off IS the confirmation)."""
    return getattr(obj, "review_state", "approved") in ("unreviewed", "stale")


def review_queue(org: Datasource) -> dict[str, Any]:
    """Build the review queue. Rule 1 (metrics — block at runtime until signed off)
    is surfaced separately from Rule 2 (relationships / entities — lazy)."""
    rule1: list[dict] = []
    rule2: list[dict] = []

    for sa in org.subject_areas:
        for mm in sa.metrics:
            if _needs_review(mm):
                rule1.append(_metric_item(sa.name, mm))
        for rel in sa.relationships:
            if _needs_review(rel):
                rule2.append(_rel_item(sa.name, rel))
        for ent in sa.entities:
            if _needs_review(ent):
                rule2.append(_entity_item(sa.name, ent))
    for mm in org.cross_subject_area_metrics:
        if _needs_review(mm):
            rule1.append(_metric_item(None, mm))
    for rel in org.cross_subject_area_relationships:
        if _needs_review(rel):
            item = _rel_item(getattr(rel, "from_subject_area", None), rel)
            item["cross_area"] = True
            rule2.append(item)

    return {
        "rule_1": rule1,            # metrics — sign-off required before queries use them
        "rule_2": rule2,            # joins / entities — lazy, self-approve as queried
        "counts": {"rule_1": len(rule1), "rule_2": len(rule2),
                   "total": len(rule1) + len(rule2)},
    }


def _tab(obj) -> str:
    rs = getattr(obj, "review_state", "approved")
    if rs == "rejected":
        return "rejected"
    if _needs_review(obj):
        return "review"
    if rs == "approved" and (getattr(obj, "signed_off_role", None) == "system"
                             or getattr(obj, "signed_off_by", None) in (None, "agami_introspect")):
        return "auto"
    return "manual"


def all_items(org: Datasource, *, scope: str = "all") -> list[dict]:
    """Every curatable entry (metric / relationship / entity), tab-classified, for
    the 4-tab review dashboard (For Review · Auto · Manual · Rejected). Tables and
    columns are curated in the model explorer, not here.

    scope filters what's returned (the renderer renders exactly this — no skill-side
    filtering, no env var):
      "all"   — every entry, all tabs (the full /agami-model Review tab).
      "rule1" — only Rule-1 items needing sign-off (metrics + named filters in the
                review tab). The agami-connect Phase 4 gate uses this; the rendered
                item count then equals the sign-off count exactly.
      "rule2" — only Rule-2 items needing review (relationships + entities).
      "preseed" — what NL→SQL seeds depend on: metrics + named filters + entities
                needing review (relationships excluded — those stay lazy). The
                agami-connect "curate before examples" gate uses this."""
    items: list[dict] = []
    for sa in org.subject_areas:
        for mm in sa.metrics:
            items.append({**_metric_item(sa.name, mm), "tab": _tab(mm)})
        for rel in sa.relationships:
            items.append({**_rel_item(sa.name, rel), "tab": _tab(rel)})
        for ent in sa.entities:
            items.append({**_entity_item(sa.name, ent), "tab": _tab(ent)})
    for mm in org.cross_subject_area_metrics:
        items.append({**_metric_item(None, mm), "tab": _tab(mm)})
    for rel in org.cross_subject_area_relationships:
        it = {**_rel_item(getattr(rel, "from_subject_area", None), rel), "tab": _tab(rel),
              "cross_area": True}
        items.append(it)
    if scope == "rule1":
        items = [it for it in items if it["rule"] == 1 and it["tab"] == "review"]
    elif scope == "rule2":
        items = [it for it in items if it["rule"] == 2 and it["tab"] == "review"]
    elif scope == "preseed":
        items = [it for it in items
                 if it["entity_type"] in ("metric", "named_filter", "entity") and it["tab"] == "review"]
    elif scope != "all":
        raise ValueError(f"unknown scope {scope!r} (expected all|rule1|rule2|preseed)")
    # stable order: Rule 1 first, then by tab (review → auto → manual → rejected)
    order = {"review": 0, "auto": 1, "manual": 2, "rejected": 3}
    items.sort(key=lambda it: (0 if it["rule"] == 1 else 1, order.get(it["tab"], 9), it["name"]))
    for i, it in enumerate(items, 1):
        it["n"] = i
    return items


_MIN_GATE_COLS = 2  # below this a table is too small to confidently call "skipped"

# A blank column whose NAME makes its meaning self-evident (keys, audit cols, flags) may
# stay blank by design. A blank column whose name is NOT self-evident is a SKIPPED
# meaningful column — enrichment should have described it or marked it `ai_unknown`. This
# is what makes the gate able to tell "intentionally blank" from "the pass didn't finish".
_SELF_EVIDENT_NAME_RE = re.compile(
    r"^(id|.*_id|.*_no|.*_uuid|.*_guid|.*_key|created.*|modified.*|updated.*|deleted.*|"
    r".*_at|.*_on|.*_by|.*_date|.*_ts|.*_time|.*timestamp|.*_count|is_.*|has_.*|.*_flag)$",
    re.IGNORECASE,
)
# More than this many skipped-meaningful columns in one table ⇒ under-enriched. A couple of
# genuinely-ambiguous columns are tolerated; a wall of them means the column pass stopped early.
_MEANINGFUL_BLANK_TOLERANCE = 2


def column_coverage(org: Datasource) -> dict:
    """Per-table column-description coverage — the enrichment-completeness check.

    The signal is **table-level**, which keeps it conflict-free with the skill's
    deliberate "leave self-evident columns (`id`, `created_at`) blank" rule. Per
    *column*, a blank description is ambiguous (correctly-self-evident vs. skipped);
    per *table*, it isn't. A real enrichment pass over any non-trivial table produces
    at least one described or `ai_unknown` column (every table has data/coded/FK
    columns worth a line). A table enrichment **never ran on** has ZERO — that's the
    failure mode where the model got table descriptions but no column pass.

    Two failure modes gate `ok`:
      - `unenriched` — a table with `described == 0 AND ai_unknown == 0` (≥ `_MIN_GATE_COLS`
        columns): the column pass never ran on it.
      - `under_enriched` — a table the pass touched but which still has more than
        `_MEANINGFUL_BLANK_TOLERANCE` blank columns whose NAMES aren't self-evident (not
        `*_id`/`*_date`/`created_*`/…). Those are SKIPPED meaningful columns — the pass should
        have described them or marked them `ai_unknown`. `blank_meaningful_columns` names them.
    `ok` is true only when neither list has entries. Rejected tables/columns are excluded.
    `coverage_pct` stays informational — self-evident blanks legitimately hold it below 100%."""
    tables: list[dict] = []
    tot = {"columns": 0, "described": 0, "ai_unknown": 0, "blank": 0, "meaningful_blank": 0}
    unenriched: list[str] = []
    under_enriched: list[str] = []
    # A detected platform (e.g. ServiceNow) may declare extra self-evident column conventions
    # (its `sys_` system columns) in the PRESET config — union them with the universal regex so
    # the gate doesn't force "name → the name" descriptions on a platform's bookkeeping columns.
    from . import metadata_sources as _MS
    _preset = _MS.detect_preset([t.name for s in org.subject_areas for t in s.tables_defined])
    _preset_self_evident = _MS.self_evident_pattern(_preset)

    def _self_evident(name: str) -> bool:
        return bool(_SELF_EVIDENT_NAME_RE.match(name)
                    or (_preset_self_evident and _preset_self_evident.match(name)))

    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if getattr(t, "review_state", "approved") == "rejected":
                continue
            described = ai_unknown = blank = meaningful_blank = 0
            blank_meaningful_names: list[str] = []
            for c in t.columns:
                if getattr(c, "review_state", "approved") == "rejected":
                    continue
                src = getattr(c, "description_source", None)
                desc = (getattr(c, "description", "") or "").strip()
                if desc:
                    described += 1
                elif src == "ai_unknown":
                    ai_unknown += 1
                else:                                  # blank + no source: self-evident OR skipped
                    blank += 1
                    if not _self_evident(c.name):
                        meaningful_blank += 1
                        blank_meaningful_names.append(c.name)
            n = described + ai_unknown + blank
            enriched = (described + ai_unknown) > 0
            # under-enriched: the pass touched the table but left a wall of meaningful columns blank
            is_under = enriched and meaningful_blank > _MEANINGFUL_BLANK_TOLERANCE
            tables.append({
                "area": sa.name, "table": t.name, "columns": n,
                "described": described, "ai_unknown": ai_unknown, "blank": blank,
                "meaningful_blank": meaningful_blank,
                "blank_meaningful_columns": blank_meaningful_names,
                "coverage_pct": round(100 * described / n) if n else 100,
                "enriched": enriched, "under_enriched": is_under,
            })
            if not enriched and n >= _MIN_GATE_COLS:
                unenriched.append(t.name)
            elif is_under:
                under_enriched.append(t.name)
            tot["columns"] += n
            tot["described"] += described
            tot["ai_unknown"] += ai_unknown
            tot["blank"] += blank
            tot["meaningful_blank"] += meaningful_blank
    # worst first: unenriched, then under-enriched, then by table
    tables.sort(key=lambda x: (x["enriched"], not x["under_enriched"], x["table"]))
    tot["coverage_pct"] = round(100 * tot["described"] / tot["columns"]) if tot["columns"] else 100
    return {
        "tables": tables,
        "totals": tot,
        "unenriched_tables": unenriched,
        "under_enriched_tables": under_enriched,
        "ok": not unenriched and not under_enriched,
    }


def unlabeled_choice_fields(org: Datasource) -> dict:
    """Coded columns whose `choice_field` skeleton still has BLANK labels — introspection
    seeded `{value: ""}` and the enrichment hasn't filled the meanings yet. The enrichment
    checks this (like `column_coverage`) to confirm the value-enum decode actually ran;
    structured `choice_field` is what lets the generator map 'high severity' → severity=1."""
    cols: list[dict] = []
    total_choice = 0
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if getattr(t, "review_state", "approved") == "rejected":
                continue
            for c in t.columns:
                cf = c.choice_field
                if not cf or getattr(c, "review_state", "approved") == "rejected":
                    continue
                total_choice += 1
                blank = [v for v, lbl in cf.items() if not (lbl or "").strip()]
                if blank:
                    cols.append({"area": sa.name, "table": t.name, "column": c.name,
                                 "values": list(cf.keys()), "blank_labels": blank})
    return {"count": len(cols), "choice_columns": total_choice, "unlabeled": cols,
            "ok": not cols}


def sensitive_columns(org: Datasource) -> dict:
    """Every column introspection (or a curator) flagged `sensitive` and hasn't excluded —
    the PII the agami-connect curate gate uses to decide whether to open the explorer.
    Deterministic + resumable (a turn-boundary-safe count, unlike remembering the
    introspect report's number)."""
    cols: list[dict] = []
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if getattr(t, "review_state", "approved") == "rejected":
                continue
            for c in t.columns:
                if c.sensitive and getattr(c, "review_state", "approved") != "rejected":
                    cols.append({"area": sa.name, "table": t.name, "column": c.name})
    return {"count": len(cols), "columns": cols}


def suspected_sensitive_columns(org: Datasource) -> dict:
    """Columns the strict flag may have MISSED — `build.suspected_pii` matches the name but the
    column isn't marked `sensitive` (e.g. `first_name` in a non-PII-named table). Surfaced so a
    PII review catches false NEGATIVES, not just confirms hits. Excludes already-sensitive and
    rejected columns. A review aid — never auto-marks."""
    from . import build as B
    cols: list[dict] = []
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if getattr(t, "review_state", "approved") == "rejected":
                continue
            for c in t.columns:
                if (not c.sensitive and B.suspected_pii(c.name)
                        and getattr(c, "review_state", "approved") != "rejected"):
                    cols.append({"area": sa.name, "table": t.name, "column": c.name, "type": c.type})
    return {"count": len(cols), "columns": cols}


def _trust(obj) -> dict:
    return {
        "confidence": getattr(obj, "confidence", None),
        "review_state": getattr(obj, "review_state", None),
        "signed_off_by": getattr(obj, "signed_off_by", None),
        "signed_off_at": getattr(obj, "signed_off_at", None),
        "signed_off_role": getattr(obj, "signed_off_role", None),
    }


def _metric_item(area: Optional[str], mm) -> dict:
    # Fields match the review-items vocabulary (entity_type) AND the dashboard
    # ITEMS_JSON contract (rule_1, signals, extra_lines, …) so the card renders the
    # calculation and the review block emits `by <email> role=` for sign-off.
    binding_lines = [{"label": d, "text": sql} for d, sql in (mm.bindings or {}).items()]
    primary_sql = next(iter((mm.bindings or {}).values()), "")
    return {"kind": "metric", "entity_type": "metric", "rule": 1, "rule_1": True,
            "area": area, "name": mm.name, "title": mm.name,
            "subtitle": f"metric · {area}" if area else "metric",
            "source_signal": mm.calculation,
            "signals": [{"ok": True, "text": mm.calculation}],
            "extra_lines": [{"label": "Definition", "text": mm.calculation}] + binding_lines,
            "inferred": primary_sql, "origin": "llm_suggested",
            "bindings": mm.bindings, "business_question": mm.business_question,
            **_trust(mm)}


def _rel_item(area: Optional[str], rel) -> dict:
    join = (f"{rel.from_table}.{rel.from_column} → {rel.to_table}.{rel.to_column}"
            if rel.from_column else f"{rel.from_table} → {rel.to_table} ON {rel.on}")
    origin = "fk" if getattr(rel, "signed_off_role", None) == "system" else "introspect_heuristic"
    return {"kind": "relationship", "entity_type": "join", "rule": 2, "rule_1": False,
            "area": area, "name": f"{rel.from_table}->{rel.to_table}", "title": join,
            # which join between these two tables: a curate op carries these to name it exactly
            "from_column": rel.from_column, "to_column": rel.to_column, "on": rel.on,
            "from_schema": rel.from_schema, "to_schema": rel.to_schema,
            "subtitle": rel.relationship, "cardinality": rel.relationship,
            "source_signal": rel.description or join,
            "signals": [{"ok": True, "text": rel.description or f"{rel.relationship} · {join}"}],
            "inferred": join, "origin": origin,
            **_trust(rel)}


def _entity_item(area: Optional[str], ent) -> dict:
    maps = [f"{m.table}.{m.column}" for m in ent.maps_to]
    why = ent.description or (("maps to " + ", ".join(maps)) if maps else "entity")
    return {"kind": "entity", "entity_type": "entity", "rule": 2, "rule_1": False,
            "area": area, "name": ent.name, "title": ent.name, "subtitle": "entity",
            "source_signal": ent.description or "",
            "signals": [{"ok": True, "text": why}],
            "inferred": ", ".join(maps), "origin": "llm_suggested",
            "maps_to": maps, **_trust(ent)}


def model_tree(org: Datasource) -> dict[str, Any]:
    """Browsable tree for the model explorer: area → table → columns, each with its
    review_state so the UI can show what's excluded. Load the org with
    include_rejected=True to see excluded entries here."""
    areas = []
    for sa in org.subject_areas:
        tables = []
        for t in sa.tables_defined:
            tables.append({
                "table": t.name, "schema": t.schema_name,
                "description": t.description, "review_state": t.review_state,
                "grain": t.grain,
                "columns": [{"name": c.name, "type": c.type, "description": c.description,
                             "sensitive": c.sensitive, "review_state": c.review_state}
                            for c in t.columns],
            })
        areas.append({"area": sa.name, "description": sa.description, "tables": tables})
    return {"datasource": org.datasource, "subject_areas": areas}


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    validated: bool = False
    committed: bool = False
    # Why nothing was committed, in a sentence the skill can pass on; empty when it was.
    commit_note: str = ""

    def as_dict(self) -> dict:
        return {"applied": self.applied, "skipped": self.skipped, "errors": self.errors,
                "validated": self.validated, "committed": self.committed,
                **({"commit_note": self.commit_note} if self.commit_note else {})}


def _area_dir(root: Path, area: str) -> Path:
    return root / "subject_areas" / area


def _dump(path: Path, obj: Any) -> None:
    path.write_text(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100),
                    encoding="utf-8")


def _snapshot(backups: "dict[Path, Optional[str]]", path: Path) -> None:
    """Record a file's prior content before the first write to it (None if new), so the
    batch can be rolled back without git — apply()'s revert used to depend on a git repo
    that the artifacts dir often isn't, which silently left half-applied models on disk."""
    if path not in backups:
        backups[path] = path.read_text(encoding="utf-8") if path.exists() else None


def set_key_terminology(root: str | Path, terms: dict, *, merge: bool = True) -> "ApplyResult":
    """Write the org-level domain glossary (term -> definition) onto datasource.yaml's
    `key_terminology`. Validated + git-committed like every other write; the prior
    datasource.yaml is restored on validation failure (no git dependency for the revert).

    `merge=True` (default) layers `terms` over the existing glossary — so an
    enrichment pass adds without clobbering a human's edits; `merge=False` replaces.
    Empty terms/definitions are dropped."""
    root = Path(root)
    res = ApplyResult()
    dsp = root / "datasource.yaml"
    if not dsp.exists():
        res.errors.append(f"no datasource.yaml at {dsp}")
        return res
    prior = dsp.read_text(encoding="utf-8")
    odoc = _load(dsp) or {}
    existing = odoc.get("key_terminology") or {}
    if not isinstance(existing, dict):
        existing = {}
    incoming = {str(k).strip(): str(v).strip()
                for k, v in (terms or {}).items() if str(k).strip() and str(v).strip()}
    merged = {**existing, **incoming} if merge else incoming
    if merged:
        odoc["key_terminology"] = merged
    else:
        odoc.pop("key_terminology", None)
    _dump(dsp, odoc)
    try:
        vres = V.validate(load_datasource(root, include_rejected=True), cache=_VALIDATION_CACHE)
        res.validated = vres.ok
        if not vres.ok:
            res.errors = vres.errors
            dsp.write_text(prior, encoding="utf-8")    # revert, git-independent
            return res
    except Exception as e:
        res.errors.append(f"validation failed to run: {e}")
        dsp.write_text(prior, encoding="utf-8")
        return res
    res.applied = [f"key_terminology: {len(merged)} term(s)"]
    res.committed, res.commit_note = _record(
        root, f"terminology: {len(merged)} term(s)",
        [{"op": "edit", "kind": "datasource", "field": "key_terminology", "value": merged, "before": existing}],
        None, None)
    return res


def set_datasource_description(root: str | Path, description: str) -> "ApplyResult":
    """Write the datasource's one-line `description` onto datasource.yaml (#327).

    It is the line `list_datasources` shows an agent to route a question by — distinct from the
    `datasource.md` narrative, which is context read after the datasource is chosen. Validated and
    committed like `set_key_terminology`, with the prior file restored on a validation failure. Only
    the first line is kept and it is stripped: a routing label that spans paragraphs stops being one."""
    root = Path(root)
    res = ApplyResult()
    dsp = root / "datasource.yaml"
    if not dsp.exists():
        res.errors.append(f"no datasource.yaml at {dsp}")
        return res
    line = (description or "").strip().splitlines()[0].strip() if (description or "").strip() else ""
    if not line:
        res.errors.append("description is empty")
        return res
    prior = dsp.read_text(encoding="utf-8")
    odoc = _load(dsp) or {}
    before = odoc.get("description")
    odoc["description"] = line
    _dump(dsp, odoc)
    try:
        vres = V.validate(load_datasource(root, include_rejected=True), cache=_VALIDATION_CACHE)
        res.validated = vres.ok
        if not vres.ok:
            res.errors = vres.errors
            dsp.write_text(prior, encoding="utf-8")
            return res
    except Exception as e:
        res.errors.append(f"validation failed to run: {e}")
        dsp.write_text(prior, encoding="utf-8")
        return res
    res.applied = ["description"]
    res.committed, res.commit_note = _record(
        root, "datasource description",
        [{"op": "edit", "kind": "datasource", "field": "description", "value": line, "before": before}],
        None, None)
    return res


class BatchError(ValueError):
    """An area-less op on a join, entity or metric that matches no entry, or more than one. A skip
    would let the batch report success with that change missing, so apply() refuses the batch (#538)."""


_VALID_OPS = {"approve", "reject", "exclude", "include", "edit"}
# exclude == reject; include == set unreviewed + clear sign-off (model-explorer verbs)


def apply(root: str | Path, ops: list[dict], *, signer: Optional[str] = None,
          role: Optional[str] = None) -> ApplyResult:
    """Apply curation ops to the on-disk model, validate, commit (best-effort),
    revert on validation failure. Each op: {op, kind, area, name, [column], [field, value],
    [reason]} — a reject's `reason` is not stored on the model, only in curation_log.jsonl."""
    root = Path(root)
    res = ApplyResult()
    backups: dict[Path, Optional[str]] = {}  # path -> prior text (None if newly created)
    logged: list[dict] = []  # the ops that took, each with the value it replaced

    for op in ops:
        try:
            _, before = _apply_one(root, op, signer, role, backups)
            res.applied.append(_op_label(op))
            logged.append({**op, "before": before})
        except BatchError as e:
            res.errors.append(f"{_op_label(op)}: {e}")
        except Exception as e:
            res.skipped.append({"op": _op_label(op), "reason": str(e)})
    if res.errors:
        _restore(list(backups.items()))
        res.applied = []
        return res

    # validate the whole model after the batch
    try:
        org = load_datasource(root, include_rejected=True)
        vres = V.validate(org, cache=_VALIDATION_CACHE)
        res.validated = vres.ok
        if not vres.ok:
            res.errors = vres.errors
            _restore(list(backups.items()))   # git-independent revert
            res.applied = []  # nothing stuck
            return res
    except Exception as e:
        res.errors.append(f"validation failed to run: {e}")
        _restore(list(backups.items()))
        res.applied = []
        return res

    if res.applied:
        res.committed, res.commit_note = _record(root, f"curation: {len(res.applied)} change(s)",
                                                 logged, signer, role)
    return res


def _op_label(op: dict) -> str:
    t = op.get("name", "?")
    if op.get("column"):
        t += f".{op['column']}"
    return f"{op.get('op')} {op.get('kind')} {op.get('area','')}/{t}".strip()


def _resolve_area(root: Path, op: dict) -> str:
    """Which subject area owns this op's target, resolved by NAME when `area` is omitted — so
    callers (enrichment, save-correction) don't hand-maintain a table->area map. table/metric/
    entity resolve by file existence; relationship by matching endpoints. A name that lives in
    two areas (e.g. a table present in two schemas) is ambiguous -> raise, asking for `area`."""
    kind, name = op.get("kind"), op.get("name")
    sa_root = root / "subject_areas"
    if not sa_root.is_dir():
        raise FileNotFoundError("model has no subject_areas/ directory")
    areas = sorted(p.name for p in sa_root.iterdir() if p.is_dir())
    hits: list[str] = []
    for area in areas:
        d = sa_root / area
        found = False
        if kind == "table":
            found = (d / "tables" / f"{name}.yaml").exists()
        elif kind == "entity":
            found = (d / "entities" / f"{_slug(name)}.yaml").exists()
        elif kind == "metric":
            found = (d / "metrics" / f"{_slug(name)}.yaml").exists()
        elif kind == "relationship":
            relf = d / "relationships.yaml"
            if relf.exists():
                frm, _, to = (name or "").partition("->")
                doc = _load(relf) or {}
                rels = doc.get("relationships", doc if isinstance(doc, list) else [])
                found = any(r.get("from_table") == frm and r.get("to_table") == to for r in rels)
        if found:
            hits.append(area)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(
            f"no {kind} named {name!r} in any subject area (looked in: {', '.join(areas) or 'none'})")
    raise ValueError(
        f"{kind} {name!r} exists in multiple areas {hits} — pass an explicit 'area' to "
        "disambiguate (a same-named table can live in two schemas).")


def _apply_one(root: Path, op: dict, signer, role,
               backups: "Optional[dict[Path, Optional[str]]]" = None) -> tuple[Path, Any]:
    """Apply one op; return the file it wrote and the value it replaced, for the curation log."""
    if backups is None:
        backups = {}
    action = op.get("op")
    if action not in _VALID_OPS:
        raise ValueError(f"unknown op {action!r}")
    kind = op.get("kind")
    name = op.get("name")
    if not kind or not name:
        raise ValueError("op needs kind + name")
    # `area` is optional — resolve it from the model by name when the caller omits it. Joins, entities
    # and metrics can also live outside every area, so they search the cross-area places themselves.
    area = op.get("area")
    if not area and kind in ("table", "subject_area"):
        area = _resolve_area(root, op)

    new_state = {"approve": "approved", "include": "unreviewed",
                 "reject": "rejected", "exclude": "rejected"}.get(action)

    if kind == "table":
        path = _area_dir(root, area) / "tables" / f"{name}.yaml"
        _snapshot(backups, path)
        doc = _load(path)
        old_groups = dict(doc.get("column_groups") or {})
        if op.get("column"):
            before = _set_column_field(doc, op["column"], op, new_state, signer, role)
        elif op.get("op") == "edit" and op.get("field") == "recommended_filters":
            # The filter list lives inside `performance_hints` beside the measured row count; editing
            # it alone keeps the count, which a whole-block edit would make the caller restate.
            before = (doc.get("performance_hints") or {}).get("recommended_filters")
            doc["performance_hints"] = {**(doc.get("performance_hints") or {}),
                                        "recommended_filters": _filter_columns(doc, op.get("value"))}
        else:
            before = _set_trust(doc, op, new_state, signer, role, desc_source=True)  # table desc has provenance
        _dump(path, doc)
        # A column_groups edit renames the groups a TableRef may expose — reconcile so we
        # don't orphan `expose_column_groups` (which would fail validation).
        if op.get("op") == "edit" and op.get("field") == "column_groups" and not op.get("column"):
            _reconcile_expose_groups(root, area, name, doc.get("schema"), old_groups,
                                     doc.get("column_groups") or {}, backups)
            # The file's own `name`: it can carry the schema (`public.wide`) the op's name leaves out.
            _reconcile_ambiguity_groups(root, path, str(doc.get("name") or name), doc.get("schema"),
                                        old_groups, doc.get("column_groups") or {}, backups)
        return path, before

    if kind in ("entity", "metric"):
        path, key, doc, item = _find_item(root, kind, name, area)
        _snapshot(backups, path)
        before = _set_trust(item, op, new_state, signer, role)
        _dump(path, {key: doc} if isinstance(doc, list) else doc)
        return path, before

    if kind == "subject_area":
        # edit-only (e.g. set the area's `description`) — a subject area carries no trust block,
        # so approve/reject/exclude don't apply. Lets the enrichment pass replace the auto-proposed
        # boilerplate with a real business description through the validated curate path.
        if op.get("op") != "edit":
            raise ValueError("subject_area supports only edit ops (e.g. field=description)")
        fld, val = op.get("field"), op.get("value")
        if not fld:
            raise ValueError("edit op needs field")
        path = _area_dir(root, area) / "subject_area.yaml"
        if not path.exists():
            raise FileNotFoundError(f"no subject area {area!r}")
        _snapshot(backups, path)
        doc = _load(path)
        before = doc.get(fld)
        doc[fld] = val
        _dump(path, doc)
        return path, before

    if kind == "relationship":
        path, key, doc, rels, hit = _find_join(root, op, area)
        _snapshot(backups, path)
        before = _set_trust(hit, op, new_state, signer, role)
        _dump(path, doc if isinstance(doc, dict) else {key: rels})
        return path, before

    raise ValueError(f"unknown kind {kind!r}")


def _area_names(root: Path) -> list[str]:
    sa_root = root / "subject_areas"
    return sorted(p.name for p in sa_root.iterdir() if p.is_dir()) if sa_root.is_dir() else []


def _entries(doc: Any, key: str) -> list:
    return doc.get(key, []) if isinstance(doc, dict) else (doc if isinstance(doc, list) else [])


# Where a cross-area entity / metric lives: a list on datasource.yaml, or its own file.
_CROSS_ITEMS = {"metric": ("cross_subject_area_metrics", "cross_subject_area_metrics.yaml", "metrics"),
                "entity": ("cross_subject_area_entities", "cross_subject_area_entities.yaml", "entities")}


def _find_item(root: Path, kind: str, name: str, area: Optional[str]) -> tuple:
    """(path, list key or None, file doc, entry) of the one entity / metric an op names. With an area,
    only that area's file, as before; without one, every area and the cross-area lists."""
    # entities/metrics are stored under the SLUGGED name (write_items uses _slug) — a multi-word name
    # ("total event sales") must find total_event_sales.yaml. _slug is idempotent.
    sub, stem = _KINDS[kind][0], _slug(name)
    if area:
        path = _area_dir(root, area) / sub / f"{stem}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"no {kind} named {name!r} in area {area!r}")
        doc = _load(path)
        return path, None, doc, doc
    found: list[tuple[str, Path, Optional[str], Any, dict]] = []
    for a in _area_names(root):
        path = _area_dir(root, a) / sub / f"{stem}.yaml"
        if path.exists():
            doc = _load(path)
            found.append((a, path, None, doc, doc))
    ds_key, fname, fkey = _CROSS_ITEMS[kind]
    for path, key in ((root / "datasource.yaml", ds_key), (root / fname, fkey)):
        if path.exists():
            doc = _load(path) or {}
            found += [("cross-area", path, key, doc, it) for it in _entries(doc, key)
                      if isinstance(it, dict) and _slug(str(it.get("name") or "")) == stem]
    if len(found) == 1:
        return found[0][1:]
    if not found:
        raise BatchError(f"no {kind} named {name!r} in any subject area or the cross-area {sub}")
    raise BatchError(f"{kind} {name!r} is in {len(found)} places ({', '.join(f[0] for f in found)}); "
                     "pass the 'area' of the one you mean")


def _join_label(where: str, r: dict) -> str:
    on = (f"{r.get('from_table')}.{r.get('from_column')} -> {r.get('to_table')}.{r.get('to_column')}"
          if r.get("from_column") else f"ON {r.get('on')}")
    return f"{where}: {on}"


def _find_join(root: Path, op: dict, area: Optional[str]) -> tuple:
    """(path, list key, file doc, list, entry) of the one join an op names. The named area's file and
    the cross-area joins are searched first, as before; when they hold none (or no area is named),
    every area's file and the cross-area joins are."""
    name = op["name"]
    frm, _, to = name.partition("->")
    # The org-level cross-area joins live in datasource.yaml or in their own file, which is where
    # add_relationships writes them.
    cross = [(root / "datasource.yaml", "cross_subject_area_relationships"),
             (root / "cross_subject_area_relationships.yaml", "edges")]

    def search(area_names: list[str]) -> list:
        places = [(_area_dir(root, a) / "relationships.yaml", "relationships", a) for a in area_names]
        places += [(p, k, None) for p, k in cross]
        # Count matches across every place before choosing: a join in the area and one between areas
        # can share a table pair, and stopping at the first place with one match would pick one.
        out = []
        for path, key, a in places:
            if path.exists():
                doc = _load(path) or {}
                rels = _entries(doc, key)
                out += [(a or f"cross-area {hit.get('from_subject_area')}->{hit.get('to_subject_area')} "
                         f"in {path.name}", path, key, doc, rels, hit)
                        for hit in _matching_joins(rels, frm, to, op)]
        return out

    found = search([area]) if area else []
    if not found:
        found = search(_area_names(root))
    if area and len(found) != 1:
        # An op naming an area keeps main's skip; only an area-less op fails the batch (#538).
        if not found:
            raise ValueError(f"relationship {name} not found in the area or the cross-area relationships")
        raise ValueError(
            f"{len(found)} joins match {name}; name the one you mean with from_column and "
            "to_column, or its `on` condition, and from_schema / to_schema when the table "
            "names repeat across schemas — the table pair alone would pick one at random")
    if not found:
        named = ", ".join(f"{k}={op[k]}" for k in ("from_column", "to_column", "on", "from_schema",
                                                    "to_schema") if op.get(k))
        raise BatchError(f"no join {name}{f' ({named})' if named else ''} in any area or the "
                         "cross-area joins")
    if len(found) > 1:
        raise BatchError(
            f"{len(found)} joins match {name} ({'; '.join(_join_label(f[0], f[5]) for f in found)}); "
            "name the one you mean with from_column and to_column, or its `on` condition, and "
            "from_schema / to_schema when the table names repeat across schemas — the table pair "
            "alone would pick one at random")
    return found[0][1:]


def _matching_joins(rels: list, frm: str, to: str, op: dict) -> list:
    """The joins an op means: same two tables, narrowed by whichever of from_schema / to_schema /
    from_column / to_column / `on` the op names (`on` compared with whitespace collapsed, so a re-wrapped condition still matches)."""
    def norm(text) -> str:
        return " ".join(str(text or "").split()).lower()

    out = [r for r in rels if isinstance(r, dict) and r.get("from_table") == frm and r.get("to_table") == to]
    # The schemas tell apart same-named tables in two schemas (sales.orders vs archive.orders); a join
    # written before schemas were recorded has none, and still matches.
    for fld in ("from_schema", "to_schema"):
        if op.get(fld):
            out = [r for r in out if not r.get(fld) or str(r[fld]).lower() == str(op[fld]).lower()]
    for fld in ("from_column", "to_column"):
        if op.get(fld):
            out = [r for r in out if (r.get(fld) or "").lower() == str(op[fld]).lower()]
    if op.get("on"):
        out = [r for r in out if norm(r.get("on")) == norm(op["on"])]
    return out


def _reconcile_expose_groups(root: Path, area: str, table: str, schema: Optional[str],
                             old_groups: dict, new_groups: dict,
                             backups: "dict[Path, Optional[str]]") -> None:
    """After a table's `column_groups` are rewritten, carry every `TableRef.expose_column_groups`
    naming it over to the new groups, in EVERY subject area, not just the owner's.

    By columns, not by name: an area shows the new groups that contain any column its old groups
    held (a new group spanning the old boundary brings its other columns along). A regroup renames groups, so matching names would leave nothing
    and silently widen the area to every column. A table a spec lists in several areas has a
    reference in each, and one left behind fails validation and rolls the whole batch back (#465).
    The field is dropped when the new groups it maps to cover every group ("expose all"). A reference
    to a same-named table in another schema is a different table and is left alone, and so is an
    area that defines its own copy of the table: its groups are its own."""
    for sap in sorted((root / "subject_areas").glob("*/subject_area.yaml")):
        if sap.parent.name != area and (sap.parent / "tables" / f"{table}.yaml").exists():
            continue
        sa = _load(sap) or {}
        changed = False
        for tr in sa.get("tables", []) or []:
            if (tr.get("table") != table or not tr.get("expose_column_groups")
                    or (tr.get("schema") or "").lower() != (schema or "").lower()):
                continue
            shown = {c for g in tr["expose_column_groups"] for c in old_groups.get(g, [])}
            kept = [g for g, cols in new_groups.items() if shown & set(cols)]
            if not kept or set(kept) == set(new_groups):
                tr.pop("expose_column_groups", None)
            elif kept != tr["expose_column_groups"]:
                tr["expose_column_groups"] = kept
            else:
                continue
            changed = True
        if changed:
            _snapshot(backups, sap)
            _dump(sap, sa)


def _reconcile_ambiguity_groups(root: Path, table_path: Path, table: str, schema: Optional[str],
                                old_groups: dict, new_groups: dict,
                                backups: "dict[Path, Optional[str]]") -> None:
    """After a table's `column_groups` are rewritten, move every ambiguity anchor that names a
    RENAMED group of it to the new name (#487).

    A rename is a new group holding exactly the old group's columns. Without this, an anchor like
    `orders.dates` survived the rename of `dates` to `calendar` and failed validation, so the whole
    edit rolled back. Anything that is not a rename (a group split, merged or emptied) is left as it
    was: the anchor then fails validation and names the ambiguity, because which of several new groups
    should open the entry is a person's call, not ours. An anchor moves only when it resolves, the way
    the validator resolves it, to exactly the edited table: one naming another schema's table, or a
    bare name two schemas share, is left alone."""
    def cols(names: Any) -> frozenset:
        return frozenset(str(c).lower() for c in (names or []))

    kept = {g.lower() for g in new_groups}
    renamed: dict[str, str] = {}
    for group, members in old_groups.items():
        if group.lower() in kept:
            continue
        same = [g for g, m in new_groups.items() if cols(m) == cols(members)]
        if len(same) == 1:
            renamed[group.lower()] = same[0]
    if not renamed:
        return
    path = root / "datasource.yaml"
    if not path.exists():
        return
    from .models import table_key

    # Every table the model serves, by identity, so an anchor resolves the way the validator resolves
    # it: an exact qualified match, else a table stored without a schema, else by bare name. Only the
    # areas datasource.yaml lists, as the loader reads them: an unreferenced directory is not served.
    ds = _load(path) or {}
    area_dirs = []
    for ref in ds.get("subject_areas") or []:
        rel = ref if isinstance(ref, str) else (ref or {}).get("path", "")
        d = root / rel
        area_dirs.append(d if d.is_dir() else root / "subject_areas" / str(rel))
    tables = [
        (table_key(str(t.get("name") or tp.stem), t.get("schema")), tp, t.get("column_groups") or {})
        for d in area_dirs
        for tp in sorted(d.glob("tables/*.yaml"))
        for t in [_load(tp) or {}]
    ]
    defs = {k for k, _tp, _g in tables}
    edited = table_key(table, schema)
    # Another area's own copy of the same table that still has a group keeps its anchor working.
    elsewhere = {
        g.lower()
        for k, tp, groups in tables
        if k == edited and tp.resolve() != table_path.resolve()
        for g in groups
    }

    def resolves_to_edited(owner: str) -> bool:
        ref_schema, bare = table_key(owner)
        if ref_schema:
            found = {k for k in defs if k == (ref_schema, bare)} or {
                k for k in defs if k == ("", bare)
            }
        else:
            found = {k for k in defs if k[1] == bare}
        return found == {edited}

    doc = _load(path) or {}
    changed = False
    for amb in doc.get("ambiguities") or []:
        refs = amb.get("column_groups") or []
        for i, ref in enumerate(refs):
            if not isinstance(ref, str) or "." not in ref:
                continue
            owner, group = ref.rsplit(".", 1)
            if group.lower() not in renamed or not resolves_to_edited(owner):
                continue
            moved = f"{owner}.{renamed[group.lower()]}"
            if group.lower() in elsewhere:
                # The other copy still exposes the old group: keep that anchor, add the new one.
                if moved not in refs:
                    refs.append(moved)
                    changed = True
                continue
            refs[i] = moved
            changed = True
    if changed:
        _snapshot(backups, path)
        _dump(path, doc)


def _filter_columns(table_doc: dict, value: Any) -> list[str]:
    """`value` as a list of this table's column names, or ValueError. Nothing downstream checks
    that a recommended filter names a real column, so a typo here would be stored silently."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("recommended_filters takes a list of column names")
    known = {(c.get("name") or "").lower(): c.get("name") for c in table_doc.get("columns", [])}
    unknown = [v for v in value if v.lower() not in known]
    if unknown:
        raise ValueError(f"not a column of this table: {', '.join(unknown)}")
    return [known[v.lower()] for v in value]


def _set_trust(doc: dict, op: dict, new_state: Optional[str], signer, role,
               *, desc_source: bool = False) -> Any:
    """Apply the op to `doc` and return the value it replaced (the edited field, else the review state)."""
    if op.get("op") == "edit":
        fld, val = op.get("field"), op.get("value")
        if not fld:
            raise ValueError("edit op needs field")
        before = doc.get(fld)
        # A generated description never replaces one a person wrote or the database's own dictionary
        # supplied — enrichment describes what is undescribed. A person can still overwrite either.
        if (fld == "description" and desc_source and op.get("source") == "ai"
                and doc.get("description_source") in ("human", "metadata")
                and (doc.get("description") or "").strip()):
            raise ValueError(f"kept the existing {doc['description_source']} description "
                             "(a generated description doesn't replace it)")
        doc[fld] = val
        # Description provenance (advisory; see DescriptionSource in models.py). ONLY tables
        # and columns carry `description_source`; relationships / metrics / entities have a
        # `description` but no source field, so stamping it there would fail `extra=forbid`.
        # An edit that sets a table/column `description` also stamps `description_source`:
        #   source:"ai"       → ai_unvalidated (agami-connect LLM generation; earns trust via use)
        #   source:"metadata" → metadata (read from the DB's own data dictionary — authoritative,
        #                       trusted, NOT validated-through-use; see metadata_sources.py)
        #   otherwise         → human (a person edited it, so it's trusted)
        #   empty value → clear it. A direct edit of `description_source` itself
        #   (e.g. confirm → "ai_validated") falls through the generic `doc[fld]=val`.
        if fld == "description" and desc_source:
            src = op.get("source")
            doc["description_source"] = (
                None if not (val or "").strip()
                else "ai_unvalidated" if src == "ai"
                else "metadata" if src == "metadata"
                else "human"
            )
        return before
    before = doc.get("review_state")
    if new_state:
        doc["review_state"] = new_state
    if new_state == "approved":
        if signer:
            doc["signed_off_by"] = signer
            doc["signed_off_role"] = role
            doc["signed_off_at"] = op.get("at")  # caller stamps (Date.now unavailable here)
    if new_state in ("unreviewed", "rejected"):
        # clear stale sign-off on un-approve
        for k in ("signed_off_by", "signed_off_at", "signed_off_role"):
            doc.pop(k, None)
    return before


def _set_column_field(table_doc: dict, col_name: str, op: dict, new_state, signer, role) -> Any:
    for c in table_doc.get("columns", []):
        if c.get("name") == col_name:
            return _set_trust(c, op, new_state, signer, role, desc_source=True)  # column desc has provenance
    raise ValueError(f"column {col_name} not found")


# ---------------------------------------------------------------------------
# git + curation log (best-effort; never block)
# ---------------------------------------------------------------------------


# Set by a git hook or a caller, these point git at ANOTHER repository whatever `-C` says, and a
# commit would land there with its whole working tree (#436).
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    return subprocess.run(["git", "-C", str(root), *args], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


_KINDS = {"metric": ("metrics", Metric), "entity": ("entities", Entity)}


def _slug(name: str) -> str:
    from .build import item_file_stem  # one naming rule for every writer of entity/metric files

    return item_file_stem(name)


def write_items(root: str | Path, area: str, kind: str, items: list[dict],
                *, signer: Optional[str] = None, role: Optional[str] = None) -> ApplyResult:
    """Create metric/entity YAML files from structured dicts — one validated,
    revertable batch. This is the packaged path for *creating* enrichment entries
    (e.g. the many metrics extracted from a LookML/dbt repo), so a skill never
    hand-writes YAML file-by-file or authors a throwaway script to loop. Each item
    is structurally validated against its Pydantic model; the whole model is then
    validated and the batch is reverted (new files removed, overwritten files
    restored) if it fails — no git dependency for the revert."""
    root = Path(root)
    res = ApplyResult()
    if kind not in _KINDS:
        res.errors.append(f"unknown kind {kind!r} (expected metric|entity)")
        return res
    subdir, Model = _KINDS[kind]
    dest = _area_dir(root, area) / subdir
    backups: list[tuple[Path, Optional[str]]] = []  # (path, prior text or None if new)

    for item in items:
        # Strip grouping/locator metadata that callers (e.g. the model-dashboard
        # "Add metric" form) attach but that isn't a model field. `area` is already
        # passed as the `area` arg; `qname` is a UI locator. Without this, a dashboard
        # new-metric fails with `extra_forbidden` on `area`.
        clean = {k: v for k, v in (item or {}).items() if k not in ("area", "qname")}
        try:
            obj = Model(**clean)  # structural validation (required fields, enums, …)
        except Exception as e:
            res.skipped.append({"item": (item or {}).get("name", "?"), "reason": str(e)})
            continue
        path = dest / f"{_slug(obj.name)}.yaml"
        backups.append((path, path.read_text(encoding="utf-8") if path.exists() else None))
        dest.mkdir(parents=True, exist_ok=True)
        _dump(path, obj.model_dump(mode="json", exclude_none=True))
        res.applied.append(f"{kind} {area}/{obj.name}")

    if not res.applied:
        return res  # nothing valid to write

    # validate the whole model; revert the batch on any failure
    try:
        vres = V.validate(load_datasource(root, include_rejected=True), cache=_VALIDATION_CACHE)
        res.validated = vres.ok
        if not vres.ok:
            res.errors = vres.errors
            _restore(backups)
            res.applied = []
            return res
    except Exception as e:
        res.errors.append(f"validation failed to run: {e}")
        _restore(backups)
        res.applied = []
        return res

    res.committed, res.commit_note = _record(
        root, f"enrich: +{len(res.applied)} {kind}(s) in {area}",
        [{"op": "add", "kind": kind, "area": area, "name": a.split("/", 1)[-1]} for a in res.applied],
        signer, role)
    return res


def _rel_label(r: dict) -> str:
    # An `on:`-form join has no from/to column; label it by its tables so it reads in the log. It also
    # labels the invalid ones being skipped, so a missing endpoint reads "?" rather than raising.
    ft, tt = r.get("from_table") or "?", r.get("to_table") or "?"
    if r.get("from_column"):
        return f"{ft}.{r['from_column']}→{tt}"
    return f"{ft}→{tt} (on)"


def add_relationships(root: str | Path, *, intra: Optional[dict[str, list[dict]]] = None,
                      cross: Optional[list[dict]] = None,
                      signer: Optional[str] = None, role: Optional[str] = None) -> ApplyResult:
    """Append inferred relationships — intra-area into <area>/relationships.yaml, cross-area into
    cross_subject_area_relationships.yaml — as ONE validated, revertable batch. The packaged
    writer so a skill/command never hand-edits relationship YAML or scripts a loop over it.
    Each edge is structurally validated; the whole model is validated and the batch reverted on
    any failure. Callers pre-route by area and pre-resolve to_column/cardinality."""
    root = Path(root)
    res = ApplyResult()
    intra, cross = intra or {}, cross or []
    backups: list[tuple[Path, Optional[str]]] = []

    # One batch: an error part-way through (after an earlier area's file is written) must restore
    # every file already touched, not leave the model half-updated.
    try:
        for area, rels in intra.items():
            if not rels:
                continue
            path = _area_dir(root, area) / "relationships.yaml"
            backups.append((path, path.read_text(encoding="utf-8") if path.exists() else None))
            doc = _load(path) if path.exists() else None
            lst = doc.get("relationships", []) if isinstance(doc, dict) else (doc or [])
            for r in rels:
                try:
                    Relationship(**r)
                except Exception as e:
                    res.skipped.append({"item": _rel_label(r), "reason": str(e)})
                    continue
                lst.append(r)
                res.applied.append(f"rel {area}/{_rel_label(r)}")
            _dump(path, {"relationships": lst})

        if cross:
            path = root / "cross_subject_area_relationships.yaml"
            backups.append((path, path.read_text(encoding="utf-8") if path.exists() else None))
            doc = _load(path) if path.exists() else None
            edges = doc.get("edges", []) if isinstance(doc, dict) else (doc or [])
            for r in cross:
                try:
                    CrossSubjectAreaRelationship(**r)
                except Exception as e:
                    res.skipped.append({"item": _rel_label(r), "reason": str(e)})
                    continue
                edges.append(r)
                res.applied.append(f"xrel {_rel_label(r)}")
            _dump(path, {"edges": edges})
    except Exception as e:
        _restore(backups)
        res.applied = []
        res.errors.append(f"adding relationships failed and every file was restored: {e}")
        return res

    if not res.applied:
        return res
    try:
        vres = V.validate(load_datasource(root, include_rejected=True), cache=_VALIDATION_CACHE)
        res.validated = vres.ok
        if not vres.ok:
            res.errors = vres.errors
            _restore(backups)
            res.applied = []
            return res
    except Exception as e:
        res.errors.append(f"validation failed to run: {e}")
        _restore(backups)
        res.applied = []
        return res
    res.committed, res.commit_note = _record(
        root, f"enrich: +{len(res.applied)} relationship(s) from metadata",
        [{"op": "add", "kind": "relationship", "name": a} for a in res.applied], signer, role)
    return res


def add_examples(root: str | Path, area: str, examples: list[dict],
                 *, signer: Optional[str] = None, role: Optional[str] = None) -> ApplyResult:
    """Append/replace scope-tagged NL→SQL examples in prompt_examples/<area>/examples.yaml.
    The packaged writer so skills never hand-edit that YAML or reverse-engineer its schema.

    Each example — required: `question`, `sql`. Optional scope tags (the ranking reads
    these): `tables`, `columns`, `metric`, `default_filters`. Optional provenance:
    `source` (seed | correction), `status` (confirmed | proposed), `created_at`.

    Dedups by `question`: a new example with an existing question replaces it (so a
    correction supersedes the earlier answer) rather than duplicating."""
    from .loader import list_prompt_examples
    root = Path(root)
    res = ApplyResult()
    # include rejected so re-adding a previously-rejected question replaces (un-rejects) it
    # rather than duplicating.
    existing = list(list_prompt_examples(root, area, include_rejected=True))
    by_q = {e.get("question"): i for i, e in enumerate(existing) if e.get("question")}
    for ex in examples:
        q, sql = (ex or {}).get("question"), (ex or {}).get("sql")
        if not q or not sql:
            res.skipped.append({"item": q or "?", "reason": "question and sql are required"})
            continue
        if q in by_q:
            old = existing[by_q[q]]
            # The same SQL is a re-save (a sign-off, a tag), not a correction: keep the fields it
            # didn't restate, or re-saving a confirmed example to sign it would strip its status.
            same_sql = " ".join(str(old.get("sql", "")).split()) == " ".join(str(sql).split())
            # A rejection is not carried over: re-adding a rejected question un-rejects it.
            kept = {k: v for k, v in old.items() if not (k == "status" and v == "rejected")}
            existing[by_q[q]] = {**kept, **ex} if same_sql else ex
            res.applied.append(f"example (replaced) {area}/{q[:50]}")
        else:
            by_q[q] = len(existing)
            existing.append(ex)
            res.applied.append(f"example {area}/{q[:50]}")
    if not res.applied:
        return res
    path = root / "prompt_examples" / area / "examples.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump(path, existing)  # bare list — list_prompt_examples accepts list or {examples:[…]}
    res.validated = True    # examples aren't model-validated; the skill EXPLAIN-checks the SQL
    res.committed, res.commit_note = _record(
        root, f"examples: +{len(res.applied)} in {area}",
        [{"op": "add", "kind": "example", "area": area, "name": a.split("/", 1)[-1]} for a in res.applied],
        signer, role)
    return res


def remove_examples(root: str | Path, area: str, questions: list[str],
                    *, signer: Optional[str] = None, role: Optional[str] = None) -> ApplyResult:
    """Reject prompt examples by `question` — set `status: rejected` so the runtime ranker
    drops them (`list_prompt_examples` filters rejected by default), while they STAY in
    examples.yaml for audit, exactly like a rejected table/column/metric. The packaged path
    so skills never hand-rewrite that YAML to drop an example. `questions` is matched on the
    trimmed question text (the dedup key). Who rejected it is recorded in the curation log."""
    from .loader import list_prompt_examples
    root = Path(root)
    res = ApplyResult()
    existing = list(list_prompt_examples(root, area, include_rejected=True))
    wanted = {(q or "").strip() for q in (questions or []) if (q or "").strip()}
    if not wanted:
        return res
    present = {(e.get("question") or "").strip() for e in existing}
    for e in existing:
        q = (e or {}).get("question", "")
        if q.strip() in wanted and e.get("status") != "rejected":
            e["status"] = "rejected"
            res.applied.append(f"example (rejected) {area}/{q[:50]}")
    for missing in sorted(wanted - present):
        res.skipped.append({"item": missing, "reason": "no example with that question"})
    if not res.applied:
        return res
    path = root / "prompt_examples" / area / "examples.yaml"
    _dump(path, existing)  # bare list — kept in the file (rejected, for audit)
    res.validated = True
    res.committed, res.commit_note = _record(
        root, f"examples: rejected {len(res.applied)} in {area}",
        [{"op": "reject", "kind": "example", "area": area, "name": a.split("/", 1)[-1]} for a in res.applied],
        signer, role)
    return res


def validate_seeds(candidates: list[dict], runner) -> tuple[list[dict], list[dict]]:
    """Split candidate examples into (passing, rejected) by validating each SQL against
    the live DB — dialect-agnostically and scanning no data. Each SQL is wrapped to
    return zero rows (`SELECT 1 FROM (<sql>) WHERE 1=0`) and run via `runner` (which
    raises on a bad query). This is the packaged Phase-5 validation loop, so the skill
    never writes a throwaway script to EXPLAIN seeds one by one. Passing examples get
    `source: seed` / `status: confirmed` defaults; rejected carry the DB error."""
    passing: list[dict] = []
    rejected: list[dict] = []
    for c in candidates:
        sql = (c or {}).get("sql")
        q = (c or {}).get("question")
        if not sql or not q:
            rejected.append({"question": q or "?", "error": "question and sql are required"})
            continue
        # `SELECT 1` (not `SELECT *`): the star-free wrapper still forces the DB to parse +
        # plan the inner query and return zero rows, but the executor's SELECT-* ban
        # (runtime.check_no_select_star) walks every SELECT in the tree — a `SELECT *` wrapper
        # would trip the ban on itself and reject every seed regardless of its own SQL.
        probe = "SELECT 1 FROM (\n" + str(sql).strip().rstrip(";") + "\n) AS _agami_seed_validate WHERE 1=0"
        try:
            runner(probe)
        except Exception as e:
            rejected.append({"question": q, "error": str(e)[:200]})
            continue
        ex = dict(c)
        ex.setdefault("source", "seed")
        ex.setdefault("status", "confirmed")
        passing.append(ex)
    return passing, rejected


def _restore(backups: list[tuple[Path, Optional[str]]]) -> None:
    for path, prior in backups:
        try:
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(prior, encoding="utf-8")
        except OSError:
            pass


def _repository_top(root: Path) -> Optional[Path]:
    """The git repository `root` sits in, found the way git finds it (searching upwards), or None.
    A folder that repository ignores (an artifacts folder under a dotfiles repo at `~`) isn't in it."""
    try:
        r = _git(root, "rev-parse", "--show-toplevel")
        if r.returncode != 0 or not r.stdout.strip():
            return None
        if _git(root, "check-ignore", "-q", ".").returncode == 0:
            return None
    except Exception:
        return None
    return Path(r.stdout.strip())


def _record(root: Path, msg: str, entries: list[dict], signer, role) -> tuple[bool, str]:
    """Model-write finalization point for every curation path (apply / write_items / terminology /
    examples / metadata relationships / apply_spec): stamp the model_version snapshot, log the change
    with that version, then commit when git can. The log comes before the commit so a profile that is
    its own repository commits it with the change; without git the log is the only history (#528)."""
    from . import snapshot
    version = snapshot.write_snapshot(root)
    _append_curation_log(root, entries, signer, role, version=version)
    return _git_commit(root, msg)


def _git_commit(root: Path, msg: str) -> tuple[bool, str]:
    """Commit the model's change when the profile folder is its own repository; else say why not.

    A profile folder inside a LARGER repository — the usual home of a deployed model, checked in with
    the team's other files — is never committed to (#436): `git add -A` there stages the whole working
    tree, the commit lands on whatever branch is checked out under whoever's identity git has, and the
    message is agami's, not the team's. The note says which folder changed so the person commits it.
    Rollback never depended on git: a change that fails validation is restored from its backups."""
    if (root / ".git").exists():
        try:
            _git(root, "add", "-A")
            r = _git(root, "commit", "-m", msg)
        except Exception:
            return False, "git could not commit here; the change is written but not committed"
        if r.returncode == 0:
            return True, ""
        return False, "git could not commit here (is git's user.name/user.email set?); the change is written"
    if shutil.which("git") is None:
        return False, "git isn't installed here, so the change is written but not versioned"
    top = _repository_top(root)
    if top is None:
        return False, ("this model isn't in git, so the change is written and recorded in its "
                       f"curation log — `sm history {root}` shows what changed; `git init` its folder "
                       "to keep full file versions too")
    return False, (f"this model is inside the git repository at {top}; agami doesn't commit there — "
                   f"review and commit the changes under {root} yourself")


CURATION_LOG = "curation_log.jsonl"


def _append_curation_log(root: Path, ops: list[dict], signer, role,
                         *, version: Optional[str] = None) -> None:
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with (root / CURATION_LOG).open("a", encoding="utf-8") as f:
            for op in ops:
                entry = {**op, "signer": signer, "role": role, "at": at}
                if version:
                    entry["version"] = version
                f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass


def _read_log_lines(root: Path) -> list[Optional[dict]]:
    """Each line of the curation log, parsed; None for a line that isn't a JSON object."""
    try:
        # bytes, so one undecodable line is that line's problem, not the whole log's
        lines = (root / CURATION_LOG).read_bytes().splitlines()
    except OSError:
        return []
    out: list[Optional[dict]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:  # UnicodeDecodeError included
            entry = None
        out.append(entry if isinstance(entry, dict) else None)
    return out


def history(root: str | Path, *, limit: Optional[int] = None) -> list[dict]:
    """The curation log's entries, newest first — what changed, when, by whom, and the value each edit
    replaced. Entries written before #528 carry no `at` / `before` / `version`; unreadable lines are skipped."""
    entries = [e for e in _read_log_lines(Path(root)) if e is not None]
    entries.reverse()
    return entries[:limit] if limit is not None and limit >= 0 else entries


def has_model_curation(root: str | Path) -> bool:
    """Whether the log records a change to the model's areas or tables. A datasource-level edit
    (its description or glossary) doesn't count, nor did it before those edits were logged; a line
    that can't be read does, as the log's mere existence once did."""
    root = Path(root)
    if not (root / CURATION_LOG).exists():
        return False
    lines = _read_log_lines(root)
    return not lines or any(e is None or e.get("kind") != "datasource" for e in lines)


__all__ = ["review_queue", "all_items", "model_tree", "column_coverage", "apply",
           "write_items", "add_examples", "validate_seeds", "set_key_terminology",
           "history", "ApplyResult"]
