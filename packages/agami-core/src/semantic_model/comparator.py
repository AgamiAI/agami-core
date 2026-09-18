"""Deciding whether two result sets say the same thing.

Canonical keys for the cells first, so two result sets can be compared at all.

A comparison asks whether the answer key's rows and the rows a generated statement produced say
the same thing. Doing that on the raw cells does not work, because the Python objects a driver
hands back are not the values a reader means:

* ``True == 1 == 1.0 == Decimal(1)`` and all four hash alike, so ``Counter`` collapses them into
  one bucket. A boolean column would compare equal to an integer column of zeros and ones, and
  the comparison would report a match it never checked.
* ``float('nan') != float('nan')``, yet dict's identity fast-path collapses the *same* NaN object
  anyway — so a row count would depend on whether the driver happened to reuse an object.
* ``Decimal('0.1') != 0.1``, because one is a decimal and the other the nearest binary float. The
  same number read through two drivers would disagree.

So every cell is first turned into a ``(type_tag, value)`` tuple: hashable, safely usable as a
``Counter`` key, and carrying the type distinction the raw value throws away. Values are
canonicalised only where two spellings genuinely mean one thing — a padded decimal, a date written
as text, a driver's ``'t'`` for true. Text itself is left exactly as it came: stripping or
case-folding would hide a real difference between two result sets, which is the one thing a
comparator must never do.

On top of those keys sits the comparison itself, in three steps that are deliberately separate:
whether the answer key asked for an ordering at all, which generated column answers which golden
one, and how far the rows agree once the columns are paired. Column identity is decided by VALUES
first — a generated statement is free to alias a total and to select it second. When several
columns hold equal values, the rows decide which pairs with which, and a name decides only what the
rows leave open. A name also decides which column a near miss that agrees on only some rows belongs
to. Rows are compared as a multiset unless the author ordered them, because duplicates are signal
and order usually is not.

``compare_result_sets`` is the one way in. It is TOTAL: a malformed result, an unreadable
statement or a band that cannot be applied all come back as a score with an error status, never as
an exception, so one bad item costs that item and not the run — the same posture
``execute_guarded`` takes and the same one ``golden.py`` takes when it reads a dataset. And what
it hands back carries verdicts, counts and column NAMES only. Never a cell, and never the answer
key's SQL: the payload being judged here is result data, and a score travels further than the run
that produced it.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal
from operator import itemgetter
from pathlib import Path
from typing import Any, Literal, NamedTuple, Optional

import sqlglot
from execute_sql import ExecResult
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from .golden import GoldenBounds, MatchLevel

# Every NaN is one bucket. The value is a plain string rather than a NaN, because a Decimal or
# float NaN as a dict key compares unequal to itself and would open a fresh bucket per cell.
_NAN_CELL: tuple[str, Any] = ("nan", "nan")

# Both contexts are explicit because the ambient decimal context is process-global and any caller
# can narrow its precision; a key that moved with it would compare two identical runs as different.
# The normalising precision is far above anything a database numeric carries, so it only ever
# strips trailing zeros. Note that it also turns Decimal('100') into Decimal('1E+2') — a different
# repr, but an equal value with an equal hash, which is all a key needs.
_NORMALIZE_CTX = Context(prec=60)
# A rounding BUCKET at nine significant digits, and deliberately not a tolerance: two numbers land
# on one key when they round alike, so two that straddle a bucket edge key apart however close they
# are. That is not a defect to be tightened later — a tolerance is not an equivalence relation, and
# a Counter needs one, so a bucket is the only form this forgiveness can take at all.
_QUANTIZE_CTX = Context(prec=9)

# What a driver or an author writes for a boolean. Postgres' text protocol emits `t`/`f`, other
# exports write the words out; none of them means the string it looks like.
_BOOL_TEXT = {"t": True, "true": True, "yes": True, "f": False, "false": False, "no": False}

# Strict, and matched with `fullmatch` so it is anchored at both ends. This pattern is the whole
# discriminator between a date and an identifier. A zero-padded account or order id is digits and
# nothing else, and coercing one into a date would make it compare equal to a different id that
# happened to land on the same day — so a bare year and a bare number are refused, exactly as
# `golden.py` refuses a loose date pattern, and for the same reason.
# The offset is limited to `±HH:MM` and the fraction to six digits because that is what
# `fromisoformat` accepts on Python 3.10, which this package still supports.
_DATE_TEXT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?"
)

# The coarse lattice a later, looser comparison judges a column by. A NULL contributes no type at
# all — it is the absence of a value, not a value of some type — and a NaN is still a number.
_CELL_TYPES: dict[str, Optional[str]] = {
    "null": None,
    "bool": "bool",
    "num": "number",
    "nan": "number",
    "date": "date",
    "text": "text",
}


def _canonical_number(value: int | float | Decimal, quantize: bool) -> tuple[str, Any]:
    """Key a numeric as a normalised Decimal, so spelling and storage type stop mattering."""
    if isinstance(value, float):
        if math.isnan(value):
            return _NAN_CELL
        # `Decimal(repr(x))` and not `Decimal(x)`: the latter expands the binary float in full, so
        # 0.1 becomes 0.1000000000000000055… and never matches a driver's Decimal('0.1').
        dec = Decimal(repr(value))
    elif isinstance(value, Decimal):
        if value.is_nan():
            return _NAN_CELL
        dec = value
    else:
        dec = Decimal(value)
    if not dec.is_finite():
        # An infinity is an ordinary comparable value and keeps its sign; normalising or rounding
        # it is meaningless, and quantizing it would raise.
        return ("num", dec)
    # A whole number is never rounded, whatever it was spelled as. It carries no floating-point
    # tail to forgive, and rounding one would put every id or count above ~1e9 in a bucket with its
    # neighbours — two genuinely different ids would then pass `values` as the same answer. The
    # test is on the VALUE and not on the Python type, because the same id arrives as an int from
    # one driver and a Decimal from another, and exempting only one of them would fail two
    # identical numbers.
    # The context is named rather than inherited. Nothing observable turns on it today —
    # `to_integral_value` is exempt from the Inexact and Rounded traps, and no rounding mode
    # changes whether a non-integral value differs from its integral form — but every other
    # decimal operation in this module names its context, and a module whose promise is
    # independence from how a number arrived should not read the process-global one at all.
    if quantize and dec != dec.to_integral_value(context=_NORMALIZE_CTX):
        dec = _QUANTIZE_CTX.plus(dec)
    return ("num", dec.normalize(context=_NORMALIZE_CTX))


def _canonical_datetime(value: datetime) -> tuple[str, Any]:
    """Key a datetime as ISO text, read as an instant in UTC.

    An aware value is converted to UTC and its offset dropped, which means a naive datetime is read
    as a UTC wall clock. That is deliberate: one driver attaches tzinfo to a timestamp column where
    another does not, and an answer key authored as text carries no offset at all, so keeping the
    offset in the key would fail every case whose two sides came through different readers.

    Midnight collapses to the bare date so that a date column read as a datetime still matches the
    date the author wrote down.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if value.time() == datetime.min.time():
        return ("date", value.date().isoformat())
    return ("date", value.isoformat())


