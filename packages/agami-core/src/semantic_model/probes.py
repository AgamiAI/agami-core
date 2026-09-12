"""Grade the parts of a statement a PERSON supplied: the joins it wrote and the values it typed.

`agami-reconcile` takes a query the analyst trusts and treats it as evidence, never as the answer.
Two of that query's parts need the warehouse to settle, and this module does the deterministic half
of each:

* **Joins.** For every join the statement wrote: is there a declared relationship between those two
  tables, does the written key match the declared one, and what SQL would show whether the keys
  really resolve. The SQL is emitted, not run.
* **Typed values.** For every literal in a filter: which column it binds to, whether the semantic
  model already lists that column's values, whether the literal is one of them, and what SQL would
  show whether the warehouse holds it. Again emitted, not run. `judge` then reads what the probes
  returned and gives each literal a grade.

**Nothing here touches a database.** The skill runs every probe through the same execution tier a
question takes, with the same guards, and hands the CSVs back. That is what keeps a value check from
becoming a second executor with weaker gates.

**Joins are classified the way the receipt classifies them.** `runtime._join_sites` hands back, for
each join, whether each endpoint is a table the semantic model could declare anything about and
whether the ON reduced to column pairs. Both are honoured here in the receipt's own order, so a CTE
that shadows a declared table, a derived table aliased as one, a comma join, a `USING`, or a
declared `on:` this layer cannot read, all come back as open states and never as a settled claim
about a key nobody read.

**Three refusals are deliberate.** A column marked `sensitive` is never probed for a value: a probe
that asks "does this email exist" is a way to confirm private values one guess at a time, and
listing its distinct values is worse. A near miss is a case and whitespace fold only: `'Paid'`
matching the listed `paid` is exact under that fold and cannot false-positive, while an
edit-distance suggestion is a guess, and this package does not guess about values. A literal
carrying a backslash or a control character is never sent to the warehouse, because engines
disagree on how to quote it; it is still checked against the semantic model's own list.

**An empty list of values means "not yet decoded".** `choice_field: {}` is the state introspection
leaves when it found a low-cardinality column and nobody has filled in the labels. Reading it as
"no legal values" would fail every literal on every such column; it reads `unresolved` until a probe
answers. And a probe that came back empty is a probe that failed: the execution tier writes CSV
only on success, so a zero-byte file is graded `unresolved` and says so, never "the column holds
nothing".

Parsing goes through `runtime._parse_reporting`, the one reader of a statement in this package, and
every join and predicate helper is runtime's own, so this module and the receipt describe one
statement one way.
"""

from __future__ import annotations

import csv
import re
from itertools import permutations
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlglot import expressions as exp

from . import dialects as D
from . import introspect as I
from . import runtime as RT
from .models import Column, Datasource, Table

CONFIRMED = "confirmed"
MODEL_GAP = "model_gap"
QUERY_DEFECT = "query_defect"
UNRESOLVED = "unresolved"

# Join statuses. Four are the receipt's own; `wrong_key` is the one state the receipt folds into
# `undeclared` and this verb keeps apart, because a person joining two declared tables on a key the
# semantic model does not declare is the case reconcile exists to name.
DECLARED = "declared"
WRONG_KEY = "wrong_key"
UNDECLARED = "undeclared"
UNDECLARABLE = "undeclarable"
UNDETERMINED = "undetermined"

# `_join_sites` takes the receipt's cap as an argument; a statement a person wrote is not bounded by
# a receipt's rendering budget, so the cap here only guards against a pathological input, and the
# payload says how many joins were dropped by it.
_MAX_JOINS = 200
# Literals are capped the same way: an `IN` list of a thousand values would plan a thousand scans.
_MAX_LITERALS = 200
# One past the enum ceiling, so an overflow shows as one extra row rather than as a full list that
# happens to be exactly the ceiling long. Recorded on the plan, so the judge reads the number the
# plan used rather than whatever this constant is when it runs.
_DISTINCT_PROBE_LIMIT = I.ENUM_MAX_DISTINCT + 1
# The sample size the overlap probe has always used.

