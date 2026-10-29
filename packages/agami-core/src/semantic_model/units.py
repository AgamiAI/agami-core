"""Deterministic unit/currency formatting.

A column or metric's `unit` (an ISO currency code like INR/USD, or a plain unit like
`percent`/`cents`/`days`) drives how its values are displayed — symbol, grouping,
decimals — WITHOUT the LLM re-interpreting a prose caveat each query. The chart
renderer and the query result formatter both call `format_value` so a number renders
identically everywhere.

A currency unit may carry a scale and a rate: `<CODE>[_<scale>][/<period>]`, e.g.
`USD_millions` (12.5 means $12.5M), `INR_crores`, `USD/day`, `USD_millions/year`.
Scales: thousands, millions, billions, lakhs, crores (singular or plural). Periods: hour,
day, week, month, quarter, year. Any other text is shown as a plain label.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Iterable, NamedTuple, Optional

# ISO code -> symbol. Extend freely; unknown codes fall back to the bare code.
CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "INR": "₹",
    "AUD": "A$", "CAD": "C$", "CHF": "CHF ", "SGD": "S$", "HKD": "HK$",
    "NZD": "NZ$", "ZAR": "R", "BRL": "R$", "RUB": "₽", "KRW": "₩", "MXN": "MX$",
    "AED": "د.إ ", "SAR": "﷼ ", "TRY": "₺", "THB": "฿", "IDR": "Rp", "MYR": "RM",
    "PHP": "₱", "VND": "₫", "NGN": "₦", "PKR": "₨", "BDT": "৳", "LKR": "Rs ",
}

# Currencies that conventionally show no minor units (no decimals).
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "IDR"}


# scale word -> (multiplier, suffix shown after the number)
SCALES: dict[str, tuple[int, str]] = {
    "thousands": (10**3, "K"), "millions": (10**6, "M"), "billions": (10**9, "B"),
    "lakhs": (10**5, "L"), "crores": (10**7, "Cr"),
}
PERIODS = ("hour", "day", "week", "month", "quarter", "year")
_UNIT_RE = re.compile(r"^([A-Za-z]{3})(?:_([A-Za-z]+?)s?)?(?:/([A-Za-z]+))?$", re.IGNORECASE)


class CurrencyUnit(NamedTuple):
    code: str              # ISO code, upper case
    scale: Optional[str]   # a SCALES key, or None
    period: Optional[str]  # one of PERIODS, or None


def parse_unit(unit: Optional[str]) -> Optional[CurrencyUnit]:
    """`USD_millions/year` -> CurrencyUnit("USD", "millions", "year"); None when `unit` is not
    a known currency in this grammar (then it is a plain label)."""
    m = _UNIT_RE.match((unit or "").strip())
    if not m or m.group(1).upper() not in CURRENCY_SYMBOLS:
        return None
    scale = (m.group(2) or "").lower()
    scale = scale + "s" if scale else None
    period = (m.group(3) or "").lower() or None
    if (scale and scale not in SCALES) or (period and period not in PERIODS):
        return None
    return CurrencyUnit(m.group(1).upper(), scale, period)


def is_currency(unit: Optional[str]) -> bool:
    return parse_unit(unit) is not None


def currency_symbol(unit: Optional[str]) -> Optional[str]:
    """Symbol for a currency unit (`USD`, `USD_millions`, …), or None if it isn't one."""
    cu = parse_unit(unit)
    return CURRENCY_SYMBOLS[cu.code] if cu else None


def _group_western(int_part: str) -> str:
    # 1234567 -> 1,234,567
    digits = int_part[::-1]
    chunks = [digits[i:i + 3] for i in range(0, len(digits), 3)]
    return ",".join(chunks)[::-1]


def _group_number(text: str) -> str:
    """Western-group a plain decimal string, sign and fraction kept: `-1234.5` -> `-1,234.5`."""
    sign = "-" if text.startswith("-") else ""
    int_part, dot, frac = text.lstrip("+-").partition(".")
    return f"{sign}{_group_western(int_part)}{dot}{frac}"


def _group_indian(int_part: str) -> str:
    # 1234567 -> 12,34,567 (last 3, then groups of 2)
    if len(int_part) <= 3:
        return int_part
    head, tail = int_part[:-3], int_part[-3:]
    head_r = head[::-1]
    chunks = [head_r[i:i + 2] for i in range(0, len(head_r), 2)]
    return ",".join(chunks)[::-1] + "," + tail