def _canonical_text(value: str) -> tuple[str, Any]:
    """Key a string, recognising only the two shapes that are unambiguously not text."""
    spelled = _BOOL_TEXT.get(value.lower())
    if spelled is not None:
        return ("bool", spelled)
    if _DATE_TEXT_RE.fullmatch(value):
        try:
            # Python 3.10's `fromisoformat` raises on a trailing `Z`, so rewrite it to the offset
            # it stands for before parsing.
            return _canonical_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            # Date-shaped but not a real day, such as '2025-13-45'. It is text after all.
            return ("text", value)
    return ("text", value)


def canonical_cell(value: Any, *, quantize: bool = False) -> tuple[str, Any]:
    """Turn one result-set cell into a hashable ``(type_tag, value)`` key.

    With `quantize`, a number that is not a whole number is additionally rounded to nine
    significant digits. That is a BUCKET and not a tolerance: two values land on one key when they
    round alike, so two straddling a bucket edge stay apart however close they are. Nothing else is
    affected.
    """
    if value is None:
        return ("null", None)
    # Before the numeric branch, because bool is a subclass of int and the whole point of tagging
    # is that True must not key the same as 1.
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value, quantize)
    # Before `date`, because datetime is a subclass of it.
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, date):
        return ("date", value.isoformat())
    if isinstance(value, str):
        return _canonical_text(value)
    return ("text", str(value))


def canonical_row(row: Sequence[Any], *, quantize: bool = False) -> tuple[tuple[str, Any], ...]:
    """Canonicalise a whole row, so the row itself is hashable and countable."""
    return tuple(canonical_cell(cell, quantize=quantize) for cell in row)


def cell_type(canon: tuple[str, Any]) -> Optional[str]:
    """The coarse type of a canonical cell, or None for a null, which contributes no type."""
    return _CELL_TYPES[canon[0]]


class RaggedRow(ValueError):
    """A row whose width disagrees with the columns it arrived with.

    ``ExecResult`` does not check that every row is as wide as its column list — that is
    convention, not a validated invariant — so a comparison can be handed a short row. It is
    raised as this module's own type so the caller can report the case as an error: an
    ``IndexError`` escaping a projection says nothing about which side was malformed.
    """


# Why an unreadable statement is read as ORDERED. The permissive reading is the dangerous one:
# assuming unordered would silently stop checking an ordering the author asked for, and every case
# whose statement did not parse would quietly pass a weaker test than the one it declares. A
# visible false failure is recoverable; a silent weakening is not.
_NO_STATEMENT = "no statement was available to read, so an ordering was assumed"
_UNPARSED = "the statement could not be parsed as SQL, so an ordering was assumed"
_UNREADABLE = "the statement could not be read, so an ordering was assumed"
_NOT_ONE_QUERY = (
    "the statement is not a single query whose ordering could be read, so an ordering was assumed"
)


def has_top_level_order_by(
    sql: Optional[str], *, dialect: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """Whether `sql` orders the rows it returns, and why the answer had to be assumed if it was.

    Read off the top-level node's own `order` argument, NOT with a search for an `Order` anywhere
    in the tree: a subquery's ORDER BY, a CTE's, an `OVER (ORDER BY …)` and an `array_agg(x ORDER
    BY x)` all order something other than the result, and a search finds every one of them. Asking
    the top node keeps the union cases right in both directions too — an ORDER BY after a UNION
    hangs off the `Union` node and is a total order, one inside a single arm is not.

    The dialect is threaded through because a generic parse does not merely lose detail on a
    backtick- or bracket-quoting engine: it raises, and the case would fall to the assumed note.

    Only a query node can carry a top-level order, and plenty of statements do not parse to one:
    a trailing comment or a second statement wraps the whole thing in a `Block`, and a construct
    sqlglot has no grammar for (EXPLAIN, say) falls back to a `Command` — with a WARNING rather
    than an error, so the unparsed net below never fires for it. Asking `args['order']` of a node
    that has no such argument always answers None, so every one of those would read as cleanly
    unordered; they take the assumed-ordered path instead.
    """
    if sql is None or not sql.strip():
        return True, _NO_STATEMENT
    try:
        # ErrorLevel.RAISE as the enum, never the string: sqlglot compares the level against enum
        # members, so a string matches no branch, every error is dropped and the tree is silently
        # truncated — which here would read a broken statement as cleanly unordered.
        tree = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except SqlglotError:
        # ParseError and TokenError both derive from this; a `None` sql raises TypeError instead,
        # which is why it is guarded above rather than caught here.
        return True, _UNPARSED
    except Exception:
        # The rest of what a parse can do, and none of it is a SqlglotError: an unknown dialect
        # raises ValueError, and a deeply nested statement a RecursionError. This function is
        # called outside the scoring call's own totality net, so anything escaping here escapes
        # that call's "never raises" contract too.
        return True, _UNREADABLE
    # A parenthesised statement parses to a `Subquery` wrapper. The ORDER BY sits on the node
    # inside it, or on the wrapper when it was written outside the parentheses — both order the
    # rows the caller receives, so the wrapper is asked before it is unwrapped.
    while isinstance(tree, exp.Subquery):
        if tree.args.get("order") is not None:
            return True, None
        tree = tree.this
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        return True, _NOT_ONE_QUERY
    return tree.args.get("order") is not None, None


def _sort_key(canon: tuple[str, Any]) -> tuple[str, str]:
    """Order canonical cells for an unordered comparison — never the raw values.

    Sorting raw cells raises: a naive datetime does not compare with an aware one, and a Decimal
    does not compare with a string. The tag leads so the type classes never interleave, and the
    value is ordered as its text, which is injective within a tag — two cells share this key only
    when they are the same cell, so the sort is total and the result deterministic.
    """
    return (canon[0], str(canon[1]))


def _canonical_rows(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], *, quantize: bool
) -> list[tuple[tuple[str, Any], ...]]:
    """Every row canonicalised, after checking it is as wide as its columns."""
    canonical = []
    for row in rows:
        if len(row) != len(columns):
            raise RaggedRow(f"a row of {len(row)} cells arrived with {len(columns)} columns")
        canonical.append(canonical_row(row, quantize=quantize))
    return canonical


def _vectors(
    canonical: Sequence[tuple[tuple[str, Any], ...]], width: int, *, ordered: bool
) -> list[tuple[tuple[str, Any], ...]]:
    """One comparable vector per column of canonical rows: its cells in row order, or sorted when
    order is not part of the answer."""
    vectors = []
    for index in range(width):
        cells = [row[index] for row in canonical]
        if not ordered:
            cells.sort(key=_sort_key)
        vectors.append(tuple(cells))
    return vectors


def _column_vectors(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
    quantize: bool,
) -> list[tuple[tuple[str, Any], ...]]:
    """One comparable vector per column: its cells in row order, or sorted when order is not part
    of the answer."""
    canonical = _canonical_rows(columns, rows, quantize=quantize)
    return _vectors(canonical, len(columns), ordered=ordered)


class ColumnPairing(NamedTuple):
    """How the golden columns paired with the generated ones, and how far each pair agrees.

    `agreement` is per golden index: the share of rows on which the pair's cells are equal, 1.0 for
    a pair the exact stage made. It is what lets a reader say "9 of 10 rows agree on this column"
    where the old all-or-nothing pairing could only say the column was missing.
    """

    pairing: dict[int, int]
    unmatched: tuple[str, ...]
    agreement: dict[int, float]


