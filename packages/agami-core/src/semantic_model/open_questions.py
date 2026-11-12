"""The questions a model's data can't settle, found while it is built (#486 part 1).

A model built from the database alone guesses wherever a business word has more than one reading
in the data: which date "sales last month" counts by, which statuses "completed orders" means,
whether an amount includes tax. Those guesses used to be found by hand as declared ambiguities
(#473), one wrong answer at a time. This module finds the candidates at build time and checks each
on the data: it runs every reading and keeps a candidate only when the readings' answers differ, so
a person is never asked about a choice that changes nothing.

The patterns, each generic and found from the model alone:

* `date`: a table with two or more business date columns (created / paid / submitted ...);
* `status`: a status column holding two or more values one business word could cover
  ("completed" = paid, shipped or delivered);
* `amount`: two or more amount columns on one table (a subtotal beside a total with tax);
* `join`: two joins from one table to the same key of another (the issued vs the redeemed sale);
* `current`: a versioned table (a current flag or a valid-to date), or a table beside its history;
* `fiscal`: a datasource whose fiscal year doesn't start in January.

Beside them come the questions only a person can answer that the build already knows it couldn't:
coded values whose meaning enrichment left blank, columns it marked unreadable, and look-alike tables
(`orders` beside `orders_old`). These need no probe.

**Bounded.** Every probe is an aggregate over one table: one statement per table for every pattern,
plus one for its date windows, built with the dialect helpers introspection uses. A table above
introspection's overlap-probe size guard is not scanned, and at most `MAX_TABLES` tables are probed.
A column flagged sensitive is never read. Only counts and sums come back; no row value is printed.

**Nothing is declared here.** Each candidate carries the `add` op that would declare it, without a
default or behaviour: the person picks the usual reading, and whether the agent asks or answers and
offers the others, on a card, and `curate.apply` writes it under their sign-off.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any, Callable, Optional

from . import build as B
from . import dialects as D
from . import introspect as I
from .models import Column, Datasource, Table
from .plain_names import plain_words

Runner = Callable[[str], list[dict]]

# At most this many questions are put to a person in one build: three cards of four.
MAX_QUESTIONS = 12
# At most this many tables are probed in one run.
MAX_TABLES = 60
# Two readings "differ" when their answers are at least this far apart, relative to the larger. A
# choice that moves the answer by less is not worth a card: on a live run it was 24 against 23.
MIN_DIFFERENCE = 0.05
# A date column filled on fewer than this share of rows records an event that happens to some rows
# (a refund, a void), not when the row's thing happened, so it is not one of the readings.
_MIN_DATE_FILL = 0.2
_MAX_READINGS = 3

# Stamps of when a row was written or changed, or of a deadline or a notification: not what a
# question about a period means by "when".
_NOT_A_BUSINESS_DATE = re.compile(
    r"(^|_)(updated|modified|changed|deleted|synced|sync|loaded|etl|ingested|imported|refreshed|"
    r"last|expires|expiry|expired|expiration|due|until|emailed|notified|reminded|sys)(_|$)"
    r"|_by$|^(valid|effective)_(to|end|thru|through)$",
    re.IGNORECASE,
)
_VALID_TO = re.compile(r"^(valid|effective)_(to|until|end|thru|through)$", re.IGNORECASE)
_CURRENT_FLAG = re.compile(r"^(is_)?(current|latest)(_(flag|record|row|version))?$", re.IGNORECASE)
_CODE_NAME = re.compile(
    r"(^|_)(code|cd|status|state|stage|phase|type|kind|class|level|tier|grade|"
    r"priority|severity|category|flag|reason)(_|$)",
    re.IGNORECASE,
)
_STATUS_NAME = re.compile(r"(^|_)(status|state|stage|phase)(_|$)", re.IGNORECASE)

# Status values one business word could cover. `_DONE` runs from the most final to the least, so
# the narrow reading of "completed" is the most final value present.
_DONE = (
    "delivered",
    "completed",
    "complete",
    "closed",
    "done",
    "finished",
    "resolved",
    "fulfilled",
    "settled",
    "shipped",
    "paid",
    "captured",
)
_OPEN = (
    "open",
    "pending",
    "active",
    "new",
    "in_progress",
    "inprogress",
    "awaiting",
    "waiting",
    "unpaid",
    "trialing",
    "submitted",
    "on_hold",
    "processing",
)
_NEGATING = frozenset({"not", "non", "no"})

# The words that make an amount column a reading of "how much"; a component (tax, discount, a fee)
# or a per-unit price is part of an amount, not another reading of it.
_AMOUNT_WORDS = frozenset(
    {
        "amount",
        "amt",
        "total",
        "subtotal",
        "gross",
        "net",
        "paid",
        "revenue",
        "sales",
        "value",
        "base",
        "spend",
        "spent",
    }
)
_COMPONENT_WORDS = frozenset(
    {
        "tax",
        "taxes",
        "gst",
        "cgst",
        "sgst",
        "igst",
        "vat",
        "discount",
        "fee",
        "fees",
        "shipping",
        "deposit",
        "refund",
        "tip",
        "tips",
        "unit",
        "price",
        "rate",
        "override",
        "max",
        "min",
        "snapshot",
        "cost",
        "balance",
        "due",
        "credit",
        "debit",
        "commission",
        "adjustment",
        "late",
    }
)

# A table named like another plus one of these words holds the other's past (history) or is a copy
# of it (look-alike). A year suffix (`orders_2023`) is a copy too.
_HISTORY_WORDS = frozenset(
    {
        "history",
        "hist",
        "log",
        "logs",
        "audit",
        "archive",
        "archived",
        "versions",
        "version",
        "snapshot",
        "snapshots",
        "changes",
    }
)
_COPY_WORDS = frozenset(
    {
        "old",
        "new",
        "bak",
        "backup",
        "copy",
        "legacy",
        "prev",
        "previous",
        "v1",
        "v2",
        "v3",
        "tmp",
        "temp",
    }
)


def _day(value: Any) -> Optional[date]:
    """The calendar day an ISO date or timestamp value starts with, or None."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def spread(values: list[Optional[float]]) -> float:
    """How far apart the readings' answers are, as a share of the largest (0 when all are zero)."""
    nums = [abs(v) if v is not None else 0.0 for v in values]
    top = max(nums, default=0.0)
    return (top - min(nums)) / top if top else 0.0