# Date storage encodings → seconds divisor for the integer value.
_EPOCH_DIVISORS = {"epoch_s": 1.0, "epoch_ms": 1_000.0, "epoch_us": 1_000_000.0,
                   "epoch_ns": 1_000_000_000.0}
_DATE_FORMATS = set(_EPOCH_DIVISORS) | {"yyyymmdd", "iso8601"}


def is_date_format(token: Optional[str]) -> bool:
    return (token or "").strip().lower() in _DATE_FORMATS


def format_date(value, date_format: Optional[str]) -> str:
    """Render a stored date/time value human-readably. Deterministic.

    - epoch_s/ms/us/ns → `YYYY-MM-DD HH:MM:SS UTC` (Unix time is UTC by definition).
    - yyyymmdd (integer 20240115) → `YYYY-MM-DD`.
    - iso8601 / anything else → passed through (the DB already returns it readable).
    Non-numeric epoch values pass through unchanged.
    """
    if value is None:
        return ""
    t = (date_format or "").strip().lower()
    if t in _EPOCH_DIVISORS:
        try:
            secs = float(value) / _EPOCH_DIVISORS[t]
        except (TypeError, ValueError):
            return str(value)
        return _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC"
    if t == "yyyymmdd":
        try:
            i = int(float(value))
        except (TypeError, ValueError):
            return str(value)
        try:
            return _dt.date(i // 10000, (i // 100) % 100, i % 100).strftime("%Y-%m-%d")
        except ValueError:
            return str(value)
    return str(value)


def format_value(value, unit: Optional[str]) -> str:
    """Format a numeric value for display given its unit. Deterministic.

    - a date encoding (epoch_s/ms/us/ns, yyyymmdd) → human-readable date via `format_date`.
    - currency → `<symbol><grouped number>` (Indian grouping for INR, western else;
      2 decimals, or 0 for zero-decimal currencies like JPY). A scaled currency keeps the
      value's own decimals and adds the scale suffix (`USD_millions`: 12.5 → `$12.5M`); a
      rate adds its period (`USD/day`: 120 → `$120.00/day`).
    - `percent`/`%` → `<n>%`.
    - any other unit → `<grouped number> <unit>` (e.g. `1,234 days`).
    - unknown/None unit, or non-numeric value → `str(value)` unchanged.
    """
    if value is None:
        return ""
    if is_date_format(unit):
        return format_date(value, unit)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    cu = parse_unit(unit)
    if cu:
        return _format_currency(num, cu)

    lower = (unit or "").strip().lower()
    if lower in ("percent", "%", "pct"):
        s = f"{num:g}"
        return f"{s}%"
    if not unit:
        return str(value)
    # generic unit label
    plain = _plain_number(value)
    return f"{_group_number(plain) if plain is not None else str(value)} {unit}"


def _format_currency(num: float, cu: CurrencyUnit) -> str:
    n = abs(num)
    if cu.scale:
        # a scaled figure is already rounded by its source: show its digits, don't pad to cents
        text = f"{n:.6f}".rstrip("0").rstrip(".")
        suffix = SCALES[cu.scale][1]
    else:
        text = f"{n:.{0 if cu.code in _ZERO_DECIMAL else 2}f}"
        suffix = ""
    int_part, _, frac = text.partition(".")
    grouped = _group_indian(int_part) if cu.code == "INR" else _group_western(int_part)
    rate = f"/{cu.period}" if cu.period else ""
    return f"{'-' if num < 0 else ''}{CURRENCY_SYMBOLS[cu.code]}{grouped}{'.' + frac if frac else ''}{suffix}{rate}"


_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
# A number never prints with a leading zero, so `07094` or `001` is a code read as a string.
_LEADING_ZERO_RE = re.compile(r"^[+-]?0\d")


def _plain_number(value) -> Optional[str]:
    """The value as a plain decimal string with no exponent and no trailing fractional zeros
    (`Decimal('12.50000000')` -> `12.5`), or None when it is not a number. Exact: a NUMERIC(38)
    value or a big id never goes through a float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        value = repr(value)
    if isinstance(value, (int, Decimal)):
        d = Decimal(value)
    else:
        s = str(value).strip().replace(",", "")
        if not _NUMBER_RE.match(s) or _LEADING_ZERO_RE.match(s):
            return None
        d = Decimal(s)
    if not d.is_finite():
        return None
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "+0") else text


def format_cell(value, unit: Optional[str]) -> str:
    """One result cell, formatted EXACTLY (no abbreviation, no precision loss) — for
    verification surfaces. A unit'd column → `format_value`; a bare number → grouped
    in full, trailing fractional zeros dropped; anything else (a code like `07094`
    included) passes through unchanged."""
    if unit:
        return format_value(value, unit)
    plain = _plain_number(value)
    if plain is not None:
        return _group_number(plain)
    return "" if value is None else str(value)


# A number in a column NAMED like a period or an identifier is a label, not a quantity: an id
# grouped as `1,234,567` misreads it. Matched on the header's words. Years are below.
_LABEL_COLUMN_RE = re.compile(
    r"(^|_)(quarter|qtr|month|period|week|day_of_week|"
    r"id|key|code|nbr|number|num|no|zip|zipcode|postal_code)$"
)


def _names_a_label(header: str) -> bool:
    return bool(_LABEL_COLUMN_RE.search(_snake(header)))


def _snake(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")


# A year or a postal code in a name (`year_built`, `ship_zip5`) is only trusted when every value has
# that shape too: `sales_last_year` holds money, not years.
_YEAR_WORD_RE = re.compile(r"(^|_)(year|yr|fy)s?(_|$)")
_POSTAL_WORD_RE = re.compile(r"(^|_)(zip|zipcode|zip5|postal|postcode|pincode)(_|$)")
_YEAR_VALUE_RE = re.compile(r"^[12]\d{3}(\.0+)?$")
_CODE_VALUE_RE = re.compile(r"^\d+(\.0+)?$")


def _values_are_labels(header: str, values: list) -> bool:
    words = _snake(header)
    if _YEAR_WORD_RE.search(words):
        shape = _YEAR_VALUE_RE
    elif _POSTAL_WORD_RE.search(words):
        shape = _CODE_VALUE_RE
    else:
        return False
    seen = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    return bool(seen) and all(shape.match(v) for v in seen)


def _as_label(value) -> str:
    """The value as written, minus a float's `.0` a driver may add to a whole number. A string is
    shown exactly as stored: "001.0" is a code, not a number."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return str(int(value))
    return str(value)


def format_rows(headers: list[str], rows: list[list], units: Optional[dict] = None,
                labels: Optional[Iterable[str]] = None) -> list[list[str]]:
    """Every cell formatted the way an answer shows it: a unit'd column with its symbol and
    grouping, a label column (a year, a code, an id) as written, any other number grouped.
    `labels` names the label columns the model knows of, by output name or position (`#i`), as
    `runtime.resolve_result_labels` returns them; a column named like a year or an id is one too.
    `format_table` renders these, and `sm seed-validate`'s preview shows them, so a value
    looks the same in the preview someone validates as in the answer they later get."""
    units = units or {}
    known = set(labels or ())
    cols = [str(h) for h in headers]

    def _unit_for(i: int) -> Optional[str]:
        # by output name first, then by position (`#i`) — the positional key carries
        # units for columns the DB auto-named (an unaliased MAX(amount), etc.)
        h = cols[i] if i < len(cols) else ""
        return units.get(h) or units.get(f"#{i}")

    def _is_label(i: int) -> bool:
        h = cols[i] if i < len(cols) else ""
        if h in known or f"#{i}" in known or _names_a_label(h):
            return True
        return _values_are_labels(h, [r[i] for r in rows if i < len(r)])

    width = max([len(cols)] + [len(r) for r in rows])
    label_cols = {i for i in range(width) if not _unit_for(i) and _is_label(i)}

    def _fmt(i: int, c) -> str:
        return _as_label(c) if i in label_cols else format_cell(c, _unit_for(i))

    return [[_fmt(i, c) for i, c in enumerate(r)] for r in rows]


def format_table(headers: list[str], rows: list[list], units: Optional[dict] = None,
                 labels: Optional[Iterable[str]] = None) -> str:
    """Render a GitHub-flavoured markdown table with every numeric cell formatted
    deterministically and in full (exact value, thousands/lakh grouping, currency
    symbol) — never abbreviated. `units` maps a header to its unit (currency code or
    label). The query skill and the MCP both call this so the numbers a user verifies
    are identical regardless of which LLM renders the surrounding answer."""
    cols = [str(h) for h in headers]
    fmt_rows = format_rows(headers, rows, units, labels)
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in fmt_rows)
    return "\n".join([head, sep, body]) if body else "\n".join([head, sep])


__all__ = ["CURRENCY_SYMBOLS", "SCALES", "PERIODS", "CurrencyUnit", "parse_unit", "is_currency", "currency_symbol", "is_date_format",
           "format_value", "format_date", "format_cell", "format_rows", "format_table"]