def _folded_name(name: str) -> str:
    """A column name as two statements would spell the same column: lowercase, the qualifier off, so
    `o.Total` and `total` are one name."""
    return name.rsplit(".", 1)[-1].strip().lower()


def _agreement(
    left: tuple[tuple[str, Any], ...], right: tuple[tuple[str, Any], ...], ordered: bool
) -> float:
    """The share of rows on which two column vectors agree: position by position when the order is
    part of the answer, as a multiset when it is not. The vectors are the same length here."""
    if not left:
        return 0.0
    if ordered:
        overlap = sum(1 for a, b in zip(left, right) if a == b)
    else:
        overlap = sum((Counter(left) & Counter(right)).values())
    return overlap / len(left)


# A best-effort pair needs more than half the rows to agree. Below that the two columns share a few
# values by coincidence (an id and a count on a short result), and pairing them would put the wrong
# column's difference on the card. Strict, so a two-row result agreeing on one row does not pair.
_MAJORITY = 0.5


def _telling(vector: tuple[tuple[str, Any], ...], ordered: bool) -> bool:
    """Whether agreeing with this column on most rows is evidence of being the same column.

    When the order counts, not when one value fills more than half the rows: any column that
    repeats that value clears the majority bar by coincidence. A flag that is N on every row agrees
    with every other mostly-N flag, and pairing on that put one column's difference on another
    column's name.

    When the order does not count, agreement is the overlap of two multisets, and two columns with
    a similar spread overlap on most rows whatever they hold: a flag True on five rows of ten
    agrees on nine with an unrelated flag True on six. So there most of the values must be
    distinct, which makes a shared value a particular one rather than a common one.

    One row is never telling. Agreeing on it means the whole vector is equal, which stage one
    decides.
    """
    if len(vector) < 2:
        return False
    if ordered:
        return Counter(vector).most_common(1)[0][1] / len(vector) <= _MAJORITY
    return len(set(vector)) / len(vector) > _MAJORITY


def _pair_equal_vectors(
    golden_columns: Sequence[str],
    golden_vectors: Sequence[tuple[tuple[str, Any], ...]],
    generated_columns: Sequence[str],
    generated_vectors: Sequence[tuple[tuple[str, Any], ...]],
    *,
    names_first: bool,
) -> dict[int, int]:
    """Stage one of `pair_columns`: each golden column with an unclaimed generated column whose
    vector is equal to its own.

    Without `names_first`, each golden column in turn takes the first partner left. With it, every
    golden column first takes a partner of its own name, and only then do the rest take the first
    partner left. Two passes and not one, so a column with no namesake cannot take the partner that
    a later column's name was waiting for.
    """
    unclaimed: dict[tuple[tuple[str, Any], ...], list[int]] = {}
    for index, vector in enumerate(generated_vectors):
        unclaimed.setdefault(vector, []).append(index)
    pairing: dict[int, int] = {}
    if names_first:
        for index, vector in enumerate(golden_vectors):
            wanted = _folded_name(golden_columns[index])
            partners = unclaimed.get(vector, [])
            named = [i for i in partners if _folded_name(generated_columns[i]) == wanted]
            if named:
                partners.remove(named[0])
                pairing[index] = named[0]
    for index, vector in enumerate(golden_vectors):
        partners = unclaimed.get(vector)
        if index not in pairing and partners:
            pairing[index] = partners.pop(0)
    return pairing


# The most assignments of equal columns an unordered pairing tries, counted over all the classes it
# searches together: 720 is every order of six columns. A class that would take the count past it
# keeps the pairing `_line_up_equal_columns` chose before its search, so a result with many
# interchangeable columns costs a bounded search and never a runaway one.
_ASSIGNMENT_CAP = 720
# The cap counts assignments and not the options at one slot, which are what the first descent —
# the part the row budget never stops — reads the result for. A class can be wide and shallow: one
# golden column against 720 equal generated ones is 720 assignments but only one column deep, and
# the descent then reads the result once per option. That is bounded by the columns, because an
# option IS a generated column, and a comparison already builds one value vector per column. So the
# widest class a result can hold costs the descent about what reading that result once more costs.
# Measured on exactly that class, one golden flag against 720 equal generated ones: at 20,000 rows
# the search took 1.0s of a 17.7s comparison, at 60,000 rows 6.7s of 55.4s. A separate cap on the
# width was tried and dropped: it saved that tenth and lost the right answer the search had found.


def _picker(indices: Sequence[int]) -> Callable[[Sequence[Any]], tuple[Any, ...]]:
    """A function that takes the cells at `indices` out of a row, always as a tuple. `itemgetter`
    alone returns a bare cell for one index and cannot take none."""
    if not indices:
        return lambda row: ()
    if len(indices) == 1:
        index = indices[0]
        return lambda row: (row[index],)
    return itemgetter(*indices)


def _overlap(
    golden_rows: Sequence[tuple[tuple[str, Any], ...]],
    generated_rows: Sequence[tuple[tuple[str, Any], ...]],
    pairing: dict[int, int],
) -> int:
    """`compare_rows`' unordered overlap, over rows that are already canonical."""
    order = sorted(pairing)
    golden = Counter(map(_picker(order), golden_rows))
    generated = Counter(map(_picker([pairing[index] for index in order]), generated_rows))
    return sum((golden & generated).values())


def _equal_classes(
    golden_vectors: Sequence[tuple[tuple[str, Any], ...]],
    generated_vectors: Sequence[tuple[tuple[str, Any], ...]],
) -> list[tuple[int, list[int], list[int]]]:
    """Every class of columns with equal vectors that can pair in more than one way: how many ways,
    then its golden and its generated columns. Fewest ways first."""
    members: dict[tuple[tuple[str, Any], ...], tuple[list[int], list[int]]] = {}
    for index, vector in enumerate(golden_vectors):
        members.setdefault(vector, ([], []))[0].append(index)
    for index, vector in enumerate(generated_vectors):
        if vector in members:
            members[vector][1].append(index)
    classes = []
    for golden, generated in members.values():
        fewer, more = sorted((len(golden), len(generated)))
        ways = math.perm(more, fewer)
        if ways > 1:
            classes.append((ways, golden, generated))
    return sorted(classes)