def differ(values: list[Optional[float]]) -> bool:
    """Whether the readings' answers are far enough apart to be worth a person's choice."""
    return spread(values) >= MIN_DIFFERENCE


def _shown(value: Optional[float]) -> str:
    if value is None:
        return "nothing"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _readable(c: Column) -> bool:
    return not c.sensitive and c.review_state != "rejected"


def _is_date(c: Column) -> bool:
    return c.type in ("date", "timestamp") or (c.type == "string" and c.date_format == "iso8601")


def _rows(t: Table) -> Optional[int]:
    return t.performance_hints.estimated_row_count if t.performance_hints else None


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", str(name).lower()) if w]


def _phrase(name: str) -> str:
    """A table's plain words for the middle of a sentence: "Order items" -> "order items", while an
    abbreviation keeps its capitals ("GST invoices")."""
    words = plain_words(name)
    first = words.split(" ", 1)[0]
    return words if first.isupper() and len(first) > 1 else words[:1].lower() + words[1:]


def _run(runner: Runner, sql: str) -> Optional[dict]:
    try:
        rows = runner(sql)
    except Exception:
        return None
    return rows[0] if rows else None


def _op(name: str, when: str, options: list[dict], applies_to: list[str]) -> dict:
    """The `add` op that declares a candidate once a person has chosen; `default` and `behaviour`
    are theirs to add."""
    return {
        "op": "add",
        "kind": "ambiguity",
        "name": name,
        "when": when,
        "options": [{"label": o["label"], "how": o["how"]} for o in options],
        "applies_to": applies_to,
    }


def _reading(label: str, how: str, value: Optional[float]) -> dict:
    return {"label": label, "how": how, "value": value, "shown": _shown(value)}


def _candidate(
    pattern: str, table: Optional[str], question: str, evidence: str, options: list[dict], op: dict
) -> dict:
    return {
        "kind": "ambiguity",
        "pattern": pattern,
        "table": table,
        "question": question,
        "evidence": evidence,
        "options": options,
        "spread": round(spread([o["value"] for o in options]), 4),
        "op": op,
    }


