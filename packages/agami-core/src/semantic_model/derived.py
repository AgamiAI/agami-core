"""Composable derived metrics (scorecard #1).

A *derived* metric defines its SQL in terms of OTHER metrics, via `{metric name}`
placeholders in its binding — so the definition lives in ONE place and never drifts:

    revenue        bindings.PostgreSQL = "SUM(amount)"
    order_count    bindings.PostgreSQL = "COUNT(DISTINCT order_id)"
    avg_order_value  base_metrics=[revenue, order_count]
                     bindings.PostgreSQL = "{revenue} / {order_count}"

`expand_binding` resolves those placeholders recursively into standalone SQL
(`SUM(amount) / COUNT(DISTINCT order_id)`), with cycle / missing-reference guards.

SCOPE: case (a) — composition of metrics over the same grain (ratios, rates, arithmetic)
— is handled by `expand_binding` (inline placeholder substitution). Case (b) — a
SECOND-ORDER statistic, an aggregate OF an aggregate at a finer grain (`AVG` of a daily
`SUM`) — IS supported, via `synthesize_second_order`: declare it with an `inner_grain` and
bind it as `OUTERAGG({base})`, and the CTE is built deterministically (see
`resolve_metric_sql`, the single entry point). A nested aggregate with NO `inner_grain`
declared is still refused — illegal `AVG(SUM(...))` is never emitted.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import bare_name, table_key

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
            "(second-order statistic) but has no `inner_grain` declared. To support it, set "
            "`inner_grain` (the dimension the base is grouped by) and bind it as "
            "OUTERAGG({base}) — the CTE is then synthesized (see synthesize_second_order)."
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


# Words most engines reserve, so a table or column named after one must be quoted.
_RESERVED_WORDS = frozenset({
    "all", "and", "any", "as", "asc", "between", "by", "case", "cast", "check", "column",
    "constraint", "create", "cross", "current_date", "current_time", "current_timestamp",
    "current_user", "default", "delete", "desc", "distinct", "drop", "else", "end", "except",
    "exists", "false", "fetch", "for", "foreign", "from", "full", "grant", "group", "having", "in",
    "inner", "insert", "intersect", "into", "is", "join", "left", "like", "limit", "natural",
    "not", "null", "offset", "on", "or", "order", "outer", "primary", "references", "right",
    "select", "session_user", "table", "then", "to", "true", "union", "unique", "update", "user",
    "using", "values", "when", "where", "window", "with",
})
_PLAIN_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _ident(name: str, storage_type: str) -> str:
    """`name` quoted for `storage_type` only when the engine needs it: special characters, a
    reserved word, or a case the engine would fold away. A plain lower-case name stays bare, so
    synthesized SQL that worked before is unchanged (and an upper-folding engine still matches it
    case-insensitively, as it always has)."""
    from .dialects import get_dialect

    try:
        dialect = get_dialect(storage_type)
    except ValueError:
        return name
    if not _PLAIN_IDENT_RE.match(name) or name.lower() in _RESERVED_WORDS:
        return dialect.quote_ident(name)
    if name != name.lower() and _engine_folds(name, storage_type):
        return dialect.quote_ident(name)
    return name


def _engine_folds(name: str, storage_type: str) -> bool:
    """True when the engine would change `name`'s case if it were left unquoted."""
    try:
        from sqlglot.dialects.dialect import Dialect

        from .sql_dialect import sqlglot_dialect

        return Dialect.get_or_raise(sqlglot_dialect(storage_type)).case_sensitive(name)
    except Exception:
        return False


def _table_ref(name: str, storage_type: str) -> str:
    """A possibly schema-qualified table name with each part quoted as it needs."""
    return ".".join(_ident(part, storage_type) for part in name.split("."))


def _same_table(a: str, a_schema: Optional[str], b: str, b_schema: Optional[str]) -> bool:
    """Two references name one table: same bare name, and the same schema when both state one."""
    sa, na = table_key(a, a_schema)
    sb, nb = table_key(b, b_schema)
    return na == nb and (not sa or not sb or sa == sb)


def _owning_area(metric, org):
    for sa in org.subject_areas:
        if any(mm is metric for mm in sa.metrics):
            return sa
    return None


def _join_edge(metric_name: str, anchor: str, other: str, base, org):
    """The single declared, usable edge directly between `anchor` and `other`."""
    area = _owning_area(base, org)
    edges = list(area.relationships if area else []) + list(org.cross_subject_area_relationships)
    found = []
    for rel in edges:
        if rel.review_state == "rejected" or rel.executable != "same_engine":
            continue
        fwd = (_same_table(rel.from_table, rel.from_schema, anchor, None)
               and _same_table(rel.to_table, rel.to_schema, other, None))
        rev = (_same_table(rel.from_table, rel.from_schema, other, None)
               and _same_table(rel.to_table, rel.to_schema, anchor, None))
        if fwd or rev:
            found.append((rel, fwd))
    if not found:
        raise DerivedError(
            f"second-order metric {metric_name!r}: no declared relationship joins {anchor!r} "
            f"directly to {other!r}; declare one (multi-hop joins aren't synthesized)"
        )
    if len(found) > 1:
        raise DerivedError(
            f"second-order metric {metric_name!r}: {len(found)} declared relationships join "
            f"{anchor!r} and {other!r}; the join is ambiguous"
        )
    rel, anchor_is_from = found[0]
    # Read from the anchor's side: a join that gives each anchor row several matches multiplies
    # the base aggregate's rows, so the inner value would be wrong.
    fans_out = rel.relationship == ("one_to_many" if anchor_is_from else "many_to_one")
    if fans_out:
        raise DerivedError(
            f"second-order metric {metric_name!r}: joining {other!r} to {anchor!r} fans out "
            f"(several {other!r} rows per {anchor!r} row), which would inflate the base "
            "aggregate; set the base metric's primary_table to the finer-grained table"
        )
    return rel