# How many rows the search may read, as a multiple of the distinct rows one step reads, and the
# floor that multiple never goes below.
#
# The budget is a multiple and not a constant because what it buys has to be the same at every size.
# A flat two million bought a hundred reads of a twenty-thousand-row result but only twenty of a
# hundred-thousand-row one, and a hundred is about what the search needs when nothing outside the
# class of equal columns holds the rows together. Every option at the first slot then lines up every
# row, so the descent picks on column order alone, and only backtracking finds the answer. Measured
# on six equal columns over a hundred thousand rows, a right answer with every column renamed and
# rotated: at twenty reads 0.0 in 7.1s, at a hundred 1.0 in 8.0s. The same answer over fifty
# thousand rows went from 4e-05 in 3.3s to 1.0 in 3.5s, and over twenty thousand nothing moved,
# because there the floor below is still what decides.
#
# The floor keeps a small result's search exactly as generous as it was: two million covers every
# assignment of six columns over some six hundred distinct rows even when nothing is pruned.
#
# What bounds the worst case is that both terms are read counts, and each read is one pass over the
# distinct rows. The first descent, which the budget never stops, reads each result a few dozen
# times (27 times for six equal columns, and once per option for a wide one). The budget adds
# `_SEARCH_ROW_FACTOR` reads on top. For scale, the whole tree for six equal columns is 3,193 reads
# — that is the shape of the tree, counted, not a measurement — and a hard case, two results with
# equal columns whose rows were drawn independently so almost nothing prunes, walked 326 of them. So
# one comparison costs a bounded number of passes over the result, and grows with the rows rather
# than with the ways to pair them.
#
# Past the budget the search keeps the best assignment it has found, which never lines up fewer rows
# than the pairing it started from but can fall short of the best. What usually gets there is a
# large result that lines up badly under every assignment, so pruning has no good assignment to
# measure the others against.
_SEARCH_ROW_FACTOR = 100
_SEARCH_ROW_FLOOR = 2_000_000


class _BudgetSpent(Exception):
    """The search has read its budget of rows after its first descent, and keeps what it has
    found."""


class _Side(NamedTuple):
    """One result's distinct rows during the search, aligned: each row's key over the columns paired
    so far, its searched cells as small integers, and how many times the row occurs."""

    keys: list[int]
    cells: list[tuple[int, ...]]
    counts: list[int]


class _Partial(NamedTuple):
    """A partial assignment during the search: both results keyed on the columns it pairs, and how
    many golden rows carry each key."""

    golden: _Side
    golden_totals: list[int]
    generated: _Side


class _GoldenKeyed(NamedTuple):
    """The golden side keyed on one more column: the new side, the table from an old key and a cell
    to a new key, and how many golden rows carry each new key."""

    side: _Side
    table: dict[int, int]
    totals: list[int]


def _extend_golden(side: _Side, position: int, width: int) -> _GoldenKeyed:
    """The golden side keyed on one more column."""
    table: dict[int, int] = {}
    keys = [
        table.setdefault(key * width + cells[position], len(table))
        for key, cells in zip(side.keys, side.cells)
    ]
    totals = [0] * len(table)
    for key, count in zip(keys, side.counts):
        totals[key] += count
    return _GoldenKeyed(side._replace(keys=keys), table, totals)


def _extend_generated(side: _Side, position: int, width: int, table: dict[int, int]) -> _Side:
    """The generated side keyed on one more column, through the golden side's table. A row whose key
    no golden row carries can never line up again, so it is dropped, and later steps skip it."""
    keys, cells, counts = [], [], []
    for key, row, count in zip(side.keys, side.cells, side.counts):
        extended = table.get(key * width + row[position])
        if extended is not None:
            keys.append(extended)
            cells.append(row)
            counts.append(count)
    return _Side(keys, cells, counts)


def _lined_up(generated: _Side, golden_totals: list[int]) -> int:
    """How many rows the two sides share, as multisets, over the columns keyed so far."""
    tally: dict[int, int] = {}
    for key, count in zip(generated.keys, generated.counts):
        tally[key] = tally.get(key, 0) + count
    return sum(min(count, golden_totals[key]) for key, count in tally.items())


def _distinct_row_steps(
    classes: Sequence[tuple[list[int], list[int]]],
    fixed: Sequence[int],
    fixed_partners: Sequence[int],
    golden_rows: Sequence[tuple[tuple[str, Any], ...]],
    generated_rows: Sequence[tuple[tuple[str, Any], ...]],
    spend: Callable[[int], None],
) -> tuple[
    Callable[[_Partial, int, int, dict[int, _GoldenKeyed]], tuple[_Partial, int]], _Partial, int
]:
    """The search's step, its starting state, and how many rows that state lines up.

    A step pairs one more column: it takes a partial assignment, a golden column and its partner,
    and the golden sides already keyed from that assignment, and returns the child and how many
    rows it lines up.

    Each side collapses to its distinct rows with a count, keyed on the fixed columns. The searched
    cells become small integers per class: only columns of one class are ever compared, and they
    all hold the same values. So a step costs one integer lookup per distinct row, and a generated
    row whose key no golden row carries is dropped for good.
    """
    searched_golden = [index for golden, _ in classes for index in golden]
    searched_generated = [index for _, generated in classes for index in generated]
    fixed_keys: dict[tuple[tuple[str, Any], ...], int] = {}
    golden_fixed, golden_searched = _picker(fixed), _picker(searched_golden)
    golden_tally: Counter[tuple[int, tuple[tuple[str, Any], ...]]] = Counter()
    for row in golden_rows:
        key = fixed_keys.setdefault(golden_fixed(row), len(fixed_keys))
        golden_tally[key, golden_searched(row)] += 1
    generated_fixed, generated_searched = _picker(fixed_partners), _picker(searched_generated)
    generated_tally: Counter[tuple[int, tuple[tuple[str, Any], ...]]] = Counter()
    for row in generated_rows:
        key = fixed_keys.get(generated_fixed(row))
        if key is not None:
            generated_tally[key, generated_searched(row)] += 1

    codes: list[dict[tuple[str, Any], int]] = [{} for _ in classes]
    class_of = {index: number for number, (golden, _) in enumerate(classes) for index in golden}

    def code(cell: tuple[str, Any], owner: int) -> int:
        return codes[owner].setdefault(cell, len(codes[owner]))

    def side(tally: Counter[tuple[int, tuple[tuple[str, Any], ...]]], owners: list[int]) -> _Side:
        coded = [tuple(map(code, cells, owners)) for _, cells in tally]
        return _Side([key for key, _ in tally], coded, list(tally.values()))

    golden = side(golden_tally, [class_of[index] for index in searched_golden])
    generated = side(
        generated_tally, [number for number, (_, members) in enumerate(classes) for _ in members]
    )
    widths = [len(table) for table in codes]
    totals = [0] * len(fixed_keys)
    for key, count in zip(golden.keys, golden.counts):
        totals[key] += count
    golden_position = {index: position for position, index in enumerate(searched_golden)}
    generated_position = {index: position for position, index in enumerate(searched_generated)}

    def step(
        state: _Partial, index: int, partner: int, shared: dict[int, _GoldenKeyed]
    ) -> tuple[_Partial, int]:
        width = widths[class_of[index]]
        if index not in shared:
            spend(len(state.golden.keys))
            shared[index] = _extend_golden(state.golden, golden_position[index], width)
        keyed = shared[index]
        spend(len(state.generated.keys))
        generated_next = _extend_generated(
            state.generated, generated_position[partner], width, keyed.table
        )
        child = _Partial(keyed.side, keyed.totals, generated_next)
        return child, _lined_up(generated_next, keyed.totals)

    return step, _Partial(golden, totals, generated), _lined_up(generated, totals)