class _Probe:
    """One table's aggregates, gathered into one statement: every pattern adds the expressions it
    needs and reads them back by alias once the statement has run."""

    def __init__(self, dialect: D.Dialect, t: Table, label: str, area: str):
        self.dialect, self.table, self.label, self.area = dialect, t, label, area
        self.fq = dialect.qualified(t.schema_name, t.name)
        self.exprs: list[str] = ["COUNT(*) AS n"]
        self.checks: list[Callable[[dict], tuple[Optional[dict], Optional[str], str]]] = []
        self.row: Optional[dict] = None

    def q(self, column: str) -> str:
        return self.dialect.quote_ident(column)

    def add(self, sql: str) -> str:
        alias = f"a{len(self.exprs)}"
        self.exprs.append(f"{sql} AS {alias}")
        return alias

    def count_where(self, condition: str) -> str:
        return self.add(f"SUM(CASE WHEN {condition} THEN 1 ELSE 0 END)")

    def sql(self) -> str:
        return f"SELECT {', '.join(self.exprs)} FROM {self.fq}"

    def n(self) -> int:
        return int(_number((self.row or {}).get("n")) or 0)

    def value(self, alias: str) -> Optional[float]:
        return _number((self.row or {}).get(alias))


# ---------------------------------------------------------------------------
# Pattern: two or more business dates on one table
# ---------------------------------------------------------------------------


def _date_columns(t: Table) -> list[Column]:
    return [
        c
        for c in t.columns
        if _readable(c) and _is_date(c) and not _NOT_A_BUSINESS_DATE.search(c.name)
    ]


def _date_windows(ends: list[date], today: date) -> list[tuple[str, date, date]]:
    """The last complete month before the data ends, then the twelve months to it."""
    ref = min([*ends, today])
    hi, last = _month_start(ref), _month_start(ref, 1)
    return [
        (f"{last:%B %Y}", last, hi),
        (f"the 12 months to {last:%B %Y}", _month_start(ref, 12), hi),
    ]


def _plan_dates(p: _Probe, runner: Runner, today: date) -> None:
    cols = _date_columns(p.table)
    if len(cols) < 2:
        return
    fills = [p.add(f"COUNT({p.q(c.name)})") for c in cols]
    maxes = [p.add(f"MAX({p.q(c.name)})") for c in cols]

    def check(_row: dict):
        n = p.n()
        keep = [(c, p.value(f) or 0, _day(p.row.get(x))) for c, f, x in zip(cols, fills, maxes)]
        keep = [k for k in keep if n and k[1] >= _MIN_DATE_FILL * n and k[2]]
        keep = sorted(keep, key=lambda k: -k[1])[:_MAX_READINGS]
        if len(keep) < 2:
            return None, "fewer than two date columns are filled on most rows", "date"
        windows = _date_windows([k[2] for k in keep], today)
        lit = p.dialect.date_literal
        exprs = [
            f"SUM(CASE WHEN {p.q(c.name)} >= {lit(lo.isoformat())} AND {p.q(c.name)} < "
            f"{lit(hi.isoformat())} THEN 1 ELSE 0 END) AS w{w}_{i}"
            for w, (_, lo, hi) in enumerate(windows)
            for i, (c, _, _) in enumerate(keep)
        ]
        row = _run(runner, f"SELECT {', '.join(exprs)} FROM {p.fq}")
        if row is None:
            return None, "the date probe failed", "date"
        tw = _phrase(p.table.name)
        for w, (name, _, _) in enumerate(windows):
            options = [
                _reading(
                    plain_words(c.name),
                    f"filter and group {p.label} by {p.label}.{c.name}",
                    _number(row.get(f"w{w}_{i}")) or 0,
                )
                for i, (c, _, _) in enumerate(keep)
            ]
            if differ([o["value"] for o in options]):
                return (
                    _candidate(
                        "date",
                        p.label,
                        f"When someone says '{tw} last month', which date do they mean?",
                        f"{name}: " + "; ".join(f"{o['shown']} by {o['label']}" for o in options),
                        options,
                        _op(
                            f"{p.table.name}_date",
                            f"a question about {tw} in a period ('last month', "
                            "'by year') that doesn't say which date",
                            options,
                            [p.label],
                        ),
                    ),
                    None,
                    "date",
                )
        return None, "every date gives the same count", "date"

    p.checks.append(check)


