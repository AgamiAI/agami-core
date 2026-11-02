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
from typing import NamedTuple, Optional

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


def expand_binding(metric, storage_type: str, idx: dict) -> str:
    """Recursively expand a metric's `{base}` placeholders for `storage_type` into
    standalone SQL. Raises DerivedError on a cycle, an unknown base, a base that
    lacks a binding for this dialect, or a second-order (case-b) nesting."""
    expanded = _expand(metric, storage_type, idx, [])
    # Judged once, on the whole expansion: a base's own nesting must not hide the one its
    # enclosing metric adds (AVG over a percentile of a SUM).
    problems = binding_problems(expanded, storage_type)
    if problems and all(p.subquery for p in problems):
        raise NestedAggregateError(
            f"metric {metric.name!r} expands to SQL {storage_type} refuses "
            f"({'; '.join(map(str, problems))}). {_SUBQUERY_FIX}",
            problems,
        )
    if problems:
        raise NestedAggregateError(
            f"metric {metric.name!r} composes an aggregate of an aggregate "
            f"({'; '.join(map(str, problems))}; a second-order statistic) but has no "
            "`inner_grain` declared. To support it, set `inner_grain` (the dimension the base is "
            "grouped by) and bind it as OUTERAGG({base}) — the CTE is then synthesized (see "
            "synthesize_second_order).",
            problems,
        )
    return expanded


