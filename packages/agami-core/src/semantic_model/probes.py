"""Grade the parts of a statement a PERSON supplied: the joins it wrote and the values it typed.

`agami-reconcile` takes a query the analyst trusts and treats it as evidence, never as the answer.
Two of that query's parts need the warehouse to settle, and this module does the deterministic half
of each:

* **Joins.** For every join the statement wrote: is there a declared relationship between those two
  tables, does the written key match the declared one, and what SQL would show whether the keys
  really resolve. The SQL is emitted, not run.
* **Typed values.** For every string literal in a filter: which column it binds to, whether the
  semantic model already lists that column's values, whether the literal is one of them, and what
  SQL would show whether the warehouse holds it. Again emitted, not run. `judge` then reads what the
  probes returned and gives each literal a grade.

**Nothing here touches a database.** The skill runs every probe through the same execution tier a
question takes, with the same guards, and hands the CSVs back. That is what keeps a value check from
becoming a second executor with weaker gates.

**Two refusals are deliberate.** A column marked `sensitive` is never probed for a value: a probe
that asks "does this email exist" is a way to confirm private values one guess at a time, so only
the count of distinct values, which names nothing, is emitted for it. And a near miss is a case and
whitespace fold only: `'Paid'` matching the listed `paid` is exact under that fold and cannot
false-positive, while an edit-distance suggestion is a guess, and this package does not guess about
values.

**An empty list of values means "not yet decoded".** `choice_field: {}` is the state introspection
leaves when it found a low-cardinality column and nobody has filled in the labels. Reading it as
"no legal values" would fail every literal on every such column; it reads `unresolved` until a probe
answers.

Parsing goes through `runtime._parse_reporting`, the one reader of a statement in this package, and
every join and predicate helper is runtime's own, so this module and the receipt describe one
statement one way.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Optional

from . import dialects as D
from . import introspect as I
from . import runtime as RT
from .models import Column, Datasource, Table

try:  # sqlglot is the model extra; the CLI already refuses to start without it
    from sqlglot import expressions as exp
    _HAVE_SQLGLOT = True
except Exception:  # pragma: no cover - exercised only where the extra is missing
    exp = None  # type: ignore[assignment]
    _HAVE_SQLGLOT = False

CONFIRMED = "confirmed"
MODEL_GAP = "model_gap"
QUERY_DEFECT = "query_defect"
UNRESOLVED = "unresolved"

# `_join_sites` takes the receipt's cap as an argument; a statement a person wrote is not bounded by
# a receipt's rendering budget, so the cap here is only a guard against a pathological input.
_MAX_JOINS = 200
# One past the enum ceiling, so an overflow shows as one extra row rather than as a full list that
# happens to be exactly the ceiling long.
_DISTINCT_PROBE_LIMIT = I.ENUM_MAX_DISTINCT + 1

_UNREADABLE = "the statement could not be read"


# --- shared -----------------------------------------------------------------


def _fold(value: str) -> str:
    """Case and whitespace fold, the ONLY normalization a near miss is allowed."""
    return re.sub(r"\s+", " ", value.strip()).lower()


def _dialects(org: Datasource) -> "tuple[str | None, D.Dialect | None]":
    """The sqlglot grammar to parse in, and the SQL dialect to write probes in. Either may be None:
    a model with no storage connection has neither, and the caller says so rather than guessing."""
    grammar = RT._dialect_of(org)[0]
    writer: Optional[D.Dialect] = None
    for sc in org.storage_connections:
        try:
            writer = D.get_dialect(sc.storage_type)
            break
        except ValueError:
            continue
    return grammar, writer


def _table_index(org: Datasource) -> dict[str, Table]:
    """Case-folded bare table name -> Table, the same fold `check_table_scope` uses."""
    return {key: pair[0] for key, pair in RT._model_table_index(org).items()}


def _declared_column(table: Table, name: str) -> Optional[Column]:
    """The column as the MODEL spells it, looked up case-insensitively. Probe SQL quotes the model's
    spelling rather than the statement's, because a case-sensitive engine would otherwise be asked
    about a column that does not exist."""
    wanted = name.lower()
    return next((c for c in table.columns if c.name.lower() == wanted), None)


def _limited(dialect: D.Dialect, select: str, n: int) -> str:
    """`select` bounded to `n` rows in the dialect's own row-limit syntax."""
    if dialect.limit_style == "top":
        return select.replace("SELECT ", f"SELECT TOP {n} ", 1)
    if dialect.limit_style == "fetch":
        return f"{select} FETCH FIRST {n} ROWS ONLY"
    return f"{select} LIMIT {n}"