# ---------------------------------------------------------------------------
# Pattern: a status where one business word covers several values
# ---------------------------------------------------------------------------


def _family(value: str) -> Optional[str]:
    words = set(_words(value))
    if words & _NEGATING:
        return None
    joined = "_".join(_words(value))
    if words & set(_DONE) or joined in _DONE:
        return "done"
    if words & set(_OPEN) or joined in _OPEN:
        return "open"
    return None


def _done_rank(value: str) -> int:
    """How final a done-like value is: 0 for the most final (`delivered`)."""
    words = [*_words(value), "_".join(_words(value))]
    return min(_DONE.index(w) for w in words if w in _DONE)


def _status_label(c: Column, value: str) -> str:
    label = ((c.choice_field or {}).get(value) or "").strip()
    return label or plain_words(value)


def _listed(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]


def _plan_status(p: _Probe) -> None:
    for c in p.table.columns:
        if not (_readable(c) and c.choice_field and _STATUS_NAME.search(c.name)):
            continue
        for fam in ("done", "open"):
            values = [v for v in c.choice_field if _family(v) == fam]
            if len(values) < 2:
                continue
            aliases = [p.count_where(f"{p.q(c.name)} = {p.dialect.quote_lit(v)}") for v in values]
            p.checks.append(_status_check(p, c, fam, values, aliases))


def _status_check(p: _Probe, c: Column, fam: str, values: list[str], aliases: list[str]):
    def check(_row: dict):
        counts = {v: p.value(a) or 0 for v, a in zip(values, aliases)}
        present = [v for v in values if counts[v]]
        if len(present) < 2:
            return None, f"only one {fam} value of {c.name} has rows", "status"
        if fam == "done":
            # In the order things happen (paid, shipped, delivered); the narrow reading is the last.
            present.sort(key=lambda v: -_done_rank(v))
            narrow = present[-1]
        else:
            narrow = max(present, key=lambda v: counts[v])
        lit = p.dialect.quote_lit
        ref = f"{p.label}.{c.name}"
        wide = _reading(
            _listed([_status_label(c, v) for v in present]),
            f"{ref} IN ({', '.join(lit(v) for v in present)})",
            sum(counts[v] for v in present),
        )
        only = _reading(
            f"Only {_status_label(c, narrow)}", f"{ref} = {lit(narrow)}", counts[narrow]
        )
        options = [only, wide]
        if not differ([o["value"] for o in options]):
            return None, f"one {fam} value of {c.name} holds nearly every row", "status"
        tw = _phrase(p.table.name)
        word = "completed" if fam == "done" else _status_label(c, narrow).lower()
        return (
            _candidate(
                "status",
                p.label,
                f"When someone says '{word} {tw}', which statuses count?",
                "; ".join(f"{_shown(counts[v])} {_status_label(c, v)}" for v in present),
                options,
                _op(
                    f"{p.table.name}_{c.name}_{fam}",
                    f"a question about {word} {tw} that doesn't name the statuses",
                    options,
                    [p.label],
                ),
            ),
            None,
            "status",
        )

    return check


# ---------------------------------------------------------------------------
# Pattern: several amount columns that could each be "how much"
# ---------------------------------------------------------------------------


def _amount_columns(t: Table) -> list[Column]:
    out = []
    for c in t.columns:
        words = set(B.name_words(c.name))
        if (
            _readable(c)
            and c.type in ("integer", "decimal", "float")
            and (c.unit or B.detect_money_column(c.name))
            and words & _AMOUNT_WORDS
            and not words & _COMPONENT_WORDS
        ):
            out.append(c)
    return out[:_MAX_READINGS]