def _search_classes(
    classes: Sequence[tuple[list[int], list[int]]],
    current: dict[int, int],
    current_overlap: int,
    golden_columns: Sequence[str],
    golden_rows: Sequence[tuple[tuple[str, Any], ...]],
    generated_columns: Sequence[str],
    generated_rows: Sequence[tuple[tuple[str, Any], ...]],
) -> dict[int, int]:
    """The assignment inside `classes` that lines up the most rows, ties going to the one with the
    most same-named pairs and then to `current`. A column outside them keeps its `current` partner.

    Branch and bound, pairing one column of each class's smaller side at a time. Pairing one more
    column can split rows that lined up but never join rows that did not, so the rows a partial
    assignment lines up bound every assignment that completes it, and a branch that cannot beat the
    best so far is dropped. Branches are taken best first, so when some assignment lines up every
    row it is usually reached at once, and every other branch is dropped early.

    The first descent always runs to its end, a complete assignment or a branch that cannot beat
    `current`. Only then can the row budget stop the search.
    """
    golden_names = [_folded_name(name) for name in golden_columns]
    generated_names = [_folded_name(name) for name in generated_columns]
    searched = {index for golden, _ in classes for index in golden}
    fixed = sorted(set(current) - searched)
    fixed_partners = [current[index] for index in fixed]
    rows_left = 0
    descending = True

    def spend(rows: int) -> None:
        nonlocal rows_left
        rows_left -= rows
        if rows_left < 0 and not descending:
            raise _BudgetSpent

    step, start, start_overlap = _distinct_row_steps(
        classes, fixed, fixed_partners, golden_rows, generated_rows, spend
    )
    # The budget is set here rather than above because it counts reads of the result, and what one
    # read costs is known only once both sides have collapsed to their distinct rows. Nothing is
    # spent before this point.
    rows_left = max(
        _SEARCH_ROW_FLOOR,
        _SEARCH_ROW_FACTOR * max(len(start.golden.keys), len(start.generated.keys)),
    )

    # A slot is a column of a class's smaller side, and its options are the other side's columns.
    slots: list[tuple[int, int, bool]] = []
    options: list[list[int]] = []
    for number, (golden, generated) in enumerate(classes):
        slot_is_golden = len(golden) <= len(generated)
        own, other = (golden, generated) if slot_is_golden else (generated, golden)
        slots.extend((number, index, slot_is_golden) for index in own)
        options.append(other)

    def name_ceiling(depth: int, used: set[tuple[int, int]]) -> int:
        """The most same-named pairs the slots from `depth` on can still make."""
        ceiling = 0
        for number in {slot[0] for slot in slots[depth:]}:
            slot_is_golden = len(classes[number][0]) <= len(classes[number][1])
            own, other = golden_names, generated_names
            if not slot_is_golden:
                own, other = other, own
            wanted = Counter(own[slot[1]] for slot in slots[depth:] if slot[0] == number)
            free = Counter(other[i] for i in options[number] if (number, i) not in used)
            ceiling += sum((wanted & free).values())
        return ceiling

    best = {index: current[index] for index in sorted(searched) if index in current}
    best_overlap = current_overlap
    best_names = sum(golden_names[g] == generated_names[p] for g, p in best.items())

    def visit(
        depth: int,
        state: _Partial,
        overlap: int,
        names: int,
        used: set[tuple[int, int]],
        chosen: dict[int, int],
    ) -> None:
        nonlocal best, best_overlap, best_names, descending
        if (overlap, names + name_ceiling(depth, used)) <= (best_overlap, best_names):
            descending = False
            return
        if depth == len(slots):
            best, best_overlap, best_names = dict(chosen), overlap, names
            descending = False
            return
        number, column, slot_is_golden = slots[depth]
        shared: dict[int, _GoldenKeyed] = {}
        children = []
        for order, option in enumerate(options[number]):
            if (number, option) in used:
                continue
            index, partner = (column, option) if slot_is_golden else (option, column)
            child, lined = step(state, index, partner, shared)
            other_name = golden_names[index] != generated_names[partner]
            children.append((-lined, other_name, order, option, index, partner, child))
        # Most rows first, then a same-named pair, then column order. `order` is unique, so the
        # sort never reaches the states.
        children.sort(key=lambda entry: entry[:3])
        for negative_lined, other_name, _, option, index, partner, child in children:
            used.add((number, option))
            chosen[index] = partner
            visit(depth + 1, child, -negative_lined, names + (not other_name), used, chosen)
            used.discard((number, option))
            del chosen[index]

    try:
        visit(0, start, start_overlap, 0, set(), {})
    except _BudgetSpent:
        pass
    pairing = {index: current[index] for index in fixed}
    pairing.update(best)
    return dict(sorted(pairing.items()))


def _line_up_equal_columns(
    golden_columns: Sequence[str],
    golden_vectors: Sequence[tuple[tuple[str, Any], ...]],
    golden_rows: Sequence[tuple[tuple[str, Any], ...]],
    generated_columns: Sequence[str],
    generated_vectors: Sequence[tuple[tuple[str, Any], ...]],
    generated_rows: Sequence[tuple[tuple[str, Any], ...]],
) -> dict[int, int]:
    """Stage one when the order does not count: of the ways to pair columns with equal vectors, one
    that lines up the most rows.

    Tried cheapest first. With no class of equal columns that can pair in two ways there is nothing
    to choose. The pairing by names is kept when it lines up every row, since nothing lines up more
    and nothing pairs more names. Otherwise the plain in-order pairing replaces it when it lines up
    more rows, which was the whole rule before the search. Then the classes are searched, fewest
    ways first while the ways multiply to at most `_ASSIGNMENT_CAP`, and a class past that keeps
    the pairing chosen so far. The search keeps that pairing on a tie, so its result never lines up
    fewer rows.
    """
    columns = (golden_columns, golden_vectors, generated_columns, generated_vectors)
    pairing = _pair_equal_vectors(*columns, names_first=True)
    classes = _equal_classes(golden_vectors, generated_vectors)
    if not classes:
        return pairing
    overlap = _overlap(golden_rows, generated_rows, pairing)
    if overlap == min(len(golden_rows), len(generated_rows)):
        return pairing
    in_order = _pair_equal_vectors(*columns, names_first=False)
    if in_order != pairing:
        in_order_overlap = _overlap(golden_rows, generated_rows, in_order)
        if in_order_overlap > overlap:
            pairing, overlap = in_order, in_order_overlap
    searched, ways = [], 1
    for class_ways, golden, generated in classes:
        if ways * class_ways <= _ASSIGNMENT_CAP:
            searched.append((golden, generated))
            ways *= class_ways
    if not searched:
        return pairing
    return _search_classes(
        searched, pairing, overlap, golden_columns, golden_rows, generated_columns, generated_rows
    )


