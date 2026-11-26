"""#492: a column NAMED like a denied keyword is an identifier, not the keyword.

ServiceNow's `incident` table has a `notify` column; `lock`, `set` and `into` are ordinary column
names elsewhere. The read-only gate's keyword deny-list used to refuse every statement that read
one, quoted or not, and told the caller the statement was a write. The exemptions are narrow and
positional — inside a quoted identifier, straight after `name.`, alone as a select-list item, or
straight after a token only an operand can follow — and everything else still refuses, so the real
statements stay caught.
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
    *(f"SELECT id, {w} FROM some_schema.some_table" for w in _WORDS if w != "into"),
    *(f"SELECT {w}, id FROM some_schema.some_table" for w in _WORDS if w != "into"),
    "SELECT DISTINCT notify FROM incident",
    "SELECT i.number FROM incident AS i WHERE i.notify = 2 GROUP BY i.number ORDER BY i.lock",
    'SELECT COUNT(*) FROM incident WHERE "set" IS NOT NULL',
    # Refused before the operand-position rule below; a name in WHERE is the issue's own example.
    "SELECT id FROM t WHERE notify = 1",
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
    # A bare keyword after a complete operand, rather than in operand position, is still the keyword.
    "SELECT id FROM t WHERE a = 1 NOTIFY",
    # An empty select list with INTO creates a table on Postgres / Redshift; `"from"` unwraps to a
    # FROM, so INTO is never exempt as a bare select-list item.
    'SELECT INTO "from" FROM t',
    'SELECT INTO "FROM"',
    'SELECT DISTINCT INTO "from" FROM t',
    'SELECT INTO /*c*/ "from" FROM t',
    "SELECT into, id FROM t",
    "SELECT id, into FROM t",
    # Two backtick names either side of a bare keyword are not one quoted name.
    "SELECT `a`INTO`b` FROM t",
    "SELECT `a`NOTIFY`b` FROM t",
]


@pytest.mark.parametrize("sql", REJECT)
def test_the_keyword_itself_is_still_refused(sql: str) -> None:
    assert check_read_only(sql) is not None, f"keyword slipped through as an identifier: {sql!r}"


# --- Bare names in operand position ------------------------------------------------------------
#
# A bare word straight after a token that can only be followed by an operand or a name (`=`, `,`,
# `WHERE`, `AND`, `BY`, `AS`, `FROM`, `JOIN`, ...) is that operand or name on every engine. The
# rule is one-sided on purpose: it never looks at what comes AFTER the word, so it cannot be fooled
# by a clause that only some engine reads there.

_OPERAND_WORDS = [w for w in _WORDS if w != "into"] + ["reset", "release", "call", "discard"]

OPERAND_ACCEPT = [
    *(f"SELECT id FROM incident WHERE {w} = 1" for w in _OPERAND_WORDS),
    *(f"SELECT id FROM incident WHERE id > 0 AND {w} IS NOT NULL" for w in _OPERAND_WORDS),
    *(f"SELECT id FROM incident WHERE id = 1 OR NOT {w}" for w in _OPERAND_WORDS),
    *(f"SELECT id FROM incident WHERE id = {w}" for w in _OPERAND_WORDS),
    *(f"SELECT id FROM incident ORDER BY {w} DESC" for w in _OPERAND_WORDS),
    *(f"SELECT {w}, COUNT(*) FROM incident GROUP BY {w}" for w in _OPERAND_WORDS),
    *(f"SELECT COUNT(*) FROM incident GROUP BY id HAVING {w} > 1" for w in _OPERAND_WORDS),
    *(f"SELECT CASE WHEN {w} = 1 THEN 'y' END FROM incident" for w in _OPERAND_WORDS),
    *(f"SELECT id AS {w} FROM incident" for w in _OPERAND_WORDS),
    *(f"SELECT {w}.id FROM incident AS {w}" for w in _OPERAND_WORDS),
    *(f"SELECT id FROM {w}" for w in _OPERAND_WORDS),
    *(f"SELECT a.id FROM incident AS a JOIN {w} AS b ON a.id = b.id" for w in _OPERAND_WORDS),
    *(f"SELECT id FROM incident ORDER BY id, {w}" for w in _OPERAND_WORDS),
    *(f"SELECT id + {w} - 1 FROM incident" for w in _OPERAND_WORDS),
    # Quoted for each engine's identifier quote, in the same positions.
    *(f'SELECT id FROM incident WHERE "{w}" = 1 ORDER BY "{w}"' for w in _WORDS),
    *(f"SELECT id FROM incident WHERE `{w}` = 1 GROUP BY `{w}`" for w in _WORDS),
    *(f'SELECT "{w}" FROM "{w}" AS "{w}"' for w in _WORDS),
    *(f"SELECT `{w}` FROM `demo`.`{w}`" for w in _WORDS),
    # Case, odd whitespace and comments between the operator and the word.
    "select id from incident where\n\tNotify = 1",
    "SELECT id FROM incident WHERE /* the flag */ notify = 1",
    "SELECT id FROM incident WHERE id = 1 AND -- the flag\n lock = 0",
    "SELECT id FROM incident ORDER   BY\r\nLOCK",
    "SELECT COUNT(DISTINCT notify) FROM incident",
    "SELECT id FROM incident ORDER BY id, lock",
    "WITH x AS (SELECT notify FROM incident WHERE lock = 1) SELECT notify FROM x ORDER BY notify",
]


@pytest.mark.parametrize("sql", OPERAND_ACCEPT)
def test_a_bare_name_in_operand_position_is_accepted(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is None, f"identifier refused as a keyword: {sql!r} -> {refusal}"


OPERAND_REJECT = [
    # Stacked statements, with and without quoted-identifier tricks ahead of the `;`.
    'SELECT "x";NOTIFY ch',
    "SELECT `x`;NOTIFY ch",
    'SELECT id FROM t WHERE "a" = 1; NOTIFY ch',
    "SELECT 1;LISTEN ch",
    "SELECT 1; SET search_path = evil",
    "SELECT 1 ; RESET ALL",
    "SELECT 1;\nCOPY t TO PROGRAM 'x'",
    "SELECT 1; CALL proc()",
    "SELECT 1; EXECUTE stmt",
    "SELECT 1; PREPARE p AS SELECT 1",
    "SELECT 1; LOCK TABLE t",
    # The statement forms themselves, in any case.
    "notify ch",
    "LiStEn ch",
    "lock table t in exclusive mode",
    "set role admin",
    "reset all",
    "copy t to stdout",
    "call proc()",
    "execute stmt",
    "prepare p as select 1",
    "select * into t2 from t",
    "SELECT * INTO TEMP t2 FROM t",
    "SELECT a FROM t INTO OUTFILE '/tmp/x'",
    # SQL Server runs statements without a `;` between them: a keyword after a complete operand
    # starts a new statement there, so it must not be exempt.
    "SELECT id FROM t WHERE a = 1 EXECUTE xp_cmdshell 'dir'",
    "SELECT id FROM t WHERE a = b SET ROWCOUNT 1",
    "SELECT id FROM t ORDER BY id EXECUTE sp_demo",
    "SELECT 1 ELSE EXECUTE sp_demo",
    "SELECT 1 IF 1 = 1 SELECT 2 ELSE DELETE FROM t",
    "SELECT 1 THEN EXECUTE sp_demo",
    "SELECT id FROM t AS a EXECUTE sp_demo",
    "SELECT id FROM t a EXECUTE sp_demo",
    "SELECT id FROM t notify",
    # The operator must be real SQL, not text inside a quoted name or a string.
    'SELECT "a =" EXECUTE sp_demo',
    "SELECT `a =` EXECUTE sp_demo",
    'SELECT "WHERE" EXECUTE sp_demo',
    "SELECT `AND` EXECUTE sp_demo",
    'SELECT "a,"EXECUTE sp_demo',
    "SELECT 'a =' EXECUTE sp_demo",
    "SELECT 1 /* = */ EXECUTE sp_demo",
    # A string blanked to a space is still a complete operand, and so is an empty quoted name
    # (a string on MySQL).
    "SELECT id FROM t WHERE a = 'x' EXECUTE sp_demo",
    "SELECT id FROM t WHERE a = $$x$$ EXECUTE sp_demo",
    "SELECT id FROM t WHERE a = N'x' EXECUTE sp_demo",
    'SELECT id FROM t WHERE a = "" LOCK IN SHARE MODE',
    "SELECT id FROM t WHERE a = `` LOCK IN SHARE MODE",
    "SELECT notify 'x'",
    "SELECT notify 'x';",
    "SELECT notify $$x$$ ;",
    "SELECT a, notify 'x'",
    'SELECT a, notify"x"',
    # A non-ASCII space is not a separator the rule accepts.
    "SELECT id FROM t WHERE\u00a0EXECUTE sp_demo",
    "SELECT DISTINCT\u00a0EXECUTE sp_demo",
    "SELECT 1 -- =\n EXECUTE sp_demo",
    # A keyword-looking word that is part of a name is not the keyword.
    "SELECT @where EXECUTE sp_demo",
    "SELECT t.where EXECUTE sp_demo",
    "SELECT x$and EXECUTE sp_demo",
    "SELECT x_by EXECUTE sp_demo",
    # Identifiers holding quotes or keywords, and backticks inside double quotes.
    'SELECT "a`" NOTIFY ch',
    'SELECT "`" EXECUTE sp_demo',
    'SELECT "x"" =" EXECUTE sp_demo',
    'SELECT "notify" FROM t; NOTIFY ch',
    'SELECT "lock" INTO t2 FROM t',
    "SELECT `notify` INTO t2 FROM t",
    # A write wrapped in a CTE, or opened from a paren, is never in operand position.
    "WITH x AS (INSERT INTO t VALUES (1) RETURNING id) SELECT id FROM x",
    "WITH x AS MATERIALIZED (DELETE FROM t RETURNING id) SELECT id FROM x",
    "WITH x AS (SELECT 1) UPDATE t SET a = 1",
    "WITH x AS (SELECT 1) DELETE FROM t",
    "WITH x AS (SELECT 1) MERGE INTO t USING x ON 1 = 1 WHEN MATCHED THEN DELETE",
    "SELECT * FROM (DELETE FROM t RETURNING id) AS d",
    "SELECT COUNT(INTO) FROM t",
    # INTO is never exempt bare, in any position.
    "SELECT id FROM t WHERE a = into",
    "SELECT id FROM t ORDER BY into",
    "SELECT id FROM into",
    "SELECT id AS into FROM t",
    # MySQL's row lock, after a complete operand.
    "SELECT id FROM t WHERE a = 1 LOCK IN SHARE MODE",
    "SELECT id FROM t ORDER BY id LOCK IN SHARE MODE",
    # The other gates still fire after an exempt name.
    "SELECT id FROM t WHERE notify = pg_sleep(10)",
    "SELECT id FROM t WHERE lock = 1 FOR UPDATE",
]


@pytest.mark.parametrize("sql", OPERAND_REJECT)
def test_operand_position_opens_no_bypass(sql: str) -> None:
    assert check_read_only(sql) is not None, f"slipped through the read-only gate: {sql!r}"