def _plan_amounts(p: _Probe) -> None:
    cols = _amount_columns(p.table)
    if len(cols) < 2:
        return
    aliases = [p.add(f"SUM({p.q(c.name)})") for c in cols]

    def check(_row: dict):
        options = [
            _reading(plain_words(c.name), f"SUM({p.label}.{c.name})", p.value(a))
            for c, a in zip(cols, aliases)
        ]
        if not differ([o["value"] for o in options]):
            return None, "the amount columns add up to the same total", "amount"
        tw = _phrase(p.table.name)
        return (
            _candidate(
                "amount",
                p.label,
                f"When someone asks how much {tw} came to, which amount do they mean?",
                "all rows: " + "; ".join(f"{o['shown']} in {o['label']}" for o in options),
                options,
                _op(
                    f"{p.table.name}_amount",
                    f"a question about how much {tw} came to that doesn't name the amount",
                    options,
                    [p.label],
                ),
            ),
            None,
            "amount",
        )

    p.checks.append(check)


# ---------------------------------------------------------------------------
# Pattern: two joins from one table to the same key of another
# ---------------------------------------------------------------------------


def _join_groups(org: Datasource) -> dict[tuple, list]:
    rels = [r for sa in org.subject_areas for r in sa.relationships] + list(
        org.cross_subject_area_relationships
    )
    groups: dict[tuple, list] = {}
    for r in rels:
        if r.on or not r.from_column or not r.to_column or r.review_state == "rejected":
            continue
        key = (r.from_table, r.from_schema, r.to_table, r.to_schema, r.to_column)
        if all(x.from_column != r.from_column for x in groups.get(key, [])):
            groups.setdefault(key, []).append(r)
    return {k: v for k, v in groups.items() if len(v) >= 2}


def _link_words(column: str) -> str:
    return plain_words(re.sub(r"(_id|_key|_ref)$", "", column, flags=re.IGNORECASE))


def _plan_joins(p: _Probe, groups: dict[tuple, list]) -> None:
    cols = {c.name: c for c in p.table.columns}
    for (ft, fs, tt, _ts, tc), rels in groups.items():
        if ft != p.table.name or (fs and p.table.schema_name and fs != p.table.schema_name):
            continue
        rels = [r for r in rels if r.from_column in cols and _readable(cols[r.from_column])][
            :_MAX_READINGS
        ]
        if len(rels) < 2:
            continue
        first = p.q(rels[0].from_column)
        linked = [p.add(f"COUNT({p.q(r.from_column)})") for r in rels]
        apart = [
            p.count_where(
                f"NOT ({first} = {p.q(r.from_column)} OR ({first} IS NULL AND "
                f"{p.q(r.from_column)} IS NULL))"
            )
            for r in rels[1:]
        ]
        p.checks.append(_join_check(p, tt, tc, rels, linked, apart))


def _join_check(p: _Probe, tt: str, tc: str, rels: list, linked: list[str], apart: list[str]):
    def check(_row: dict):
        n = p.n()
        moved = max((p.value(a) or 0 for a in apart), default=0)
        if not n or moved < MIN_DIFFERENCE * n:
            return None, f"the joins to {tt} link nearly every row to the same row", "join"
        options = [
            _reading(
                _link_words(r.from_column),
                f"join {p.label}.{r.from_column} = {tt}.{tc}",
                p.value(a),
            )
            for r, a in zip(rels, linked)
        ]
        fw = _phrase(p.table.name)
        one = I._singular(_phrase(tt))
        return (
            _candidate(
                "join",
                p.label,
                f"When someone asks about {fw} by {one}, which {one} do they mean?",
                f"{_shown(moved)} of {_shown(n)} rows link to a different {one} depending on the join; "
                + "; ".join(f"{o['shown']} rows have a {o['label']}" for o in options),
                options,
                _op(
                    f"{p.table.name}_{tt}_link",
                    f"a question about {fw} by {one} that doesn't say which {one}",
                    options,
                    [p.label],
                ),
            ),
            None,
            "join",
        )

    return check


# ---------------------------------------------------------------------------
# Pattern: current rows beside past ones
# ---------------------------------------------------------------------------


