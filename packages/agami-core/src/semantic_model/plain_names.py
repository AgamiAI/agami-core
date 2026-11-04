"""Model identifiers in plain words, for the text of a question card.

A card read "What currency is `widgets.price` in?": the person deciding is often not the one who
named the columns, and a card is plain text, so a code name there is noise. Every command whose
output a skill turns into card text carries these labels, so the wording is the same on every
card and is not left to each skill to improvise. The identifier still travels in the output's own
fields, for the op that acts on the answer.
"""
from __future__ import annotations

import re

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
    bare = str(identifier or "").strip().strip('"`[]').rsplit(".", 1)[-1]
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


def join_label(from_table: str, to_table: str) -> str:
    """`orders`, `customers` -> "Orders to Customers"."""
    return f"{plain_words(from_table)} to {plain_words(to_table)}"
