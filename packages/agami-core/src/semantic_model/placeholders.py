"""Placeholder values: stored values that stand in for "unknown", "still open" or "not set" (#445).

An Unknown dimension row keyed -99999 and dated 2099-12-31, a valid-to of 9999-12-31 on the current
version of a row, '-' as filler text: each looks like data, so an answer that groups by it or takes
its MAX reports it as a real value. Introspection finds them by shape (`find`); a person says what
each one means, and that choice is stored on the column (`Column.placeholders`) with a caveat in
words. Nothing here filters a query: whether to leave a row out depends on the question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

# What each meaning tells the SQL writer. Served beside the value, and the source of the caveat.
MEANINGS: dict[str, str] = {
    "unknown": ("stands for an unknown member: leave its rows out of breakdowns (GROUP BY), date "
                "ranges and latest/earliest (MAX/MIN), and keep them in totals"),
    "current": ("marks a row that is still current (open-ended): these are the rows an \"as of now\" "
                "question wants; never report the value itself as a real date or the latest one"),
    "not_set": "means not set: treat it like NULL",
}


def caveat(value: str, meaning: str) -> str:
    """The column caveat a confirmed placeholder writes, in words."""
    return f"'{value}' {MEANINGS[meaning]}"


@dataclass(frozen=True)
class Finding:
    value: str
    reason: str
    rows: Optional[int] = None   # rows holding it, when the scan counted them


# Text a system writes when it has nothing to say. Compared stripped and case-folded.
FILLER_TOKENS = frozenset({
    "", "-", "--", "---", ".", "?", "n/a", "na", "#n/a", "none", "null", "nil", "tbd", "tba",
    "unknown", "unk", "not set", "not applicable", "not available", "missing", "x", "xx", "xxx",
})
# A filler token is only worth asking about when it covers this share of the column's rows.
FILLER_MIN_SHARE = 0.05
# Dates: anything before this year, or more than this many years ahead, is not a real event date.
_FIRST_REAL_YEAR = 1901
_YEARS_AHEAD = 50
# "No date" stand-ins that fall inside the plausible range, flagged only when far from every other
# value in the column.
_IN_RANGE_NO_DATES = ("1970-01-01",)
_FAR_YEARS = 10
_MAX_PER_COLUMN = 3

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_INT_RE = re.compile(r"^-?\d+$")
_NINES_RE = re.compile(r"^9{5,}$")


def _year_of(value: str, *, yyyymmdd: bool = False) -> Optional[int]:
    v = str(value).strip()
    if yyyymmdd:
        return int(v) // 10000 if _INT_RE.match(v) and len(v.lstrip("-")) == 8 else None
    m = _DATE_RE.match(v)
    return int(m.group(1)) if m else None


def _as_int(value: Optional[str]) -> Optional[int]:
    v = str(value).strip() if value is not None else ""
    return int(v) if _INT_RE.match(v) else None


def find(
    kinds: set[str], top: list[tuple[str, int]], lo: Optional[str], hi: Optional[str],
    total: int, rest: list[str], *, yyyymmdd: bool = False, today: Optional[date] = None,
) -> list[Finding]:
    """The values of one column that look like placeholders.

    `kinds` is what the column is: any of `date`, `year`, `key`, `code`. `top` is its most frequent
    values with their row counts, `lo`/`hi` its MIN and MAX, `total` its row count, and `rest` a
    sample of its other values, which "far from the rest" is judged against. The known sentinels
    (2099-12-31, 9999-12-31, -1, 999999, 'N/A') are examples of each shape, not a list matched."""
    now = (today or date.today()).year
    counts = dict(top)
    out: dict[str, Finding] = {}

    def add(value: Optional[str], reason: str) -> None:
        if value is None or value in out:
            return
        out[value] = Finding(value, reason, counts.get(value))

    candidates = [v for v in (lo, hi, *counts) if v is not None and v != ""]
    if "date" in kinds:
        others = [y for y in (_year_of(v, yyyymmdd=yyyymmdd) for v in rest) if y is not None]
        for v in candidates:
            y = _year_of(v, yyyymmdd=yyyymmdd)
            if y is None:
                continue
            if y < _FIRST_REAL_YEAR:
                add(v, "a zero date" if y == 0 else f"a date before {_FIRST_REAL_YEAR}")
            elif y > now + _YEARS_AHEAD:
                add(v, f"a date more than {_YEARS_AHEAD} years ahead")
            elif v[:10] in _IN_RANGE_NO_DATES and v == lo:
                real = [o for o in others if o != y]
                if real and min(abs(o - y) for o in real) >= _FAR_YEARS:
                    add(v, "a date far before every other one in the column")
    if "year" in kinds:
        for v in candidates:
            y = _as_int(v)
            if y is not None and (y < _FIRST_REAL_YEAR or y > now + _YEARS_AHEAD):
                add(v, "a year far outside the others" if y else "a year of 0")
    if kinds & {"key", "code"}:
        low, high = _as_int(lo), _as_int(hi)
        if low is not None and high is not None and low < 0 < high:
            for v in candidates:
                n = _as_int(v)
                if n is not None and n < 0:
                    add(v, "a negative key among positive ones" if "key" in kinds
                        else "a negative value among positive ones")
        if high is not None and _NINES_RE.match(str(hi).strip()):
            others = [n for n in (_as_int(v) for v in rest) if n is not None and n != high]
            if not others or max(others) * 10 < high:
                add(hi, "a 999999-style value far above the rest")
    if total > 0:
        for v, n in top:
            if v.strip().casefold() in FILLER_TOKENS and n >= 2 and n / total >= FILLER_MIN_SHARE:
                add(v, f"filler text on {round(100 * n / total)}% of rows")
    return list(out.values())[:_MAX_PER_COLUMN]