def _plan_current(p: _Probe, today: date) -> None:
    for c in p.table.columns:
        if not _readable(c):
            continue
        if _CURRENT_FLAG.match(c.name) and (c.type == "boolean" or c.type == "integer"):
            cond = p.dialect.flag_rate_expr(p.q(c.name), boolean=c.type == "boolean")
            alias = p.add(f"SUM({cond})")
            how = f"only rows where {p.label}.{c.name} is true"
        elif _VALID_TO.match(c.name) and _is_date(c):
            alias = p.count_where(
                f"{p.q(c.name)} IS NULL OR {p.q(c.name)} >= "
                f"{p.dialect.date_literal(today.isoformat())}"
            )
            how = f"only rows where {p.label}.{c.name} is empty or not yet passed (>= CURRENT_DATE)"
        else:
            continue
        p.checks.append(_current_check(p, alias, how))
        return


def _current_check(p: _Probe, alias: str, how: str):
    def check(_row: dict):
        options = [
            _reading("Only current ones", how, p.value(alias)),
            _reading("Every row, past ones too", f"every row of {p.label}", p.n()),
        ]
        if not differ([o["value"] for o in options]):
            return None, "nearly every row is current", "current"
        tw = _phrase(p.table.name)
        return (
            _candidate(
                "current",
                p.label,
                f"When someone asks how many {tw} there are, do they mean only the current ones?",
                f"{options[0]['shown']} current of {options[1]['shown']} rows",
                options,
                _op(
                    f"{p.table.name}_current",
                    f"a question that counts {tw} without saying current or all",
                    options,
                    [p.label],
                ),
            ),
            None,
            "current",
        )

    return check


def _relatives(tables: list[tuple[Table, str, str]]) -> list[tuple[str, tuple, tuple]]:
    """(kind, base, other) for each table named like another plus a history or copy word."""
    by_words = {tuple(_words(t.name)): (t, label, area) for t, label, area in tables}
    out = []
    for words, other in by_words.items():
        for cut, extra in ((words[:-1], words[-1:]), (words[1:], words[:1])):
            base = by_words.get(cut) if cut else None
            if not base:
                continue
            word = extra[0]
            kind = (
                "history"
                if word in _HISTORY_WORDS
                else "look_alike"
                if word in _COPY_WORDS or re.fullmatch(r"(19|20)\d\d", word)
                else None
            )
            if kind:
                out.append((kind, base, other))
                break
    return out


def _history_candidate(base: _Probe, other: _Probe) -> tuple[Optional[dict], Optional[str], str]:
    options = [
        _reading(f"Only {plain_words(base.table.name)}", f"count rows of {base.label}", base.n()),
        _reading(
            f"{plain_words(other.table.name)} as well",
            f"count rows of {other.label} (every recorded version)",
            other.n(),
        ),
    ]
    if not differ([o["value"] for o in options]):
        return None, f"{other.label} holds as many rows as {base.label}", "current"
    bw = _phrase(base.table.name)
    return (
        _candidate(
            "current",
            base.label,
            f"When someone asks about {bw}, should {_phrase(other.table.name)} count too?",
            f"{options[0]['shown']} rows in {plain_words(base.table.name)}, {options[1]['shown']} in "
            f"{plain_words(other.table.name)}",
            options,
            _op(
                f"{base.table.name}_history",
                f"a question about {bw} that doesn't say current rows or their history",
                options,
                [base.label, other.label],
            ),
        ),
        None,
        "current",
    )


def _look_alike(base: tuple, other: tuple, probes: dict) -> dict:
    (bt, bl, ba), (ot, ol, oa) = base, other
    counts = {
        label: probes[label].n() if label in probes and probes[label].row else None
        for label in (bl, ol)
    }
    names = [plain_words(bt.name), plain_words(ot.name)]
    return {
        "kind": "look_alike",
        "tables": [
            {"table": bl, "area": ba, "label": names[0], "rows": counts[bl]},
            {"table": ol, "area": oa, "label": names[1], "rows": counts[ol]},
        ],
        "question": f"{names[0]} and {names[1]} look alike. Which one holds the current data?",
    }


# ---------------------------------------------------------------------------
# Pattern: a fiscal year that doesn't start in January
# ---------------------------------------------------------------------------