def pair_columns(
    golden_columns: Sequence[str],
    golden_rows: Sequence[Sequence[Any]],
    generated_columns: Sequence[str],
    generated_rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
    quantize: bool = False,
) -> ColumnPairing:
    """Pair golden columns with generated columns, by values first and then by best effort.

    Stage one pairs on whole value-vector equality, whatever the name or position: a generated
    statement that aliases the total and selects it second still answered the question. It is
    greedy and deliberately NOT a maximum-matching algorithm: equality is transitive, so the
    candidate sets are equivalence classes, and taking any unclaimed partner can never strand a
    later column that had an option of its own.

    Inside a class the partners are interchangeable for the pairing, but not always for the rows.
    When the order does not count, the vectors are sorted. Two different flags that are each Y on
    half the rows are then equal vectors, and pairing each with the other's partner misaligns every
    row, so an identical answer scored as a mismatch. A name alone cannot settle it either: a
    statement can swap two labels, alias one of two columns that share a label, or rename every
    column. So when the order does not count, the assignments inside the classes are searched for
    the one that lines up the most rows, ties going to the one that pairs the most same-named
    columns (see `_line_up_equal_columns`). The search is bounded in two ways. Past
    `_ASSIGNMENT_CAP` assignments a class keeps the pairing by names or in order, whichever lines up
    more rows. And once its first descent has ended, the search reads a budget of rows
    (`_SEARCH_ROW_FACTOR` reads of the result, and at least `_SEARCH_ROW_FLOOR` rows) and then keeps
    the best assignment found so far. When the order counts
    there is nothing to search: equal vectors are equal row for row, so every choice inside a class
    lines up the same rows, and a golden column takes a partner of its own name first (see
    `_pair_equal_vectors`).

    Stage two is for what stage one left: one differing cell would otherwise unpair a column that
    is plainly there, and the score would read "no generated column carries the values of total"
    for a column agreeing on nine rows of ten. Over the golden columns still unmatched, in order,
    and the generated columns still unclaimed: a candidate with the same folded name pairs at any
    agreement (the name says it is the same column; the agreement says how much of it differs).
    Without a namesake, a golden column pairs by values only when its values are telling (see
    `_telling`); a flag that is N on most rows is not. Then the candidate with the highest share of
    agreeing rows pairs when that share is above one half, ties going to generated order. A golden
    column with no such partner is reported unmatched.
    """
    golden_canonical = _canonical_rows(golden_columns, golden_rows, quantize=quantize)
    generated_canonical = _canonical_rows(generated_columns, generated_rows, quantize=quantize)
    golden_vectors = _vectors(golden_canonical, len(golden_columns), ordered=ordered)
    generated_vectors = _vectors(generated_canonical, len(generated_columns), ordered=ordered)
    if ordered:
        pairing = _pair_equal_vectors(
            golden_columns, golden_vectors, generated_columns, generated_vectors, names_first=True
        )
    else:
        pairing = _line_up_equal_columns(
            golden_columns, golden_vectors, golden_canonical,
            generated_columns, generated_vectors, generated_canonical,
        )
    agreement: dict[int, float] = dict.fromkeys(pairing, 1.0)

    claimed = set(pairing.values())
    for index, vector in enumerate(golden_vectors):
        if index in pairing:
            continue
        candidates = [i for i in range(len(generated_vectors)) if i not in claimed]
        if not candidates:
            continue
        wanted = _folded_name(golden_columns[index])
        by_name = [i for i in candidates if _folded_name(generated_columns[i]) == wanted]
        if by_name:
            chosen = by_name[0]
        elif not _telling(vector, ordered):
            # Named nowhere and not distinctive enough to find by its values: reported unmatched,
            # which is what it is, rather than paired with a column that agrees with it by chance.
            continue
        else:
            shares = [(_agreement(vector, generated_vectors[i], ordered), -i) for i in candidates]
            best_share, negative_index = max(shares)
            if best_share <= _MAJORITY:
                continue
            chosen = -negative_index
        pairing[index] = chosen
        claimed.add(chosen)
        agreement[index] = _agreement(vector, generated_vectors[chosen], ordered)

    unmatched = tuple(name for index, name in enumerate(golden_columns) if index not in pairing)
    return ColumnPairing(pairing, unmatched, agreement)


def match_columns(
    golden_columns: Sequence[str],
    golden_rows: Sequence[Sequence[Any]],
    generated_columns: Sequence[str],
    generated_rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
    quantize: bool = False,
) -> tuple[dict[int, int], tuple[str, ...]]:
    """`pair_columns` as the golden-index → generated-index pairing and the golden column names that
    found no partner, the shape every caller and pin has read since Slice 1."""
    paired = pair_columns(
        golden_columns, golden_rows, generated_columns, generated_rows,
        ordered=ordered, quantize=quantize,
    )
    return paired.pairing, paired.unmatched


def _project(
    row: Sequence[Any], indices: Sequence[int], quantize: bool
) -> tuple[tuple[str, Any], ...]:
    """The paired cells of one row, canonicalised, in golden column order."""
    try:
        cells = [row[index] for index in indices]
    except IndexError:
        raise RaggedRow(f"a row of {len(row)} cells is too short for the matched columns") from None
    return canonical_row(cells, quantize=quantize)


def compare_rows(
    golden_rows: Sequence[Sequence[Any]],
    generated_rows: Sequence[Sequence[Any]],
    pairing: dict[int, int],
    *,
    ordered: bool,
    quantize: bool = False,
) -> tuple[int, int, int]:
    """How far the two sides agree over their paired columns, as (overlap, golden, generated).

    Ordered results are compared position by position, because the ordering is part of what the
    author asked for. Unordered ones are compared as multisets and not as sets: a row returned
    twice where the answer key has it once is a different answer, usually a join that fanned out,
    and a set comparison is exactly the one that hides it.
    """
    golden_indices = sorted(pairing)
    generated_indices = [pairing[index] for index in golden_indices]
    golden = [_project(row, golden_indices, quantize) for row in golden_rows]
    generated = [_project(row, generated_indices, quantize) for row in generated_rows]
    if ordered:
        overlap = sum(1 for left, right in zip(golden, generated) if left == right)
    else:
        overlap = sum((Counter(golden) & Counter(generated)).values())
    return overlap, len(golden_rows), len(generated_rows)


@dataclass(frozen=True)
class ItemScore:
    """One item's verdict: how it was judged and how far the two sides agreed.

    A returned value and never an authored file, so a frozen dataclass like ``ExecResult`` and
    ``Envelope`` rather than a pydantic model — ``golden.py``'s models are the other case, parsing
    what a person wrote down.

    ``accuracy`` is None exactly when nothing was scored. 0.0 is a score an item can legitimately
    earn — a comparison that ran and found no agreement — and collapsing the two would report a
    wrong answer and an unrunnable case as the same thing.

    An item passes at exactly 1.0, and there is no second threshold: the match level already says
    how loose the comparison is, and a fractional pass mark on top of it would loosen every level
    again, invisibly.
    """

    status: Literal["scored", "unscored", "error"]
    accuracy: Optional[float]
    reason: str
    unmatched_golden_columns: tuple[str, ...] = ()
    # Which golden column paired with which generated column, by VALUES, and which generated columns
    # paired with none. A renamed column is the same column here; a reader that compared names would
    # call it missing. Additive; empty where no column-level comparison ran.
    column_pairs: tuple[tuple[str, str], ...] = ()
    unmatched_generated_columns: tuple[str, ...] = ()
    golden_row_count: Optional[int] = None
    generated_row_count: Optional[int] = None
    order_sensitive: Optional[bool] = None
    notes: tuple[str, ...] = ()
    # Beside `column_pairs`, pair for pair: the share of rows on which that pair agrees (1.0 for a
    # pair made on whole-vector equality). And the share of rows on which EVERY paired column agrees,
    # computed even when a golden column has no partner, so a reader can tell "identical on the
    # paired columns, one column missing" from "the paired columns disagree too". None when nothing
    # paired or the row counts differ. Additive, like the pairs.
    column_agreement: tuple[float, ...] = ()
    paired_row_share: Optional[float] = None


