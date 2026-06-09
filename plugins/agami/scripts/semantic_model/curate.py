"""Curation engine for the semantic model — the model-native replacement for the
OSI-era compute_confidence.py + apply_model_exclusions.py + the review/explorer
item-building logic.

Two read views + one write path, all over the on-disk model tree:

  review_queue(org)   -> the trust-review items: entries needing sign-off
                         (review_state != approved) or low confidence
                         (proposed / inferred), partitioned Rule 1 (metrics) vs
                         Rule 2 (relationships / entities). Feeds /agami-review.
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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import validator as V
from .loader import load_organization
from .models import Organization


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


def _trust(obj) -> dict:
    return {
        "confidence": getattr(obj, "confidence", None),
        "review_state": getattr(obj, "review_state", None),
        "signed_off_by": getattr(obj, "signed_off_by", None),
        "signed_off_at": getattr(obj, "signed_off_at", None),
        "signed_off_role": getattr(obj, "signed_off_role", None),
    }


def _metric_item(area: Optional[str], mm) -> dict:
    return {"kind": "metric", "rule": 1, "area": area, "name": mm.name,
            "title": mm.name, "source_signal": mm.calculation,
            "bindings": mm.bindings, "business_question": mm.business_question,
            **_trust(mm)}


def _rel_item(area: Optional[str], rel) -> dict:
    join = (f"{rel.from_table}.{rel.from_column} → {rel.to_table}.{rel.to_column}"
            if rel.from_column else f"{rel.from_table} → {rel.to_table} ON {rel.on}")
    return {"kind": "relationship", "rule": 2, "area": area,
            "name": f"{rel.from_table}->{rel.to_table}", "title": join,
            "cardinality": rel.relationship, "source_signal": rel.description or join,
            **_trust(rel)}


def _entity_item(area: Optional[str], ent) -> dict:
    return {"kind": "entity", "rule": 2, "area": area, "name": ent.name,
            "title": ent.name, "source_signal": ent.description or "",
            "maps_to": [f"{m.table}.{m.column}" for m in ent.maps_to], **_trust(ent)}


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


def _apply_one(root: Path, op: dict, signer, role) -> Optional[Path]:
    action = op.get("op")
    if action not in _VALID_OPS:
        raise ValueError(f"unknown op {action!r}")
    kind = op.get("kind")
    area = op.get("area")
    name = op.get("name")
    if not kind or not name:
        raise ValueError("op needs kind + name")

    new_state = {"approve": "approved", "include": "unreviewed",
                 "reject": "rejected", "exclude": "rejected"}.get(action)

    if kind == "table":
        path = _area_dir(root, area) / "tables" / f"{name}.yaml"
        doc = _load(path)
        if op.get("column"):
            _set_column_field(doc, op["column"], op, new_state, signer, role)
        else:
            _set_trust(doc, op, new_state, signer, role)
        _dump(path, doc)
        return path

    if kind == "entity":
        path = _area_dir(root, area) / "entities" / f"{name}.yaml"
        doc = _load(path)
        _set_trust(doc, op, new_state, signer, role)
        _dump(path, doc)
        return path

    if kind == "metric":
        path = _area_dir(root, area) / "metrics" / f"{name}.yaml"
        doc = _load(path)
        _set_trust(doc, op, new_state, signer, role)
        _dump(path, doc)
        return path

    if kind == "relationship":
        path = _area_dir(root, area) / "relationships.yaml"
        doc = _load(path) or {}
        rels = doc.get("relationships", doc if isinstance(doc, list) else [])
        frm, _, to = name.partition("->")
        hit = None
        for r in rels:
            if r.get("from_table") == frm and r.get("to_table") == to:
                hit = r
                break
        if hit is None:
            raise ValueError(f"relationship {name} not found in {path}")
        _set_trust(hit, op, new_state, signer, role)
        _dump(path, {"relationships": rels})
        return path

    raise ValueError(f"unknown kind {kind!r}")


def _set_trust(doc: dict, op: dict, new_state: Optional[str], signer, role) -> None:
    if op.get("op") == "edit":
        fld, val = op.get("field"), op.get("value")
        if not fld:
            raise ValueError("edit op needs field")
        doc[fld] = val
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
            _set_trust(c, op, new_state, signer, role)
            return
    raise ValueError(f"column {col_name} not found")


# ---------------------------------------------------------------------------
# git + curation log (best-effort; never block)
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


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


__all__ = ["review_queue", "model_tree", "apply", "ApplyResult"]
