"""What the data holds, read at build time (#448): every join checked on the data, and a column profile.

A model built from the schema alone passes exactly the quirks that give a plausible wrong number: a
join that drops or multiplies rows, a column that is mostly blank or one stand-in value, a date
column whose values don't fit its name. This module reads them, bounded, and the result goes to the
person reviewing the build and to the steps that describe the model, never into rule or caveat text.

**Joins.** A join is checked on every row of the table it starts from when that table is known to be
under `JOIN_WHOLE_MAX_ROWS`, and on a bounded read of `JOIN_SAMPLE_ROWS` rows otherwise: spread across
the table where the engine samples whole pages or blocks (`Dialect.tablesample`), the first rows read
where it can't. The table it points at is read whole (its keys, grouped once) when it is known to be
under `JOIN_TARGET_MAX_ROWS`; otherwise only the keys the starting rows hold are looked up in it, which
an index serves. One read of each side gives the match rate (of rows: the rows an inner join keeps),
rows with no partner, keys on the other side no row points at (whole reads only), and whether the
referenced key repeats. Where a bounded read can still scan a whole object (`Dialect.scans_unsized`),
an object of unknown size isn't read at all.

**Batched.** Over a pooled cloud connection each statement costs a round trip, and checking joins
one at a time was most of a build. Up to `CHECKS_PER_STATEMENT` checks go in one statement (a UNION
ALL of one-row aggregates), and up to `WORKERS` statements run at once, so a build holds up to that
many connections beside its own. A refused statement is retried check by check; a statement that ran
out of time is not, and its checks are reported as not run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from . import dialects as D
from .models import Column, Table

Runner = Callable[[str], list[dict]]

# Under this many rows (the catalog's estimate) the table a join starts from is read whole.
JOIN_WHOLE_MAX_ROWS = 500_000
# Otherwise, this many of its rows are read.
JOIN_SAMPLE_ROWS = 5_000
# Under this many rows the table a join points at is read whole; above it, only the keys looked up.
JOIN_TARGET_MAX_ROWS = 3_000_000
# Joins checked per run, declared and inferred alike.
JOIN_CHECK_CAP = 300
# A join whose rows find their partner at this rate or above is borne out by the data.
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


def percent(rate: float) -> str:
    """A share as a whole percent, rounded DOWN: 94.96% shows as 94%, never as the 95% a rule needs."""
    return f"{int(rate * 100 + 1e-9)}%"


def too_slow(exc: BaseException) -> bool:
    """A statement stopped by a time or size bound, rather than refused for what it says."""
    text = str(exc).lower()
    return (getattr(exc, "kind", None) in ("timeout", "resource_limit") or "resource_limit" in text
            or "timed out" in text or "timeout" in text)


@dataclass
class JoinCheck:
    """One join, checked on the data: `ft.fc` (its rows) against `tt.tc` (the key it points at).
    `target_is_one`: the join says each row has at most one partner, so a repeating key is a fan-out."""

    ft: Table
    fc: list[str]
    tt: Table
    tc: list[str]
    target_is_one: bool = True
    read: str = ""            # "whole", "sample" (spread out), "first_rows", or "" when not run
    target_read: str = ""     # "whole", or "lookup" (only the keys the rows hold)
    failed: bool = False
    rows_read: int = 0        # rows of ft read
    rows_keyed: int = 0       # of those, rows whose key is filled in
    rows_matched: int = 0     # of those, rows whose key is found in tt
    values: int = 0           # distinct keys among the rows read
    values_matched: int = 0   # of those, found in tt
    to_keys: int = 0          # distinct keys in tt (the matched ones, on a lookup)
    to_rows: int = 0          # rows of tt holding them

    @property
    def ran(self) -> bool:
        return bool(self.read) and not self.failed

    @property
    def rate(self) -> Optional[float]:
        """Share of the rows read with a key filled in whose key is found: None when none had one."""
        if not self.ran or not self.rows_keyed:
            return None
        return self.rows_matched / self.rows_keyed

    @property
    def borne_out(self) -> bool:
        return self.rate is not None and self.rate >= JOIN_MIN_MATCH

    @property
    def fans_out(self) -> bool:
        """The join says one partner, and the referenced key repeats, so a row joins to several."""
        return self.ran and self.target_is_one and self.to_rows > self.to_keys > 0

    def _name(self, t: Table, cols: list[str]) -> str:
        return f"{t.name}.{cols[0]}" if len(cols) == 1 else f"{t.name}({', '.join(cols)})"

    def summary(self) -> str:
        f, t = self._name(self.ft, self.fc), self._name(self.tt, self.tc)
        cols = self.fc[0] if len(self.fc) == 1 else ", ".join(self.fc)
        if not self.ran:
            return "the check could not compare the two columns"
        if not self.rows_read:
            return f"{self.ft.name} has no rows" if self.read == "whole" else f"no {self.ft.name} rows were read"
        if not self.rows_keyed:
            return f"{f} is blank on every row read"
        whole = self.read == "whole"
        if whole and self.rows_matched == self.rows_keyed:
            head = (f"the one value of {f} is found in {t}" if self.values == 1
                    else f"all {self.values:,} values of {f} are found in {t}")
            parts = [head + " (whole table checked)"]
        elif whole:
            parts = [f"{self.rows_matched:,} of {self.rows_keyed:,} {self.ft.name} rows find their {cols} in {t} "
                     f"({percent(self.rate)}; whole table checked, {self.values_matched:,} of {self.values:,} values)"]
        else:
            how = "a spread-out sample" if self.read == "sample" else "the first rows read"
            parts = [f"{self.rows_matched:,} of {self.rows_keyed:,} sampled {self.ft.name} rows find their {cols} "
                     f"in {t} ({percent(self.rate)}; {how}, {self.rows_read:,} rows)"]
        lost = self.rows_read - self.rows_matched
        if lost and whole:
            blank = self.rows_read - self.rows_keyed
            what = (f"leave {cols} blank" if blank == lost
                    else "have no match" + (f" ({blank:,} blank)" if blank else ""))
            parts.append(f"{lost:,} of {self.rows_read:,} {self.ft.name} rows {what}: an inner join drops them")
        unused = self.to_keys - self.values_matched
        if whole and self.target_read == "whole" and unused > 0:
            parts.append(f"{unused:,} of {self.to_keys:,} {self.tt.name} keys have no {self.ft.name} row")
        if self.fans_out:
            parts.append(f"{t} repeats ({self.to_rows:,} rows for {self.to_keys:,} values): "
                         "each row joins to several, which multiplies totals")
        return "; ".join(parts)

    def evidence(self) -> dict:
        out: dict = {"read": self.read or "not_run", "summary": self.summary()}
        if self.ran:
            out.update(target_read=self.target_read, rows_read=self.rows_read, rows_keyed=self.rows_keyed,
                       rows_matched=self.rows_matched, values=self.values, values_matched=self.values_matched,
                       to_keys=self.to_keys, to_rows=self.to_rows)
            if self.rate is not None:
                out["match_rate"] = round(self.rate, 4)
        return out


def checkable(dialect: D.Dialect, ft: Table, tt: Table, fc: list[str]) -> Optional[str]:
    """Why a join can't be checked, or None when it can. An object of unknown size is read only where
    a bounded read stays bounded; a key into a table too big to read whole is looked up, which needs
    a one-column key."""
    if not dialect.scans_unsized and (_rows(ft) is None or _rows(tt) is None):
        return "a table it joins is of unknown size, and a bounded read of it here can still scan all of it"
    if _target_whole(tt) is None and len(fc) > 1:
        return "a key over several columns into a table too large to read whole"
    return None


def _target_whole(tt: Table) -> Optional[bool]:
    rows = _rows(tt)
    return True if rows is not None and rows <= JOIN_TARGET_MAX_ROWS else None


def _from_rows(dialect: D.Dialect, c: JoinCheck, spread: bool) -> tuple[str, str]:
    """The rows of the starting table a check reads, and how: whole, a spread-out sample, or first rows."""
    picked = ", ".join(f"{dialect.quote_ident(col)} AS v{i}" for i, col in enumerate(c.fc))
    q = dialect.qualified(c.ft.schema_name, c.ft.name)
    rows = _rows(c.ft)
    if rows is not None and rows <= JOIN_WHOLE_MAX_ROWS:
        return f"SELECT {picked} FROM {q}", "whole"
    return dialect.sampled_rows(f"SELECT {picked} FROM {q}", JOIN_SAMPLE_ROWS, rows if spread else None)


def check_sql(dialect: D.Dialect, c: JoinCheck, label: int, *, spread: bool = True) -> tuple[str, str, str]:
    """One check as a one-row aggregate, labelled so several can share a statement; with how each side
    was read.

    Each side is grouped by key once and the two are stacked (UNION ALL) and grouped again, so every
    count comes from one read of each side and a sample is drawn once. No join and no correlated
    subquery, which some engines refuse inside an aggregate. On a lookup the other side is narrowed
    to the keys the starting rows hold (an IN list an index serves), so it is never read whole."""
    target = "whole" if _target_whole(c.tt) else "lookup"
    # A lookup reads the starting rows twice (once for the keys to look up), so they must be the same
    # rows both times: the first rows, never a random share.
    src, how = _from_rows(dialect, c, spread and target == "whole")
    vs = [f"v{i}" for i in range(len(c.fc))]
    ks = [dialect.quote_ident(k) for k in c.tc]
    fq_to = dialect.qualified(c.tt.schema_name, c.tt.name)
    keyed = " AND ".join(f"g.{v} IS NOT NULL" for v in vs)
    filled = " AND ".join(f"{k} IS NOT NULL" for k in ks)
    if target == "lookup":   # one-column keys only (`checkable`)
        filled += f" AND {ks[0]} IN (SELECT s2.v0 FROM ({src}) s2 WHERE s2.v0 IS NOT NULL)"
    to_cols = ", ".join(f"{k} AS v{i}" for i, k in enumerate(ks))
    stacked = (f"SELECT {', '.join(vs)}, COUNT(*) AS f, 0 AS t FROM ({src}) s GROUP BY {', '.join(vs)} "
               f"UNION ALL SELECT {to_cols}, 0 AS f, COUNT(*) AS t FROM {fq_to} WHERE {filled} "
               f"GROUP BY {', '.join(ks)}")
    sql = (
        f"SELECT {label} AS probe, SUM(g.f) AS rows_read, "
        f"SUM(CASE WHEN {keyed} THEN g.f ELSE 0 END) AS rows_keyed, "
        f"SUM(CASE WHEN g.t > 0 THEN g.f ELSE 0 END) AS rows_matched, "
        f"SUM(CASE WHEN {keyed} AND g.f > 0 THEN 1 ELSE 0 END) AS vals, "
        f"SUM(CASE WHEN g.f > 0 AND g.t > 0 THEN 1 ELSE 0 END) AS vals_matched, "
        f"SUM(CASE WHEN g.t > 0 THEN 1 ELSE 0 END) AS to_keys, SUM(g.t) AS to_rows "
        f"FROM (SELECT {', '.join(vs)}, SUM(f) AS f, SUM(t) AS t FROM ({stacked}) u GROUP BY {', '.join(vs)}) g"
    )
    return sql, how, target


def _apply(c: JoinCheck, row: dict, how: str, target: str) -> None:
    c.read, c.target_read = how, target
    c.rows_read, c.rows_keyed, c.rows_matched = _num(row, "rows_read"), _num(row, "rows_keyed"), _num(row, "rows_matched")
    c.values, c.values_matched = _num(row, "vals"), _num(row, "vals_matched")
    c.to_keys, c.to_rows = _num(row, "to_keys"), _num(row, "to_rows")


def _one(dialect: D.Dialect, runner: Runner, c: JoinCheck, spread_first: bool = True) -> None:
    """One check alone. A sample clause the engine refuses on this object (a view, say), or a share that
    came back empty, falls back to the first rows; a check that ran out of time is not tried again."""
    for spread in (True, False) if spread_first else (False,):
        sql, how, target = check_sql(dialect, c, 0, spread=spread)
        try:
            rows = runner(sql)
        except Exception as exc:
            if too_slow(exc):
                break
            rows = None
        if rows:
            _apply(c, rows[0], how, target)
            if how != "sample" or c.rows_read:
                return
        if how != "sample":
            break
    c.read, c.failed = how, True


def _batch(dialect: D.Dialect, runner: Runner, batch: list[JoinCheck]) -> None:
    built = [check_sql(dialect, c, i) for i, c in enumerate(batch)]
    rows = None
    if len(batch) > 1:
        try:
            rows = runner(" UNION ALL ".join(sql for sql, _, _ in built))
        except Exception as exc:
            if too_slow(exc):
                # Retried one by one, each would run to the same limit: report them as not run.
                for c in batch:
                    c.read, c.failed = built[0][1], True
                return
    by_label = {_num(r, "probe"): r for r in rows or []}
    if rows is None or len(by_label) != len(batch):
        for c in batch:   # one failing pair (a type mismatch, say) costs only itself
            _one(dialect, runner, c)
        return
    for i, c in enumerate(batch):
        _apply(c, by_label[i], built[i][1], built[i][2])
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


# ---------------------------------------------------------------------------
# Column profile
# ---------------------------------------------------------------------------

# A table under this many rows is profiled whole; above it, a sample of PROFILE_SAMPLE_ROWS rows.
PROFILE_WHOLE_MAX_ROWS = JOIN_WHOLE_MAX_ROWS
PROFILE_SAMPLE_ROWS = 10_000
PROFILE_TABLE_CAP = 200
# Columns read per table, in catalog order: one statement holds them all.
PROFILE_MAX_COLUMNS = 80
# A coded column gets its most frequent values with their shares: at most this many distinct values,
# the same line (and the same per-value checks) as a value list written into the model.
PROFILE_TOP_MAX_DISTINCT = 25
PROFILE_TOP = 10
# Names whose values stay out of the profile even when they look coded: a person's or a party's
# name, a contact or tax number, a licence. Checked beside `pii.suspected_pii`, which these escape.
_WITHHELD_WORDS = frozenset({"name", "names", "contact", "phone", "mobile", "email", "pan", "gstin", "vat",
                             "tax", "taxid", "tin", "ssn", "licence", "license", "passport", "national"})
# Pay to a person: its range is withheld (what one person earns), its counts kept.
_PAY_WORDS = frozenset({"salary", "salaries", "wage", "wages", "pay", "payrate", "compensation", "bonus",
                        "commission", "stipend", "income", "earnings"})
_DATE_TYPES = ("date", "timestamp")
# Not float: some warehouses refuse to group or count distinct floating-point values.
_DISTINCT_TYPES = ("string", "integer", "decimal", "boolean", "date", "timestamp", "uuid")
_RANGE_TYPES = ("integer", "decimal", "float", "date", "timestamp")
_MAX_DATE_PAIRS_COLS = 6
# Words that name when something happened, in the order a record usually goes through them. Two
# dates whose names rank differently are expected in that order; the data saying the reverse on
# nearly every row means one of the names doesn't say what the column holds.
_LIFECYCLE = (("created", "opened", "submitted", "requested", "ordered", "placed", "booked", "registered",
               "signup", "joined", "start", "started", "issued", "invoice", "invoiced"),
              ("approved", "confirmed", "accepted", "assigned", "scheduled"),
              ("paid", "payment", "shipped", "dispatched", "sent", "billed"),
              ("delivered", "received", "arrived", "returned"),
              ("closed", "completed", "resolved", "finished", "ended", "end", "cancelled", "canceled"))
# Names of a date that has already happened: a value past today means it holds something else.
_PAST_WORDS = frozenset({"created", "opened", "submitted", "ordered", "placed", "paid", "payment", "shipped",
                         "delivered", "received", "closed", "completed", "resolved", "birth", "born",
                         "signup", "joined", "registered", "updated", "modified", "sent", "posted"})
_ORDER_MIN_ROWS = 20
_ORDER_DOMINANT = 0.9
_MOSTLY_BLANK = 0.9


@dataclass
class _ColStat:
    name: str
    type: str
    personal: bool            # no values at all: no frequent values, no range
    is_date: bool = False     # a date or timestamp, or text holding ISO dates (which sort as dates)
    no_range: bool = False    # pay to a person: no range either
    filled: int = 0
    blank: int = 0
    distinct: Optional[int] = None
    lo: Optional[str] = None
    hi: Optional[str] = None


def _words(name: str) -> set[str]:
    import re
    return {w for w in re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", name.lower()) if w}


def _stage(name: str) -> Optional[int]:
    w = _words(name)
    return next((i for i, words in enumerate(_LIFECYCLE) if w & set(words)), None)


def withheld(table: str, c: Column, personal: bool) -> bool:
    """No values of this column go into the profile: it is personal, or named like personal data."""
    from . import pii
    return personal or pii.suspected_pii(c.name, table) or bool(_words(c.name) & _WITHHELD_WORDS)


def _table_rows(dialect: D.Dialect, t: Table, cols: list[str]) -> tuple[str, str]:
    picked = ", ".join(dialect.quote_ident(c) for c in cols)
    q = dialect.qualified(t.schema_name, t.name)
    rows = _rows(t)
    if rows is not None and rows <= PROFILE_WHOLE_MAX_ROWS:
        return f"SELECT {picked} FROM {q}", "whole"
    return dialect.sampled_rows(f"SELECT {picked} FROM {q}", PROFILE_SAMPLE_ROWS, rows)


def stats_sql(dialect: D.Dialect, src: str, stats: list[_ColStat], dates: list[tuple[int, int]],
              *, fill_only: bool = False) -> str:
    """One pass over the rows read: per column its filled and blank counts, distinct count and range,
    and for each pair of same-typed dates how often one comes before the other. `fill_only` keeps
    just the counts every type supports, for a table where one odd column refused the rest."""
    parts = ["COUNT(*) AS n"]
    for i, c in enumerate(stats):
        q = dialect.quote_ident(c.name)
        parts.append(f"COUNT({q}) AS f{i}")
        if fill_only:
            continue
        if c.type == "string":   # through text: an enum or other user type "string" may refuse TRIM
            parts.append(f"SUM(CASE WHEN TRIM({dialect.text_cast(q)}) = '' THEN 1 ELSE 0 END) AS b{i}")
        if c.type in _DISTINCT_TYPES:
            parts.append(f"COUNT(DISTINCT {q}) AS d{i}")
        if (c.type in _RANGE_TYPES or c.is_date) and not (c.personal or c.no_range):
            parts += [f"MIN({q}) AS lo{i}", f"MAX({q}) AS hi{i}"]
    for a, b in [] if fill_only else dates:
        qa, qb = dialect.quote_ident(stats[a].name), dialect.quote_ident(stats[b].name)
        parts += [f"SUM(CASE WHEN {qa} < {qb} THEN 1 ELSE 0 END) AS lt{a}_{b}",
                  f"SUM(CASE WHEN {qa} > {qb} THEN 1 ELSE 0 END) AS gt{a}_{b}"]
    return f"SELECT {', '.join(parts)} FROM ({src}) s"


def top_sql(dialect: D.Dialect, src: str, stats: list[tuple[int, _ColStat]]) -> str:
    """The values of each low-cardinality column with their row counts, as text so the arms line up."""
    arms = [f"SELECT {i} AS c, {dialect.text_cast(dialect.quote_ident(c.name))} AS v, COUNT(*) AS n "
            f"FROM ({src}) s GROUP BY {dialect.quote_ident(c.name)}" for i, c in stats]
    return dialect.limited(f"SELECT c, v, n FROM ({' UNION ALL '.join(arms)}) x",
                           len(stats) * (PROFILE_TOP_MAX_DISTINCT + 1))


def _text(v) -> Optional[str]:
    return None if v is None or v == "" else str(v)


def _share(n: int, total: int) -> float:
    return round(n / total, 3) if total else 0.0


def profile_table(dialect: D.Dialect, runner: Runner, t: Table, personal: Callable[[Table, Column], bool],
                  sample: Optional[list] = None, today: Optional[str] = None) -> Optional[dict]:
    """The profile of one table, or None when it couldn't be read. Two statements: the counts, then
    the frequent values of the coded columns. A personal column, or one named like personal data,
    gets counts only: none of its values, not even a range, is read into the profile."""
    from datetime import date

    live = [c for c in t.columns if c.review_state not in ("rejected", "stale")][:PROFILE_MAX_COLUMNS]
    if not live or (not dialect.scans_unsized and _rows(t) is None):
        return None
    stats = [_ColStat(c.name, c.type, withheld(t.name, c, personal(t, c)),
                      c.type in _DATE_TYPES or c.date_format == "iso8601", bool(_words(c.name) & _PAY_WORDS))
             for c in live]
    src, how = _table_rows(dialect, t, [c.name for c in live])
    di = [i for i, c in enumerate(stats) if c.is_date and not c.personal][:_MAX_DATE_PAIRS_COLS]
    dates = [(a, b) for k, a in enumerate(di) for b in di[k + 1:] if stats[a].type == stats[b].type]
    rows = None
    for fill_only in (False, True):   # one column of an odd type must not cost the whole table
        try:
            rows = runner(stats_sql(dialect, src, stats, dates, fill_only=fill_only))
            break
        except Exception as exc:
            if too_slow(exc):
                break
    if not rows:
        return None
    r = rows[0]
    n = _num(r, "n")
    for i, c in enumerate(stats):
        c.filled, c.blank = _num(r, f"f{i}"), _num(r, f"b{i}")
        c.distinct = _num(r, f"d{i}") if f"d{i}" in r else None
        c.lo, c.hi = _text(r.get(f"lo{i}")), _text(r.get(f"hi{i}"))
    # A coded column only, by the test a value list written into the model passes: a few values,
    # each on two rows or more on average. A column of one-off values (names, reference numbers) on
    # a small table also has few, and isn't listed.
    listed = [(i, c) for i, c in enumerate(stats) if not c.personal and c.distinct is not None
              and 1 < c.distinct <= PROFILE_TOP_MAX_DISTINCT and c.filled >= max(10, 2 * c.distinct)]
    top: dict[int, list[tuple[str, int]]] = {}
    if listed:
        try:
            # Read from the listed columns only, so no other column's values are selected at all.
            only, _ = _table_rows(dialect, t, [c.name for _, c in listed])
            for row in runner(top_sql(dialect, only, listed)):
                top.setdefault(_num(row, "c"), []).append((_text(row.get("v")), _num(row, "n")))
        except Exception:
            top = {}
    cols: dict = {}
    findings: list[dict] = []
    now = today or date.today().isoformat()
    by_name = {c.name: c for c in live}
    for i, c in enumerate(stats):
        entry: dict = {"type": c.type, "filled": _share(c.filled, n)}
        if c.type == "string":
            entry["blank_text"] = _share(c.blank, n)
        if c.distinct is not None:
            entry["distinct"] = c.distinct
        if c.lo is not None:
            entry["min"], entry["max"] = c.lo, c.hi
        values = top.get(i)
        if values and _coded([v for v, _ in values], c.filled):
            total = sum(k for _, k in values)
            entry["top"] = [{"value": v, "share": _share(k, total)}
                            for v, k in sorted(values, key=lambda x: (-x[1], x[0] or ""))[:PROFILE_TOP] if v is not None]
        stand_ins = [{"value": p.value, "reason": p.reason} for p in by_name[c.name].placeholders]
        if stand_ins:
            entry["stand_ins"] = stand_ins
        if c.personal:
            entry["personal"] = True
        cols[c.name] = entry
        findings += _column_findings(c, n, now, sample)
    order = []
    for a, b in dates:
        lt, gt = _num(r, f"lt{a}_{b}"), _num(r, f"gt{a}_{b}")
        if lt + gt < _ORDER_MIN_ROWS:
            continue
        first, then, k = (stats[a].name, stats[b].name, lt) if lt >= gt else (stats[b].name, stats[a].name, gt)
        share = _share(k, lt + gt)
        order.append({"earlier": first, "later": then, "share": share, "rows": lt + gt})
        sf, st = _stage(first), _stage(then)
        if share >= _ORDER_DOMINANT and sf is not None and st is not None and sf > st:
            findings.append({"column": first, "kind": "date_order",
                             "detail": f"{first} falls before {then} on {share:.0%} of rows that have both, "
                                       "the reverse of what the names say: one of them holds a different date"})
    out = {"read": how, "rows_read": n, "columns": cols}
    if _rows(t) is not None:
        out["rows_estimated"] = _rows(t)
    if len(t.columns) > len(live):
        out["columns_not_read"] = len(t.columns) - len(live)
    if order:
        out["date_order"] = order
    if findings:
        out["findings"] = findings
    return out


def _coded(values: list, rows: int) -> bool:
    """The values pass the per-value checks a value list written into the model passes: none looks
    like an email or a phone number, none is a GUID. One that fails withholds the whole column."""
    # introspect imports this module, so this import waits until it is called
    from .introspect import _maybe_choice
    return _maybe_choice([v for v in values if v is not None], rows=rows) is not None


def scrub(tables: dict, model: list[Table], withhold: Callable[[Table, Column], bool]) -> dict:
    """`tables` (a profile's `tables`) cut to what the model serves now, before it is written or read:
    a table or column the model doesn't serve is dropped, and a column it now marks personal, or
    named like personal data, loses every value (`top`, `min`, `max`, `stand_ins`)."""
    served = {(f"{t.schema_name}.{t.name}" if t.schema_name else t.name): t
              for t in model if t.review_state not in ("rejected", "stale")}
    out: dict = {}
    for key, prof in tables.items():
        t = served.get(key)
        if t is None:
            continue
        cols = {c.name: c for c in t.columns if c.review_state not in ("rejected", "stale")}
        kept = {}
        for name, entry in prof.get("columns", {}).items():
            c = cols.get(name)
            if c is None:
                continue
            if withheld(t.name, c, withhold(t, c)):
                entry = {k: v for k, v in entry.items() if k not in ("top", "min", "max", "stand_ins")}
                entry["personal"] = True
            elif _words(name) & _PAY_WORDS:
                entry = {k: v for k, v in entry.items() if k not in ("min", "max")}
            kept[name] = entry
        out[key] = {**prof, "columns": kept,
                    **({"findings": [f for f in prof["findings"] if f.get("column") in kept]}
                       if "findings" in prof else {})}
    return out


def _column_findings(c: _ColStat, n: int, today: str, sample: Optional[list]) -> list[dict]:
    if not n:
        return []
    out = []
    empty = n - c.filled + c.blank
    if c.filled == 0:
        out.append({"column": c.name, "kind": "empty", "detail": f"{c.name} is empty on every row read"})
    elif empty / n >= _MOSTLY_BLANK:
        out.append({"column": c.name, "kind": "mostly_blank",
                    "detail": f"{c.name} is blank on {empty / n:.0%} of rows"})
    elif c.distinct == 1 and c.filled == n and n >= _ORDER_MIN_ROWS:
        out.append({"column": c.name, "kind": "constant", "detail": f"{c.name} holds one value on every row"})
    if c.is_date and c.hi and c.hi[:10] > today and _words(c.name) & _PAST_WORDS:
        out.append({"column": c.name, "kind": "date_range",
                    "detail": f"{c.name} has dates after today, which a date named like this can't be"})
    if c.type == "string" and not c.is_date and sample and not c.personal:
        mixed = _mixed_formats([row.get(c.name) for row in sample])
        if mixed:
            out.append({"column": c.name, "kind": "mixed_formats", "detail": f"{c.name} mixes {mixed}"})
    return out


_SHAPES = (("ISO dates (2024-01-31)", r"^\d{4}-\d{2}-\d{2}"),
           ("slash dates (31/01/2024)", r"^\d{1,2}/\d{1,2}/\d{2,4}$"),
           ("dotted or dashed dates (31-01-2024)", r"^\d{1,2}[-.]\d{1,2}[-.]\d{4}$"),
           ("numbers", r"^[+-]?\d+(\.\d+)?$"))
_MIXED_MIN_SHARE = 0.05
_MIXED_MIN_VALUES = 20


def _mixed_formats(values: list) -> Optional[str]:
    """Which shapes a text column's values come in, when it holds more than one date shape, or
    numbers beside dates: one column, two writers."""
    import re
    vals = [str(v).strip() for v in values if v not in (None, "") and str(v).strip()]
    if len(vals) < _MIXED_MIN_VALUES:
        return None
    counts = {label: sum(1 for v in vals if re.match(rx, v)) for label, rx in _SHAPES}
    seen = [label for label, k in counts.items() if k / len(vals) >= _MIXED_MIN_SHARE]
    dates = [s for s in seen if "dates" in s]
    if len(dates) >= 2 or (dates and "numbers" in seen):
        return " and ".join(seen)
    return None


def profile_tables(dialect: D.Dialect, runner: Runner, tables: list[Table],
                   personal: Callable[[Table, Column], bool],
                   samples: Optional[dict] = None, done: Optional[Callable[[int], None]] = None) -> dict:
    """The profile of each table (at most PROFILE_TABLE_CAP), keyed `schema.table`, a few at once."""
    picked = tables[:PROFILE_TABLE_CAP]

    def one(t: Table):
        sample = (samples or {}).get((t.schema_name, t.name), (None,))[0]
        return t, profile_table(dialect, runner, t, personal, sample)

    out: dict = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for k, (t, prof) in enumerate(pool.map(one, picked), 1):
            if prof is not None:
                out[f"{t.schema_name}.{t.name}" if t.schema_name else t.name] = prof
            if done:
                done(k)
    return out