_UNREADABLE = "the statement could not be read"
_UNQUOTABLE = re.compile(r"[\\\x00-\x1f\x7f]")


# --- shared -----------------------------------------------------------------


def _fold(value: str) -> str:
    """Case and whitespace fold, the ONLY normalization a near miss is allowed."""
    return re.sub(r"\s+", " ", value.strip()).lower()


def _near_miss(candidates, literal: str) -> Optional[str]:
    """The one candidate equal to `literal` under the fold, or None. The single place the rule lives."""
    want = _fold(literal)
    return next((c for c in candidates if _fold(str(c)) == want), None)


def _dialects(org: Datasource) -> "tuple[str | None, D.Dialect | None]":
    """The sqlglot grammar to parse in, and the SQL dialect to write probes in.

    Both come from the one engine `resolve_datasource_dialect` settles on, so a datasource whose
    connections disagree gets neither rather than a grammar of None beside probes written for
    whichever connection happened to be listed first.
    """
    grammar = RT._dialect_of(org)[0]
    if grammar is None or not org.storage_connections:
        return grammar, None
    return grammar, D.get_dialect(org.storage_connections[0].storage_type)


def _table_index(org: Datasource) -> dict[str, Table]:
    """Case-folded bare table name -> Table, the same fold `check_table_scope` uses."""
    return {key: pair[0] for key, pair in RT._model_table_index(org).items()}


def _declared_column(table: Optional[Table], name: str) -> Optional[Column]:
    """The column as the semantic model spells it, looked up case-insensitively. Probe SQL quotes
    the semantic model's spelling rather than the statement's, because a case-sensitive engine would
    otherwise be asked about a column that does not exist."""
    if table is None:
        return None
    wanted = name.lower()
    return next((c for c in table.columns if c.name.lower() == wanted), None)


def _pair_list(pair: "frozenset[tuple[str, str]]") -> list[list[str]]:
    return sorted([table, column] for table, column in pair)


def _cell(row: dict, key: str) -> Optional[str]:
    """One CSV cell by its header, falling back to the first column when the header differs. The
    probes alias every column they emit, so the fallback only matters for a tier that renames."""
    value = row.get(key)
    if value is None and row:
        value = next(iter(row.values()))
    return value


def _probe_csv(path: Path) -> "list[dict] | str | None":
    """A probe's CSV as rows; None when the file is absent; the string `failed` when it is empty.

    The execution tier writes CSV to stdout only on success, so a zero-byte file is a probe that was
    refused or failed. A header-only file is the legitimately empty result.
    """
    if not path.exists():
        return None
    if path.stat().st_size == 0:
        return "failed"
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- join probes --------------------------------------------------------------


def _join_status(site, rels: list) -> "tuple[str, str | None]":
    """The receipt's ladder over one written join, plus `wrong_key`. Returns the status and, for the
    open states, the reason the question stays open."""
    if not site.right_declarable:
        return UNDECLARABLE, ("the right side is a relation the statement computed (a CTE, a derived "
                             "table or a VALUES list), which no declaration can be about")
    if not site.pinned:
        return UNDETERMINED, "the join condition did not reduce to a pair of columns"
    if not site.left_declarable:
        return UNDECLARABLE, ("the left side is a relation the statement computed, which no "
                             "declaration can be about")
    if not site.pairs:
        if site.predicate is None and site.node.args.get("kind") == "CROSS":
            return UNDECLARED, None
        return UNDETERMINED, ("the join wrote no condition this layer can read as column pairs "
                             "(USING, NATURAL, a comma join or an expression)")
    readable = [pairs for _rel, pairs in rels if pairs is not None]
    if any(pairs <= site.pairs for pairs in readable):
        return DECLARED, None
    if any(pairs is None for _rel, pairs in rels):
        return UNDETERMINED, ("a declared relationship between these tables could not be read, so "
                             "whether the written key is the declared one is not decided")
    if rels:
        return WRONG_KEY, None
    return UNDECLARED, None