class _Verdict(NamedTuple):
    """What one level decided, before the counts and ordering every score carries are attached."""

    status: Literal["scored", "unscored", "error"]
    accuracy: Optional[float]
    reason: str
    unmatched: tuple[str, ...] = ()


# Deliberately NOT a pass. Two empty results agree about nothing: no value was compared, and
# scoring that as a full match is how a statement that returns nothing at all gates like a right
# one. It applies only to the levels that consult the answer key — `nonempty` and `bounded` have
# no answer key by design, so an empty golden side is their NORMAL shape, and dropping the pair
# there would excuse the returned-nothing failure those two levels exist to catch.
_BOTH_EMPTY = "both result sets are empty, so the comparison would check no value"
_KEYED_LEVELS = ("exact", "values", "shape")


def _accuracy(overlap: int, row_count: int) -> float:
    """The share of the rows the two sides agreed on.

    One denominator, because a row-count difference is decided before any column is paired and the
    two sides are the same height by the time this is reached. The value is the raw share and is
    NOT rounded: an item passes at exactly 1.0, and rounding would hand the pass mark to a near
    miss — 4002 of 4004 rows is 0.99950…, which rounds up. Three-decimal presentation is the
    report renderer's business, not the score's.
    """
    return overlap / row_count


def _score_values(
    golden: ExecResult,
    generated: ExecResult,
    *,
    ordered: bool,
    loose: bool,
) -> _Verdict:
    """Pair the columns by value, then score how far the rows agree over the pairing.

    `loose` is the `values` level: it forgives a floating-point tail and an extra column the
    question did not ask for. One flag rather than two, because no level asks for one and not the
    other.
    """
    if len(golden.rows) != len(generated.rows):
        # Before any pairing, because a column vector's LENGTH is the row count: when the counts
        # differ no column can pair, and reporting that through the unmatched-column branch below
        # tells a reader a column is absent when every one of them is present.
        return _Verdict(
            "scored",
            0.0,
            f"the answer key has {len(golden.rows)} rows and the generated result has "
            f"{len(generated.rows)}",
        )
    pairing, unmatched = match_columns(
        golden.columns,
        golden.rows,
        generated.columns,
        generated.rows,
        ordered=ordered,
        quantize=loose,
    )
    if unmatched:
        # Not a partial answer. The question asked for that column and the result does not carry
        # it, so how well the remaining columns overlap says nothing about whether it is right.
        return _Verdict(
            "scored",
            0.0,
            "no generated column carries the values of: " + ", ".join(unmatched),
            unmatched,
        )
    if not loose and len(generated.columns) > len(golden.columns):
        extra = len(generated.columns) - len(golden.columns)
        return _Verdict(
            "scored", 0.0, f"the generated result carries {extra} column(s) the answer key does not"
        )
    overlap, golden_count, generated_count = compare_rows(
        golden.rows, generated.rows, pairing, ordered=ordered, quantize=loose
    )
    accuracy = _accuracy(overlap, golden_count)
    if accuracy == 1.0:
        return _Verdict("scored", 1.0, "")
    return _Verdict(
        "scored",
        accuracy,
        f"{overlap} of the answer key's {golden_count} rows matched, "
        f"and the generated statement returned {generated_count} rows",
    )


def _column_types(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[frozenset[str]]:
    """The coarse types present in each column. Reuses the vector build so a ragged row is caught
    here too, and orders the cells because a set of types does not care."""
    vectors = _column_vectors(columns, rows, ordered=True, quantize=False)
    return [
        frozenset(tag for tag in (cell_type(cell) for cell in vector) if tag is not None)
        for vector in vectors
    ]


def _score_shape(golden: ExecResult, generated: ExecResult) -> _Verdict:
    """Counts and the coarse type lattice, never a value.

    Columns are compared POSITIONALLY here, which the value levels never do — with the values off
    limits there is nothing to pair them on, and pairing by type would call any two text columns
    interchangeable.
    """
    if len(golden.rows) != len(generated.rows):
        return _Verdict(
            "scored",
            0.0,
            f"the answer key has {len(golden.rows)} rows and the generated result has "
            f"{len(generated.rows)}",
        )
    if len(golden.columns) != len(generated.columns):
        return _Verdict(
            "scored",
            0.0,
            f"the answer key has {len(golden.columns)} columns and the generated result has "
            f"{len(generated.columns)}",
        )
    pairs = zip(
        golden.columns,
        _column_types(golden.columns, golden.rows),
        _column_types(generated.columns, generated.rows),
    )
    for name, golden_types, generated_types in pairs:
        # An empty set is an all-NULL column, or a result with no rows at all, and it constrains
        # nothing: a NULL is the absence of a value, not a value of some type.
        if golden_types and generated_types and golden_types != generated_types:
            return _Verdict(
                "scored", 0.0, f"column {name!r} does not carry the type the answer key does"
            )
    return _Verdict("scored", 1.0, "")


def _score_bounds(generated: ExecResult, bounds: Optional[GoldenBounds]) -> _Verdict:
    """Judge the generated result against the authored band. Row bounds count the GENERATED rows —
    a bounded item has no answer key to count against, which is why it is bounded."""
    if bounds is None:
        return _Verdict(
            "error",
            None,
            "a bounded item is judged against a band, and no bounds were given to judge it with",
        )
    row_band = bounds.min_rows is not None or bounds.max_rows is not None
    if bounds.min_value is not None or bounds.max_value is not None:
        if not generated.rows:
            # An empty result is a WRONG answer here and not an unjudgeable one: catching a
            # statement that returned nothing is half of why a band gets authored at all.
            return _Verdict("scored", 0.0, "the generated statement returned no rows")
        number = _single_number(generated)
        if number is None:
            # The value band asks about one number and there is not one to ask about. When the
            # author also wrote a row band, that band judges this result perfectly well, so it is
            # preferred over reporting an item nobody can ever judge; without one, an error is all
            # that is left — the case cannot be decided, and calling it wrong would blame the run.
            if not row_band:
                return _Verdict(
                    "error",
                    None,
                    "a value band judges a single numeric cell, and the generated result is not "
                    "one",
                )
        # Both edges are INCLUSIVE: an author writing `max_value: 10` means ten is an acceptable
        # answer. Compared through str, because Decimal(float) expands the binary float in full
        # and would put an edge of 0.1 a hair away from where the author wrote it.
        elif bounds.min_value is not None and number < Decimal(str(bounds.min_value)):
            return _Verdict("scored", 0.0, "the generated value is below the band")
        elif bounds.max_value is not None and number > Decimal(str(bounds.max_value)):
            return _Verdict("scored", 0.0, "the generated value is above the band")
    count = len(generated.rows)
    if bounds.min_rows is not None and count < bounds.min_rows:
        return _Verdict("scored", 0.0, f"the generated result has {count} rows, below the band")
    if bounds.max_rows is not None and count > bounds.max_rows:
        return _Verdict("scored", 0.0, f"the generated result has {count} rows, above the band")
    return _Verdict("scored", 1.0, "")


def _single_number(generated: ExecResult) -> Optional[Decimal]:
    """The one number a result carries, or None when it does not carry exactly one."""
    if len(generated.rows) != 1 or len(generated.columns) != 1 or len(generated.rows[0]) != 1:
        return None
    tag, value = canonical_cell(generated.rows[0][0])
    # A NaN is tagged apart from `num`, so it never reaches a band comparison that it would answer
    # False to in both directions.
    return value if tag == "num" else None


def _judge(
    golden: ExecResult,
    generated: ExecResult,
    match: MatchLevel,
    bounds: Optional[GoldenBounds],
    ordered: bool,
) -> _Verdict:
    """Dispatch to the level the author asked for, loosening left to right."""
    if match in _KEYED_LEVELS and not golden.rows and not generated.rows:
        return _Verdict("unscored", None, _BOTH_EMPTY)
    if match in ("exact", "values"):
        # `values` forgives a floating-point tail and an extra column the question did not ask
        # for; `exact` forgives neither, which is the only difference between the two.
        return _score_values(golden, generated, ordered=ordered, loose=match == "values")
    if match == "shape":
        return _score_shape(golden, generated)
    if match == "bounded":
        return _score_bounds(generated, bounds)
    if match == "nonempty":
        if generated.rows:
            return _Verdict("scored", 1.0, "")
        return _Verdict("scored", 0.0, "the generated statement returned no rows")
    return _Verdict("error", None, f"{match!r} is not a match level this comparison knows")


def _pairing_facts(
    golden: ExecResult, generated: ExecResult, match: MatchLevel, ordered: bool
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[float, ...], Optional[float]]:
    """(golden column, generated column) pairs, the generated columns left over, each pair's
    agreement and the share of rows every pair agrees on, for the two levels that pair columns at
    all. Never raises; a shape the pairing cannot read reports nothing rather than failing a score
    that already ran."""
    nothing: tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[float, ...], Optional[float]]
    nothing = ((), (), (), None)
    if match not in ("exact", "values") or not golden.rows or not generated.rows:
        return nothing
    if len(golden.rows) != len(generated.rows):
        # Pairing is by value vectors, and two vectors of different length are never equal, so
        # every column would read unpaired: not a fact about the columns, only about the counts,
        # which the score already reports. Nothing is claimed here.
        return nothing
    quantize = match == "values"
    try:
        paired = pair_columns(
            golden.columns, golden.rows, generated.columns, generated.rows,
            ordered=ordered, quantize=quantize,
        )
        share: Optional[float] = None
        if paired.pairing:
            overlap, golden_count, _generated_count = compare_rows(
                golden.rows, generated.rows, paired.pairing, ordered=ordered, quantize=quantize
            )
            share = _accuracy(overlap, golden_count)
    except Exception:
        # The score itself has already reported a ragged or malformed result as an error with a
        # value-free reason; the pairing is a courtesy on top and must never turn that into a raise.
        return nothing
    ordered_pairs = sorted(paired.pairing.items())
    pairs = tuple((golden.columns[g], generated.columns[i]) for g, i in ordered_pairs)
    agreement = tuple(paired.agreement[g] for g, _ in ordered_pairs)
    taken = set(paired.pairing.values())
    extra = tuple(name for i, name in enumerate(generated.columns) if i not in taken)
    return pairs, extra, agreement, share