def _pair_list(pair: "frozenset[tuple[str, str]]") -> list[list[str]]:
    return sorted([table, column] for table, column in pair)


# --- join probes --------------------------------------------------------------


def join_probes(org: Datasource, sql: str) -> dict[str, Any]:
    """Every join the statement wrote, beside the declaration it matched and the probes that would
    test it. Describes; never grades. The ledger reads this together with the probe results."""
    grammar, writer = _dialects(org)
    tree, why = RT._parse_reporting(sql, grammar)
    if tree is None:
        return {"joins": [], "unreadable": why or _UNREADABLE, "dialect": grammar}
    tidx = _table_index(org)
    visible = set(tidx) - RT._cte_names(tree)
    sites, _written = RT._join_sites(tree, visible, _MAX_JOINS)
    declared = [(rel, RT._declared_pairs(rel, grammar)) for rel in RT._cardinality_index(org)]

    joins: list[dict[str, Any]] = []
    for n, site in enumerate(sites, 1):
        left, right = (RT._tkey(RT._bare(site.endpoints[0])),
                       RT._tkey(RT._bare(site.endpoints[1])))
        between = {left, right}
        rels_between = [(rel, pairs) for rel, pairs in declared if RT._rel_tables(rel) == between]
        declared_pairs = sorted(
            {_pair_list(pair).__repr__(): _pair_list(pair)
             for _rel, pairs in rels_between if pairs is not None for pair in pairs}.values()
        )
        written = site.pairs
        matches = bool(written) and any(
            pairs <= written for _rel, pairs in rels_between if pairs is not None
        )
        left_table, right_table = tidx.get(left), tidx.get(right)
        too_big = any(I._too_big_to_probe(t) for t in (left_table, right_table) if t is not None)
        probes: dict[str, Any] = {"overlap": [], "cardinality": {}}
        if (writer is not None and not too_big and left_table is not None
                and right_table is not None and len(written) == 1):
            (pair,) = written
            (ta, ca), (tb, cb) = sorted(pair)
            for (t1, c1), (t2, c2) in (((ta, ca), (tb, cb)), ((tb, cb), (ta, ca))):
                T1, T2 = tidx.get(t1), tidx.get(t2)
                col1 = _declared_column(T1, c1) if T1 else None
                col2 = _declared_column(T2, c2) if T2 else None
                if T1 is None or T2 is None or col1 is None or col2 is None:
                    continue
                probes["overlap"].append({
                    "from": f"{t1}.{c1}", "into": f"{t2}.{c2}",
                    "sql": I.overlap_sql(writer, T1, col1.name, T2, col2.name),
                })
                probes["cardinality"][f"{t1}.{c1}"] = writer.count_distinct_sql(
                    T1.schema_name, T1.name, col1.name)
        joins.append({
            "id": f"join-{n}",
            "endpoints": [site.endpoints[0], site.endpoints[1]],
            "predicate": site.predicate,
            "scope": site.scope,
            "pair": _pair_list(next(iter(written))) if len(written) == 1 else None,
            "pairs": sorted(_pair_list(pair) for pair in written),
            "declared_between_tables": bool(rels_between),
            "written_matches_declared": matches,
            "declared_pairs": declared_pairs,
            "too_big_to_probe": too_big,
            "probes": probes,
        })
    return {"joins": joins, "unreadable": None, "dialect": grammar}


# --- filter values: plan --------------------------------------------------------


def _literal_sites(conj: "exp.Expression"):
    """(column, literal text, operator) for every string literal a filtering conjunct compares a
    column against. Only the shapes that name ONE value are read; `LIKE` is reported as a pattern
    so the judge can say why it was not checked rather than checking the wrong thing."""
    if isinstance(conj, (exp.EQ, exp.NEQ)):
        op = "=" if isinstance(conj, exp.EQ) else "<>"
        for col, lit in ((conj.this, conj.expression), (conj.expression, conj.this)):
            if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string:
                yield col, lit.this, op, False
        return
    negated = False
    if isinstance(conj, exp.Not) and isinstance(conj.this, exp.In):
        conj, negated = conj.this, True
    if isinstance(conj, exp.In) and isinstance(conj.this, exp.Column):
        for lit in conj.expressions:
            if isinstance(lit, exp.Literal) and lit.is_string:
                yield conj.this, lit.this, "not in" if negated else "in", False
        return
    if isinstance(conj, (exp.Like, exp.ILike)):
        col, lit = conj.this, conj.expression
        if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string:
            yield col, lit.this, "like", True