def join_probes(org: Datasource, sql: str) -> dict[str, Any]:
    """Every join the statement wrote, beside the declaration it matched and the probes that would
    test it. Describes; never grades. The ledger reads this together with the probe results."""
    grammar, writer = _dialects(org)
    tree, why = RT._parse_reporting(sql, grammar)
    if tree is None:
        return {"joins": [], "joins_written": 0, "dropped": 0, "cardinality": {},
                "unique_by_model": {}, "unreadable": why or _UNREADABLE, "dialect": grammar}
    tidx = _table_index(org)
    visible = set(tidx) - RT._cte_names(tree)
    sites, written_total = RT._join_sites(tree, visible, _MAX_JOINS)
    declared = [(rel, RT._declared_pairs(rel, grammar)) for rel in RT._cardinality_index(org)]

    # Cardinality probes are per COLUMN, not per join: a hub key joined from four tables is one
    # scan, not four. None where the semantic model already says the column is unique.
    cardinality: dict[str, Optional[str]] = {}
    unique_by_model: dict[str, bool] = {}
    joins: list[dict[str, Any]] = []
    for n, site in enumerate(sites, 1):
        left, right = site.endpoints
        between = {RT._tkey(RT._bare(left)), RT._tkey(RT._bare(right))}
        rels = [(rel, pairs) for rel, pairs in declared if RT._rel_tables(rel) == between]
        status, why_open = _join_status(site, rels)
        declared_pairs = sorted(
            _pair_list(pair)
            for pair in {p for _rel, pairs in rels if pairs is not None for p in pairs}
        )
        entry: dict[str, Any] = {
            "id": f"join-{n}",
            "endpoints": [left, right],
            "predicate": site.predicate,
            "scope": site.scope,
            "status": status,
            "pairs": sorted(_pair_list(pair) for pair in site.pairs),
            "declared_between_tables": bool(rels) and status not in (UNDECLARABLE, UNDETERMINED),
            "written_matches_declared": status == DECLARED,
            "declared_pairs": declared_pairs,
            "too_big_to_probe": False,
            "probes": {"overlap": [], "cardinality": []},
            "not_probed_because": why_open,
        }
        if status == UNDECLARED:
            _emit_join_probes(entry, site, tidx, writer, cardinality, unique_by_model)
        elif status != UNDECLARED and why_open is None:
            entry["not_probed_because"] = (
                "the join is on the declared key; nothing to probe" if status == DECLARED
                else "the join is on a different key than the declared one; the declaration decides")
        joins.append(entry)
    return {"joins": joins, "joins_written": written_total, "dropped": written_total - len(sites),
            "cardinality": cardinality, "unique_by_model": unique_by_model,
            "unreadable": None, "dialect": grammar}


