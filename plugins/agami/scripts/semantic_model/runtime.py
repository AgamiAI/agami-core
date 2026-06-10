"""Runtime traversal for the agami semantic-model-v2 path.

Implements the design doc's "Traversal" + "Runtime walkthrough" primitives as
pure functions over a parsed `Organization` model, so they're equally usable from
the MCP server (`mcp_server.py`) and the skill CLI, and fully unit-testable
without a live database. Anything that needs to touch the DB (entity probing) is
injected as a `probe` callable — the caller wires in a real prober; tests pass a
fake.

Primitives (examples-first canonical loop):
  list_subject_areas        — pick area by description / intent
  get_prompt_examples       — examples FIRST; short-circuit on high-confidence match
  resolve_entities          — lexical match query -> entities (cold-start)
  resolve_metrics           — lexical match query -> metrics (cold-start)
  identify_entity           — opaque-literal type ID via value_pattern + probe-confirm
  resolve_entity_instance   — strategy chosen at runtime from sensitive + cardinality
  pre_flight_check          — fan-trap / chasm-trap detection + rewrite-vs-refuse
  build_receipt             — receipt-panel assembly

Pre-flight scope note (documented decision, recorded in the PR description):
The cardinality field on every relationship is the day-1 structural gate. The
detector here is **complete and deterministic** for both fan-trap and chasm-trap.
For the *rewrite*, we auto-rewrite the textbook aggregation-only fan-trap (drop a
redundant fan-out join → correct scalar) because we can guarantee the rewrite is
result-preserving. For chasm-traps and fan-traps where the many-side participates
in SELECT/WHERE/GROUP BY, we return the policy decision + the suggested fix rather
than synthesizing a CTE rewrite we can't prove correct — never emit wrong SQL.
Generic CTE synthesis is the planner follow-on (plan: "fan-trap detector v1.5+").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Optional

try:
    import sqlglot
    from sqlglot import expressions as exp

    _HAVE_SQLGLOT = True
except ImportError:  # pragma: no cover
    _HAVE_SQLGLOT = False

from .models import Entity, Metric, Organization, Relationship, SubjectArea

# A prober resolves a literal/value against the DB. Returns True if the value
# exists in <table>.<column>. Injected so runtime stays DB-agnostic.
Prober = Callable[[str, str, str], bool]

# Ambiguity threshold — "ask, don't guess" when top-two are within this delta.
AMBIGUITY_DELTA = 0.15

# Instance-resolution strategy thresholds.
CACHED_INDEX_MAX_CARDINALITY = 10_000
ENUM_MAX_CARDINALITY = 50


# ---------------------------------------------------------------------------
# Step 1 — subject areas
# ---------------------------------------------------------------------------


def list_subject_areas(org: Organization) -> list[dict[str, Any]]:
    """Compact listing for area selection — also the one-call model map. The counts
    tell a caller the whole shape of each area (and where things live: relationships
    and entities/metrics are area-level, not per-table) without reading any YAML."""
    return [
        {
            "name": sa.name,
            "description": sa.description,
            "table_count": len(sa.tables),
            "entity_count": len(sa.entities),
            "metric_count": len(sa.metrics),
            "relationship_count": len(sa.relationships),
            "default_time_window": sa.default_time_window,
        }
        for sa in org.subject_areas
    ]


# ---------------------------------------------------------------------------
# Step 2 — examples first
# ---------------------------------------------------------------------------


@dataclass
class ExampleMatch:
    example: dict[str, Any]
    score: float


def get_prompt_examples(
    query: str, examples: list[dict[str, Any]], *, top_k: int = 5
) -> list[ExampleMatch]:
    """Rank scope-tagged examples by similarity to `query`. Highest first.

    Each example is a dict with at least a `question` (and typically `sql`,
    `tables`, `columns`, `metric`, `default_filters` scope tags). A top match with
    score >= HIGH_CONFIDENCE short-circuits the cold-start path (caller's job).
    """
    scored: list[ExampleMatch] = []
    for ex in examples:
        q = ex.get("question") or ex.get("nl") or ""
        scored.append(ExampleMatch(ex, _similarity(query, q)))
    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:top_k]


HIGH_CONFIDENCE_EXAMPLE = 0.82


def is_high_confidence(matches: list[ExampleMatch]) -> bool:
    return bool(matches) and matches[0].score >= HIGH_CONFIDENCE_EXAMPLE


# ---------------------------------------------------------------------------
# Step 3 — resolve entities / metrics (cold-start, lexical)
# ---------------------------------------------------------------------------


def _area_entities(org: Organization, area: Optional[str]) -> list[tuple[Optional[str], Entity]]:
    out: list[tuple[Optional[str], Entity]] = []
    for sa in org.subject_areas:
        if area and sa.name != area:
            continue
        for e in sa.entities:
            out.append((sa.name, e))
    for e in org.cross_subject_area_entities:
        out.append((None, e))
    return out


def resolve_entities(
    query: str, org: Organization, *, area: Optional[str] = None, top_k: int = 5
) -> list[dict[str, Any]]:
    """Lexically match query terms to entity name / plural / other_names."""
    q = query.lower()
    ranked: list[tuple[float, dict[str, Any]]] = []
    for area_name, ent in _area_entities(org, area):
        names = [ent.name] + ([ent.plural] if ent.plural else []) + list(ent.other_names)
        score = max((_term_score(q, n) for n in names if n), default=0.0)
        if score > 0:
            primary = next((m for m in ent.maps_to if m.primary), None) or (
                ent.maps_to[0] if ent.maps_to else None
            )
            ranked.append(
                (
                    score,
                    {
                        "entity": ent.name,
                        "subject_area": area_name,
                        "score": round(score, 3),
                        "primary_mapping": (
                            {"table": primary.table, "column": primary.column}
                            if primary
                            else None
                        ),
                        "value_pattern": ent.value_pattern,
                    },
                )
            )
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in ranked[:top_k]]


def resolve_metrics(
    query: str, org: Organization, *, area: Optional[str] = None, top_k: int = 5
) -> list[dict[str, Any]]:
    q = query.lower()
    ranked: list[tuple[float, dict[str, Any]]] = []
    metrics: list[tuple[Optional[str], Metric]] = []
    for sa in org.subject_areas:
        if area and sa.name != area:
            continue
        for mm in sa.metrics:
            metrics.append((sa.name, mm))
    for mm in org.cross_subject_area_metrics:
        metrics.append((None, mm))
    for area_name, mm in metrics:
        names = [mm.name] + list(mm.other_names)
        score = max((_term_score(q, n) for n in names if n), default=0.0)
        if score > 0:
            ranked.append(
                (
                    score,
                    {
                        "metric": mm.name,
                        "subject_area": area_name,
                        "score": round(score, 3),
                        "calculation": mm.calculation,
                        "bindings": mm.bindings,
                        "confidence": mm.confidence,
                    },
                )
            )
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in ranked[:top_k]]


# ---------------------------------------------------------------------------
# Entity resolution — type identification (value_pattern + probe)
# ---------------------------------------------------------------------------


@dataclass
class IdentifyResult:
    status: str  # "resolved" | "clarify" | "unrecognized"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    question_template: Optional[str] = None


def identify_entity(
    literal: str,
    org: Organization,
    *,
    area: Optional[str] = None,
    probe: Optional[Prober] = None,
    query_context: str = "",
) -> IdentifyResult:
    """Identify what kind of thing an opaque literal is.

    1. value_pattern regex match across entities.
    2. For pattern matches, probe each candidate's primary mapping to confirm
       the value exists (when a prober is supplied).
    3. single confirmed -> resolved; multiple -> clarify; none -> probe small
       candidates as fallback; still none -> unrecognized.
    """
    pattern_hits: list[tuple[Optional[str], Entity]] = []
    for area_name, ent in _area_entities(org, area):
        if ent.value_pattern:
            try:
                if re.search(ent.value_pattern, literal):
                    pattern_hits.append((area_name, ent))
            except re.error:
                continue

    confirmed: list[dict[str, Any]] = []
    for area_name, ent in pattern_hits:
        ok = True
        mapping = next((m for m in ent.maps_to if m.primary), None) or (
            ent.maps_to[0] if ent.maps_to else None
        )
        if probe and mapping:
            try:
                ok = probe(mapping.table, mapping.column, literal)
            except Exception:
                ok = False
        confirmed.append(
            {
                "entity": ent.name,
                "subject_area": area_name,
                "matched_pattern": ent.value_pattern,
                "probe_confirmed": ok if probe else None,
                "mapping": (
                    {"table": mapping.table, "column": mapping.column} if mapping else None
                ),
            }
        )

    # filter to probe-confirmed when probing happened
    effective = [c for c in confirmed if c["probe_confirmed"] in (True, None)]
    if probe:
        effective = [c for c in confirmed if c["probe_confirmed"] is True] or []

    if len(effective) == 1:
        return IdentifyResult("resolved", effective)
    if len(effective) > 1:
        names = " or ".join(c["entity"] for c in effective)
        return IdentifyResult(
            "clarify",
            effective,
            question_template=(
                f"'{literal}' could be a {names}. Which did you mean?"
            ),
        )

    # no pattern/probe match: fallback probe of small-cardinality candidates
    # (caller supplies cardinalities via resolve_entity_instance normally; here
    # we just report unrecognized when nothing matched).
    if not pattern_hits:
        return IdentifyResult("unrecognized")
    # pattern matched but probe disconfirmed all
    return IdentifyResult("unrecognized", confirmed)


def resolve_entity_instance(
    entity: Entity,
    *,
    sensitive: Optional[bool] = None,
    cardinality: Optional[int] = None,
) -> str:
    """Decide the instance-resolution strategy generically from properties.

    sensitive -> db_probe (never extract).
    cardinality > 10K -> db_probe.
    cardinality <= 50 -> enum.
    else -> cached_index.
    A per-entity clarification_strictness=high doesn't change strategy; it's a
    runtime ask-always flag honored by the caller.
    """
    if sensitive is None:
        # infer from any mapped column flagged sensitive is the caller's job; default false
        sensitive = False
    if sensitive:
        return "db_probe"
    if cardinality is None:
        return "db_probe"  # unknown -> safest live probe
    if cardinality <= ENUM_MAX_CARDINALITY:
        return "enum"
    if cardinality <= CACHED_INDEX_MAX_CARDINALITY:
        return "cached_index"
    return "db_probe"


# ---------------------------------------------------------------------------
# Pre-flight: fan-trap / chasm-trap
# ---------------------------------------------------------------------------


@dataclass
class PreFlightResult:
    risk: Optional[str]  # "fan_trap" | "chasm_trap" | None
    action: str  # "auto_rewrite" | "refuse" | "allow"
    original_sql: str
    rewritten_sql: Optional[str] = None
    reason: str = ""
    suggestion: Optional[str] = None
    triggering_joins: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "action": self.action,
            "original_sql": self.original_sql,
            "rewritten_sql": self.rewritten_sql,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "triggering_joins": self.triggering_joins,
        }


def _cardinality_index(org: Organization) -> list[Relationship]:
    rels: list[Relationship] = []
    for sa in org.subject_areas:
        rels.extend(sa.relationships)
    rels.extend(org.cross_subject_area_relationships)
    return rels


def _one_side_facing_many(rels: list[Relationship], table: str, others: set[str]) -> list[Relationship]:
    """Relationships where `table` is the ONE side and a joined `other` is the MANY side."""
    hits = []
    for r in rels:
        ft, tt = _bare(r.from_table), _bare(r.to_table)
        if r.relationship == "many_to_one" and tt == table and ft in others:
            hits.append(r)
        elif r.relationship == "one_to_many" and ft == table and tt in others:
            hits.append(r)
    return hits


def _many_side_facing_one(rels: list[Relationship], table: str, dim: str) -> bool:
    """Is `table` the MANY side of a join to dimension `dim` (the ONE side)?"""
    for r in rels:
        ft, tt = _bare(r.from_table), _bare(r.to_table)
        if r.relationship == "many_to_one" and ft == table and tt == dim:
            return True
        if r.relationship == "one_to_many" and tt == table and ft == dim:
            return True
    return False


def pre_flight_check(sql: str, org: Organization) -> PreFlightResult:
    """Detect fan-trap / chasm-trap and decide rewrite-vs-refuse-vs-allow."""
    if not _HAVE_SQLGLOT:
        return PreFlightResult(None, "allow", sql, reason="sqlglot unavailable; skipped")
    try:
        tree = sqlglot.parse_one(sql, error_level="ignore")
    except Exception as e:
        return PreFlightResult(None, "allow", sql, reason=f"unparseable; skipped ({e})")
    if tree is None or not isinstance(tree, exp.Select):
        return PreFlightResult(None, "allow", sql, reason="not a SELECT; skipped")

    rels = _cardinality_index(org)
    tables_in_scope = _tables_in_scope(tree)  # alias -> table
    table_set = set(tables_in_scope.values())

    # aggregates: list of (table, is_aggregate)
    agg_sources = _aggregate_source_tables(tree, tables_in_scope)
    has_raw_columns = _has_raw_non_grouped_columns(tree, tables_in_scope)
    has_aggregate = bool(agg_sources)
    no_aggregation = not has_aggregate

    # explicit cross-product (no aggregation) -> allow with caveat
    if no_aggregation:
        return PreFlightResult(
            None,
            "allow",
            sql,
            reason="no aggregation present; join is not a fan/chasm trap",
        )

    # CHASM: two distinct aggregate source tables both 'many' to a shared dim
    if len(agg_sources) >= 2:
        shared = _shared_dimension(agg_sources, table_set, rels)
        if shared:
            srcs = sorted(agg_sources)
            return PreFlightResult(
                "chasm_trap",
                "refuse",  # documented: CTE synthesis is the planner follow-on
                sql,
                reason=(
                    f"chasm trap: independent measures from {srcs} both join shared "
                    f"dimension {shared!r}; cross-product inflates both aggregates."
                ),
                suggestion=(
                    "Compute each measure in its own CTE pre-aggregated by "
                    f"{shared!r}, then outer-join the CTEs on {shared!r}."
                ),
                triggering_joins=[f"{s} -> {shared}" for s in srcs],
            )

    # FAN: an aggregate over a measure on the ONE side of a one-to-many in scope
    for measure_table in agg_sources:
        others = table_set - {measure_table}
        fan_rels = _one_side_facing_many(rels, measure_table, others)
        if not fan_rels:
            continue
        many_tables = {
            _bare(r.from_table) if _bare(r.to_table) == measure_table else _bare(r.to_table)
            for r in fan_rels
        }
        # Can we safely auto-rewrite? Only if the many side participates ONLY in
        # the FROM/JOIN (not in SELECT / WHERE / GROUP BY / HAVING / ORDER BY)
        # AND the SELECT is aggregation-only (no raw rows).
        many_aliases = {a for a, t in tables_in_scope.items() if t in many_tables}
        referenced_elsewhere = _tables_referenced_outside_from(tree, tables_in_scope) & many_tables
        if not has_raw_columns and not referenced_elsewhere:
            rewritten = _drop_fanout_joins(sql, many_aliases | many_tables)
            if rewritten and rewritten.strip() != sql.strip():
                return PreFlightResult(
                    "fan_trap",
                    "auto_rewrite",
                    sql,
                    rewritten_sql=rewritten,
                    reason=(
                        f"fan trap: aggregating {measure_table!r} (one side) across a "
                        f"join to {sorted(many_tables)} (many side) would multiply the "
                        "measure. Redundant fan-out join dropped; result shape unchanged."
                    ),
                    triggering_joins=[
                        f"{measure_table} (1) <- {mt} (N)" for mt in sorted(many_tables)
                    ],
                )
        # mixed raw + aggregate, or many-side used in WHERE/GROUP BY -> refuse
        return PreFlightResult(
            "fan_trap",
            "refuse",
            sql,
            reason=(
                f"fan trap: aggregating {measure_table!r} (one side) across a join to "
                f"{sorted(many_tables)} (many side). Rewrite would change result shape."
            ),
            suggestion=(
                "Either pre-aggregate the one-side measure in a CTE before joining, "
                "or move the aggregate into a window function to keep the raw rows."
            ),
            triggering_joins=[f"{measure_table} (1) <- {mt} (N)" for mt in sorted(many_tables)],
        )

    return PreFlightResult(None, "allow", sql, reason="no fan/chasm trap detected")


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    sql: str,
    relationships_used: Optional[list[Relationship]] = None,
    pre_flight: Optional[PreFlightResult] = None,
    caveats: Optional[list[str]] = None,
    default_filters_applied: Optional[list[str]] = None,
    model_version: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the receipt panel: SQL, relationships (+ confidence + signers),
    any auto-rewrites, applied default_filters, and relevant caveats."""
    receipt: dict[str, Any] = {"sql": sql}
    if model_version:
        receipt["model_version"] = model_version
    if relationships_used:
        receipt["relationships"] = [
            {
                "from": f"{r.from_table}.{r.from_column}" if r.from_column else r.from_table,
                "to": f"{r.to_table}.{r.to_column}" if r.to_column else r.to_table,
                "on": r.on,
                "cardinality": r.relationship,
                "confidence": r.confidence,
                "review_state": r.review_state,
                "signed_off_by": r.signed_off_by,
                "signed_off_at": r.signed_off_at,
                "signed_off_role": r.signed_off_role,
            }
            for r in relationships_used
        ]
    if default_filters_applied:
        receipt["default_filters_applied"] = default_filters_applied
    if caveats:
        receipt["caveats"] = caveats
    if pre_flight and pre_flight.risk:
        receipt["pre_flight"] = {
            "risk": pre_flight.risk,
            "action": pre_flight.action,
            "reason": pre_flight.reason,
        }
        if pre_flight.action == "auto_rewrite":
            receipt["pre_flight"]["original_sql"] = pre_flight.original_sql
            receipt["pre_flight"]["rewritten_sql"] = pre_flight.rewritten_sql
    return receipt


# ---------------------------------------------------------------------------
# SQL helpers (sqlglot)
# ---------------------------------------------------------------------------


def _bare(name: str) -> str:
    return name.split(".")[-1]


def _tables_in_scope(tree: "exp.Select") -> dict[str, str]:
    """alias (or table name) -> bare table name."""
    out: dict[str, str] = {}
    for tbl in tree.find_all(exp.Table):
        name = tbl.name
        alias = tbl.alias_or_name
        out[alias] = name
    return out


def _aggregate_source_tables(tree: "exp.Select", scope: dict[str, str]) -> set[str]:
    """Tables whose columns appear inside an aggregate function in the SELECT."""
    sources: set[str] = set()
    for select_expr in tree.expressions:
        for agg in select_expr.find_all(exp.AggFunc):
            for col in agg.find_all(exp.Column):
                t = _resolve_col_table(col, scope)
                if t:
                    sources.add(t)
    return sources


def _has_raw_non_grouped_columns(tree: "exp.Select", scope: dict[str, str]) -> bool:
    """Does the SELECT include bare (non-aggregated) columns? (GROUP BY keys count
    as raw context for the mixed-shape refuse case.)"""
    for select_expr in tree.expressions:
        # an expression that is itself not an aggregate and contains a column
        if select_expr.find(exp.AggFunc):
            continue
        if select_expr.find(exp.Column):
            return True
    return False


def _tables_referenced_outside_from(tree: "exp.Select", scope: dict[str, str]) -> set[str]:
    """Tables whose columns are referenced in SELECT/WHERE/GROUP BY/HAVING/ORDER BY
    (i.e. anywhere that is NOT just the FROM/JOIN ON-clause)."""
    referenced: set[str] = set()

    def collect(node):
        if node is None:
            return
        for col in node.find_all(exp.Column):
            t = _resolve_col_table(col, scope)
            if t:
                referenced.add(t)

    for e in tree.expressions:  # SELECT list
        collect(e)
    where = tree.args.get("where")
    collect(where.this if where else None)
    group = tree.args.get("group")
    if group:
        for e in group.expressions:
            collect(e)
    having = tree.args.get("having")
    collect(having.this if having else None)
    order = tree.args.get("order")
    if order:
        for e in order.expressions:
            collect(e)
    return referenced


def _resolve_col_table(col: "exp.Column", scope: dict[str, str]) -> Optional[str]:
    if col.table:
        return scope.get(col.table, col.table)
    # unqualified column: ambiguous; only safe to attribute if single table
    if len(scope) == 1:
        return next(iter(scope.values()))
    return None


def _shared_dimension(
    agg_sources: set[str], table_set: set[str], rels: list[Relationship]
) -> Optional[str]:
    """Find a dimension table that >=2 of the aggregate sources are each MANY-to
    (the ONE side), with the sources not directly related to each other."""
    for dim in table_set:
        if dim in agg_sources:
            continue
        many_sources = [s for s in agg_sources if _many_side_facing_one(rels, s, dim)]
        if len(many_sources) >= 2:
            return dim
    return None


def _drop_fanout_joins(sql: str, drop_tables: set[str]) -> Optional[str]:
    """Remove JOINs whose target table/alias is in drop_tables. Used for the
    safe aggregation-only fan-trap rewrite."""
    try:
        tree = sqlglot.parse_one(sql, error_level="ignore")
    except Exception:
        return None
    if not isinstance(tree, exp.Select):
        return None
    joins = tree.args.get("joins") or []
    kept = []
    changed = False
    for j in joins:
        target = j.this
        tname = target.alias_or_name if isinstance(target, exp.Table) else None
        tbare = target.name if isinstance(target, exp.Table) else None
        if tname in drop_tables or tbare in drop_tables:
            changed = True
            continue
        kept.append(j)
    if not changed:
        return None
    tree.set("joins", kept)
    return tree.sql()


def apply_default_filters(
    sql: str,
    org: Organization,
    *,
    area: Optional[str] = None,
    params: Optional[dict[str, str]] = None,
) -> tuple[str, list[str]]:
    """Conservatively AND each in-scope table's default_filters into the SQL's WHERE.

    Safety-first (this is the trust layer): only the outermost SELECT is touched;
    a filter is injected only when its table appears as a base table there and the
    `{alias}` placeholder can be bound to the actual alias used. Filters with
    unresolved `:param` markers are skipped unless `params` supplies them. If
    anything is ambiguous, the filter is left out (never emit wrong SQL) and the
    caller can see which were applied via the returned list.
    """
    from .loader import _find_table  # local import to avoid cycle at module load

    params = params or {}
    if not _HAVE_SQLGLOT:
        return sql, []
    try:
        tree = sqlglot.parse_one(sql, error_level="ignore")
    except Exception:
        return sql, []
    if not isinstance(tree, exp.Select):
        return sql, []

    scope = _tables_in_scope(tree)  # alias -> bare table
    applied: list[str] = []
    conditions: list[str] = []
    for alias, table_name in scope.items():
        table = _find_table(org, table_name, area)
        if table is None or not table.default_filters:
            continue
        for flt in table.default_filters:
            resolved = flt.replace("{alias}", alias)
            # bind known params; skip if any :param remains unresolved
            for k, val in params.items():
                resolved = resolved.replace(f":{k}", str(val))
            if re.search(r":\w+", resolved):
                continue  # unresolved bind param -> skip (can't run raw)
            conditions.append(resolved)
            applied.append(resolved)

    if not conditions:
        return sql, []

    combined = " AND ".join(f"({c})" for c in conditions)
    try:
        cond_expr = sqlglot.parse_one(f"SELECT 1 WHERE {combined}").args["where"].this
        where = tree.args.get("where")
        if where is not None:
            tree.set("where", exp.Where(this=exp.and_(where.this, cond_expr)))
        else:
            tree.set("where", exp.Where(this=cond_expr))
        return tree.sql(), applied
    except Exception:
        return sql, []


def _similarity(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    base = SequenceMatcher(None, a, b).ratio()
    # boost on shared significant tokens
    ta = {t for t in re.findall(r"\w+", a) if len(t) > 2}
    tb = {t for t in re.findall(r"\w+", b) if len(t) > 2}
    if ta and tb:
        jacc = len(ta & tb) / len(ta | tb)
        return max(base, 0.4 * base + 0.6 * jacc)
    return base


def _term_score(query_lower: str, name: str) -> float:
    n = name.lower().strip()
    if not n:
        return 0.0
    if n in query_lower:
        return 1.0
    ntoks = {t for t in re.findall(r"\w+", n) if len(t) > 2}
    qtoks = set(re.findall(r"\w+", query_lower))
    if ntoks and ntoks <= qtoks:
        return 0.9
    if ntoks & qtoks:
        return 0.5 * len(ntoks & qtoks) / len(ntoks)
    return 0.0


def resolve_result_units(org: Organization, sql: str) -> dict[str, str]:
    """Map each SELECT output column -> display unit, **tracing the SQL** (not matching
    names): an aggregate/expression over a column inherits that column's unit, so
    `SUM(amount) AS total_outstanding` correctly resolves to amount's currency — the
    BI-common total that a bare name match would miss. Rules:
      - output name that matches a metric name -> the metric's unit;
      - otherwise inherit the unit of the column(s) referenced, IF they share exactly
        one unit (so SUM/AVG/MIN/MAX/`col*1.1` of a currency column stay that currency);
      - COUNT(...) and ratios (any division) get NO currency unit (a count / rate isn't
        money). Returns {} if the model carries no units or sqlglot can't parse.
    """
    import sqlglot
    from sqlglot import expressions as exp

    col_units: dict[str, str] = {}
    metric_units: dict[str, str] = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            for c in t.columns:
                if c.unit:
                    col_units.setdefault(c.name.lower(), c.unit)
        for m in sa.metrics:
            if m.unit:
                metric_units.setdefault(m.name.lower(), m.unit)
    for m in getattr(org, "cross_subject_area_metrics", []):
        if getattr(m, "unit", None):
            metric_units.setdefault(m.name.lower(), m.unit)
    if not col_units and not metric_units:
        return {}

    try:
        tree = sqlglot.parse_one(sql, error_level="ignore")
    except Exception:
        return {}
    select = tree.find(exp.Select) if tree is not None else None
    if select is None:
        return {}

    out: dict[str, str] = {}
    for proj in select.expressions:
        name = proj.alias_or_name
        if not name:
            continue
        if name.lower() in metric_units:
            out[name] = metric_units[name.lower()]
            continue
        # a count or a ratio is not currency, regardless of the columns inside it
        if proj.find(exp.Count) is not None or proj.find(exp.Div) is not None:
            continue
        units = {col_units[c.name.lower()] for c in proj.find_all(exp.Column)
                 if c.name and c.name.lower() in col_units}
        if len(units) == 1:
            out[name] = next(iter(units))
    return out


__all__ = [
    "Prober",
    "AMBIGUITY_DELTA",
    "resolve_result_units",
    "list_subject_areas",
    "ExampleMatch",
    "get_prompt_examples",
    "is_high_confidence",
    "HIGH_CONFIDENCE_EXAMPLE",
    "resolve_entities",
    "resolve_metrics",
    "IdentifyResult",
    "identify_entity",
    "resolve_entity_instance",
    "PreFlightResult",
    "pre_flight_check",
    "apply_default_filters",
    "build_receipt",
]