def _place_column(col: "exp.Column", scope: dict[str, str],
                  tidx: dict[str, Table]) -> "tuple[str | None, Table | None, Column | None]":
    """Which model table and column a written column names, or None where the statement does not
    say and this layer will not guess."""
    written = RT._resolve_col_table(col, scope)
    if written:
        table = tidx.get(RT._tkey(RT._bare(written)))
        return (table.name if table else None), table, (_declared_column(table, col.name) if table else None)
    # Unqualified, over several sources: attributable only when exactly one in-scope table has it.
    owners = []
    for bound in set(scope.values()):
        table = tidx.get(RT._tkey(RT._bare(bound)))
        if table is not None and _declared_column(table, col.name) is not None:
            owners.append(table)
    if len(owners) == 1:
        return owners[0].name, owners[0], _declared_column(owners[0], col.name)
    return None, None, None


def _choice_state(column: Optional[Column]) -> str:
    if column is None or column.choice_field is None:
        return "absent"
    return "populated" if column.choice_field else "empty"


def filter_values_plan(org: Datasource, sql: str) -> dict[str, Any]:
    """Every string literal the statement filters on, what the semantic model already knows about
    that column, and the probe SQL that would settle the rest."""
    grammar, writer = _dialects(org)
    tree, why = RT._parse_reporting(sql, grammar)
    if tree is None:
        return {"literals": [], "unreadable": why or _UNREADABLE, "dialect": grammar}
    tidx = _table_index(org)
    literals: list[dict[str, Any]] = []
    n = 0
    for sel in tree.find_all(exp.Select):
        scope = RT._own_alias_map(sel)
        for conj in RT._filtering_conjuncts(sel):
            for col, literal, op, pattern in _literal_sites(conj):
                n += 1
                literals.append(_plan_entry(f"lit-{n}", col, literal, op, pattern, scope, tidx, writer))
    return {"literals": literals, "unreadable": None, "dialect": grammar}


def _plan_entry(lit_id: str, col: "exp.Column", literal: str, op: str, pattern: bool,
                scope: dict[str, str], tidx: dict[str, Table],
                writer: Optional[D.Dialect]) -> dict[str, Any]:
    table_name, table, column = _place_column(col, scope, tidx)
    state = _choice_state(column)
    in_list: Optional[bool] = None
    near: Optional[str] = None
    if state == "populated" and column is not None:
        keys = list(column.choice_field or {})
        in_list = literal in keys
        if not in_list:
            near = next((k for k in keys if _fold(k) == _fold(literal)), None)
    sensitive = bool(column.sensitive) if column is not None else False
    too_big = I._too_big_to_probe(table) if table is not None else False
    entry: dict[str, Any] = {
        "id": lit_id,
        "table": table_name,
        "column": col.name,
        "literal": literal,
        "op": op,
        "pattern": pattern,
        "choice_field": state,
        "in_choice_field": in_list,
        "choice_near_miss": near,
        "sensitive": sensitive,
        "too_big_to_probe": too_big,
        "probes": {},
        "note": None,
    }
    if table is None or column is None:
        entry["note"] = "the column could not be placed on one table of the semantic model"
        return entry
    if pattern:
        entry["note"] = "a LIKE pattern is not one value and is not probed"
        return entry
    if writer is None:
        entry["note"] = "the datasource declares no storage engine, so no probe can be written"
        return entry
    q = writer.qualified(table.schema_name, table.name)
    c = writer.quote_ident(column.name)
    lit = writer.quote_lit(literal)
    probes: dict[str, Optional[str]] = {
        "cardinality": writer.count_distinct_sql(table.schema_name, table.name, column.name),
        "distinct": None, "exists": None, "exists_folded": None,
    }
    if not sensitive:
        if not too_big:
            probes["distinct"] = _limited(
                writer, f"SELECT DISTINCT {c} AS v FROM {q} WHERE {c} IS NOT NULL",
                _DISTINCT_PROBE_LIMIT)
        probes["exists"] = f"SELECT COUNT(*) AS n FROM {q} WHERE {c} = {lit}"
        probes["exists_folded"] = (
            f"SELECT {c} AS v, COUNT(*) AS n FROM {q} "
            f"WHERE LOWER(TRIM({c})) = LOWER(TRIM({lit})) GROUP BY {c}"
        )
    else:
        entry["note"] = "sensitive column: only the count of distinct values is probed"
    entry["probes"] = probes
    return entry