def _emit_join_probes(entry: dict[str, Any], site, tidx: dict[str, Table],
                      writer: Optional[D.Dialect], cardinality: dict[str, Optional[str]],
                      unique_by_model: dict[str, bool]) -> None:
    """Fill `entry["probes"]` for an undeclared join, or say why nothing could be emitted."""
    if not site.pairs:
        entry["not_probed_because"] = "the join wrote no column pair to probe (a cross join)"
        return
    if len(site.pairs) != 1:
        entry["not_probed_because"] = "the join is on more than one pair of columns"
        return
    (pair,) = site.pairs
    if len(pair) != 2:
        entry["not_probed_because"] = "the join compares a column with itself"
        return
    ends: list[tuple[str, Table, Column]] = []
    for table_name, column_name in sorted(pair):
        table = tidx.get(table_name)
        column = _declared_column(table, column_name)
        if table is None or column is None:
            entry["not_probed_because"] = "a joined column is not in the semantic model"
            return
        ends.append((table_name, table, column))
    if writer is None:
        entry["not_probed_because"] = "the datasource declares no single storage engine, so no probe can be written"
        return
    if any(I._too_big_to_probe(table) for _name, table, _col in ends):
        entry["too_big_to_probe"] = True
        entry["not_probed_because"] = "a table is over the size guard for probes"
        return
    for (t1, table1, col1), (t2, table2, col2) in permutations(ends, 2):
        if col1.primary_key:
            # Sampling a declared key and asking whether the other side holds it says nothing about
            # the join: a parent with no children is not a broken key. Only the other direction tests
            # whether the written keys resolve.
            continue
        entry["probes"]["overlap"].append({
            "from": f"{t1}.{col1.name}", "into": f"{t2}.{col2.name}",
            "sql": I.overlap_sql(writer, table1, col1.name, table2, col2.name),
        })
    if not entry["probes"]["overlap"]:
        entry["not_probed_because"] = ("both join columns are declared keys, and no overlap probe is "
                                       "written from a key")
    for name, table, column in ends:
        key = f"{name}.{column.name}"
        entry["probes"]["cardinality"].append(key)
        if key not in cardinality:
            unique_by_model[key] = bool(column.primary_key)
            cardinality[key] = (None if column.primary_key
                                else writer.count_distinct_sql(table.schema_name, table.name, column.name))


# --- filter values: plan --------------------------------------------------------


def _literal_sites(conj: "exp.Expression") -> Iterator[tuple]:
    """What a filtering conjunct compares a column against.

    Yields `("literal", column, text, quoted, op, pattern)` for the shapes that name one value
    (`=`, `<>`, `IN`, `NOT IN`; `LIKE` as a pattern), and `("skipped", column, op, reason)` for the
    shapes that name a range or no value at all, so the plan can say what it did not read rather
    than reporting an empty list as "nothing to check".
    """
    if isinstance(conj, (exp.EQ, exp.NEQ)):
        op = "=" if isinstance(conj, exp.EQ) else "<>"
        for col, lit in ((conj.this, conj.expression), (conj.expression, conj.this)):
            if isinstance(col, exp.Column) and isinstance(lit, exp.Neg) and isinstance(lit.this, exp.Literal):
                # `-3` parses as a negation over the literal `3`; the value the person typed is `-3`.
                yield "literal", col, f"-{lit.this.this}", False, op, False
            elif isinstance(col, exp.Column) and isinstance(lit, exp.Literal):
                yield "literal", col, str(lit.this), bool(lit.is_string), op, False
        return
    negated = False
    if isinstance(conj, exp.Not) and isinstance(conj.this, exp.In):
        conj, negated = conj.this, True
    if isinstance(conj, exp.In) and isinstance(conj.this, exp.Column):
        for lit in conj.expressions:
            if isinstance(lit, exp.Literal):
                yield "literal", conj.this, str(lit.this), bool(lit.is_string), ("not in" if negated else "in"), False
        return
    if isinstance(conj, (exp.Like, exp.ILike)):
        col, lit = conj.this, conj.expression
        if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string:
            yield "literal", col, str(lit.this), True, "like", True
        return
    if isinstance(conj, exp.Between) and isinstance(conj.this, exp.Column):
        yield "skipped", conj.this, "between", "a range is not one value"
        return
    if isinstance(conj, exp.Is) and isinstance(conj.this, exp.Column):
        yield "skipped", conj.this, "is", "IS NULL names no value"


