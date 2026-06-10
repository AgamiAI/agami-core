"""Deterministic unit/currency formatting.

A column or metric's `unit` (an ISO currency code like INR/USD, or a plain unit like
`percent`/`cents`/`days`) drives how its values are displayed — symbol, grouping,
decimals — WITHOUT the LLM re-interpreting a prose caveat each query. The chart
renderer and the query result formatter both call `format_value` so a number renders
identically everywhere.
"""

from __future__ import annotations

from typing import Optional

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


def _norm(unit: Optional[str]) -> str:
    return (unit or "").strip().upper()


def is_currency(unit: Optional[str]) -> bool:
    return _norm(unit) in CURRENCY_SYMBOLS


def currency_symbol(unit: Optional[str]) -> Optional[str]:
    """Symbol for an ISO currency code, or None if `unit` isn't a known currency."""
    return CURRENCY_SYMBOLS.get(_norm(unit))


def _group_western(int_part: str) -> str:
    # 1234567 -> 1,234,567
    digits = int_part[::-1]
    chunks = [digits[i:i + 3] for i in range(0, len(digits), 3)]
    return ",".join(chunks)[::-1]


def _group_indian(int_part: str) -> str:
    # 1234567 -> 12,34,567 (last 3, then groups of 2)
    if len(int_part) <= 3:
        return int_part
    head, tail = int_part[:-3], int_part[-3:]
    head_r = head[::-1]
    chunks = [head_r[i:i + 2] for i in range(0, len(head_r), 2)]
    return ",".join(chunks)[::-1] + "," + tail


def format_value(value, unit: Optional[str]) -> str:
    """Format a numeric value for display given its unit. Deterministic.

    - currency → `<symbol><grouped number>` (Indian grouping for INR, western else;
      2 decimals, or 0 for zero-decimal currencies like JPY).
    - `percent`/`%` → `<n>%`.
    - any other unit → `<grouped number> <unit>` (e.g. `1,234 days`).
    - unknown/None unit, or non-numeric value → `str(value)` unchanged.
    """
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    code = _norm(unit)
    if code in CURRENCY_SYMBOLS:
        decimals = 0 if code in _ZERO_DECIMAL else 2
        neg = num < 0
        n = abs(num)
        int_part = str(int(round(n))) if decimals == 0 else f"{n:.{decimals}f}".split(".")[0]
        frac = "" if decimals == 0 else "." + f"{n:.{decimals}f}".split(".")[1]
        grouped = _group_indian(int_part) if code == "INR" else _group_western(int_part)
        return f"{'-' if neg else ''}{CURRENCY_SYMBOLS[code]}{grouped}{frac}"

    lower = (unit or "").strip().lower()
    if lower in ("percent", "%", "pct"):
        s = f"{num:g}"
        return f"{s}%"
    if not unit:
        return str(value)
    # generic unit label
    if num == int(num):
        grouped = _group_western(str(int(num)))
    else:
        grouped = str(num)
    return f"{grouped} {unit}"


def _looks_numeric(s: str) -> bool:
    t = (s or "").strip().replace(",", "")
    if t in ("", "-", "+"):
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def format_cell(value, unit: Optional[str]) -> str:
    """One result cell, formatted EXACTLY (no abbreviation, no precision loss) — for
    verification surfaces. A unit'd column → `format_value`; a bare number → grouped
    in full; anything else passes through unchanged."""
    if unit:
        return format_value(value, unit)
    s = "" if value is None else str(value)
    if _looks_numeric(s):
        n = float(s.replace(",", ""))
        if n == int(n):
            return _group_western(str(int(n)))
        # keep the source's exact decimals — don't round
        ip, fp = s.replace(",", "").lstrip("-").split(".") if "." in s else (s.replace(",", "").lstrip("-"), "")
        grouped = _group_western(ip)
        return ("-" if n < 0 else "") + grouped + ("." + fp if fp else "")
    return s


def format_table(headers: list[str], rows: list[list], units: Optional[dict] = None) -> str:
    """Render a GitHub-flavoured markdown table with every numeric cell formatted
    deterministically and in full (exact value, thousands/lakh grouping, currency
    symbol) — never abbreviated. `units` maps a header to its unit (currency code or
    label). The query skill and the MCP both call this so the numbers a user verifies
    are identical regardless of which LLM renders the surrounding answer."""
    units = units or {}
    cols = [str(h) for h in headers]
    fmt_rows = [[format_cell(c, units.get(cols[i] if i < len(cols) else "")) for i, c in enumerate(r)]
                for r in rows]
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in fmt_rows)
    return "\n".join([head, sep, body]) if body else "\n".join([head, sep])


__all__ = ["CURRENCY_SYMBOLS", "is_currency", "currency_symbol",
           "format_value", "format_cell", "format_table"]