def _join_sql(rel, other: str, storage_type: str) -> str:
    if rel.on:
        cond = rel.on
    else:
        left = f"{_ident(bare_name(rel.from_table), storage_type)}.{_ident(rel.from_column, storage_type)}"
        right = f"{_ident(bare_name(rel.to_table), storage_type)}.{_ident(rel.to_column, storage_type)}"
        cond = f"{left} = {right}"
    return f" INNER JOIN {_table_ref(other, storage_type)} ON {cond}"


def _table_def(name: str, org):
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if _same_table(t.name, t.schema_name, name, None):
                return t
    return None


def _qualified_grain(metric_name: str, col: str, froms: list[str], org, storage_type: str) -> str:
    """A bare `inner_grain` column qualified by the one source table that declares it."""
    if _DOTTED_IDENT_RE.match(col):
        return _table_ref(col, storage_type)
    if not _PLAIN_IDENT_RE.match(col):
        raise DerivedError(
            f"second-order metric {metric_name!r}: inner_grain {col!r} must name a column "
            "when the base metric spans several tables"
        )
    owners = [t for t in froms
              if (td := _table_def(t, org)) is not None
              and any(c.name.lower() == col.lower() for c in td.columns)]
    if not owners:
        raise DerivedError(
            f"second-order metric {metric_name!r}: inner_grain column {col!r} is not declared "
            f"on any of the base metric's tables {froms}"
        )
    if len(owners) > 1:
        raise DerivedError(
            f"second-order metric {metric_name!r}: inner_grain column {col!r} is ambiguous; "
            f"it is declared on {owners}. Write it as table.column"
        )
    return f"{_ident(bare_name(owners[0]), storage_type)}.{_ident(col, storage_type)}"


def _grain_sql(col: str, storage_type: str) -> str:
    """A single-table grain entry: a column name is quoted as it needs, anything else verbatim."""
    if _PLAIN_IDENT_RE.match(col) or _DOTTED_IDENT_RE.match(col):
        return _table_ref(col, storage_type)
    return col


def _from_clause(metric_name: str, base, froms: list[str], org, storage_type: str) -> str:
    """FROM the anchor table, INNER JOINed directly to each other source table."""
    if org is None:
        raise DerivedError(
            f"second-order metric {metric_name!r}: its base spans {froms}, and joining them "
            "needs the model's relationships"
        )
    anchor = base.primary_table if base.primary_table in froms else froms[0]
    sql = _table_ref(anchor, storage_type)
    for other in froms:
        if other == anchor:
            continue
        sql += _join_sql(_join_edge(metric_name, anchor, other, base, org), other, storage_type)
    return sql


def synthesize_second_order(metric, storage_type: str, idx: dict, org=None) -> str:
    """Deterministically build the CTE for a second-order statistic — compute the base
    aggregate at `inner_grain`, then apply the outer aggregate across it. Emitted as a
    scalar subquery so it slots in wherever a metric expression goes:

        (SELECT AVG(daily_revenue) FROM (
           SELECT order_date, SUM(amount) AS daily_revenue
           FROM orders GROUP BY order_date) _inner)

    A base over several tables is joined from its anchor (`primary_table`, else the first
    source table) to each other table by the one relationship declared directly between them,
    which needs `org`. Identifiers are quoted only where the engine needs it.

    Raises DerivedError if the shape isn't `OUTERAGG({base})`, the base is unknown / not a
    plain aggregate, `inner_grain` is empty, it has no source table, or a multi-table join
    can't be built safely (no direct edge, several edges, a fan-out, an unresolvable grain)."""
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
    froms = list(dict.fromkeys(base.source_tables or metric.source_tables))
    if not froms:
        raise DerivedError(
            f"second-order metric {metric.name!r}: synthesis needs the base metric's source table"
        )
    if len(froms) == 1:
        from_sql = _table_ref(froms[0], storage_type)
        grain = ", ".join(_grain_sql(g, storage_type) for g in metric.inner_grain)
    else:
        from_sql = _from_clause(metric.name, base, froms, org, storage_type)
        grain = ", ".join(
            _qualified_grain(metric.name, g, froms, org, storage_type) for g in metric.inner_grain
        )
    alias = _ident(_slug(base_name), storage_type)
    return (
        f"(SELECT {outer_func}({alias}) FROM ("
        f"SELECT {grain}, {inner_sql} AS {alias} "
        f"FROM {from_sql} GROUP BY {grain}) _inner)"
    )


def resolve_metric_sql(metric, storage_type: str, idx: dict, org=None) -> str:
    """The one entry point: standalone SQL for a metric's binding in `storage_type`.
    Second-order (case b) → synthesized CTE; otherwise placeholder expansion (case a /
    first-order). `org` supplies the relationships a multi-table second-order base joins on.
    Raises DerivedError on any composition problem."""
    if is_second_order(metric):
        return synthesize_second_order(metric, storage_type, idx, org)
    return expand_binding(metric, storage_type, idx)


def expanded_bindings(metric, idx: dict, org=None) -> dict:
    """All of a metric's bindings resolved to standalone SQL (placeholder expansion for
    first-order, CTE synthesis for second-order). Raises DerivedError on failure."""
    out: dict = {}
    for stype in (getattr(metric, "bindings", None) or {}):
        out[stype] = resolve_metric_sql(metric, stype, idx, org)
    return out