def _place_column(col: "exp.Column", scope: dict[str, str], tidx: dict[str, Table],
                  hidden: set[str]) -> "tuple[Table | None, Column | None, str | None]":
    """Which table and column of the semantic model a written column names, or None with the reason.

    `hidden` holds every name the statement bound to a result of its own: CTE names and computed
    relations. A qualifier naming one resolves to nothing here, because a literal graded against the
    real table's list would be graded against a table the statement never read.
    """
    written = RT._resolve_col_table(col, scope)
    if written:
        key = RT._tkey(RT._bare(written))
        if key in hidden:
            return None, None, "the column belongs to a relation the statement computed, not to a table"
        table = tidx.get(key)
        column = _declared_column(table, col.name)
        if table is None:
            return None, None, "the column's table is not in the semantic model"
        if column is None:
            return table, None, "the column is not declared on its table in the semantic model"
        return table, column, None
    owners = []
    for bound in set(scope.values()):
        key = RT._tkey(RT._bare(bound))
        table = tidx.get(key)
        if key not in hidden and table is not None and _declared_column(table, col.name) is not None:
            owners.append(table)
    if len(owners) == 1:
        return owners[0], _declared_column(owners[0], col.name), None
    if not owners:
        return None, None, "no in-scope table of the semantic model declares this column"
    return None, None, "the column is not qualified and more than one in-scope table could own it"


def _choice_state(column: Optional[Column]) -> str:
    if column is None or column.choice_field is None:
        return "absent"
    return "populated" if column.choice_field else "empty"


def filter_values_plan(org: Datasource, sql: str) -> dict[str, Any]:
    """Every literal the statement filters on, what the semantic model already knows about that
    column, and the probe SQL that would settle the rest. Probes that belong to the column (its
    distinct values) are emitted once per column under `columns`; probes that belong to the value
    are on the literal."""
    grammar, writer = _dialects(org)
    tree, why = RT._parse_reporting(sql, grammar)
    if tree is None:
        return {"literals": [], "columns": {}, "skipped": [], "unreadable": why or _UNREADABLE,
                "dialect": grammar}
    tidx = _table_index(org)
    cte_names = RT._cte_names(tree)
    columns: dict[str, dict[str, Any]] = {}
    literals: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    n = 0
    for sel in tree.find_all(exp.Select):
        scope = RT._own_alias_map(sel)
        hidden = set(cte_names) | set(RT._computed_relations(sel))
        for conj in RT._filtering_conjuncts(sel):
            hits = list(_literal_sites(conj))
            if not hits:
                # A shape this walker does not read (two conditions joined by OR, a function over
                # the column, a cast, a comparison against another column). Said, not dropped: an
                # empty `literals` list must never read as "no value was typed".
                first = conj.find(exp.Column)
                skipped.append({"column": first.name if first is not None else None,
                                "op": type(conj).__name__.lower(),
                                "reason": "a filter shape the plan does not read"})
                continue
            for hit in hits:
                if hit[0] == "skipped":
                    _kind, col, op, reason = hit
                    skipped.append({"column": col.name, "op": op, "reason": reason})
                    continue
                _kind, col, text, quoted, op, pattern = hit
                n += 1
                if n > _MAX_LITERALS:
                    continue
                literals.append(_plan_entry(f"lit-{n}", col, text, quoted, op, pattern, scope, tidx,
                                            hidden, writer, columns))
    return {"literals": literals, "columns": columns, "skipped": skipped,
            "dropped": max(0, n - _MAX_LITERALS), "unreadable": None, "dialect": grammar}


