"""Check a receipt's `matched` claims against the numbers (ACE-177 criterion 10).

A `matched` item says: this output column is the declared metric, counted over the rows its
`restrictions` describe, then kept only where its `result_filters` hold. That is a claim about
numbers, so it is checked by computing them twice on the same data:

  * the statement's own arm, projecting its group keys and the column;
  * the same arm with the column replaced by the metric's binding, as the model author wrote it,
    and its WHERE replaced by the listed restrictions plus the table's applied declared filters
    (which the receipt does not list, because they are part of what the table means).

Joins and GROUP BY are kept, but each inner join's ON and the WHERE keep only their JOINING
predicates — a bare `column = column` across two references, decided HERE (unqualified sides
resolved through the model's own table definitions) and never by the code under check. So a value
condition the receipt failed to list, in a WHERE or hidden in an ON, makes the two disagree. HAVING, QUALIFY, ORDER BY, LIMIT and DISTINCT are dropped from
both: they choose which groups are shown, and the claim is about each group's value.

The two must agree group by group. One side may hold a group the other lacks only where a FILTER or
CASE condition and a WHERE carry the same restriction: a group none of whose rows qualify is absent
under the WHERE and present, as NULL or 0, under the FILTER. That is the one difference the claim
allows (see `runtime._peeled`), so a group on one side only must hold the value of an aggregate over
no rows. Anything else is a mismatch.

A claim this module cannot re-run — a withheld value, a list cut short by the receipt's caps, a
grouping it cannot rebuild, a statement the engine rejects — is counted as SKIPPED and never as
agreement, so a caller can assert how much was actually compared.

Reusable: ACE-178 calls `check` with its own statements over the same fixture (`load_fixture`).
"""

from __future__ import annotations

import decimal
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from semantic_model import runtime as rt
from sqlglot import exp

# --- the data ------------------------------------------------------------------------------------
#
# Synthetic. Chosen for the shapes where two ways of computing a number part company: NULL values
# and NULL keys, a group with no qualifying row, duplicate keys on both sides of a join (an order id
# and a customer id twice), a join key with no partner, and `orders.customer_id` doubling as a
# reference to `orders.id`, so a self-join returns rows.

TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "change_requests": (("number", "TEXT"), ("type", "TEXT"), ("state", "TEXT"),
                        ("opened", "TEXT")),
    "orders": (("id", "INTEGER"), ("customer_id", "INTEGER"), ("status", "TEXT"),
               ("total_amount", "INTEGER"), ("is_deleted", "BOOLEAN"), ("opened", "TEXT"),
               ("closed", "TEXT")),
    "customers": (("id", "INTEGER"), ("region", "TEXT"), ("status", "TEXT"),
                  ("is_deleted", "BOOLEAN"), ("email", "TEXT")),
    "payments": (("id", "INTEGER"), ("order_id", "INTEGER"), ("status", "TEXT"),
                 ("amount", "INTEGER")),
}

ROWS: dict[str, list[tuple]] = {
    "change_requests": [
        ("C1", "normal", "Work in Progress", "2026-01-01"),
        ("C2", "normal", "Closed", "2026-01-02"),
        ("C2", "emergency", "Work in Progress", "2026-01-03"),   # a duplicate number
        ("C3", None, None, None),
        ("C4", "standard", "Closed", "2026-01-04"),              # a type with no WIP row
    ],
    "orders": [
        (1, 1, "paid", 10, False, "2026-01-05", "2026-01-05"),
        (2, 1, "open", None, False, "2026-02-01", None),
        (3, 2, "paid", 7, True, "2025-12-30", "2026-01-02"),
        (4, 2, None, 5, False, None, None),
        (5, 3, "paid", None, False, "2026-03-01", "2026-03-01"),
        (6, 3, "cancelled", 4, False, "2026-03-02", None),
        (7, 5, "open", 3, False, "2026-01-01", "2026-01-01"),     # a customer that does not exist
        (8, 1, "shipped", 6, False, "2026-01-07", None),
        (8, 2, "paid", 2, False, "2026-01-08", "2026-01-09"),     # a duplicate order id
        (9, None, "paid", 1, False, "2026-01-09", None),          # a NULL key
    ],
    "customers": [
        (1, "EU", "active", False, "a@example.com"),
        (2, "US", "paid", False, "b@example.com"),
        (3, None, "active", True, None),
        (3, "EU", "active", False, "c@example.com"),              # a duplicate customer id
        (4, "EU", "active", False, "d@example.com"),              # a customer with no orders
    ],
    "payments": [
        (1, 1, "paid", 10),
        (2, 1, "paid", None),
        (3, 3, "refunded", 7),
        (4, 8, "paid", 6),
        (5, 8, "paid", 2),
        (6, 99, "paid", 1),                                       # an order that does not exist
    ],
}