def _fiscal_candidate(
    org: Datasource, runner: Runner, dialect: D.Dialect, probes: dict, today: date
) -> tuple[Optional[dict], Optional[str]]:
    m = org.fiscal_year_start_month
    dated = [(p, c) for p in probes.values() if p.row for c in _date_columns(p.table)[:1]]
    if not dated:
        return None, "no table with a business date was probed"
    p, c = max(dated, key=lambda pc: pc[0].n())
    end = _run(runner, f"SELECT MAX({p.q(c.name)}) AS x FROM {p.fq}")
    last = _day((end or {}).get("x"))
    if not last:
        return None, "the fiscal probe failed"
    # "This year" both ways, as of when the data ends: the calendar year and the fiscal year it is in.
    ref = min(last, today)
    y, fy = ref.year, ref.year if ref.month >= m else ref.year - 1
    lit, q = dialect.date_literal, p.q(c.name)
    win = {"cal": (date(y, 1, 1), date(y + 1, 1, 1)), "fis": (date(fy, m, 1), date(fy + 1, m, 1))}
    row = _run(
        runner,
        "SELECT "
        + ", ".join(
            f"SUM(CASE WHEN {q} >= {lit(lo.isoformat())} AND {q} < {lit(hi.isoformat())} THEN 1 ELSE 0 END) AS {k}"
            for k, (lo, hi) in win.items()
        )
        + f" FROM {p.fq}",
    )
    if row is None:
        return None, "the fiscal probe failed"
    month = calendar.month_name[m]
    options = [
        _reading("Calendar year", "January to December", _number(row.get("cal")) or 0),
        _reading(
            f"Fiscal year from {month}",
            f"{month} to {calendar.month_name[(m - 2) % 12 + 1]}; the fiscal year starts in month {m}",
            _number(row.get("fis")) or 0,
        ),
    ]
    if not differ([o["value"] for o in options]):
        return None, "the calendar and the fiscal year give the same count"
    tw = _phrase(p.table.name)
    return _candidate(
        "fiscal",
        None,
        f"When someone says 'this year', do they mean the calendar year or the fiscal year from {month}?",
        f"{tw} by {plain_words(c.name)}: {options[0]['shown']} in {y}, {options[1]['shown']} in the "
        f"fiscal year from {month} {fy}",
        options,
        _op(
            "fiscal_year",
            "a question about a year ('this year', 'last year') that doesn't say "
            "calendar or fiscal",
            options,
            [],
        ),
    ), None


# ---------------------------------------------------------------------------
# What the build already knows it couldn't read
# ---------------------------------------------------------------------------


def _code_like(value: str) -> bool:
    v = str(value).strip()
    return bool(re.search(r"\d", v)) or len(v) <= 3 or (v.isupper() and len(v) <= 5)


def _codes(values: list[str]) -> bool:
    """Whether most values are codes a reader can't decode (`1`, `P2`, `HSN`), not words."""
    return bool(values) and sum(_code_like(v) for v in values) * 2 >= len(values)


def _unread_columns(org: Datasource, labels: dict) -> tuple[list[dict], list[dict]]:
    coded, unknown = [], []
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            if t.review_state == "rejected":
                continue
            for c in t.columns:
                if not _readable(c):
                    continue
                blank = [v for v, lbl in (c.choice_field or {}).items() if not (lbl or "").strip()]
                if (c.type != "string" and not _CODE_NAME.search(c.name)) or not _codes(blank):
                    blank = []  # a count of 1 to 5 is a number, and "east" or "enterprise" says itself
                where = {"area": sa.name, "table": labels[id(t)], "column": c.name}
                if blank:
                    shown = ", ".join(f"{v} = …" for v in blank[:2])
                    coded.append(
                        {
                            "kind": "coded_values",
                            **where,
                            "values": blank,
                            "question": f"What do the values of {plain_words(c.name)} on "
                            f"{plain_words(t.name)} mean? Type them in Other, e.g. {shown}",
                        }
                    )
                elif c.description_source == "ai_unknown" and not (c.description or "").strip():
                    unknown.append(
                        {
                            "kind": "unknown_column",
                            **where,
                            "question": f"What does {plain_words(c.name)} on {plain_words(t.name)} "
                            "hold? Type a short description in Other.",
                        }
                    )
    return coded, unknown


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _labels(org: Datasource) -> dict[int, str]:
    """Each table's name as an `applies_to` entry: schema-qualified only when the bare name is shared."""
    tables = [t for sa in org.subject_areas for t in sa.tables_defined]
    count: dict[str, int] = {}
    for t in tables:
        count[t.name.lower()] = count.get(t.name.lower(), 0) + 1
    return {
        id(t): (
            f"{t.schema_name}.{t.name}" if count[t.name.lower()] > 1 and t.schema_name else t.name
        )
        for t in tables
    }


