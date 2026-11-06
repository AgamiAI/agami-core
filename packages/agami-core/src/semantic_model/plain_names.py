"""Model identifiers in plain words, for the text of a question card.

A card read "What currency is `widgets.price` in?": the person deciding is often not the one who
named the columns, and a card is plain text, so a code name there is noise. Every command whose
output a skill turns into card text carries these labels, so the wording is the same on every
card and is not left to each skill to improvise. The identifier still travels in the output's own
fields, for the op that acts on the answer.
"""
from __future__ import annotations

import re
from collections import Counter

# Words a name spells in lower case that read wrong unless upper case. Kept short on purpose: a
# word missing here reads as an ordinary word ("Ela score"), which is still plain.
_UPPER = frozenset({
    "id", "ids", "url", "uri", "ip", "sku", "api", "kpi", "pii", "utc", "ssn", "vat", "gst", "usd",
    "eur", "gbp", "inr", "jpy", "cad", "aud", "sql", "csv", "pdf", "crm", "erp", "uuid", "mrr", "arr",
})
_SPLIT = re.compile(r"[_\-\s.]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def plain_words(identifier: str) -> str:
    """`in_stock` -> "In stock", `order_items` -> "Order items", `CustomerID` -> "Customer ID".

    A schema- or table-qualified name keeps only its last part: `sales.orders` -> "Orders".
    """
    # Split before unquoting: `"sales"."Order Items"` stripped first leaves a quote on its last part.
    bare = str(identifier or "").strip().rsplit(".", 1)[-1].strip('"`[] ')
    words: list[str] = []
    for chunk in _SPLIT.split(bare):
        words += [w for w in _CAMEL.split(chunk) if w]
    if not words:
        return str(identifier or "")
    out = []
    for w in words:
        low = w.lower()
        # A word in capitals inside a name that is not all capitals (`ELA_score`) keeps them, as
        # does a known abbreviation; an all-capitals name (`ORDER_ITEMS`) is just the engine's case.
        out.append(w.upper() if low in _UPPER or (w.isupper() and len(w) > 1 and not bare.isupper())
                   else low)
    first = out[0]
    out[0] = first if first.isupper() and len(first) > 1 else first[:1].upper() + first[1:]
    return " ".join(out)


def column_label(table: str, column: str) -> str:
    """`widgets`, `price` -> "Price on Widgets": short enough for an option label."""
    return f"{plain_words(column)} on {plain_words(table)}"


def join_label(from_table: str, to_table: str, via: str | None = None) -> str:
    """`orders`, `customers` -> "Orders to Customers"; with the joining column, `billing_customer_id`
    -> "Orders to Customers by Billing customer ID", since two joins between one pair of tables
    would otherwise read the same on their sign-off cards."""
    label = f"{plain_words(from_table)} to {plain_words(to_table)}"
    return f"{label} by {plain_words(via)}" if via else label


def distinct_labels(items: list[dict], key: str = "label") -> list[dict]:
    """Add the subject area to any `key` that more than one item shares, in place.

    A label drops the schema and area, so one table name in two areas reads the same twice, and a
    card would offer two identical options that act on different columns."""
    seen = Counter(it.get(key) for it in items)
    for it in items:
        if seen[it.get(key)] > 1 and it.get("area"):
            it[key] = f"{it[key]} ({plain_words(it['area'])})"
    return items