# --- filter values: judge -------------------------------------------------------


def _read_csv(path: Path) -> "list[dict[str, str]] | None":
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _first_int(rows: "list[dict[str, str]] | None", key: str) -> Optional[int]:
    if not rows:
        return None
    row = rows[0]
    raw = row.get(key)
    if raw is None and row:
        raw = next(iter(row.values()))
    try:
        return int(float(raw)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _values(rows: "list[dict[str, str]] | None", key: str) -> "list[str] | None":
    if rows is None:
        return None
    out: list[str] = []
    for row in rows:
        v = row.get(key)
        if v is None and row:
            v = next(iter(row.values()))
        if v is not None:
            out.append(v)
    return out


def filter_values_judge(plan: dict[str, Any], results_dir: Path) -> dict[str, Any]:
    """Fold the probe results back onto the plan: one verdict per literal, with the tier that decided
    it, the values observed where a whole list was, the near miss where there is one, and how many
    rows the literal selects as written."""
    verdicts = [_judge_one(lit, results_dir) for lit in plan.get("literals", [])]
    return {"literals": verdicts, "dialect": plan.get("dialect")}


def _judge_one(lit: dict[str, Any], results_dir: Path) -> dict[str, Any]:
    lit_id = lit["id"]
    literal = lit["literal"]
    exists_n = _first_int(_read_csv(results_dir / f"{lit_id}.exists.csv"), "n")
    folded = _read_csv(results_dir / f"{lit_id}.exists_folded.csv")
    distinct = _values(_read_csv(results_dir / f"{lit_id}.distinct.csv"), "v")
    out: dict[str, Any] = {
        "id": lit_id, "table": lit.get("table"), "column": lit.get("column"), "literal": literal,
        "tier": "none", "verdict": UNRESOLVED, "observed": None, "near_miss": None,
        "rows_contributed": exists_n, "note": "",
    }

    def near_from(values: "list[str] | None") -> Optional[str]:
        if not values:
            return None
        return next((v for v in values if _fold(v) == _fold(literal)), None)

    if lit.get("pattern"):
        out["note"] = "a LIKE pattern is not one value and was not checked"
        return out
    if lit.get("table") is None:
        out["note"] = "the column could not be placed on one table, so nothing was checked"
        return out

    if lit.get("choice_field") == "populated":
        out["tier"] = "choice_field"
        if lit.get("in_choice_field"):
            out["verdict"] = CONFIRMED
            out["note"] = "the value is in the semantic model's list for this column"
        elif exists_n is not None and exists_n > 0:
            out["verdict"] = MODEL_GAP
            out["note"] = (f"the semantic model's list does not include it, but the warehouse holds "
                           f"{exists_n} row(s) with it; the list is stale")
        else:
            out["verdict"] = QUERY_DEFECT
            out["near_miss"] = lit.get("choice_near_miss")
            out["note"] = ("not one of the values the semantic model lists for this column"
                           + (f"; did you mean {out['near_miss']!r}" if out["near_miss"] else ""))
        return out

    if lit.get("sensitive"):
        out["note"] = "sensitive column: its values are never probed and no list exists to answer from"
        return out

    if distinct is not None and len(distinct) <= I.ENUM_MAX_DISTINCT:
        out["tier"] = "distinct"
        out["observed"] = sorted(distinct)
        if literal in distinct:
            out["verdict"] = CONFIRMED
            out["note"] = "the value is one the column holds"
        else:
            out["verdict"] = QUERY_DEFECT
            out["near_miss"] = near_from(distinct)
            out["note"] = ("not a value the column holds"
                           + (f"; did you mean {out['near_miss']!r}" if out["near_miss"] else ""))
        return out

    if exists_n is not None:
        out["tier"] = "exists"
        if exists_n > 0:
            out["verdict"] = CONFIRMED
            out["note"] = f"the warehouse holds {exists_n} row(s) with this value"
        else:
            out["verdict"] = QUERY_DEFECT
            out["near_miss"] = near_from(_values(folded, "v"))
            out["note"] = ("no row holds this value"
                           + (f"; did you mean {out['near_miss']!r}" if out["near_miss"] else ""))
        return out

    out["note"] = ("no probe result was supplied, so the value could not be checked"
                   + ("; the semantic model's list for this column is empty" if lit.get("choice_field") == "empty" else ""))
    return out
