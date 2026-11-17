"""#492: a column NAMED like a denied keyword is an identifier, not the keyword.

ServiceNow's `incident` table has a `notify` column; `lock`, `set` and `into` are ordinary column
names elsewhere. The read-only gate's keyword deny-list used to refuse every statement that read
one, quoted or not, and told the caller the statement was a write. The exemptions are narrow and
positional — inside a quoted identifier, straight after `name.`, or alone as a select-list item —
and everything else still refuses, so the real statements stay caught.
"""

from __future__ import annotations

import pytest
from sql_guard import check_read_only

_WORDS = ["notify", "lock", "set", "into", "listen", "copy", "execute", "cluster", "prepare"]

ACCEPT = [
    *(f'SELECT "{w}" FROM some_schema.some_table LIMIT 1' for w in _WORDS),
    *(f"SELECT `{w}` FROM some_dataset.some_table LIMIT 1" for w in _WORDS),
    *(f"SELECT t.{w} FROM some_schema.some_table AS t LIMIT 1" for w in _WORDS),
    *(f'SELECT t."{w}" FROM some_schema.some_table AS t' for w in _WORDS),
    *(f"SELECT id, {w} FROM some_schema.some_table" for w in _WORDS),
    *(f"SELECT {w}, id FROM some_schema.some_table" for w in _WORDS),
    "SELECT DISTINCT notify FROM incident",
    "SELECT i.number FROM incident AS i WHERE i.notify = 2 GROUP BY i.number ORDER BY i.lock",
    'SELECT COUNT(*) FROM incident WHERE "set" IS NOT NULL',
]


@pytest.mark.parametrize("sql", ACCEPT)
def test_a_column_named_like_a_keyword_is_accepted(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is None, f"identifier refused as a keyword: {sql!r} -> {refusal}"


REJECT = [
    "SELECT 1; NOTIFY x",
    "NOTIFY chan",
    "LOCK TABLE t",
    "SET x = 1",
    "SELECT * INTO t2 FROM t",
    "SELECT a, b INTO t2 FROM t",
    "SELECT a INTO t2",
    "SELECT 1. INTO t2 FROM t",  # `1.` is a numeric literal, not a qualifier
    "SELECT 1e5.into t2 FROM t",
    "WITH x AS (LISTEN ch) SELECT 1",
    "WITH x AS (UPDATE t SET a = 1 RETURNING id) SELECT id FROM x",
    "WITH x AS (DELETE FROM t RETURNING id) SELECT id FROM x",
    "WITH x AS (SELECT 1), y AS (DELETE FROM t RETURNING id) SELECT id FROM x, y",
    "SELECT notify INTO t2 FROM t",
    "SELECT t.notify INTO t2 FROM t",
    'SELECT "x"INTO evil FROM t',
    "SELECT t. INTO evil FROM t",
    "SELECT notify FROM t FOR UPDATE",
    # A bare keyword not standing alone as a select-list item is still the keyword.
    "SELECT id FROM t WHERE notify = 1",
]


@pytest.mark.parametrize("sql", REJECT)
def test_the_keyword_itself_is_still_refused(sql: str) -> None:
    assert check_read_only(sql) is not None, f"keyword slipped through as an identifier: {sql!r}"
