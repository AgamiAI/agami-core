"""Curation engine for the semantic model — the model-native replacement for the
OSI-era compute_confidence.py + apply_model_exclusions.py + the review/explorer
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
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import validator as V
from .loader import load_organization
from .models import Entity, Metric, Organization


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------


def _needs_review(obj) -> bool:
    """review_state is the gate: an entry needs a human iff it's unreviewed or has
    drifted stale. Approval/rejection clears it regardless of the confidence label
    (a human signing off IS the confirmation)."""
    return getattr(obj, "review_state", "approved") in ("unreviewed", "stale")


def review_queue(org: Organization) -> dict[str, Any]:
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


def all_items(org: Organization, *, scope: str = "all") -> list[dict]:
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


def column_coverage(org: Organization) -> dict:
    """Per-table column-description coverage — the enrichment-completeness check.

    The signal is **table-level**, which keeps it conflict-free with the skill's
    deliberate "leave self-evident columns (`id`, `created_at`) blank" rule. Per
    *column*, a blank description is ambiguous (correctly-self-evident vs. skipped);
    per *table*, it isn't. A real enrichment pass over any non-trivial table produces
    at least one described or `ai_unknown` column (every table has data/coded/FK
    columns worth a line). A table enrichment **never ran on** has ZERO — that's the
    failure mode where the model got table descriptions but no column pass.

    A table is `unenriched` when `described == 0 AND ai_unknown == 0` and it has at
    least `_MIN_GATE_COLS` columns. `ok` is true when no kept table is unenriched.
    Rejected tables/columns are excluded. `coverage_pct` (described / columns) is
    informational — self-evident blanks legitimately hold it below 100%, so it is
    surfaced for the eye, not gated on."""
    tables: list[dict] = []
    tot = {"columns": 0, "described": 0, "ai_unknown": 0, "blank": 0}
    unenriched: list[str] = []
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if getattr(t, "review_state", "approved") == "rejected":
                continue
            described = ai_unknown = blank = 0
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
            n = described + ai_unknown + blank
            enriched = (described + ai_unknown) > 0
            tables.append({
                "area": sa.name, "table": t.name, "columns": n,
                "described": described, "ai_unknown": ai_unknown, "blank": blank,
                "coverage_pct": round(100 * described / n) if n else 100,
                "enriched": enriched,
            })
            if not enriched and n >= _MIN_GATE_COLS:
                unenriched.append(t.name)
            tot["columns"] += n
            tot["described"] += described
            tot["ai_unknown"] += ai_unknown
            tot["blank"] += blank
    tables.sort(key=lambda x: (x["enriched"], x["table"]))   # unenriched tables first
    tot["coverage_pct"] = round(100 * tot["described"] / tot["columns"]) if tot["columns"] else 100
    return {
        "tables": tables,
        "totals": tot,
        "unenriched_tables": unenriched,
        "ok": not unenriched,
    }


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
    # calculation and the feedback generator emits `by <email> role=` for sign-off.
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


def model_tree(org: Organization) -> dict[str, Any]:
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
    return {"organization": org.organization, "subject_areas": areas}


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

    def as_dict(self) -> dict:
        return {"applied": self.applied, "skipped": self.skipped, "errors": self.errors,
                "validated": self.validated, "committed": self.committed}


def _area_dir(root: Path, area: str) -> Path:
    return root / "subject_areas" / area


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _dump(path: Path, obj: Any) -> None:
    path.write_text(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100),
                    encoding="utf-8")


_VALID_OPS = {"approve", "reject", "exclude", "include", "edit"}
# exclude == reject; include == set unreviewed + clear sign-off (model-explorer verbs)


def apply(root: str | Path, ops: list[dict], *, signer: Optional[str] = None,
          role: Optional[str] = None) -> ApplyResult:
    """Apply curation ops to the on-disk model, validate, commit (best-effort),
    revert on validation failure. Each op: {op, kind, area, name, [column], [field, value]}."""
    root = Path(root)
    res = ApplyResult()
    touched: set[Path] = set()

    for op in ops:
        try:
            path = _apply_one(root, op, signer, role)
            if path is not None:
                touched.add(path)
                res.applied.append(_op_label(op))
        except Exception as e:
            res.skipped.append({"op": _op_label(op), "reason": str(e)})

    # validate the whole model after the batch
    try:
        org = load_organization(root, include_rejected=True)
        vres = V.validate(org)
        res.validated = vres.ok
        if not vres.ok:
            res.errors = vres.errors
            _git_revert(root, touched)
            res.applied = []  # nothing stuck
            return res
    except Exception as e:
        res.errors.append(f"validation failed to run: {e}")
        _git_revert(root, touched)
        res.applied = []
        return res

    if res.applied:
        _append_curation_log(root, ops, signer, role)
        res.committed = _git_commit(root, f"curation: {len(res.applied)} change(s)")
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


def _apply_one(root: Path, op: dict, signer, role) -> Optional[Path]:
    action = op.get("op")
    if action not in _VALID_OPS:
        raise ValueError(f"unknown op {action!r}")
    kind = op.get("kind")
    name = op.get("name")
    if not kind or not name:
        raise ValueError("op needs kind + name")
    # `area` is optional — resolve it from the model by name when the caller omits it.
    area = op.get("area") or _resolve_area(root, op)

    new_state = {"approve": "approved", "include": "unreviewed",
                 "reject": "rejected", "exclude": "rejected"}.get(action)

    if kind == "table":
        path = _area_dir(root, area) / "tables" / f"{name}.yaml"
        doc = _load(path)
        if op.get("column"):
            _set_column_field(doc, op["column"], op, new_state, signer, role)
        else:
            _set_trust(doc, op, new_state, signer, role, desc_source=True)  # table desc has provenance
        _dump(path, doc)
        return path

    if kind == "entity":
        # entities/metrics are stored under the SLUGGED name (write_items uses _slug), so resolve
        # the same way — a multi-word name ("total event sales") must find total_event_sales.yaml,
        # not a literal "total event sales.yaml". _slug is idempotent, so an already-slugged name
        # still resolves. (Tables keep their literal name — they're written verbatim, not slugged.)
        path = _area_dir(root, area) / "entities" / f"{_slug(name)}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"no entity named {name!r} in area {area!r}")
        doc = _load(path)
        _set_trust(doc, op, new_state, signer, role)
        _dump(path, doc)
        return path

    if kind == "metric":
        path = _area_dir(root, area) / "metrics" / f"{_slug(name)}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"no metric named {name!r} in area {area!r}")
        doc = _load(path)
        _set_trust(doc, op, new_state, signer, role)
        _dump(path, doc)
        return path

    if kind == "relationship":
        frm, _, to = name.partition("->")
        path = _area_dir(root, area) / "relationships.yaml"
        if path.exists():
            doc = _load(path) or {}
            rels = doc.get("relationships", doc if isinstance(doc, list) else [])
            hit = next((r for r in rels if r.get("from_table") == frm and r.get("to_table") == to), None)
            if hit is not None:
                _set_trust(hit, op, new_state, signer, role)
                _dump(path, {"relationships": rels})
                return path
        # Cross-area (cross-schema / cross-datasource) join — it lives at the org level, not
        # in an area's relationships.yaml. Fall back to org.yaml's cross_subject_area_relationships.
        orgp = root / "org.yaml"
        odoc = _load(orgp) or {}
        crels = odoc.get("cross_subject_area_relationships", [])
        chit = next((r for r in crels if r.get("from_table") == frm and r.get("to_table") == to), None)
        if chit is None:
            raise ValueError(f"relationship {name} not found in {path} or org cross-area relationships")
        _set_trust(chit, op, new_state, signer, role)
        _dump(orgp, odoc)
        return orgp

    raise ValueError(f"unknown kind {kind!r}")


def _set_trust(doc: dict, op: dict, new_state: Optional[str], signer, role,
               *, desc_source: bool = False) -> None:
    if op.get("op") == "edit":
        fld, val = op.get("field"), op.get("value")
        if not fld:
            raise ValueError("edit op needs field")
        doc[fld] = val
        # Description provenance (advisory; see DescriptionSource in models.py). ONLY tables
        # and columns carry `description_source`; relationships / metrics / entities have a
        # `description` but no source field, so stamping it there would fail `extra=forbid`.
        # An edit that sets a table/column `description` also stamps `description_source`:
        #   source:"ai" → ai_unvalidated (agami-connect generation)
        #   otherwise   → human (a person edited it, so it's trusted)
        #   empty value → clear it. A direct edit of `description_source` itself
        #   (e.g. confirm → "ai_validated") falls through the generic `doc[fld]=val`.
        if fld == "description" and desc_source:
            doc["description_source"] = (
                None if not (val or "").strip()
                else ("ai_unvalidated" if op.get("source") == "ai" else "human")
            )
        return
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


def _set_column_field(table_doc: dict, col_name: str, op: dict, new_state, signer, role) -> None:
    for c in table_doc.get("columns", []):
        if c.get("name") == col_name:
            _set_trust(c, op, new_state, signer, role, desc_source=True)  # column desc has provenance
            return
    raise ValueError(f"column {col_name} not found")


# ---------------------------------------------------------------------------
# git + curation log (best-effort; never block)
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


_KINDS = {"metric": ("metrics", Metric), "entity": ("entities", Entity)}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "unnamed"


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
        vres = V.validate(load_organization(root, include_rejected=True))
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

    _append_curation_log(
        root, [{"op": "add", "kind": kind, "area": area, "name": a.split("/", 1)[-1]}
               for a in res.applied], signer, role)
    res.committed = _git_commit(root, f"enrich: +{len(res.applied)} {kind}(s) in {area}")
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
            existing[by_q[q]] = ex
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
    _append_curation_log(root, [{"op": "add", "kind": "example", "area": area,
                                 "name": a.split("/", 1)[-1]} for a in res.applied], signer, role)
    res.committed = _git_commit(root, f"examples: +{len(res.applied)} in {area}")
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
    _append_curation_log(root, [{"op": "reject", "kind": "example", "area": area,
                                 "name": a.split("/", 1)[-1]} for a in res.applied], signer, role)
    res.committed = _git_commit(root, f"examples: rejected {len(res.applied)} in {area}")
    return res


def validate_seeds(candidates: list[dict], runner) -> tuple[list[dict], list[dict]]:
    """Split candidate examples into (passing, rejected) by validating each SQL against
    the live DB — dialect-agnostically and scanning no data. Each SQL is wrapped to
    return zero rows (`SELECT * FROM (<sql>) WHERE 1=0`) and run via `runner` (which
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
        probe = "SELECT * FROM (\n" + str(sql).strip().rstrip(";") + "\n) AS _agami_seed_validate WHERE 1=0"
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


def _git_commit(root: Path, msg: str) -> bool:
    if not (root / ".git").exists():
        return False
    try:
        _git(root, "add", "-A")
        r = _git(root, "commit", "-m", msg)
        return r.returncode == 0
    except Exception:
        return False


def _git_revert(root: Path, paths: set[Path]) -> None:
    if not (root / ".git").exists():
        return
    for p in paths:
        try:
            _git(root, "checkout", "--", str(p))
        except Exception:
            pass


def _append_curation_log(root: Path, ops: list[dict], signer, role) -> None:
    try:
        log = root / "curation_log.jsonl"
        with log.open("a", encoding="utf-8") as f:
            for op in ops:
                f.write(json.dumps({**op, "signer": signer, "role": role}, default=str) + "\n")
    except OSError:
        pass


__all__ = ["review_queue", "all_items", "model_tree", "column_coverage", "apply",
           "write_items", "add_examples", "validate_seeds", "ApplyResult"]
