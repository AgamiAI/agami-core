"""What an engine rejects that a PostgreSQL-trained writer reaches for, and what to write instead (#325).

The client is told the engine (`database_type`, and the model's `storage_type`) and still writes
PostgreSQL, because a name says nothing about which features that engine lacks. Every such statement
costs a warehouse round trip and a full client turn to rewrite. Handing the client the gaps BEFORE it
writes is the only fix that removes the retry: a refusal naming the rewrite still spends the turn.

Rules are per ENGINE, never per customer, and read-path only — the guard admits nothing but SELECT,
so a rule about INSERT or RETURNING would be tokens spent on something that cannot happen. Each
rewrite must return the same result as the construct it replaces, NULLs and empty inputs included: a
rule that trades an error for a quietly different answer is worse than the error.

Keyed by `StorageType` exactly as the model spells it (`Redshift`), the same key
`semantic_model.sql_dialect` maps to a sqlglot dialect. An engine with no entry gets nothing, which
is the right default: a rules block for an engine the writer already handles is noise it pays for on
every schema call.
"""

from __future__ import annotations

import re

_REDSHIFT = """\
Amazon Redshift is PostgreSQL-derived but rejects several PostgreSQL features. Write these instead:
- No FILTER on aggregates. COUNT(*) FILTER (WHERE c) -> COUNT(CASE WHEN c THEN 1 END); \
SUM(v) FILTER (WHERE c) -> SUM(CASE WHEN c THEN v END); AVG likewise. Leave the ELSE out so \
non-matching rows are NULL and ignored.
- No STRING_AGG. Use LISTAGG(col, ', ') WITHIN GROUP (ORDER BY col). The delimiter must be a \
constant, and every ordered aggregate in one SELECT (LISTAGG, PERCENTILE_CONT, MEDIAN) must use the \
same ordering. There is no ARRAY_AGG and no array result type; LISTAGG returns text, so use it only \
when a text list is what the question wants.
- LISTAGG, MEDIAN and PERCENTILE_CONT cannot share a SELECT with any DISTINCT aggregate \
(COUNT(DISTINCT ...)). Compute them in separate CTEs and join the CTEs.
- Date arithmetic takes the unit first, unquoted: DATEADD(day, -30, CURRENT_DATE), \
DATEDIFF(day, start_ts, end_ts). DATE_TRUNC('month', ts) is fine. Use GETDATE() or CURRENT_DATE.
- Use SUBSTRING(s, start, length), not SUBSTR.
- A BOOLEAN cannot be cast to VARCHAR or passed to a string function (BTRIM, CONCAT). Use \
CASE WHEN col THEN 'true' WHEN NOT col THEN 'false' END, which keeps NULL as NULL.
- No DISTINCT ON and no LATERAL joins. For one row per group use ROW_NUMBER() OVER (PARTITION BY ... \
ORDER BY ...) in a subquery and keep row 1 (QUALIFY also works).
- Many correlated subqueries are rejected. Rewrite them as a JOIN to a CTE that aggregates once.
- FULL JOIN needs a plain equality join condition.
- Double-quote any identifier (column, table or alias) that is a reserved word, e.g. "table", "end", \
"user".
- No GENERATE_SERIES against tables. No REGEXP_MATCHES; use REGEXP_SUBSTR, REGEXP_REPLACE, \
REGEXP_COUNT or ~.
"""

_RULES: dict[str, str] = {"Redshift": _REDSHIFT}