def _expand(metric, storage_type: str, idx: dict, stack: list) -> str:
    """`expand_binding` without the nesting check."""
    if metric.name in stack:
        raise DerivedError(
            "derived metric cycle: " + " -> ".join(stack + [metric.name])
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
        inner = _expand(base, storage_type, idx, stack + [metric.name])
        return "(" + inner + ")"

    return PLACEHOLDER_RE.sub(_sub, binding)


class BindingProblem(NamedTuple):
    """One aggregate, or window function, written inside another aggregate or window function of the
    same SELECT; or, on an engine that refuses it, a subquery inside an aggregate."""

    outer: str    # the enclosing aggregate's name, e.g. "AVG"
    inner: str    # the nested aggregate's name, e.g. "SUM"
    clause: str   # where it sits: "argument", "within_group", "filter" or "over"
    window: bool = False  # the nested call is a window function, `SUM(x) OVER ()`
    outer_window: bool = False  # the enclosing call is a window function too
    subquery: bool = False  # the nested thing is a subquery, refused only on some engines

    def __str__(self) -> str:
        where = {"within_group": " WITHIN GROUP (ORDER BY ...)",
                 "filter": " FILTER (WHERE ...)"}.get(self.clause, "")
        if self.subquery:
            return f"a subquery is inside {self.outer}(...){where}"
        inner = f"the window function {self.inner}(...) OVER (...)" if self.window else f"{self.inner}(...)"
        if self.outer_window:
            where = "the OVER (...) clause of " if self.clause == "over" else ""
            return f"{inner} is nested inside {where}the window function {self.outer}(...) OVER (...)"
        return f"{inner} is nested inside {self.outer}(...){where}"


def _aggregate_unit(agg):
    """The aggregate together with its WITHIN GROUP / FILTER / IGNORE NULLS wrappers. The database
    reads those clauses in the aggregate's own scope, so an aggregate written in one is nested just
    like one in the argument."""
    from sqlglot import expressions as exp

    unit = agg
    while isinstance(unit.parent, (exp.WithinGroup, exp.Filter, exp.IgnoreNulls, exp.RespectNulls)):
        unit = unit.parent
    return unit


def _is_aggregate(node) -> bool:
    """An aggregate call of this SELECT: not a window function, and not the two-argument scalar
    MAX(a, b) / MIN(a, b) some engines have."""
    from sqlglot import expressions as exp

    if not isinstance(node, exp.AggFunc):
        return False
    if isinstance(node, (exp.Max, exp.Min)) and node.expressions:
        return False
    return not isinstance(_aggregate_unit(node).parent, exp.Window)


def _selects_above(node) -> list:
    """The SELECTs enclosing `node`, innermost first."""
    from sqlglot import expressions as exp

    return [a for a in _ancestors_until(node, None) if isinstance(a, exp.Select)]


def _ident_key(ident, fold: Optional[str]) -> Optional[str]:
    """An identifier as the engine compares it: a quoted name keeps its case where the engine folds
    unquoted ones (`fold`, as `validator._identifier_fold`), so `"ORDERS"` is not `orders` there."""
    from sqlglot import expressions as exp

    if not isinstance(ident, exp.Identifier) or not ident.name:
        return None
    if fold and ident.args.get("quoted"):
        return ident.name
    return ident.name.upper() if fold == "upper" else ident.name.lower()


def _from_sources(sel) -> list:
    """The sources in a SELECT's FROM and JOINs, a parenthesized `(orders JOIN customers ...)`
    opened up into its tables, since its names stay visible to the SELECT."""
    from sqlglot import expressions as exp

    out = []
    pending = [sel.args.get("from_") or sel.args.get("from")] + list(sel.args.get("joins") or [])
    pending = [s.this for s in pending if s is not None]
    while pending:
        src = pending.pop(0)
        if (isinstance(src, exp.Subquery) and not src.alias
                and isinstance(src.this, (exp.Table, exp.Subquery))):
            pending.insert(0, src.this)
            continue
        out.append(src)
        if isinstance(src, exp.Table):
            pending[:0] = [j.this for j in src.args.get("joins") or []]
    return out


def _names_source(col, src, fold: Optional[str]) -> bool:
    """Whether column `col`'s qualifier names source `src`. An alias hides the table's own name. A
    qualifier part the other side leaves out is taken to match (`public.orders.amount` over
    `FROM orders`), so a subquery that may read its own rows is never judged correlated."""
    from sqlglot import expressions as exp

    alias = src.args.get("alias")
    if alias is not None and alias.this is not None:
        return (col.args.get("db") is None
                and _ident_key(col.args.get("table"), fold) == _ident_key(alias.this, fold))
    if not isinstance(src, exp.Table):
        return False
    for part in ("table", "db", "catalog"):
        mine = col.args.get(part)
        theirs = src.args.get("this" if part == "table" else part)
        if part != "table" and (mine is None or theirs is None):
            continue
        if _ident_key(mine, fold) is None or _ident_key(mine, fold) != _ident_key(theirs, fold):
            return False
    return True


def _column_level(col, fold: Optional[str]):
    """The SELECT whose FROM a column reference resolves to, or None for the query a bare binding
    is placed in. Without a schema an unqualified column is taken to be the innermost FROM's, so a
    subquery that may read its own rows is never judged correlated."""
    for sel in _selects_above(col):
        sources = _from_sources(sel)
        if not col.table:
            if sources:
                return sel
        elif any(_names_source(col, s, fold) for s in sources):
            return sel
    return None


def _owning_select(agg, fold: Optional[str]):
    """The SELECT an aggregate is evaluated over. Like PostgreSQL and SQLite, an aggregate in a
    subquery whose arguments read only outer columns belongs to the nearest of those outer levels:
    `AVG((SELECT SUM(orders.amount)))` nests SUM in AVG."""
    from sqlglot import expressions as exp

    levels = _selects_above(agg)
    own = levels[0] if levels else None
    unit = _aggregate_unit(agg)
    cols = [c for c in unit.find_all(exp.Column)
            if not any(isinstance(a, exp.Query) for a in _ancestors_until(c, unit))]
    if not cols:
        return own
    found = [_column_level(c, fold) for c in cols]
    if any(f is own for f in found):
        return own
    return next((sel for sel in levels if any(f is sel for f in found)), None)


def _ancestors_until(node, stop):
    """`node`'s ancestors, up to (not including) `stop`."""
    node = node.parent
    while node is not None and node is not stop:
        yield node
        node = node.parent


def _correlated_in(query, agg, fold: Optional[str]):
    """The aggregates inside the subquery `query` that are evaluated over `agg`'s own SELECT."""
    owner = _owning_select(agg, fold)
    for node in query.walk():
        if _is_aggregate(node) and _owning_select(node, fold) is owner:
            yield node


def _nested_in(unit, agg, fold: Optional[str]):
    """The aggregates and window functions directly inside `unit` in the same SELECT. A subquery is
    a scope of its own: an aggregate over a derived table or a scalar subquery is legal SQL, and
    only an aggregate in it that reads nothing but `agg`'s columns is nested (`_owning_select`).
    A window function is yielded, not entered, since no engine allows one inside an aggregate; a
    window over an aggregate, `SUM(SUM(x)) OVER ()`, never reaches here."""
    from sqlglot import expressions as exp

    for child in unit.iter_expressions():
        if isinstance(child, exp.Query):
            yield from _correlated_in(child, agg, fold)
            continue
        call = child
        # a nested aggregate's own WITHIN GROUP / FILTER belongs to it, not to `agg`
        while isinstance(call, (exp.WithinGroup, exp.Filter, exp.IgnoreNulls, exp.RespectNulls)):
            call = call.this
        if isinstance(child, exp.Window) or (call is not agg and _is_aggregate(call)):
            yield child if isinstance(child, exp.Window) else call
            continue
        yield from _nested_in(child, agg, fold)


def _call_name(node) -> str:
    """The function a nested call names; for a window, the function under OVER."""
    from sqlglot import expressions as exp

    while isinstance(node, (exp.Window, exp.WithinGroup, exp.Filter, exp.IgnoreNulls, exp.RespectNulls)):
        node = node.this
    return node.sql_name() if isinstance(node, exp.Func) else "?"


def _grouped_select(node) -> bool:
    """Whether the SELECT `node` belongs to has a GROUP BY."""
    from sqlglot import expressions as exp

    scope = node.parent
    while scope is not None and not isinstance(scope, exp.Query):
        scope = scope.parent
    return isinstance(scope, exp.Select) and scope.args.get("group") is not None


def _oracle_allows(agg, inner, clause: str, storage_type: str, fold: Optional[str]) -> bool:
    """Oracle accepts two levels of aggregate in the argument of a grouped SELECT,
    `SELECT AVG(SUM(amount)) FROM orders GROUP BY customer_id`. A bare binding has no GROUP BY of
    its own and still nests, and a third level is refused there too."""
    from sqlglot import expressions as exp

    if storage_type != "Oracle" or clause != "argument" or not _is_aggregate(inner):
        return False
    if (not _grouped_select(agg)
            or next(_nested_in(_aggregate_unit(inner), inner, fold), None) is not None):
        return False
    outer = _aggregate_unit(agg).parent
    while outer is not None and not isinstance(outer, exp.Query):
        if _is_aggregate(outer):
            return False
        outer = outer.parent
    return True


def _clause_of(inner, agg) -> str:
    """Whether `inner` sits in `agg`'s argument or in its WITHIN GROUP / FILTER clause. Only `agg`'s
    own wrappers count: `inner`'s, as in `AVG(SUM(a) FILTER (WHERE a > 0))`, leave it in the argument."""
    from sqlglot import expressions as exp

    wrappers = []
    node = agg.parent
    while isinstance(node, (exp.WithinGroup, exp.Filter, exp.IgnoreNulls, exp.RespectNulls)):
        wrappers.append(node)
        node = node.parent
    node = _aggregate_unit(inner).parent
    while node is not None and node is not agg:
        if any(node is w for w in wrappers):
            if isinstance(node, exp.WithinGroup):
                return "within_group"
            if isinstance(node, exp.Filter):
                return "filter"
            break
        node = node.parent
    return "argument"


def binding_problems(sql: str, storage_type: str) -> list[BindingProblem]:
    """The one rule for whether a metric binding nests aggregates (#519), judged per SELECT.

    An aggregate directly inside another aggregate of the same SELECT is refused (`AVG(SUM(a))`),
    and so is one in the aggregate's WITHIN GROUP or FILTER clause
    (`PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY SUM(a))`), and a window function inside an
    aggregate (`AVG(SUM(a) OVER ())`) or inside another window function
    (`SUM(SUM(a) OVER ()) OVER ()`). The one exception is Oracle, which runs two levels of
    aggregate in the argument when that SELECT has a GROUP BY. An aggregate over a derived table or a
    scalar subquery is legal and is not a problem, except on SQL Server, which refuses any subquery
    inside an aggregate. Parsed in the binding's own engine; a binding
    that does not parse (a `{placeholder}`, an engine with no grammar) yields nothing, so this never
    blocks on what it cannot read."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
        from sqlglot.errors import ErrorLevel

        from .sql_dialect import sqlglot_dialect

        dialect = sqlglot_dialect(storage_type)
        tree = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return []
    if tree is None:
        return []
    from .validator import _identifier_fold

    fold = _identifier_fold(storage_type)
    problems: list[BindingProblem] = []
    for agg in [n for n in tree.walk() if _is_aggregate(n)]:
        unit = _aggregate_unit(agg)
        for inner in _nested_in(unit, agg, fold):
            clause = _clause_of(inner, agg)
            if _oracle_allows(agg, inner, clause, storage_type, fold):
                continue
            problems.append(BindingProblem(agg.sql_name(), _call_name(inner), clause,
                                           isinstance(inner, exp.Window)))
        if storage_type in _NO_SUBQUERY_IN_AGGREGATE:
            sub = next((q for q in unit.find_all(exp.Query) if q is not unit), None)
            if sub is not None:
                problems.append(BindingProblem(agg.sql_name(), "subquery", _clause_of(sub, agg),
                                               subquery=True))
    for win in tree.find_all(exp.Window):
        problems.extend(_window_in_window(win))
    return problems


# Engines that refuse a subquery anywhere in an aggregate's argument, correlated or not: SQL Server
# (error 130, "Cannot perform an aggregate function on an expression containing an aggregate or a
# subquery").
_NO_SUBQUERY_IN_AGGREGATE = frozenset({"SQLServer"})

_SUBQUERY_FIX = ("Aggregate over a derived table instead: "
                 "(SELECT AVG(t.v) FROM (SELECT ... AS v FROM ...) AS t).")


def _window_in_window(win) -> list[BindingProblem]:
    """A window function inside another's argument or OVER clause in the same SELECT, which no
    engine runs: `SUM(SUM(x) OVER ()) OVER ()`. An aggregate there is legal, `SUM(SUM(x)) OVER ()`,
    and so is anything in a subquery."""
    from sqlglot import expressions as exp

    out = []
    for part, clause in ((win.this, "argument"), (win.args.get("partition_by"), "over"),
                         (win.args.get("order"), "over")):
        for node in part if isinstance(part, list) else [part]:
            if node is None:
                continue
            for inner in node.find_all(exp.Window):
                if inner is win or any(isinstance(a, exp.Query)
                                       for a in _ancestors_until(inner, win)):
                    continue
                # a window or aggregate in between reports this one itself
                if any(isinstance(a, exp.Window) or _is_aggregate(a)
                       for a in _ancestors_until(inner, win)):
                    continue
                out.append(BindingProblem(_call_name(win), _call_name(inner), clause,
                                          window=True, outer_window=True))
    return out


class NestedAggregateError(DerivedError):
    """A metric's SQL, once expanded, nests an aggregate inside another (`binding_problems`)."""

    def __init__(self, message: str, problems: list[BindingProblem]) -> None:
        super().__init__(message)
        self.problems = problems


def _has_nested_aggregate(sql_fragment: str, storage_type: str) -> bool:
    """True if `sql_fragment` nests an aggregate inside another (see `binding_problems`)."""
    return bool(binding_problems(sql_fragment, storage_type))


def metric_binding_problems(metric, idx: dict, org=None) -> dict[str, list[BindingProblem]]:
    """{storage type: problems} for each binding of `metric` that nests aggregates. A plain
    binding is judged as written; a derived or second-order one as it expands. Any other
    composition failure is not reported here: `_check_derived_metrics` owns those."""
    out: dict[str, list[BindingProblem]] = {}
    for stype, sql in (getattr(metric, "bindings", None) or {}).items():
        if not (is_derived(metric) or is_second_order(metric)):
            problems = binding_problems(sql or "", stype)
        else:
            try:
                resolve_metric_sql(metric, stype, idx, org)
                problems = []
            except NestedAggregateError as e:
                problems = e.problems
            except DerivedError:
                problems = []
        if problems:
            out[stype] = problems
    return out


def nested_aggregate_reason(metric_name: str, problems: dict[str, list[BindingProblem]]) -> str:
    """One sentence naming each nested aggregate and the forms that do run."""
    found = "; ".join(f"{stype}: {p}" for stype, ps in problems.items() for p in ps)
    if all(p.subquery for ps in problems.values() for p in ps):
        return (f"metric {metric_name!r} puts a subquery inside an aggregate ({found}), which the "
                f"database refuses. {_SUBQUERY_FIX}")
    return (
        f"metric {metric_name!r} nests an aggregate inside another ({found}), which the database "
        "refuses. Write a plain aggregate (AVG(col) already ignores missing values), aggregate "
        "over a subquery that groups first, or, for an average of per-group totals, declare a "
        "second-order metric: bind OUTERAGG({base metric}) and set inner_grain."
    )


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
# Reserved on one engine beyond the list above. SQLite's are the keywords it refuses as a bare
# table or column name (measured on 3.45). Not listed for an engine that folds unquoted names to
# upper case: quoting a lower-case name there would change which object it names.
_ENGINE_RESERVED_WORDS = {
    "SQLite": frozenset({
        "add", "alter", "autoincrement", "collate", "commit", "deferrable", "escape", "index",
        "isnull", "nothing", "notnull", "raise", "returning", "set", "transaction",
    }),
    "MySQL": frozenset({
        "analyze", "both", "call", "cascade", "change", "char", "character", "condition",
        "cume_dist", "database", "databases", "decimal", "dense_rank", "describe", "div", "double",
        "dual", "empty", "explain", "first_value", "float", "fulltext", "function", "generated",
        "groups", "if", "index", "int", "integer", "interval", "iterate", "key", "keys", "lag",
        "last_value", "lateral", "lead", "leading", "leave", "linear", "load", "lock", "long",
        "loop", "match", "maxvalue", "mod", "nth_value", "ntile", "numeric", "of", "option",
        "over", "percent_rank", "procedure", "range", "rank", "read", "real", "recursive",
        "release", "rename", "repeat", "replace", "require", "restrict", "return", "row",
        "row_number", "rows", "schema", "schemas", "separator", "set", "show", "signal",
        "spatial", "sql", "stored", "system", "trailing", "trigger", "unlock", "unsigned",
        "usage", "use", "varchar", "virtual", "while", "write", "zerofill",
    }),
    "SQLServer": frozenset({
        "backup", "browse", "close", "file", "identity", "index", "key", "open", "percent",
        "pivot", "plan", "proc", "procedure", "rule", "schema", "set", "top", "tran", "unpivot",
    }),
    "PostgreSQL": frozenset({
        "analyse", "analyze", "array", "asymmetric", "authorization", "both", "collate",
        "collation", "concurrently", "current_catalog", "current_role", "current_schema",
        "deferrable", "do", "freeze", "ilike", "initially", "isnull", "lateral", "leading",
        "localtime", "localtimestamp", "notnull", "only", "overlaps", "placing", "returning",
        "similar", "some", "symmetric", "tablesample", "trailing", "variadic", "verbose",
    }),
}
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
    if not _PLAIN_IDENT_RE.match(name):
        return dialect.quote_ident(name)
    if _is_reserved(name.lower(), storage_type):
        # On an upper-folding engine a lower-case name means the stored upper-case one, and a
        # quoted lower-case name would name a different object.
        if name == name.lower() and _engine_folds("a", storage_type):
            return dialect.quote_ident(name.upper())
        return dialect.quote_ident(name)
    if name != name.lower() and _engine_folds(name, storage_type):
        return dialect.quote_ident(name)
    return name


def _is_reserved(word: str, storage_type: str) -> bool:
    """A word the engine reserves: the shared list, then (on an engine that does not fold
    unquoted names to upper case) its own list and the installed sqlglot's list for it."""
    if word in _RESERVED_WORDS:
        return True
    if _engine_folds("a", storage_type):
        return False
    return word in _ENGINE_RESERVED_WORDS.get(storage_type, ()) or word in _sqlglot_reserved(storage_type)


def _sqlglot_reserved(storage_type: str) -> frozenset:
    """The installed sqlglot's reserved words for the engine; older releases list few or none."""
    try:
        from sqlglot.dialects.dialect import Dialect

        from .sql_dialect import sqlglot_dialect

        dialect = Dialect.get_or_raise(sqlglot_dialect(storage_type))
        generator = getattr(dialect, "generator_class", None) or dialect.Generator
        return frozenset(w.lower() for w in getattr(generator, "RESERVED_KEYWORDS", ()) or ())
    except Exception:
        return frozenset()


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


def _table_def(name: str, org, base=None):
    """The table's definition, from the base metric's own area first: an area can keep its own
    curated copy of a table another area also defines."""
    home = _owning_area(base, org) if base is not None else None
    areas = ([home] if home else []) + [sa for sa in org.subject_areas if sa is not home]
    for sa in areas:
        for t in sa.tables_defined:
            if _same_table(t.name, t.schema_name, name, None):
                return t
    return None


def _declares(table, col: str) -> bool:
    return table is not None and any(c.name.lower() == col.lower() for c in table.columns)


def _dotted_grain(col: str, froms: list[str], org, base) -> list[tuple[str, str]]:
    """Every (source table, declared column) a `table.column` grain entry can be read as; a
    table or column name may itself contain a dot or a space, so each split is tried."""
    found = []
    for i, ch in enumerate(col):
        if ch != ".":
            continue
        prefix, suffix = col[:i].strip(), col[i + 1:].strip()
        for t in froms:
            if (prefix and suffix and _same_table(t, None, prefix, None)
                    and _declares(_table_def(t, org, base), suffix)):
                found.append((t, suffix))
    return found


def _qualified_grain(metric_name: str, col: str, froms: list[str], org, storage_type: str,
                     base=None) -> str:
    """An `inner_grain` column qualified by its source table: `table.column` as written, else
    the one source table that declares the bare column."""
    dotted = _dotted_grain(col, froms, org, base) if "." in col else []
    if len(dotted) > 1:
        raise DerivedError(
            f"second-order metric {metric_name!r}: inner_grain {col!r} is ambiguous; it can be "
            f"read as {[f'{t}.{c}' for t, c in dotted]}"
        )
    if dotted:
        table, column = dotted[0]
        return f"{_ident(bare_name(table), storage_type)}.{_ident(column, storage_type)}"
    owners = [t for t in froms if _declares(_table_def(t, org, base), col)]
    if not owners and _DOTTED_IDENT_RE.match(col):
        raise DerivedError(
            f"second-order metric {metric_name!r}: inner_grain {col!r} does not name a column "
            f"declared by one of the base metric's tables {froms}"
        )
    if not owners and not _PLAIN_IDENT_RE.match(col):
        raise DerivedError(
            f"second-order metric {metric_name!r}: inner_grain {col!r} must name a column "
            "when the base metric spans several tables"
        )
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


def _grain_sql(col: str, storage_type: str, table=None) -> str:
    """A single-table grain entry: a column name (or a column `table` declares) is quoted as it
    needs, anything else verbatim."""
    if _PLAIN_IDENT_RE.match(col) or _DOTTED_IDENT_RE.match(col):
        return _table_ref(col, storage_type)
    if _declares(table, col):
        return _ident(col, storage_type)
    return col


def _anchor(metric_name: str, base, froms: list[str], org, storage_type: str,
            inner_sql: str) -> str:
    """The source table the base metric measures: its `primary_table`, else the one table that
    declares every column its aggregates read. Never guessed: joining from the wrong table
    would repeat the measured rows."""
    if base.primary_table:
        match = [t for t in froms if _same_table(t, None, base.primary_table, None)]
        if not match:
            raise DerivedError(
                f"second-order metric {metric_name!r}: the base metric's primary_table "
                f"{base.primary_table!r} is not one of its source tables {froms}"
            )
        return match[0]
    owners = _measured_tables(inner_sql, base, froms, org, storage_type)
    if len(owners) != 1:
        raise DerivedError(
            f"second-order metric {metric_name!r}: can't tell which of {froms} the base metric "
            f"{base.name!r} measures; set primary_table to say which table the metric measures"
        )
    return owners.pop()


def _measured_tables(inner_sql: str, base, froms: list[str], org, storage_type: str) -> set:
    """The source tables owning the columns inside the base binding's aggregates; empty when a
    column can't be pinned to exactly one table (or none is read, as in COUNT(*))."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
        from sqlglot.errors import ErrorLevel

        from .sql_dialect import sqlglot_dialect

        tree = sqlglot.parse_one(inner_sql, dialect=sqlglot_dialect(storage_type),
                                 error_level=ErrorLevel.RAISE)
    except Exception:
        return set()
    owners: set = set()
    for agg in tree.find_all(exp.AggFunc):
        for col in agg.find_all(exp.Column):
            if col.table:
                tables = [t for t in froms if _same_table(t, None, col.table, None)]
            else:
                tables = [t for t in froms if _declares(_table_def(t, org, base), col.name)]
            if len(tables) != 1:
                return set()
            owners.add(tables[0])
    return owners


def _from_clause(metric_name: str, base, froms: list[str], org, storage_type: str,
                 inner_sql: str) -> str:
    """FROM the anchor table, INNER JOINed directly to each other source table."""
    if org is None:
        raise DerivedError(
            f"second-order metric {metric_name!r}: its base spans {froms}, and joining them "
            "needs the model's relationships"
        )
    bare = [table_key(t, None)[1] for t in froms]
    clash = sorted({t for t, b in zip(froms, bare) if bare.count(b) > 1})
    if clash:
        # The join and the grain qualify columns by bare table name, which would be ambiguous.
        raise DerivedError(
            f"second-order metric {metric_name!r}: its base reads tables with the same name "
            f"from different schemas ({clash}); synthesis can't tell them apart"
        )
    anchor = _anchor(metric_name, base, froms, org, storage_type, inner_sql)
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

    A base over several tables is joined from its anchor (`primary_table`, else the one table
    whose columns its aggregates read) to each other table by the one relationship declared directly between them,
    which needs `org`. Identifiers are quoted only where the engine needs it.

    Raises DerivedError if the shape isn't `OUTERAGG({base})`, the base is unknown / not a
    plain aggregate, `inner_grain` is empty, it has no source table, or a multi-table join
    can't be built safely (an unclear anchor, no direct edge, several edges, a fan-out, an
    unresolvable grain)."""
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
    if _has_nested_aggregate(inner_sql, storage_type):
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
        table = _table_def(froms[0], org, base) if org is not None else None
        grain = ", ".join(_grain_sql(g, storage_type, table) for g in metric.inner_grain)
    else:
        from_sql = _from_clause(metric.name, base, froms, org, storage_type, inner_sql)
        grain = ", ".join(
            _qualified_grain(metric.name, g, froms, org, storage_type, base)
            for g in metric.inner_grain
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