def load_fixture(conn: Any, engine: str) -> None:
    """Create and fill the fixture tables. TEMP on Postgres, so a shared database is never touched."""
    temp = "TEMP " if engine == "postgres" else ""
    mark = "%s" if engine == "postgres" else "?"
    cur = conn.cursor()
    for name, cols in TABLES.items():
        cur.execute(f"CREATE {temp}TABLE {name} ("
                    + ", ".join(f"{c} {t}" for c, t in cols) + ")")
        cur.executemany(f"INSERT INTO {name} VALUES ({', '.join([mark] * len(cols))})",
                        ROWS[name])
    if engine == "sqlite":
        conn.commit()


# --- the engines ---------------------------------------------------------------------------------


def _sqlite() -> Any:
    return sqlite3.connect(":memory:")


def _duckdb() -> Any:
    import duckdb

    return duckdb.connect()


def _postgres() -> Any:
    """Postgres when `AGAMI_IT_PG_PASSWORD` is set, the integration tests' convention. Temp tables
    only, so a read-only login is enough. None when it is not configured or not reachable."""
    if not os.environ.get("AGAMI_IT_PG_PASSWORD"):
        return None
    import psycopg2

    try:
        conn = psycopg2.connect(
            host=os.environ.get("AGAMI_IT_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("AGAMI_IT_PG_PORT", "55432")),
            user=os.environ.get("AGAMI_IT_PG_USER", "agami_test"),
            password=os.environ["AGAMI_IT_PG_PASSWORD"],
            dbname=os.environ.get("AGAMI_IT_PG_DB", "postgres"),
            connect_timeout=3,
        )
    except Exception:
        return None
    conn.autocommit = True
    return conn


# Engine name -> (connect, the `storage_type` a model declares for it).
ENGINES: dict[str, tuple[Callable[[], Any], str]] = {
    "sqlite": (_sqlite, "SQLite"),
    "duckdb": (_duckdb, "DuckDB"),
    "postgres": (_postgres, "PostgreSQL"),
}


# --- the check -----------------------------------------------------------------------------------


@dataclass
class Outcome:
    """What `check` did: how many claims were compared, which were skipped and why, and every
    disagreement found (statement, column, what differed)."""

    compared: int = 0
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    mismatches: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, other: "Outcome") -> None:
        self.compared += other.compared
        self.skipped += other.skipped
        self.mismatches += other.mismatches


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, decimal.Decimal)):
        return round(float(value), 6)
    return value


def _rows(conn: Any, sql: str) -> "list[tuple] | str":
    """Rows, or the error's class name."""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]
    except Exception as error:
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception:
                pass
        return type(error).__name__


def _group_keys(sel: "exp.Select") -> "list[exp.Expression] | None":
    group = sel.args.get("group")
    if group is None:
        return []
    if not rt._arm_is_grouped(sel):
        return None          # ROLLUP, CUBE, GROUPING SETS: not rebuilt here
    keys = []
    for g in group.expressions:
        if isinstance(g, exp.Literal) and not g.is_string:
            g = sel.expressions[int(g.this) - 1]
            g = g.this if isinstance(g, exp.Alias) else g
        keys.append(g.copy())
    return keys


def _value_ref(expr: "exp.Expression") -> "str | None":
    """The one qualifier the column's VALUE reads: its columns, leaving out any inside its own
    FILTER clause or a CASE WHEN condition (those choose rows, they are not what is added up).
    Decided here, not by the code under check."""
    refs = set()
    for col in expr.find_all(exp.Column):
        if col.find_ancestor(exp.Where) is not None:
            continue
        case_branch = col.find_ancestor(exp.If)
        if case_branch is not None and _under(col, case_branch.this):
            continue
        if col.table:
            refs.add(col.table.lower())
    return next(iter(refs)) if len(refs) == 1 else None


def _under(node: "exp.Expression", root: "exp.Expression") -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _model_columns(org: Any) -> dict[str, set[str]]:
    """table name (lowercased) -> its column names (lowercased), read off the model directly."""
    out: dict[str, set[str]] = {}
    for area in org.subject_areas:
        for table in area.tables_defined:
            out.setdefault(table.name.lower(), set()).update(c.name.lower() for c in table.columns)
    return out


def _references(sel: "exp.Select", org: Any) -> "dict[str, set[str] | None]":
    """alias (lowercased) -> the model's columns for the table it reads; None when unknown."""
    tables = _model_columns(org)
    out: dict[str, Any] = {}
    for child in sel.iter_expressions():
        if isinstance(child, (exp.From, exp.Join)) and child.this is not None:
            source = child.this
            known = isinstance(source, exp.Table) and source.name.lower() in tables
            out[source.alias_or_name.lower()] = tables[source.name.lower()] if known else None
    return out


def _joins(cond: "exp.Expression", refs: "dict[str, set[str] | None]") -> bool:
    """A bare `col = col` whose two sides are known to read two different references — the only
    conjunct the reference keeps as a join. An unqualified side is resolved through the model; one
    that cannot be resolved makes the conjunct a value condition, which the receipt has to list."""
    if not (isinstance(cond, exp.EQ) and isinstance(cond.this, exp.Column)
            and isinstance(cond.expression, exp.Column)):
        return False
    ends = []
    for col in (cond.this, cond.expression):
        if col.table:
            ends.append(col.table.lower())
            continue
        if any(cols is None for cols in refs.values()):
            return False
        hits = [ref for ref, cols in refs.items() if col.name.lower() in cols]
        if len(hits) != 1:
            return False
        ends.append(hits[0])
    return ends[0] != ends[1]


