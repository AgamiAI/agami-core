"""What the data holds, read at build time (#448): every join checked on the data, and a column profile.

A model built from the schema alone passes exactly the quirks that give a plausible wrong number: a
join that drops or multiplies rows, a column that is mostly blank or one stand-in value, a date
column whose values don't fit its name. This module reads them, bounded, and the result goes to the
person reviewing the build and to the steps that describe the model, never into rule or caveat text.

**Joins.** A join is checked on the whole key when the table it starts from is under
`JOIN_WHOLE_MAX_ROWS`, and on a sample of `JOIN_SAMPLE_ROWS` rows above it: spread across the table
where the engine can sample (`Dialect.tablesample`), the first rows read where it can't, and the
evidence says which. One check gives both directions (rows that find no partner, which an inner join
drops, and keys on the other side no row points at) and whether the referenced key repeats (a join
that multiplies rows). A table it would join to above `JOIN_TARGET_MAX_ROWS` is not read at all.

**Batched.** Over a pooled cloud connection each statement costs a round trip, and checking joins
one at a time was most of a build. Up to `CHECKS_PER_STATEMENT` checks go in one statement (a
UNION ALL of one-row aggregates), and up to `WORKERS` statements run at once. A statement that fails
is retried check by check, so one bad pair costs only itself.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from . import dialects as D
from .models import Table

Runner = Callable[[str], list[dict]]

# Under this many rows (the catalog's estimate) the table a join starts from is read whole.
JOIN_WHOLE_MAX_ROWS = 500_000
# Above it, this many rows are read.
JOIN_SAMPLE_ROWS = 5_000
# A table a join points at is read whole (its distinct keys); past this size the join is not checked.
JOIN_TARGET_MAX_ROWS = 3_000_000
# Joins checked per run, across every pass that checks them.
JOIN_CHECK_CAP = 300
# A join whose values are found on the other side at this rate or above is borne out by the data.
JOIN_MIN_MATCH = 0.95
CHECKS_PER_STATEMENT = 8
WORKERS = 4


def _rows(t: Table) -> Optional[int]:
    perf = t.performance_hints
    rows = perf.estimated_row_count if perf else None
    return rows if rows is not None and rows >= 0 else None


def _num(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


@dataclass
class JoinCheck:
    """One join, checked on the data: `ft.fc` (its rows) against `tt.tc` (the key it points at)."""

    ft: Table
    fc: list[str]
    tt: Table
    tc: list[str]
    read: str = ""            # "whole", "sample" (spread out), "first_rows", or "" when not run
    failed: bool = False
    rows_read: int = 0        # rows of ft read
    rows_keyed: int = 0       # of those, rows whose key is filled in
    rows_matched: int = 0     # of those, rows whose key is found in tt
    values: int = 0           # distinct keys among the rows read
    values_matched: int = 0   # of those, found in tt
    to_keys: int = 0          # distinct keys in tt
    to_rows: int = 0          # rows of tt with the key filled in

    @property
    def ran(self) -> bool:
        return bool(self.read) and not self.failed

    @property
    def rate(self) -> Optional[float]:
        """Share of distinct values found (whole key), or of sampled rows (a sample): None when nothing
        was compared."""
        if not self.ran:
            return None
        if self.read == "whole":
            return self.values_matched / self.values if self.values else None
        return self.rows_matched / self.rows_keyed if self.rows_keyed else None

    @property
    def borne_out(self) -> bool:
        return self.rate is not None and self.rate >= JOIN_MIN_MATCH

    @property
    def fans_out(self) -> bool:
        """The referenced key repeats, so each row joins to several."""
        return self.ran and self.to_rows > self.to_keys > 0

    def _name(self, t: Table, cols: list[str]) -> str:
        return f"{t.name}.{cols[0]}" if len(cols) == 1 else f"{t.name}({', '.join(cols)})"

    def summary(self) -> str:
        f, t = self._name(self.ft, self.fc), self._name(self.tt, self.tc)
        if not self.ran:
            return "the check could not compare the two columns"
        if not self.rows_read:
            return f"no {self.ft.name} rows were read"
        if not self.values:
            return f"{f} is blank on every row read"
        if self.read == "whole":
            head = (f"all {self.values:,} values of {f} are found in {t}" if self.values_matched == self.values
                    else f"{self.values_matched:,} of {self.values:,} values of {f} are found in {t} "
                         f"({self.rate:.0%})")
            parts = [head + " (whole table checked)"]
        else:
            how = "a spread-out sample" if self.read == "sample" else "the first rows read"
            parts = [f"{self.rows_matched:,} of {self.rows_keyed:,} sampled {self.ft.name} rows are found in {t} "
                     f"({self.rate:.0%}; {how}, {self.rows_read:,} rows)"]
        lost = self.rows_read - self.rows_matched
        if lost and self.read == "whole":
            blank = self.rows_read - self.rows_keyed
            parts.append(f"{lost:,} of {self.rows_read:,} {self.ft.name} rows have no match"
                         + (f" ({blank:,} blank)" if blank else "") + ": an inner join drops them")
        unused = self.to_keys - self.values_matched
        if self.read == "whole" and unused > 0:
            parts.append(f"{unused:,} of {self.to_keys:,} {self.tt.name} keys have no {self.ft.name} row")
        if self.fans_out:
            parts.append(f"{t} repeats ({self.to_rows:,} rows for {self.to_keys:,} values): "
                         "each row joins to several, which multiplies totals")
        return "; ".join(parts)

    def evidence(self) -> dict:
        out: dict = {"read": self.read or "not_run", "summary": self.summary()}
        if self.ran:
            out.update(rows_read=self.rows_read, rows_keyed=self.rows_keyed, rows_matched=self.rows_matched,
                       values=self.values, values_matched=self.values_matched,
                       to_keys=self.to_keys, to_rows=self.to_rows)
            if self.rate is not None:
                out["match_rate"] = round(self.rate, 4)
        return out


def checkable(dialect: D.Dialect, ft: Table, tt: Table) -> bool:
    """A join can be checked unless the table it points at is too large to read its keys."""
    rows = _rows(tt)
    return rows is None or rows <= JOIN_TARGET_MAX_ROWS


def _from_rows(dialect: D.Dialect, c: JoinCheck, spread: bool) -> tuple[str, str]:
    """The rows of the starting table a check reads, and how: whole, a spread-out sample, or first rows."""
    picked = ", ".join(f"{dialect.quote_ident(col)} AS v{i}" for i, col in enumerate(c.fc))
    q = dialect.qualified(c.ft.schema_name, c.ft.name)
    rows = _rows(c.ft)
    if rows is not None and rows <= JOIN_WHOLE_MAX_ROWS:
        return f"SELECT {picked} FROM {q}", "whole"
    return dialect.sampled_rows(f"SELECT {picked} FROM {q}", JOIN_SAMPLE_ROWS, rows if spread else None)


def check_sql(dialect: D.Dialect, c: JoinCheck, label: int, *, spread: bool = True) -> tuple[str, str]:
    """One check as a one-row aggregate, labelled so several can share a statement.

    The starting rows are grouped by key once (`g`, each key with its row count) and left-joined to
    the distinct keys of the other table: one pass over each side, so a sample is drawn once and
    every count comes from the same rows. No correlated subquery, which some engines refuse inside
    an aggregate."""
    src, how = _from_rows(dialect, c, spread)
    vs = [f"v{i}" for i in range(len(c.fc))]
    ks = [dialect.quote_ident(k) for k in c.tc]
    fq_to = dialect.qualified(c.tt.schema_name, c.tt.name)
    keyed = " AND ".join(f"g.{v} IS NOT NULL" for v in vs)
    to_filled = " AND ".join(f"{k} IS NOT NULL" for k in ks)
    on = " AND ".join(f"t.k{i} = g.{v}" for i, v in enumerate(vs))
    to_keys = ", ".join(f"{k} AS k{i}" for i, k in enumerate(ks))
    hit = "t.k0 IS NOT NULL"
    sql = (
        f"SELECT {label} AS probe, SUM(g.n) AS rows_read, "
        f"SUM(CASE WHEN {keyed} THEN g.n ELSE 0 END) AS rows_keyed, "
        f"SUM(CASE WHEN {hit} THEN g.n ELSE 0 END) AS rows_matched, "
        f"SUM(CASE WHEN {keyed} THEN 1 ELSE 0 END) AS vals, "
        f"SUM(CASE WHEN {hit} THEN 1 ELSE 0 END) AS vals_matched, "
        f"(SELECT COUNT(*) FROM (SELECT DISTINCT {', '.join(ks)} FROM {fq_to} WHERE {to_filled}) u) AS to_keys, "
        f"(SELECT COUNT(*) FROM {fq_to} WHERE {to_filled}) AS to_rows "
        f"FROM (SELECT {', '.join(vs)}, COUNT(*) AS n FROM ({src}) s GROUP BY {', '.join(vs)}) g "
        f"LEFT JOIN (SELECT DISTINCT {to_keys} FROM {fq_to} WHERE {to_filled}) t ON {on}"
    )
    return sql, how


def _apply(c: JoinCheck, row: dict, how: str) -> None:
    c.read = how
    c.rows_read, c.rows_keyed, c.rows_matched = _num(row, "rows_read"), _num(row, "rows_keyed"), _num(row, "rows_matched")
    c.values, c.values_matched = _num(row, "vals"), _num(row, "vals_matched")
    c.to_keys, c.to_rows = _num(row, "to_keys"), _num(row, "to_rows")


def _one(dialect: D.Dialect, runner: Runner, c: JoinCheck, spread_first: bool = True) -> None:
    # A sample clause the engine refuses on this object (a view, say) falls back to the first rows.
    for spread in (True, False) if spread_first else (False,):
        sql, how = check_sql(dialect, c, 0, spread=spread)
        try:
            rows = runner(sql)
        except Exception:
            rows = None
        if rows:
            _apply(c, rows[0], how)
            return
        if how != "sample":
            break
    c.read, c.failed = how, True


def _batch(dialect: D.Dialect, runner: Runner, batch: list[JoinCheck]) -> None:
    built = [check_sql(dialect, c, i) for i, c in enumerate(batch)]
    try:
        rows = runner(" UNION ALL ".join(sql for sql, _ in built)) if len(batch) > 1 else None
    except Exception:
        rows = None
    by_label = {_num(r, "probe"): r for r in rows or []}
    if rows is None or len(by_label) != len(batch):
        for c in batch:   # one failing pair (a type mismatch, say) costs only itself
            _one(dialect, runner, c)
        return
    for i, c in enumerate(batch):
        _apply(c, by_label[i], built[i][1])
        if c.read == "sample" and not c.rows_read:
            # A share of a table whose estimate was stale can come back empty: read the first rows.
            _one(dialect, runner, c, spread_first=False)


def run_checks(dialect: D.Dialect, runner: Runner, checks: list[JoinCheck]) -> None:
    """Run every check in place: several to a statement, a few statements at once."""
    batches = [checks[i:i + CHECKS_PER_STATEMENT] for i in range(0, len(checks), CHECKS_PER_STATEMENT)]
    if len(batches) <= 1:
        for b in batches:
            _batch(dialect, runner, b)
        return
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(lambda b: _batch(dialect, runner, b), batches))
