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


# A second-order metric's binding is exactly OUTERAGG({base metric}) — e.g. "AVG({daily_revenue})".
SECOND_ORDER_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\{([^{}]+)\}\s*\)\s*$")


def is_second_order(metric) -> bool:
    """A declared second-order statistic — has `inner_grain` set (the dimension(s) its
    base aggregate is grouped by before the outer aggregate)."""
    return bool(getattr(metric, "inner_grain", None))


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name.lower()).strip("_")
    return s or "inner_value"


def synthesize_second_order(metric, storage_type: str, idx: dict) -> str:
    """Deterministically build the CTE for a second-order statistic — compute the base
    aggregate at `inner_grain`, then apply the outer aggregate across it. Emitted as a
    scalar subquery so it slots in wherever a metric expression goes:

        (SELECT AVG(daily_revenue) FROM (
           SELECT order_date, SUM(amount) AS daily_revenue
           FROM orders GROUP BY order_date) _inner)

    Raises DerivedError if the shape isn't `OUTERAGG({base})`, the base is unknown / not a
    plain aggregate, `inner_grain` is empty, or the base spans more than one source table
    (multi-table inner joins aren't synthesized yet)."""
    binding = (getattr(metric, "bindings", None) or {}).get(storage_type)
    if binding is None:
        raise DerivedError(f"metric {metric.name!r} has no {storage_type} binding")
    mt = SECOND_ORDER_RE.match(binding)
    if not mt:
        raise DerivedError(
            f"second-order metric {metric.name!r} must bind as OUTERAGG({{base_metric}}) "
            f"(e.g. \"AVG({{daily_revenue}})\"); got {binding!r}"
        )
    outer_func, base_name = mt.group(1).upper(), mt.group(2).strip()
    if not metric.inner_grain:
        raise DerivedError(f"second-order metric {metric.name!r} needs inner_grain set")
    base = idx.get(base_name)
    if base is None:
        raise DerivedError(f"metric {metric.name!r} references unknown base metric {base_name!r}")
    # The inner aggregate is the base's own (first-order) binding — expand_binding refuses a
    # base that is itself nested, so we never double-nest.
    inner_sql = expand_binding(base, storage_type, idx)
    if _has_nested_aggregate(inner_sql):
        raise DerivedError(
            f"second-order metric {metric.name!r}: base {base_name!r} is itself a "
            "second-order statistic — only one level of nesting is synthesized"
        )
    froms = list(base.source_tables) or list(metric.source_tables)
    if len(froms) != 1:
        raise DerivedError(
            f"second-order metric {metric.name!r}: synthesis needs exactly one source table "
            f"(got {froms or 'none'}); multi-table inner joins aren't synthesized yet"
        )
    table = froms[0]
    grain = ", ".join(metric.inner_grain)
    alias = _slug(base_name)
    return (
        f"(SELECT {outer_func}({alias}) FROM ("
        f"SELECT {grain}, {inner_sql} AS {alias} "
        f"FROM {table} GROUP BY {grain}) _inner)"
    )


def resolve_metric_sql(metric, storage_type: str, idx: dict) -> str:
    """The one entry point: standalone SQL for a metric's binding in `storage_type`.
    Second-order (case b) → synthesized CTE; otherwise placeholder expansion (case a /
    first-order). Raises DerivedError on any composition problem."""
    if is_second_order(metric):
        return synthesize_second_order(metric, storage_type, idx)
    return expand_binding(metric, storage_type, idx)


def expanded_bindings(metric, idx: dict) -> dict:
    """All of a metric's bindings resolved to standalone SQL (placeholder expansion for
    first-order, CTE synthesis for second-order). Raises DerivedError on failure."""
    out: dict = {}
    for stype in (getattr(metric, "bindings", None) or {}):
        out[stype] = resolve_metric_sql(metric, stype, idx)
    return out
