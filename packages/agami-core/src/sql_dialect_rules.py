"""What an engine rejects that a PostgreSQL-trained writer reaches for, and what to write instead (#325).

The client is told the engine (`database_type`, and the model's `storage_type`) and still writes
PostgreSQL, because a name says nothing about which features that engine lacks. Every such statement
costs a warehouse round trip and a full client turn to rewrite. Handing the client the gaps BEFORE it
writes is the only fix that removes the retry: a refusal naming the rewrite still spends the turn.

Rules are per ENGINE, never per customer, and read-path only — the guard admits nothing but SELECT,
so a rule about INSERT or RETURNING would be tokens spent on something that cannot happen.

Keyed by `StorageType` exactly as the model spells it (`Redshift`), the same key
`semantic_model.sql_dialect` maps to a sqlglot dialect. An engine with no entry gets nothing, which
is the right default: a rules block for an engine the writer already handles is noise it pays for on
every schema call.
"""

from __future__ import annotations

_REDSHIFT = """\
Amazon Redshift is PostgreSQL-derived but rejects several PostgreSQL features. Write these instead:
- No FILTER on aggregates. COUNT(*) FILTER (WHERE c) -> SUM(CASE WHEN c THEN 1 ELSE 0 END); a \
filtered average -> AVG(CASE WHEN c THEN v END) (NULL, not 0, in the ELSE).
- No STRING_AGG or ARRAY_AGG. Use LISTAGG(col, ', ') WITHIN GROUP (ORDER BY col). The delimiter must \
be a constant, and every LISTAGG in one SELECT must use the same WITHIN GROUP ordering.
- LISTAGG, MEDIAN and PERCENTILE_CONT cannot share a SELECT with any DISTINCT aggregate \
(COUNT(DISTINCT ...)). Compute them in separate CTEs and join the CTEs.
- Date arithmetic takes the unit first, unquoted: DATEADD(day, -30, CURRENT_DATE), \
DATEDIFF(day, start_ts, end_ts). DATE_TRUNC('month', ts) is fine. Use GETDATE() or CURRENT_DATE.
- Use SUBSTRING(s, start, length), not SUBSTR.
- A BOOLEAN cannot be cast to VARCHAR or passed to a string function (BTRIM, CONCAT). Use \
CASE WHEN col THEN 'true' ELSE 'false' END.
- No DISTINCT ON and no LATERAL joins. For one row per group use ROW_NUMBER() OVER (PARTITION BY ... \
ORDER BY ...) in a subquery and keep row 1 (QUALIFY also works).
- Many correlated subqueries are rejected. Rewrite them as a JOIN to a CTE that aggregates once.
- FULL JOIN needs a plain equality join condition.
- Double-quote a column whose name is a reserved word, e.g. "table", "end", "user".
- No GENERATE_SERIES against tables. No REGEXP_MATCHES; use REGEXP_SUBSTR, REGEXP_REPLACE, \
REGEXP_COUNT or ~.
"""

_RULES: dict[str, str] = {"Redshift": _REDSHIFT}


def dialect_rules_for(storage_type: str | None) -> str | None:
    """The rules block for one declared engine, or None when it has none (or the engine is unknown)."""
    return _RULES.get(storage_type) if storage_type else None
