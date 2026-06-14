"""Composable derived metrics (scorecard #1).

A *derived* metric defines its SQL in terms of OTHER metrics, via `{metric name}`
placeholders in its binding — so the definition lives in ONE place and never drifts:

    revenue        bindings.PostgreSQL = "SUM(amount)"
    order_count    bindings.PostgreSQL = "COUNT(DISTINCT order_id)"
    avg_order_value  base_metrics=[revenue, order_count]
                     bindings.PostgreSQL = "{revenue} / {order_count}"

`expand_binding` resolves those placeholders recursively into standalone SQL
(`SUM(amount) / COUNT(DISTINCT order_id)`), with cycle / missing-reference guards.

SCOPE: this handles case (a) — composition of metrics over the same grain (ratios,
rates, arithmetic). Case (b) — a SECOND-ORDER statistic, an aggregate OF an aggregate
at a finer grain (`AVG` of a daily `SUM`) — needs nested CTE composition and is
deferred to the grain-attributed planner (#4); it's detected here and refused with a
clear message rather than emitting illegal `AVG(SUM(...))`.
"""

from __future__ import annotations

import re
from typing import Optional

# {metric name} — names may contain spaces/underscores/digits (agami metric names do).
PLACEHOLDER_RE = re.compile(r"\{\s*([^{}]+?)\s*\}")


class DerivedError(ValueError):
    """A derived metric can't be composed (cycle, missing base, or a case-(b) nesting)."""


def metric_index(org) -> dict:
    """name -> Metric across every subject area + org-level cross metrics."""
    idx: dict = {}
    for sa in org.subject_areas:
        for mm in sa.metrics:
            idx[mm.name] = mm
    for mm in getattr(org, "cross_subject_area_metrics", []) or []:
        idx[mm.name] = mm
    return idx


def binding_refs(binding_sql: Optional[str]) -> list[str]:
    """The metric names a single binding references via {…} placeholders."""
    if not binding_sql:
        return []
    return [m.strip() for m in PLACEHOLDER_RE.findall(binding_sql)]


def is_derived(metric) -> bool:
    """True if the metric composes other metrics — declared via base_metrics or
    detected from {…} placeholders in any binding."""
    if getattr(metric, "base_metrics", None):
        return True
    return any(binding_refs(b) for b in (getattr(metric, "bindings", None) or {}).values())


def expand_binding(metric, storage_type: str, idx: dict, *, _stack: Optional[list] = None) -> str:
    """Recursively expand a metric's `{base}` placeholders for `storage_type` into
    standalone SQL. Raises DerivedError on a cycle, an unknown base, a base that
    lacks a binding for this dialect, or a second-order (case-b) nesting."""
    _stack = list(_stack or [])
    if metric.name in _stack:
        raise DerivedError(
            "derived metric cycle: " + " -> ".join(_stack + [metric.name])
        )
    binding = (getattr(metric, "bindings", None) or {}).get(storage_type)
    if binding is None:
        raise DerivedError(
            f"metric {metric.name!r} has no {storage_type} binding to expand"
        )

    def _sub(mo: "re.Match") -> str:
        ref = mo.group(1).strip()
        base = idx.get(ref)
        if base is None:
            raise DerivedError(
                f"metric {metric.name!r} references unknown base metric {ref!r}"
            )
        inner = expand_binding(base, storage_type, idx, _stack=_stack + [metric.name])
        return "(" + inner + ")"

    expanded = PLACEHOLDER_RE.sub(_sub, binding)
    if _has_nested_aggregate(expanded):
        raise DerivedError(
            f"metric {metric.name!r} composes an aggregate of an aggregate "
            "(second-order statistic) — that needs CTE composition and is deferred to "
            "the grain-attributed planner (#4). Express it with a direct binding for now."
        )
    return expanded


def _has_nested_aggregate(sql_fragment: str) -> bool:
    """True if `sql_fragment` contains an aggregate inside another aggregate
    (e.g. AVG(SUM(x))) — illegal SQL and the signature of a case-(b) second-order
    statistic. Best-effort via sqlglot; if it can't parse, don't block."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:
        return False
    try:
        tree = sqlglot.parse_one(sql_fragment, error_level="ignore")
    except Exception:
        return False
    if tree is None:
        return False
    for agg in tree.find_all(exp.AggFunc):
        for inner in agg.find_all(exp.AggFunc):
            if inner is not agg:
                return True
    return False


def expanded_bindings(metric, idx: dict) -> dict:
    """All of a metric's bindings with placeholders resolved. For a non-derived
    metric this is just its bindings; for a derived one, each dialect is composed.
    Raises DerivedError if any dialect can't be expanded."""
    out: dict = {}
    for stype in (getattr(metric, "bindings", None) or {}):
        out[stype] = expand_binding(metric, stype, idx)
    return out