def pair(oc: "rt.OutputColumn", item: dict, applied: list[str], org: Any,
         dialect: "str | None") -> "tuple[str, str] | str":
    """(the statement's arm, the reference) as SQL, or why it cannot be rebuilt.

    The reference keeps only what the receipt does not have to list: the joins themselves, each
    inner join's ON cut down to its joining predicates (so a value condition hidden in an ON
    disappears from the reference and the numbers disagree), the WHERE's joining predicates, and
    outer, SEMI and ANTI joins as written. Every value condition comes from the receipt.
    """
    keys = _group_keys(oc.sel)
    if keys is None:
        return "grouping sets"
    binding = rt._parse_sql(f"SELECT {item['expression']}", dialect)
    if not isinstance(binding, exp.Select):
        return "binding does not parse"
    binding = binding.expressions[0]
    ref = _value_ref(oc.expr)
    if ref is not None:
        for col in binding.find_all(exp.Column):
            col.set("table", exp.to_identifier(ref))
    refs = _references(oc.sel, org)
    where = oc.sel.args.get("where")
    conds = [c.copy() for c in rt._and_conjuncts(where.this if where else None) if _joins(c, refs)]
    for text in [*item["restrictions"], *applied]:
        parsed = rt._parse_declared_predicate(text, dialect)
        if parsed is None:
            if "JOIN" in text.upper():
                continue          # a SEMI or ANTI join: kept as written in the FROM
            return "a restriction does not parse"
        conds.append(parsed)
    base = oc.sel.copy()
    for arg in ("order", "limit", "offset", "distinct", "having", "qualify"):
        base.set(arg, None)
    statement, reference = base.copy(), base.copy()
    for join in reference.args.get("joins") or []:
        inner = not join.args.get("side") and (join.args.get("kind") or "").upper() not in (
            "SEMI", "ANTI")
        if inner and join.args.get("on") is not None:
            kept = [c.copy() for c in rt._and_conjuncts(join.args["on"]) if _joins(c, refs)]
            join.set("on", exp.and_(*kept) if kept else exp.true())
    named = [k.copy().as_(f"k{i}") for i, k in enumerate(keys)]
    statement.set("expressions", [*named, oc.expr.copy().as_("v")])
    reference.set("expressions", [*(k.copy() for k in named), binding.as_("v")])
    reference.set("where", exp.Where(this=exp.and_(*conds)) if conds else None)
    return statement.sql(dialect=dialect), reference.sql(dialect=dialect)


def _compare(got: list[tuple], want: list[tuple]) -> "str | None":
    stmt = {tuple(_norm(v) for v in r[:-1]): _norm(r[-1]) for r in got}
    ref = {tuple(_norm(v) for v in r[:-1]): _norm(r[-1]) for r in want}
    if len(stmt) != len(got) or len(ref) != len(want):
        return "a group key repeats"
    for key in stmt.keys() & ref.keys():
        if stmt[key] != ref[key]:
            return f"group {key}: statement {stmt[key]!r}, metric {ref[key]!r}"
    for side, rows, other in (("statement", stmt, ref), ("metric", ref, stmt)):
        for key, value in rows.items():
            if key not in other and value not in (None, 0.0):
                return f"group {key} only from the {side}, value {value!r}"
    return None


def check(org: Any, sql: str, conn: Any) -> Outcome:
    """Every `matched` claim the receipt makes about `sql`, checked on `conn`'s data."""
    out = Outcome()
    dialect = rt._dialect_of(org)[0]
    receipt = rt.assemble_receipt(org, sql)
    tree = rt._parse_sql(sql, dialect)
    section = receipt["columns"]
    items = [i for i in section["items"] if i["kind"] == "output"]
    applied: dict[str, list[str]] = {}
    for table in receipt["tables"]["items"]:
        for flt in table.get("filters") or []:
            if flt["status"] == "applied":
                applied.setdefault(table["scope"], []).append(flt["expr"])
    cut = "only some of their conditions" in (section["undetermined"] or "")
    for item, oc in zip(items, rt._output_columns(tree)):
        if item["status"] != rt.MATCHED:
            continue
        why = None
        texts = item["restrictions"] + item["result_filters"]
        if any(rt._WITHHELD in t for t in texts):
            why = "a value is withheld"
        elif cut:
            why = "a list was cut short"
        rebuilt = None if why else pair(oc, item, applied.get(item["scope"], []), org,
                                            dialect)
        if isinstance(rebuilt, str):
            why = rebuilt
        if why is None:
            got, want = _rows(conn, rebuilt[0]), _rows(conn, rebuilt[1])
            if isinstance(got, str) or isinstance(want, str):
                why = f"the engine rejected it ({got if isinstance(got, str) else want})"
            else:
                differs = _compare(got, want)
                out.compared += 1
                if differs:
                    out.mismatches.append((sql, item["column"], differs))
                continue
        out.skipped.append((sql, item["column"], why))
    return out