def _plan_entry(lit_id: str, col: "exp.Column", literal: str, quoted: bool, op: str, pattern: bool,
                scope: dict[str, str], tidx: dict[str, Table], hidden: set[str],
                writer: Optional[D.Dialect], columns: dict[str, dict[str, Any]]) -> dict[str, Any]:
    table, column, why = _place_column(col, scope, tidx, hidden)
    state = _choice_state(column)
    in_list: Optional[bool] = None
    near: Optional[str] = None
    if state == "populated" and column is not None:
        keys = list(column.choice_field or {})
        in_list = literal in keys
        if not in_list:
            near = _near_miss(keys, literal)
    sensitive = bool(column.sensitive) if column is not None else False
    too_big = I._too_big_to_probe(table) if table is not None else False
    unquotable = quoted and _UNQUOTABLE.search(literal) is not None
    entry: dict[str, Any] = {
        "id": lit_id,
        "table": table.name if table is not None else None,
        "column": col.name,
        "column_key": f"{table.name}.{column.name}" if table is not None and column is not None else None,
        "literal": literal,
        "quoted": quoted,
        "op": op,
        "pattern": pattern,
        "choice_field": state,
        "in_choice_field": in_list,
        "choice_near_miss": near,
        "sensitive": sensitive,
        "too_big_to_probe": too_big,
        "probes": {"exists": None, "exists_folded": None},
        "exists_folded_when": "exists returns 0",
        "note": why,
    }
    if table is None or column is None:
        return entry
    key = entry["column_key"]
    if pattern:
        entry["note"] = "a LIKE pattern is not one value and is not probed"
        return entry
    if writer is None:
        entry["note"] = "the datasource declares no single storage engine, so no probe can be written"
        return entry
    if key not in columns:
        columns[key] = _column_probe(key, table, column, state, sensitive, too_big, writer)
    if sensitive:
        entry["note"] = "sensitive column: its values are never probed"
        return entry
    if unquotable:
        entry["note"] = ("the value carries a backslash or a control character, which engines quote "
                         "differently; it is checked against the semantic model's list only")
        return entry
    q = writer.qualified(table.schema_name, table.name)
    c = writer.quote_ident(column.name)
    lit = writer.quote_lit(literal) if quoted else literal
    entry["probes"]["exists"] = f"SELECT COUNT(*) AS n FROM {q} WHERE {c} = {lit}"
    if too_big:
        entry["note"] = "the table is over the size guard, so only the existence probe is emitted"
    elif quoted:
        entry["probes"]["exists_folded"] = (
            f"SELECT {c} AS v, COUNT(*) AS n FROM {q} "
            f"WHERE LOWER(TRIM({c})) = LOWER(TRIM({lit})) GROUP BY {c}"
        )
    return entry


def _column_probe(key: str, table: Table, column: Column, state: str, sensitive: bool,
                  too_big: bool, writer: D.Dialect) -> dict[str, Any]:
    probe: dict[str, Any] = {
        "table": table.name, "column": column.name, "choice_field": state,
        "sensitive": sensitive, "too_big_to_probe": too_big,
        "distinct": None, "distinct_limit": _DISTINCT_PROBE_LIMIT, "note": None,
    }
    if sensitive:
        probe["note"] = "sensitive column: its values are never listed"
    elif too_big:
        probe["note"] = "the table is over the size guard, so its values are not listed"
    else:
        q = writer.qualified(table.schema_name, table.name)
        c = writer.quote_ident(column.name)
        probe["distinct"] = writer.limited(
            f"SELECT DISTINCT {c} AS v FROM {q} WHERE {c} IS NOT NULL", _DISTINCT_PROBE_LIMIT)
    return probe


# --- filter values: judge -------------------------------------------------------