# Which rule explains which database error (#566). The rules reach a client only through
# list_datasources, and a client that already knows its datasource skips that call and writes the
# PostgreSQL the rule warns against. The database then says "function string_agg(...) does not
# exist", which the error classifier must not relay (ACE-039) and which used to read as a missing
# column. Answering that error with the rule turns one wasted turn into a correct retry.
#
# Each entry names a rule by the words its bullet starts with, so the rule text stays single-sourced
# in the block above, and lists the lowercase fragments of the engine's own error message that only
# that construct produces. A fragment that could come from something else does not belong here: a
# wrong rule sends the client to rewrite the part of the statement that was fine.
_REDSHIFT_FIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("No STRING_AGG", ("function string_agg(", "function array_agg(")),
    ("LISTAGG, MEDIAN and PERCENTILE_CONT", ("aggregate functions with other distinct aggregate",)),
    (
        "Date arithmetic",
        (
            "function pg_catalog.date_add(",
            "function pg_catalog.date_sub(",
            "function date_add(",
            "function date_sub(",
            'function dateadd("unknown"',
            'function datediff("unknown"',
        ),
    ),
    ("Use SUBSTRING", ("function substr(",)),
    (
        "A BOOLEAN cannot",
        (
            "cannot cast type boolean to character varying",
            "cannot cast type boolean to varchar",
            "cannot cast type boolean to text",
            "function btrim(boolean",
        ),
    ),
    ("Many correlated subqueries", ("correlated subquery pattern is not supported",)),
    ("FULL JOIN", ("full join is only supported",)),
    ("No GENERATE_SERIES", ("function regexp_matches(",)),
)

_FIXES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {"Redshift": _REDSHIFT_FIXES}


def dialect_rules_for(storage_type: str | None) -> str | None:
    """The rules block for one declared engine, or None when it has none (or the engine is unknown)."""
    return _RULES.get(storage_type) if storage_type else None


def _engine_key(engine: str | None) -> str | None:
    """The `_RULES` key for an engine spelled either way: the model's `Redshift` or the credentials'
    `redshift`. The executor knows the engine only by its credentials' lowercase type."""
    if not engine:
        return None
    return next((key for key in _RULES if key.lower() == engine.strip().lower()), None)


_FIX_LEAD = "This database does not support part of the statement."


def _rule_line(key: str, starts: str) -> str:
    """The bullet of `key`'s rules block that starts with `starts`, without its leading "- "."""
    return next(line[2:] for line in _RULES[key].splitlines() if line.startswith(f"- {starts}"))


# Built once, at import, so an entry whose words match no bullet fails here rather than inside the
# database-error handler at the moment a client needed the rule.
_FIX_MESSAGE: dict[tuple[str, str], str] = {
    (key, starts): f"{_FIX_LEAD} {_rule_line(key, starts)}"
    for key, fixes in _FIXES.items()
    for starts, _fragments in fixes
}

#: Every message `dialect_fix_for` can return. The forked transport relays a child's line only when
#: it is exactly one of these, so a rule crosses the process boundary and nothing else does.
FIX_MESSAGES: frozenset[str] = frozenset(_FIX_MESSAGE.values())


# The primary message of a Redshift driver error, which `redshift_connector` reports as a dict of
# PostgreSQL error fields: `{'S': 'ERROR', 'C': '42883', 'M': '<message>', 'H': ..., 'P': ...}`.
_PRIMARY_FIELD = re.compile(r"""'M':\s*(['"])(?P<message>.*?)(?<!\\)\1""", re.DOTALL)


def primary_diagnostic(error_text: str) -> str:
    """The database's own one-line diagnosis, without the fields that echo the statement.

    A driver error carries more than the message: a HINT, the failing line of SQL, an internal query.
    Those echo the caller's statement, so matching the whole text would let a column named like a
    construct, or a literal in the SQL, pick a rule that has nothing to do with the failure. The `M`
    field when the driver reports fields, and the first line otherwise (psycopg2 puts LINE and HINT on
    the lines after it).
    """
    match = _PRIMARY_FIELD.search(error_text)
    if match is not None:
        return match.group("message")
    return error_text.strip().splitlines()[0] if error_text.strip() else ""


def dialect_fix_for(engine: str | None, error_text: str | None) -> str | None:
    """The caller-facing message for a database error one dialect rule explains, or None.

    `error_text` is the engine's own message and is only read here, never returned: the answer is
    a fixed lead plus a rule from the block above, so nothing the database said reaches the caller.
    None when the engine has no rules or no rule claims the error.
    """
    key = _engine_key(engine)
    lowered = primary_diagnostic(error_text or "").lower()
    if key is None or not lowered:
        return None
    for starts, fragments in _FIXES.get(key, ()):
        if any(fragment in lowered for fragment in fragments):
            return _FIX_MESSAGE[(key, starts)]
    return None