def compare_result_sets(
    golden: ExecResult,
    generated: ExecResult,
    *,
    match: MatchLevel = "exact",
    golden_sql: Optional[str] = None,
    bounds: Optional[GoldenBounds] = None,
    dialect: Optional[str] = None,
    ordered: Optional[bool] = None,
) -> ItemScore:
    """Score one generated result against its answer key. Never raises.

    `golden_sql` is read for one thing only — whether the author ordered the rows — and the
    generated statement is deliberately not a parameter: the ordering that has to hold is the one
    the ANSWER KEY asked for, so a generated statement that drops the ORDER BY is still judged
    against it rather than excused by it.

    `ordered`, when given, decides instead of the statement. Reconcile passes False: it compares two
    trusted statements whose ordering is a claim of its own, so the rows are a set here and the
    ORDER BY is judged where it is named. The golden run never passes it.
    """
    if ordered is None:
        ordered, note = has_top_level_order_by(golden_sql, dialect=dialect)
    else:
        note = None if ordered else "row order was not compared"
    try:
        verdict = _judge(golden, generated, match, bounds, ordered)
    except RaggedRow as exc:
        # Its message counts cells and names no value, so it can be reported as it stands.
        verdict = _Verdict("error", None, str(exc))
    except Exception as exc:
        # The totality net. The exception TYPE only and never its message: an arbitrary error
        # quotes the value that broke it, and this score travels.
        verdict = _Verdict(
            "error", None, f"the comparison failed with an unexpected {type(exc).__name__}"
        )
    pairs, extra, agreement, share = _pairing_facts(golden, generated, match, ordered)
    return ItemScore(
        status=verdict.status,
        accuracy=verdict.accuracy,
        reason=verdict.reason,
        unmatched_golden_columns=verdict.unmatched,
        column_pairs=pairs,
        unmatched_generated_columns=extra,
        golden_row_count=len(golden.rows),
        generated_row_count=len(generated.rows),
        order_sensitive=ordered,
        notes=(note,) if note else (),
        column_agreement=agreement,
        paired_row_share=share,
    )


# A number as the execute_sql CSV wire spells it. Deliberately narrow: a digit string with a leading
# zero (`007`, `02134`) stays text, because a padded id or a postal code that reads as a number would
# compare equal to its unpadded twin, and a text column that happens to hold digits is text.
_NUMERIC_TEXT = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?$")


def result_from_csv(path: str | Path) -> ExecResult:
    """The execute_sql CSV wire read back into an ``ExecResult``, for a comparison of two files.

    The wire lost every type, so this puts back the two it can without guessing: numeric text
    becomes ``Decimal`` (the shape ``_canonical_number`` normalises), and an empty cell becomes
    ``None``, which is what the wire writes for a NULL. Everything else stays text and is keyed by
    ``_canonical_text`` like any other string. A digit string with a leading zero stays text on
    purpose; see ``_NUMERIC_TEXT``.

    A zero-byte file raises rather than reading as an empty result: the execution tier writes CSV
    only on success, so an empty file is a statement that was refused or failed, and scoring it as
    "zero rows" would turn a failed run into a wrong answer.
    """
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path} is empty; the statement that should have written it did not succeed")

    def cell(value: str):
        if value == "":
            return None
        if _NUMERIC_TEXT.match(value):
            return Decimal(value)
        return value

    rows = list(csv.reader(io.StringIO(text)))
    return ExecResult(columns=rows[0], rows=[tuple(cell(v) for v in row) for row in rows[1:]])


# The scoring call and the value it hands back, and nothing else. The rest of this module is how
# the two are built rather than what a caller is invited to reach for; `MatchLevel` and
# `GoldenBounds` stay out because they belong to `golden`, which is where a caller should take them
# from rather than through here. `result_from_csv` is reached by name by the one CLI verb that
# compares two files, and stays off this list on purpose: it is a reader for one wire, not part of
# what the comparator promises.
__all__ = ["ItemScore", "compare_result_sets"]