def _count(rows) -> "int | str | None":
    """An `n` cell as an int; `failed` passes through; None when absent or unreadable."""
    if rows == "failed" or rows is None:
        return rows
    raw = _cell(rows[0], "n") if rows else None
    try:
        return int(float(raw)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _values(rows) -> "list[str] | str | None":
    if rows == "failed" or rows is None:
        return rows
    return [v for v in (_cell(row, "v") for row in rows) if v is not None]


def filter_values_judge(plan: dict[str, Any], results_dir: Path) -> dict[str, Any]:
    """Fold the probe results back onto the plan: one verdict per literal, with the tier that decided
    it, the values observed where a whole list was read, the near miss where there is one, and how
    many rows hold the value as written."""
    if "literals" not in plan:
        raise ValueError("the plan carries no literals; it is not the output of filter-values plan")
    if plan.get("unreadable"):
        return {"literals": [], "unreadable": plan["unreadable"], "dialect": plan.get("dialect")}
    columns = plan.get("columns", {})
    verdicts = [_judge_one(lit, columns.get(lit.get("column_key") or "", {}), results_dir)
                for lit in plan["literals"]]
    return {"literals": verdicts, "unreadable": None, "dialect": plan.get("dialect")}


def _judge_one(lit: dict[str, Any], column: dict[str, Any], results_dir: Path) -> dict[str, Any]:
    lit_id, literal = lit["id"], lit["literal"]
    exists = _count(_probe_csv(results_dir / f"{lit_id}.exists.csv"))
    folded = _values(_probe_csv(results_dir / f"{lit_id}.exists_folded.csv"))
    distinct = (_values(_probe_csv(results_dir / f"{lit['column_key']}.distinct.csv"))
                if lit.get("column_key") else None)
    limit = column.get("distinct_limit", _DISTINCT_PROBE_LIMIT)
    exists_n = exists if isinstance(exists, int) else None
    out: dict[str, Any] = {
        "id": lit_id, "table": lit.get("table"), "column": lit.get("column"), "literal": literal,
        "op": lit.get("op"), "tier": "none", "verdict": UNRESOLVED, "observed": None,
        "near_miss": None, "rows_with_value": exists_n, "note": "",
    }
    probe_failed = ("; the probe file is empty, so the probe likely failed"
                    if "failed" in (exists, distinct, folded) else "")
    # A header-only distinct file is a column that returned no values at all: an empty table, an
    # all-null column, or a truncated write. None of those is evidence about the value typed.
    empty_distinct = isinstance(distinct, list) and not distinct

    if lit.get("pattern"):
        out["note"] = "a LIKE pattern is not one value and was not checked"
        return out
    if lit.get("table") is None or lit.get("column_key") is None:
        out["note"] = lit.get("note") or "the column could not be placed on one table, so nothing was checked"
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
        elif exists_n == 0:
            out["verdict"] = QUERY_DEFECT
            out["near_miss"] = lit.get("choice_near_miss")
            out["note"] = "not one of the values the semantic model lists, and no row holds it"
        else:
            out["near_miss"] = lit.get("choice_near_miss")
            out["note"] = ("not one of the values the semantic model lists; "
                           + ("no probe can be run on a sensitive column to tell a stale list from a mistake"
                              if lit.get("sensitive") else
                              "run the existence probe to tell a stale list from a mistake")
                           + probe_failed)
        return _with_suggestion(out)

    if lit.get("sensitive"):
        out["note"] = "sensitive column: its values are never probed and no list exists to answer from"
        return out
    if (lit.get("probes") or {}).get("exists") is None:
        out["note"] = lit.get("note") or "no probe was emitted for this value"
        return out

    if isinstance(distinct, list) and 0 < len(distinct) < limit:
        out["tier"] = "distinct"
        out["observed"] = sorted(distinct)
        if literal in distinct:
            out["verdict"] = CONFIRMED
            out["note"] = "the value is one the column holds"
        else:
            out["verdict"] = QUERY_DEFECT
            out["near_miss"] = _near_miss(distinct, literal)
            out["note"] = "not a value the column holds"
        return _with_suggestion(out)

    if exists_n is not None:
        out["tier"] = "exists"
        if exists_n > 0:
            out["verdict"] = CONFIRMED
            out["note"] = f"the warehouse holds {exists_n} row(s) with this value"
        elif empty_distinct:
            out["note"] = ("the column returned no values at all, so no value can be checked against it; "
                           "the table may be empty or the column all null")
        else:
            out["verdict"] = QUERY_DEFECT
            out["near_miss"] = _near_miss(folded, literal) if isinstance(folded, list) else None
            out["note"] = "no row holds this value" + probe_failed
        return _with_suggestion(out)

    out["note"] = ("no probe result was supplied, so the value could not be checked" + probe_failed
                   + ("; the distinct probe returned no values at all" if empty_distinct else "")
                   + ("; the semantic model's list for this column is empty"
                      if lit.get("choice_field") == "empty" else ""))
    return out


def _with_suggestion(out: dict[str, Any]) -> dict[str, Any]:
    if out.get("near_miss"):
        out["note"] += f"; did you mean {out['near_miss']!r}"
    return out