def open_questions(
    org: Datasource, runner: Runner, dialect: D.Dialect, *, today: Optional[date] = None
) -> dict:
    """Every candidate the data shows, checked by running its readings, then what the build couldn't
    read, capped at `MAX_QUESTIONS`; see the module docstring."""
    today = today or date.today()
    calls = [0]

    def counted(sql: str) -> list[dict]:
        calls[0] += 1
        return runner(sql)

    labels = _labels(org)
    declared = {a.name for a in org.ambiguities}
    groups = _join_groups(org)
    tables = [
        (t, labels[id(t)], sa.name)
        for sa in org.subject_areas
        for t in sa.tables_defined
        if t.review_state != "rejected"
    ]
    relatives = _relatives(tables)
    related = {lbl for _, base, other in relatives for lbl in (base[1], other[1])}

    probes: dict[str, _Probe] = {}
    skipped: list[dict] = []
    for t, label, area in tables:
        p = _Probe(dialect, t, label, area)
        _plan_dates(p, counted, today)
        _plan_status(p)
        _plan_amounts(p)
        _plan_joins(p, groups)
        _plan_current(p, today)
        if not p.checks and label not in related:
            continue
        if t.source_type != "table" or I._too_big_to_probe(t):
            skipped.append(
                {
                    "table": label,
                    "reason": "too large to scan"
                    if t.source_type == "table"
                    else "defined by SQL, not a table",
                }
            )
        elif _rows(t) == 0:
            continue
        elif len(probes) >= MAX_TABLES:
            skipped.append({"table": label, "reason": f"over the {MAX_TABLES}-table probe cap"})
        else:
            probes[label] = p

    candidates: list[dict] = []
    dropped: list[dict] = []
    for label, p in probes.items():
        p.row = _run(counted, p.sql())
        if p.row is None:
            dropped += [{"table": label, "reason": "the probe failed"}]
            continue
        for check in p.checks:
            cand, why, pattern = check(p.row)
            if cand and cand["op"]["name"] in declared:
                dropped.append({"table": label, "pattern": pattern, "reason": "already declared"})
            elif cand:
                candidates.append(cand)
            else:
                dropped.append({"table": label, "pattern": pattern, "reason": why})

    look_alikes = []
    for kind, base, other in relatives:
        if (
            kind == "history"
            and base[1] in probes
            and other[1] in probes
            and probes[base[1]].row
            and probes[other[1]].row
        ):
            cand, why, _ = _history_candidate(probes[base[1]], probes[other[1]])
            if cand and cand["op"]["name"] not in declared:
                candidates.append(cand)
            elif why:
                dropped.append({"table": base[1], "pattern": "current", "reason": why})
        elif kind == "look_alike":
            look_alikes.append(_look_alike(base, other, probes))

    if org.fiscal_year_start_month != 1 and "fiscal_year" not in declared:
        cand, why = _fiscal_candidate(org, counted, dialect, probes, today)
        if cand:
            candidates.append(cand)
        else:
            dropped.append({"table": None, "pattern": "fiscal", "reason": why})

    candidates.sort(key=lambda c: -c["spread"])
    coded, unknown = _unread_columns(org, labels)
    questions = candidates + look_alikes + coded + unknown
    return {
        "questions": questions[:MAX_QUESTIONS],
        "more": max(0, len(questions) - MAX_QUESTIONS),
        "counts": {
            "ambiguities": len(candidates),
            "look_alike": len(look_alikes),
            "coded_values": len(coded),
            "unknown_columns": len(unknown),
        },
        "dropped": dropped,
        "skipped": skipped,
        "tables_probed": len(probes),
        "statements": calls[0],
    }
