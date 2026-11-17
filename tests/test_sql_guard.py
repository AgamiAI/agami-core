"""
Tests for `sql_guard` — the hardened read-only / dangerous-SQL gate shared by the
stdio server, the HTTP/OAuth server, the agami-query skill, and cron.

Two things are pinned here:

  1. **Security.** Every write / DDL / transaction-control / session-state /
     dangerous-function / multi-statement / comment-or-quote-bypass vector is
     rejected — including the vectors that historically bypassed naive guards
     (comment-in-string, PG-quoted function names, comment-welded word boundaries).

  2. **No false positives.** A large corpus of the analytics SQL an assistant emits
     every day MUST pass. Over-tightening the deny-list silently degrades every
     query, so this corpus is the primary safety net.

Bare `pg_catalog` / `information_schema` / environment-introspection blocking is a
deferred follow-up (see `test_known_deferred_gaps_currently_pass` for the pinned
current behavior).

`sql_guard.check_read_only` returns None (safe) or a `guardrail.Refusal` (rejected).

Every reject corpus below is a module-level constant rather than an inline literal in its
`@pytest.mark.parametrize`. That is what lets `test_ace035_read_only_refusal.py` assert the refusal
contract over the WHOLE corpus by importing it, instead of copying ~150 strings into a second file
where the two would drift apart the first time a vector is added here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest
from guardrail import RULE_READ_ONLY
from sql_guard import _MAX_SQL_CHARS, _REMEDIATION, _GuardReject, _neutralize, check_read_only

# ---------------------------------------------------------------------------
# Accept — valid single read-only statements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",  # case-insensitive
        "  SELECT 1  ",  # leading/trailing whitespace
        "SELECT 1;",  # one trailing semicolon allowed
        "SELECT 1 ; ",  # trailing semicolon with whitespace
        "WITH a AS (SELECT 1) SELECT * FROM a",  # CTE
        "(SELECT 1) UNION (SELECT 2)",  # parenthesized set operation
        "-- comment\nSELECT 1",  # leading line comment
        "/* block comment */ SELECT 1",  # leading block comment
        "/* multi\nline\ncomment */SELECT 1",  # multi-line block comment
        # Identifiers whose substrings overlap DML/DDL keywords — the `\b`
        # word-boundary in the deny-list must NOT false-positive on these.
        "SELECT updated_at FROM users",
        "SELECT deleted_at, updated_at FROM accounts",
        "SELECT * FROM deleted_records",
        "SELECT drop_count FROM stats",
        "SELECT * FROM events WHERE event_name = 'user_created'",
        "SELECT * FROM jsonb_call_data WHERE id = 1",
    ],
)
def test_accepts_valid_selects(sql: str) -> None:
    assert check_read_only(sql) is None, f"Expected pass, got rejection: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # Identifiers overlapping the expanded deny-list keywords.
        "SELECT committed_at FROM events",  # COMMIT
        "SELECT rollback_count FROM stats",  # ROLLBACK
        "SELECT begin_date, end_date FROM bookings",  # BEGIN / END
        "SELECT set_id FROM datasets",  # SET
        "SELECT reset_count FROM users",  # RESET
        "SELECT discard_pile FROM games",  # DISCARD
        "SELECT lock_version FROM accounts",  # LOCK
        "SELECT prepared_at FROM orders",  # PREPARE
        "SELECT pinto_color FROM cars",  # 'into' substring
        "SELECT intolerance_level FROM patients",  # starts with 'into'
        "SELECT set_config_id FROM audit",  # not a set_config( call
        "SELECT pg_advisory_lock_id FROM custom_table",  # column, not a call
        "SELECT a.id FROM accounts a JOIN contacts c ON a.id = c.account_id",  # aliases
    ],
)
def test_accepts_identifiers_with_keyword_substrings(sql: str) -> None:
    assert check_read_only(sql) is None, (
        f"Legit identifier-with-keyword-substring rejected: {sql!r}"
    )


@pytest.mark.parametrize(
    "sql",
    [
        # `CASE ... END` — END must NOT be in the deny-list.
        "SELECT CASE WHEN status = 'open' THEN 1 ELSE 0 END AS is_open FROM tickets",
        "SELECT name, CASE x WHEN 1 THEN 'a' WHEN 2 THEN 'b' ELSE 'c' END AS bucket FROM t",
        "SELECT SUM(CASE WHEN region = 'NA' THEN amount END) FROM orders",
        "SELECT CASE WHEN a > 0 THEN CASE WHEN b > 0 THEN 'pp' ELSE 'pn' END ELSE 'n' END FROM t",
    ],
)
def test_accepts_case_when_end(sql: str) -> None:
    assert check_read_only(sql) is None, f"CASE...END false-positived: {sql!r}"


# ---------------------------------------------------------------------------
# Reject — non-SELECT (DML / DDL)
# ---------------------------------------------------------------------------


REJECT_NON_SELECT = [
    "INSERT INTO x VALUES (1)",
    "UPDATE x SET a = 1",
    "DELETE FROM x",
    "DROP TABLE x",
    "TRUNCATE TABLE x",
    "ALTER TABLE x ADD COLUMN y INT",
    "CREATE TABLE x (a INT)",
    "COPY x FROM '/etc/passwd'",
    "GRANT SELECT ON x TO public",
    "REVOKE ALL ON x FROM public",
    "VACUUM x",
    "MERGE INTO x USING y ON x.a = y.a",
    "CALL my_proc()",
]


@pytest.mark.parametrize("sql", REJECT_NON_SELECT)
def test_rejects_non_select(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Expected rejection: {sql!r}"


REJECT_MULTI_STATEMENT = [
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE x",
    "SELECT 1;DELETE FROM x",  # no whitespace around semicolon
    "SELECT 1; -- second statement after comment\nSELECT 2",
    "WITH a AS (SELECT 1) SELECT * FROM a; DROP TABLE a",
]


@pytest.mark.parametrize("sql", REJECT_MULTI_STATEMENT)
def test_rejects_multi_statement(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Expected rejection: {sql!r}"


REJECT_EMPTY = ["", "   ", None, "-- just a comment", "/* only a comment */"]


@pytest.mark.parametrize("sql", REJECT_EMPTY)
def test_rejects_empty(sql: Any) -> None:
    assert check_read_only(sql) is not None


# ---------------------------------------------------------------------------
# Reject — known bypasses
# ---------------------------------------------------------------------------


REJECT_COMMENT_IN_STRING = [
    # Comment-inside-string bypass: prior strip-order (comments first) let `--`
    # inside a literal eat through end-of-line and hide an injected `; DROP ...`.
    "SELECT '--'; DROP TABLE x",
    "SELECT 'foo' || '--'; UPDATE t SET a=1",
    "SELECT '-- not a comment' AS s; DELETE FROM t",
    "SELECT '--' ; DROP TABLE x",
]


@pytest.mark.parametrize("sql", REJECT_COMMENT_IN_STRING)
def test_rejects_comment_in_string_bypass(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Comment-in-string bypass not caught: {sql!r}"


REJECT_HASH_AMBIGUOUS = [
    # `#` outside a literal has four meanings across the engines the executor speaks, and all
    # four are refused. This lexer has no dialect, so it declines to pick one — the same call
    # the bare `--x` case makes. The list is the measured blast radius, not just the case the
    # fix was written for: a widening nobody wrote down is a widening nobody can review.
    #
    # 1. MySQL / MariaDB line comment — the over-refusal being cured.
    "SELECT a FROM t # DROP TABLE t",     # body used to be reported as a denied keyword
    "SELECT a FROM t #no space",          # MySQL needs no space after `#`
    "SELECT a FROM t # trailing note",    # inert on both readings, still ambiguous
    "SELECT a FROM t\n# DROP TABLE t",    # comment on its own line
    # 2. PostgreSQL operators. `#` is bit-string XOR and geometric intersection; `#>` and `#>>`
    #    are the jsonb path operators, which are ordinary analytics SQL and the likeliest real
    #    casualty of this branch. Newly refused, deliberately.
    "SELECT a # b FROM t",
    "SELECT payload #>> '{ssn}' FROM orders",
    "SELECT payload #> '{cust,ssn}' FROM orders",
    # 3. SQL Server temp tables. Newly refused, and note there is no comment anywhere in these.
    "SELECT a FROM #temp",
    "SELECT a FROM ##global_temp",
    # 4. Bracket quoted identifiers containing `#`. This scan does not consume that form, so
    #    the `#` is seen as if bare. Refusing is the safe direction. (A backtick-quoted name is
    #    consumed now, so `#` inside one is data — see the test below.)
    "SELECT [col#name] FROM t",
]


@pytest.mark.parametrize("sql", REJECT_HASH_AMBIGUOUS)
def test_rejects_hash_as_dialect_ambiguous(sql: str) -> None:
    """Every `#` outside a literal is refused, and always for the ambiguity rather than for
    whatever its body happened to spell.

    The bug this closes: `#` was not a comment to the neutralizer, so the body reached the
    deny-list and the statement-separator check. `# DROP TABLE t` came back as "keyword 'DROP' is
    not allowed" and `# note; more` as "multiple statements are not allowed" — both refusals of
    valid MySQL, and both naming a fix that would not have helped, since neither the DROP nor the
    second statement was ever going to run.

    Asserting one exact detail across all eleven vectors is the point rather than a shortcut: the
    defect was a refusal whose stated reason depended on what the unread text happened to contain,
    so the fix is only real if every one of these reports the same thing. That is also why the
    detail does not use the word "comment" — four of these vectors contain no comment at all.
    """
    refusal = check_read_only(sql)
    assert refusal is not None, f"ambiguous `#` form not caught: {sql!r}"
    assert refusal.rule == RULE_READ_ONLY
    assert refusal.detail == (
        "'#' means different things on different engines (a line comment in MySQL, an operator "
        "in PostgreSQL, a temp-table prefix in SQL Server), so a statement carrying one outside "
        "a string literal cannot be read the same way on all of them"
    )
    assert refusal.remediation == _REMEDIATION["hash_ambiguous"]


@pytest.mark.parametrize(
    "sql",
    [
        # A `#` inside a span this scan consumes is data, not syntax. Those branches advance
        # past their own body before the `#` branch can see it, so these must keep passing —
        # the fix above must not become a ban on the character.
        "SELECT '#' AS h FROM t",
        "SELECT a FROM t WHERE b = '# not a comment, really'",
        "SELECT a FROM t WHERE b = 'DROP TABLE t # nope'",
        'SELECT "col#name" FROM t',
        "SELECT `col#name` FROM t",        # backtick-quoted identifier
        "SELECT $$ # not a comment $$ AS s FROM t",   # dollar-quoted body
        "SELECT a FROM t -- # inside a line comment\n",
        "SELECT a FROM t /* # inside a block comment */",
    ],
)
def test_hash_inside_a_consumed_span_is_still_data(sql: str) -> None:
    """Scoped deliberately to the spans `_neutralize` actually consumes: single quotes, double
    quotes, dollar quotes, and the two comment forms.

    It is NOT true of every quoted identifier. There is no bracket branch in this scan, so
    `[col#name]` is refused — it is in `REJECT_HASH_AMBIGUOUS` above rather than here. That is
    the safe direction and it is recorded rather than implied.
    """
    assert check_read_only(sql) is None, f"`#` in a consumed span must not be refused: {sql!r}"


REJECT_DATA_MODIFYING_CTES = [
    # Data-modifying CTEs — open with WITH so the opener alone lets them through;
    # the deny-list scan catches the DML keyword.
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM x",
    "WITH x AS (SELECT 1), y AS (DELETE FROM t RETURNING *) SELECT * FROM x, y",
]


@pytest.mark.parametrize("sql", REJECT_DATA_MODIFYING_CTES)
def test_rejects_data_modifying_ctes(sql: str) -> None:
    assert check_read_only(sql) is not None, f"DML CTE not caught: {sql!r}"


REJECT_TRANSACTION_CONTROL = [
    "COMMIT",
    "COMMIT; SELECT 1",
    "ROLLBACK",
    "BEGIN",
    "BEGIN TRANSACTION READ ONLY",
    "SAVEPOINT sp1",
    "RELEASE SAVEPOINT sp1",
    "START TRANSACTION",
    "END",
]


@pytest.mark.parametrize("sql", REJECT_TRANSACTION_CONTROL)
def test_rejects_transaction_control(sql: str) -> None:
    assert check_read_only(sql) is not None, f"TCL not blocked: {sql!r}"


REJECT_SESSION_STATE = [
    "SET statement_timeout = 0",
    "SET search_path = pg_catalog, public",
    "RESET ALL",
    "RESET statement_timeout",
    "DISCARD ALL",
    "DISCARD TEMP",
]


@pytest.mark.parametrize("sql", REJECT_SESSION_STATE)
def test_rejects_session_state(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Session-state mutation not blocked: {sql!r}"


REJECT_PUBSUB_LOCK_PREPARED = [
    "LISTEN channel1",
    "NOTIFY channel1, 'payload'",
    "UNLISTEN channel1",
    "LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE",
    "PREPARE plan1 AS SELECT 1",
    "DEALLOCATE plan1",
    "DEALLOCATE ALL",
    "WITH x AS (LISTEN ch) SELECT 1",  # deny-list catches it mid-statement
]


@pytest.mark.parametrize("sql", REJECT_PUBSUB_LOCK_PREPARED)
def test_rejects_pubsub_lock_prepared(sql: str) -> None:
    assert check_read_only(sql) is not None, f"pub/sub | lock | prepared not blocked: {sql!r}"


REJECT_ROW_LEVEL_LOCKS = [
    # `FOR UPDATE` / `FOR NO KEY UPDATE` are caught by the `UPDATE` keyword deny
    # (which runs before the row-lock rule) — still rejected, just with the
    # keyword message. `FOR SHARE` / `FOR KEY SHARE` fall through to the row-lock rule.
    "SELECT * FROM accounts FOR UPDATE",
    "SELECT id FROM accounts WHERE id = 1 FOR SHARE",
    "SELECT * FROM accounts FOR NO KEY UPDATE",
    "SELECT id FROM accounts FOR KEY SHARE OF accounts",
]


@pytest.mark.parametrize("sql", REJECT_ROW_LEVEL_LOCKS)
def test_rejects_row_level_locks(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Row-level lock not blocked: {sql!r}"


REJECT_NAMED_ROW_LOCK = ["SELECT id FROM accounts FOR SHARE", "SELECT id FROM t FOR KEY SHARE"]


@pytest.mark.parametrize("sql", REJECT_NAMED_ROW_LOCK)
def test_row_lock_rule_names_the_lock(sql: str) -> None:
    # These use SHARE (not a deny keyword) so the row-lock rule is what fires.
    refusal = check_read_only(sql)
    assert refusal is not None and "lock" in refusal.detail.lower(), refusal


REJECT_SELECT_INTO = [
    "SELECT * INTO new_table FROM users",
    "SELECT id, email INTO archive_users FROM users WHERE deleted_at IS NOT NULL",
    "SELECT 1 INTO scratch FROM dual",
]


@pytest.mark.parametrize("sql", REJECT_SELECT_INTO)
def test_rejects_select_into_write_path(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"SELECT INTO not blocked: {sql!r}"
    assert "INTO" in refusal.detail


REJECT_DANGEROUS_FUNCTIONS = [
        # Time-wasters / file I/O / OS exec / remote SQL / process control.
        "SELECT pg_sleep(10)",
        "SELECT pg_sleep_for('10s')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_read_binary_file('/etc/passwd')",
        "SELECT pg_ls_dir('/var/lib/postgresql')",
        "SELECT pg_stat_file('/etc/passwd')",
        "SELECT pg_read_server_files('/')",
        "SELECT pg_write_server_files('/tmp/x', 'data')",
        "SELECT lo_export(12345, '/tmp/leak')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT dblink('host=evil.example.com', 'select 1')",
        "SELECT dblink_exec('host=evil.example.com', 'drop table x')",
        "SELECT pg_terminate_backend(123)",
        "SELECT pg_cancel_backend(123)",
        "SELECT pg_reload_conf()",
        # Sequence mutation — real writes that open with SELECT.
        "SELECT setval('users_id_seq', 100)",
        "SELECT nextval('order_seq')",
        # Server / replication / stats control.
        "SELECT pg_stat_reset()",
        "SELECT pg_stat_reset_shared('bgwriter')",
        "SELECT pg_stat_statements_reset()",
        "SELECT pg_switch_wal()",
        "SELECT pg_create_restore_point('x')",
        "SELECT pg_drop_replication_slot('s')",
        "SELECT pg_replication_slot_advance('s', '0/0')",
        # Post-audit additions.
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT current_setting('statement_timeout')",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_advisory_xact_lock(1)",
        "SELECT pg_advisory_unlock(1)",
        "SELECT pg_advisory_unlock_all()",
        "SELECT query_to_xml('SELECT * FROM pg_tables', false, false, '')",
        "SELECT query_to_xmlschema('SELECT 1', false, false, '')",
        "SELECT query_to_json('SELECT 1')",
        "SELECT cursor_to_xml('foo', 1, false, false, '')",
        "SELECT pg_rotate_logfile()",
        "SELECT pg_logfile_rotate()",
        "SELECT copy_program('cat /etc/passwd')",
]


@pytest.mark.parametrize("sql", REJECT_DANGEROUS_FUNCTIONS)
def test_rejects_dangerous_functions(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Dangerous function not blocked: {sql!r}"


# The same class as `REJECT_DANGEROUS_FUNCTIONS` above, on the engines that list did not speak.
# Kept as its own corpus because the reason each one is here is the engine, not the verb: the list
# above was written against Postgres server-side primitives, and `execute_sql` dispatches to eleven
# more. The tool advertises the MCP `readOnlyHint` on every one of them, so a vector that passes on
# MySQL falsifies the same claim that a `pg_sleep` would.
REJECT_DIALECT_SIDE_EFFECT_FUNCTIONS = [
    # ----- Postgres holes in families the list above already claimed -----
    # `pg_notify` is the function spelling of the denied `NOTIFY` keyword; `\bNOTIFY\b` cannot see
    # it, because `_` is a word character.
    "SELECT pg_notify('deploy', 'go')",
    # The advisory-lock family beyond the four names that were listed.
    "SELECT pg_try_advisory_lock(1)",
    "SELECT pg_try_advisory_xact_lock(1)",
    "SELECT pg_advisory_lock_shared(1)",
    "SELECT pg_try_advisory_lock_shared(1)",
    "SELECT pg_advisory_unlock_shared(1)",
    # Backup / recovery / replication control — the siblings of the already-denied
    # `pg_drop_replication_slot`. `pg_logical_slot_get_changes` is a destructive read.
    "SELECT pg_promote()",
    "SELECT pg_wal_replay_pause()",
    "SELECT pg_wal_replay_resume()",
    "SELECT pg_backup_start('label')",
    "SELECT pg_stop_backup()",
    "SELECT pg_create_logical_replication_slot('s', 'test_decoding')",
    "SELECT pg_copy_physical_replication_slot('a', 'b')",
    "SELECT pg_logical_emit_message(true, 'prefix', 'payload')",
    "SELECT pg_logical_slot_get_changes('s', NULL, NULL)",
    "SELECT pg_replication_origin_create('o')",
    # ----- MySQL / MariaDB -----
    "SELECT SLEEP(30)",
    "SELECT BENCHMARK(1000000, MD5('x'))",
    "SELECT GET_LOCK('x', 10)",
    "SELECT RELEASE_LOCK('x')",
    "SELECT RELEASE_ALL_LOCKS()",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT MASTER_POS_WAIT('binlog.000001', 4)",
    "SELECT SOURCE_POS_WAIT('binlog.000001', 4)",
    "SELECT WAIT_FOR_EXECUTED_GTID_SET('uuid:1')",
    # ----- Snowflake — the namespace, not a member list -----
    "SELECT SYSTEM$ABORT_SESSION(123)",
    "SELECT SYSTEM$CANCEL_ALL_QUERIES(123)",
    "SELECT SYSTEM$WAIT(30)",
    "SELECT SYSTEM$PIPE_FORCE_RESUME('p')",
    # ----- Databricks / Spark SQL — JVM reflection from a SELECT -----
    "SELECT reflect('java.lang.Runtime', 'getRuntime')",
    "SELECT java_method('java.lang.Runtime', 'getRuntime')",
    # ----- SQL Server — remote SQL and server-file access from the FROM clause -----
    "SELECT x FROM OPENROWSET(BULK '/etc/passwd', SINGLE_CLOB) AS t(x)",
    "SELECT x FROM OPENQUERY(linked_server, 'SELECT 1')",
    "SELECT x FROM OPENDATASOURCE('SQLOLEDB', 'Data Source=evil').db.dbo.t",
    # ----- BigQuery — federated remote SQL -----
    "SELECT x FROM EXTERNAL_QUERY('conn', 'SELECT 1')",
    # ----- Oracle — package-qualified sleep, pipes, nested SQL and network egress -----
    # `DBMS_LOCK.REQUEST` and `DBMS_PIPE.RECEIVE_MESSAGE` are FUNCTIONS, so these are call
    # shapes Oracle actually accepts in a bare SELECT. `DBMS_LOCK.SLEEP` is a procedure and is
    # not spelled here for that reason — it needs a PL/SQL block this gate never sees.
    "SELECT DBMS_LOCK.REQUEST(1, 6, 10, TRUE) FROM dual",
    "SELECT DBMS_PIPE.RECEIVE_MESSAGE('p', 10) FROM dual",
    "SELECT DBMS_XMLGEN.GETXML('SELECT 1 FROM dual') FROM dual",
    "SELECT DBMS_XSLPROCESSOR.READ2CLOB('/etc', 'passwd') FROM dual",
    "SELECT UTL_HTTP.REQUEST('http://evil.example.com') FROM dual",
    "SELECT UTL_INADDR.GET_HOST_ADDRESS('evil.example.com') FROM dual",
    "SELECT HTTPURITYPE('http://evil.example.com').GETCLOB() FROM dual",
    # ----- SQLite / DuckDB — loading a shared library is code execution -----
    "SELECT load_extension('/tmp/evil.so')",
]


@pytest.mark.parametrize("sql", REJECT_DIALECT_SIDE_EFFECT_FUNCTIONS)
def test_rejects_dialect_side_effect_functions(sql: str) -> None:
    """The `readOnlyHint` is one claim over every engine, so every engine's side effects are denied.

    Each vector is a SINGLE SELECT: it opens with the allowed keyword, carries no denied keyword and
    no row lock, and reaches the database untouched on `main` before this list existed. The gate that
    has to stop it is the dangerous-function step and nothing earlier, which is why these are worth
    pinning separately from the write/DDL vectors that three other steps would also catch.
    """
    assert check_read_only(sql) is not None, f"Side-effecting function not blocked: {sql!r}"


# The cost of the bare-word entries, pinned as a refusal rather than argued in a comment.
#
# `\bWORD\s*\(` matches the two SQL constructs with that shape — a function call, and the column
# list of a CTE or derived table — and cannot tell them apart. These four are legitimate analytics
# SQL that `origin/main` accepted and this gate now refuses, naming a function the statement does
# not contain. They are here, not in the false-positive corpus, because refusing them is the
# CHOSEN behaviour: a lookaround narrow enough to admit them would let a real call through in some
# position, and this gate fails closed. `test_false_positive_guard_legitimate_analytics_sql` pins
# the neighbouring shapes that do still pass (`sleep_minutes`, `AVG(sleep)`, a `benchmark` column),
# so the boundary is recorded from both sides. If the trade is ever judged the wrong way round,
# the fix is to drop `sleep` and `benchmark` from the deny-list — they are pure time-wasters, and
# the executor's per-statement timeout already bounds them — and to move these rows across.
REJECT_CTE_NAME_COLLISION = [
    "WITH benchmark (region, target) AS (SELECT region, 100 FROM regions) SELECT region FROM benchmark",
    "WITH sleep (patient_id, hours) AS (SELECT patient_id, hours FROM sleep_log) SELECT AVG(hours) FROM sleep",
    "WITH reflect (id, score) AS (SELECT id, score FROM surveys) SELECT id FROM reflect",
    "SELECT a.region FROM actuals a JOIN (SELECT region FROM t) AS benchmark (region) ON a.region = benchmark.region",
]


@pytest.mark.parametrize("sql", REJECT_CTE_NAME_COLLISION)
def test_a_cte_named_after_a_denied_function_is_refused(sql: str) -> None:
    """Pins the known over-refusal, and that it is the deny-list step producing it.

    Asserting the STEP matters: if a later change made these pass, this test would fail and the
    trade-off gets re-decided deliberately instead of drifting.
    """
    refusal = check_read_only(sql)
    assert refusal is not None, f"expected the pinned over-refusal: {sql!r}"
    assert "is not allowed" in refusal.detail, refusal.detail
    assert refusal.remediation == _REMEDIATION["dangerous_function"]


def test_dialect_side_effect_rejection_names_the_function() -> None:
    """The refusal echoes the caller's own token, so an agent can see which call to drop.

    `detail` is the only caller-specific text any rejection carries (see `_REMEDIATION`), and a
    dialect vector is the case where a generic "dangerous function" would leave the author guessing:
    the denied name may be one of several calls in the statement.
    """
    refusal = check_read_only("SELECT id, SYSTEM$WAIT(30) FROM orders")
    assert refusal is not None
    assert "SYSTEM$WAIT" in refusal.detail
    assert refusal.remediation == _REMEDIATION["dangerous_function"]


def over_length_payload() -> str:
    """SQL past the length cap. A function rather than a constant so the ~50KB string is built only
    by the tests that need it (here and the refusal-contract corpus), not at every import."""
    return "SELECT 1, " + ("a, " * 30_000) + "1"


def test_rejects_over_length_cap() -> None:
    payload = over_length_payload()
    assert len(payload) > _MAX_SQL_CHARS
    refusal = check_read_only(payload)
    assert refusal is not None
    assert "50000" in refusal.detail or "caps" in refusal.detail


def test_length_cap_exact_boundary() -> None:
    """At exactly `_MAX_SQL_CHARS` accept; one over must reject. Off-by-one guard."""
    at_cap = "SELECT 1" + (" " * (_MAX_SQL_CHARS - len("SELECT 1")))
    assert len(at_cap) == _MAX_SQL_CHARS
    assert check_read_only(at_cap) is None

    over_cap = at_cap + " "
    assert len(over_cap) == _MAX_SQL_CHARS + 1
    assert check_read_only(over_cap) is not None


# ---------------------------------------------------------------------------
# Adversarial / red-team
# ---------------------------------------------------------------------------


REJECT_QUOTED_DANGEROUS_FN = [
        # PG-quoted identifier for a dangerous function name. The gate must strip
        # the `"` chars (not the contents) so `"pg_sleep"(10)` reduces to `pg_sleep(10)`.
        'SELECT "pg_sleep"(10)',
        'SELECT "pg_sleep" ( 10 )',
        'SELECT "PG_SLEEP"(10)',
        'SELECT "Pg_Sleep"(10)',
        'SELECT pg_catalog."pg_sleep"(10)',
        'SELECT "pg_catalog".pg_sleep(10)',
        'SELECT "pg_catalog"."pg_sleep"(10)',
        "SELECT \"pg_read_file\"('/etc/passwd')",
        'SELECT "pg_terminate_backend"(123)',
        "SELECT \"set_config\"('statement_timeout', '0', false)",
        "SELECT \"dblink\"('host=evil', 'select 1')",
        'SELECT "pg_advisory_lock"(1)',
        "SELECT \"query_to_xml\"('SELECT 1', false, false, '')",
]


@pytest.mark.parametrize("sql", REJECT_QUOTED_DANGEROUS_FN)
def test_red_team_quoted_dangerous_fn_bypass(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Quoted-fn bypass NOT blocked: {sql!r}"


REJECT_WELDED_QUOTED_DANGEROUS_FN = [
        # WELDED quoted identifier — no whitespace between the preceding keyword and the
        # opening `"`. A delimited identifier is self-delimiting in SQL (the quote IS the
        # token boundary), so these are valid statements the engine happily runs; verified
        # on PostgreSQL 16 (`SELECT"pg_read_file"('/tmp/x')` returns the file contents).
        # Dropping the quotes without re-supplying a separator fuses two tokens into one
        # (`FROM"pg_class"` -> `FROMpg_class`), which destroys the `\b` anchor every
        # deny-list pattern relies on and silently blinds the gate.
        "SELECT\"pg_read_file\"('/etc/passwd')",
        "SELECT*FROM\"pg_read_file\"('/etc/passwd')",
        'SELECT"pg_sleep"(10)',
        'SELECT"dblink"(\'host=evil\', \'select 1\')',
        'SELECT"pg_terminate_backend"(123)',
        'SELECT"set_config"(\'statement_timeout\', \'0\', false)',
        'SELECT 1 FROM"pg_class"WHERE"pg_sleep"(10) IS NULL',
        # The weld can also appear mid-statement, after a non-keyword word char.
        'SELECT a FROM t WHERE b="pg_sleep"(1)',
        # The CLOSING quote delimits too, so a keyword can hide on the trailing side.
        # `SELECT ... INTO <table>` is a write that opens with SELECT, which is exactly
        # why INTO is in the DML/DDL list; `\bINTO\b` does not match `xINTO`. Verified on
        # PostgreSQL 16: each of these creates a table holding the source rows.
        'SELECT "x"INTO evil FROM t',
        'SELECT"x"INTO evil FROM t',
        'SELECT t."x"INTO evil FROM t',
        'SELECT 1 AS"a"INTO evil',
        'WITH c AS (SELECT 1 AS "x")SELECT "x"INTO evil FROM c',
        # Row-lock rule, same root cause. FOR UPDATE survives only because bare UPDATE is
        # independently in the DML list; FOR SHARE has no such backstop.
        'SELECT * FROM t AS"a"FOR SHARE',
        'SELECT * FROM t AS"a"FOR KEY SHARE',
]


@pytest.mark.parametrize("sql", REJECT_WELDED_QUOTED_DANGEROUS_FN)
def test_red_team_welded_quoted_dangerous_fn_bypass(sql: str) -> None:
    """A quoted identifier welded to the preceding token must not escape the gate.

    Regression for the `_neutralize` weld: every pre-existing case in the corpus above
    happens to carry a space before the `"`, so the missing separator was invisible.
    """
    assert check_read_only(sql) is not None, f"Welded quoted-fn bypass NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # The separator must be re-supplied ONLY where the quote was actually delimiting
        # two word chars. A blanket space would break qualified names — `t."col"` must stay
        # `t.col`, not become `t. col` — so these legitimate reads must still pass.
        'SELECT t."current_user" FROM t',
        'SELECT "order id" FROM orders',
        'SELECT "name", "email" FROM customers',
        'SELECT c."email" FROM customers c',
        'SELECT "schema"."table" FROM "schema"."table"',
        'SELECT a."b" FROM x a',
    ],
)
def test_welded_fix_does_not_over_block_legitimate_quoted_identifiers(sql: str) -> None:
    """The weld fix must not turn a qualified quoted column into a false positive."""
    assert check_read_only(sql) is None, f"Legitimate quoted identifier wrongly blocked: {sql!r}"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # A separator is re-supplied ONLY where the quote actually separated two word
        # chars — so a qualified name survives as ONE token.
        ('SELECT t."current_user" FROM t', "SELECT t.current_user FROM t"),
        ('SELECT "schema"."table" FROM "schema"."table"', "SELECT schema.table FROM schema.table"),
        ('SELECT c."email" FROM customers c', "SELECT c.email FROM customers c"),
        # ...and IS re-supplied on both sides where it was separating word chars.
        ('SELECT * FROM"pg_class"', "SELECT * FROM pg_class"),
        ('SELECT "x"INTO evil FROM t', "SELECT x INTO evil FROM t"),
        ('SELECT 1 AS"a"INTO evil', "SELECT 1 AS a INTO evil"),
    ],
)
def test_neutralize_preserves_token_structure(sql: str, expected: str) -> None:
    """Pin the *shape* of the neutralized text, not just the gate's yes/no.

    Every rule here is `\\b`-anchored, so a blanket separator on both sides would block the
    same attacks and pass the same negatives — the gate-level tests alone cannot tell the
    two designs apart, and a future refactor could silently swap one for the other. What a
    blanket space would change is token *structure*: `t."col"` would become `t. col`, two
    tokens where the statement meant one. Any rule that reasons about qualification (a
    pattern anchored on a preceding `.`, say) would then read a qualified column as a bare
    identifier. Asserting the neutralized string keeps that decision checkable.

    The assertion compares `.text` rather than the bare return value: ACE-039 gave
    `_neutralize` a second output (the quoted-identifier spans the recon gate's niladic
    matcher consults) and folded the caller's `.strip()` inside, so one coordinate frame
    exists instead of three. A deliberate contract change, not a weakening — the six cases
    and the property they pin are untouched.
    """
    assert _neutralize(sql).text == expected


REJECT_COMMENT_BREAKS_GATE = [
    # Block comments between keywords must not defeat `\b` word boundaries —
    # comments are stripped to SPACE, not empty.
    "SELECT 1 FROM users WHERE id IN (SELECT/**/pg_sleep(10))",
    "SELECT 1 FROM users WHERE x = (SELECT/**/pg_read_file('/etc/passwd'))",
    'SELECT 1 FROM users WHERE id IN (SELECT "pg_sleep"(10))',
    "SELECT 1 INTO/**/new_table FROM users",
]


@pytest.mark.parametrize("sql", REJECT_COMMENT_BREAKS_GATE)
def test_red_team_comment_breaks_gate(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Comment-bypass NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # Legitimate SELECT with Unicode / non-space whitespace separators must PASS
        # (`\s` is Unicode-aware for str patterns in Python's re).
        "SELECT" + chr(0x00A0) + "1",  # NBSP
        "SELECT" + chr(0x2028) + "1",  # line separator
        "SELECT\t*\nFROM\nusers",
        "SELECT\r\n*\r\nFROM\r\nusers",
    ],
)
def test_red_team_unicode_whitespace_accepts_valid(sql: str) -> None:
    assert check_read_only(sql) is None, f"Unicode-whitespace SELECT should pass: {sql!r}"


REJECT_UNICODE_WHITESPACE_DENY = [
    # Deny-list keywords separated by Unicode whitespace must STILL reject.
    "SELECT\t1\tINTO\tnew_table FROM users",
    "SELECT" + chr(0x00A0) + "1" + chr(0x00A0) + "INTO" + chr(0x00A0) + "new_table FROM users",
]


@pytest.mark.parametrize("sql", REJECT_UNICODE_WHITESPACE_DENY)
def test_red_team_unicode_whitespace_does_not_bypass_deny(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Unicode-whitespace INTO bypass: {sql!r}"


REJECT_DOLLAR_QUOTED_STACKING = [
        # Dollar-quote statement stacking. A `'` inside a `$$...$$` / `$tag$...$tag$`
        # body used to desync the single-quote stripper and smuggle a real second
        # statement past the multi-statement check. The lexer-faithful scan
        # neutralizes the whole dollar body, so the injected `;` stays visible and is
        # blocked. (Regression: arbitrary statement execution regardless of DB role.)
        r"SELECT $$'$$ ; DROP TABLE users -- '",
        r"SELECT $tag$'$tag$ ; DELETE FROM accounts -- '",
        r"SELECT $$won't$$ ; CREATE TABLE evil(x int) -- '",
        # Numeric-tag `$1$` is not a real PG dollar-quote (tags can't start with a
        # digit), so the raw payload is a DB syntax error — but the scan still treats
        # any `$…$` span as opaque, so a `'` inside can't desync it and expose the `;`.
        r"SELECT $1$'$1$ ; DROP TABLE users -- '",
        # An UNTERMINATED `$tag$` opener must not blank to EOF and swallow the trailing
        # `; DROP ...` (a fail-open the `$…$`-as-opaque broadening introduced): with no
        # matching close tag, the `;` stays visible and trips the guard.
        r"SELECT 1 AS $tag$; DROP TABLE users",
        r"SELECT 1 $$x ; DROP TABLE users",
        # A `$$` that OPENS inside a line comment must not be treated as a real
        # dollar-quote and swallow the statement that follows the newline.
        "SELECT 1 --$$\n;DROP TABLE x--$$",
        # A DO-block is procedural, not a SELECT — rejected on the opening-keyword check.
        "DO $$ BEGIN DELETE FROM users; END $$",
]


@pytest.mark.parametrize("sql", REJECT_DOLLAR_QUOTED_STACKING)
def test_red_team_dollar_quoted_stacking_blocked(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Dollar-quote stacking NOT blocked: {sql!r}"


REJECT_MYSQL_COMMENT_LEXING = [
    # MySQL/MariaDB `--` is a comment ONLY when followed by whitespace; `--0` is
    # `- -0`, so blanking it PG-style would hide the stacked `;DROP`. Refuse the
    # dialect-ambiguous form.
    "SELECT 1--0;DROP TABLE users",
    "SELECT 1--x\nUNION SELECT 2",
    # MySQL executable comments run their body as live SQL server-side.
    "SELECT 1/*!;DROP TABLE t*/",
    "SELECT 1/*!50000 ;DROP TABLE t*/",
    "SELECT * FROM t /*!UNION SELECT * FROM secrets*/",
]


@pytest.mark.parametrize("sql", REJECT_MYSQL_COMMENT_LEXING)
def test_red_team_mysql_comment_lexing_blocked(sql: str) -> None:
    assert check_read_only(sql) is not None, f"MySQL comment bypass NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # The whitespace-led `--` comment is a comment in BOTH dialects — must pass.
        "SELECT 1 --\tvalue FROM t",
        # A plain `/* ... */` block (not `/*!`) stays a normal comment.
        "SELECT 1 /* note */ FROM t",
    ],
)
def test_unambiguous_comments_still_pass(sql: str) -> None:
    assert check_read_only(sql) is None, f"Legit comment wrongly rejected: {sql!r}"


# ---------------------------------------------------------------------------
# Where a quoted span or comment ends must be the same on every engine. Text this scan blanks as
# "inside a string" is executed by an engine that ends the string earlier — a second statement
# included. Each vector below passed the gate before the fix; the first wrote rows on a Postgres
# reading backslash escapes (standard_conforming_strings off, as Redshift always does).
# ---------------------------------------------------------------------------

def _lexer_remediation(sql: str) -> str | None:
    """The remediation `_neutralize` refuses `sql` with, or None. The raw `;` rule in
    `check_read_only` catches most of these vectors first; asserting on the lexer as well is what
    keeps each lexer rule tested on its own, so neither layer can rot behind the other."""
    try:
        _neutralize(sql)
    except _GuardReject as reject:
        return reject.remediation
    return None


REJECT_BACKSLASH_QUOTE = [
    # The reproduced bypass: `\'` ends nothing to the engine, so its literal closes at the next
    # quote and the INSERT after it is live.
    r"SELECT o.id FROM sales.orders o WHERE o.id::text = 'a\' AND o.id::text = '; "
    r"INSERT INTO sales.canary SELECT o2.id::text FROM sales.orders o2; --'",
    # Two and three literals deep, so the odd quote is not the first one.
    r"SELECT a FROM t WHERE b = 'x' AND c = 'y\' AND d = '; DELETE FROM t; --'",
    r"SELECT a FROM t WHERE b = 'x' AND c = 'y' AND d = 'z\' OR 1 = '; DROP TABLE t; --'",
    # Inside a CTE, a subquery and a UNION arm.
    r"WITH c AS (SELECT a FROM t WHERE b = 'q\' ) SELECT * FROM c; DELETE FROM t; --') SELECT 1",
    r"SELECT a FROM (SELECT a FROM t WHERE b = 'q\' ) s; UPDATE t SET a = 1; --') s",
    r"SELECT a FROM t UNION SELECT b FROM u WHERE c = 'q\' UNION SELECT pg_sleep(9) --'",
    # Hiding a dangerous call or keyword, not a second statement.
    r"SELECT a FROM t WHERE b = 'a\' , pg_read_file('/etc/passwd') -- '",
    r"SELECT a FROM t WHERE b = 'a\' INTO OUTFILE '/tmp/x",
    # A comment opener the engine reads, after the backslash.
    r"SELECT a FROM t WHERE b = 'a\' /* ' ; DELETE FROM t; --*/",
    # Odd runs longer than one: `\\\'` is an escaped backslash then an escaped quote.
    r"SELECT a FROM t WHERE b = 'a\\\' ; DELETE FROM t; --'",
    r"SELECT a FROM t WHERE b = '\\\\\' ; DELETE FROM t; --'",
    # Before a DOUBLED quote: this scan reads `''` as one escape, the engine reads `\'` then a
    # closing `'`.
    r"SELECT a FROM t WHERE b = 'a\'' ; DELETE FROM t; --'",
    # Postgres E'' strings read backslash escapes whatever the server setting.
    r"SELECT E'a\' ; DELETE FROM t; --' AS s",
    r"SELECT e'\' ; DELETE FROM t; --' AS s",
    # MySQL / BigQuery / Databricks double-quoted strings, and a backtick name on BigQuery.
    r'SELECT "\"' + "'" + r'" , SLEEP(10) -- ' + "'",
    r'SELECT "a\"b" , current_user FROM t',
    r"SELECT `a\` , SLEEP(9) FROM t -- `",
    # The known cost: `ESCAPE '\'` is valid Postgres, but a backslash-escaping engine never closes
    # that literal, so it is refused too. Write it `ESCAPE '!'` (or any other character).
    r"SELECT a FROM t WHERE b LIKE '%\%%' ESCAPE '\'",
]


@pytest.mark.parametrize("sql", REJECT_BACKSLASH_QUOTE)
def test_rejects_a_quote_whose_end_depends_on_backslash_escaping(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"backslash-escaped quote NOT caught: {sql!r}"
    assert refusal.rule == RULE_READ_ONLY
    assert _lexer_remediation(sql) == _REMEDIATION["backslash_quote"]
    # Static text: a refusal never echoes the statement back.
    assert "\\" not in refusal.detail and "canary" not in refusal.detail


REJECT_QUOTE_RUN = [
    # BigQuery reads `'''`/`"""` as a triple-quoted string running to the next triple.
    "SELECT '''a' b'''; DELETE FROM ds.t WHERE TRUE; SELECT '1'",
    'SELECT """a" \'""" ; DELETE FROM t WHERE TRUE; SELECT \'1\'',
    # Three in a row inside a literal, not at its opening.
    "SELECT 'a''' , 1 FROM t",
    "SELECT a FROM t WHERE b = ''''",
]


@pytest.mark.parametrize("sql", REJECT_QUOTE_RUN)
def test_rejects_three_quotes_in_a_row(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"quote run NOT caught: {sql!r}"
    assert _lexer_remediation(sql) == _REMEDIATION["quote_run"]


REJECT_Q_QUOTE = [
    # Oracle ends `q'[…]'` at `]'`, so the `'` inside is data and UTL_HTTP runs.
    "SELECT q'[a']' , UTL_HTTP.REQUEST('http://x') FROM dual --'",
    "SELECT Q'{a'}' , 1 FROM dual",
    "SELECT nq'<a'>' FROM dual",
    "SELECT a FROM t WHERE b =q'!x!'",
]


@pytest.mark.parametrize("sql", REJECT_Q_QUOTE)
def test_rejects_oracle_alternative_quoting(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"q-quote NOT caught: {sql!r}"
    assert _lexer_remediation(sql) == _REMEDIATION["q_quote"]


REJECT_UNICODE_ESCAPE = [
    # Postgres decodes `U&"…"` before it resolves the name, so the escaped call runs while this
    # scan sees `pg_sl\0065ep` and no denied function.
    r'SELECT U&"pg_sl\0065ep"(3)',
    r'SELECT u&"current_us\0065r"() AS who',
    "SELECT U&\"d!0062link_exec\" UESCAPE '!' (1) FROM t",
    # The string form decodes too, so its content cannot be read the same way either.
    r"SELECT a FROM t WHERE b = U&'d\0061ta'",
    r"SELECT a FROM t WHERE b =u&'x'",
]


@pytest.mark.parametrize("sql", REJECT_UNICODE_ESCAPE)
def test_rejects_unicode_escaped_names_and_strings(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"U& form NOT caught: {sql!r}"
    assert _lexer_remediation(sql) == _REMEDIATION["unicode_escape"]


@pytest.mark.parametrize(
    "sql",
    [
        # `&` after a longer word is the bitwise-and operator, not a U& prefix.
        "SELECT menu&'1' AS m FROM t",
        'SELECT flags & 4, "u" FROM t',
        "SELECT a FROM t WHERE b = 'U&x'",
    ],
)
def test_and_operator_and_lookalikes_still_pass(sql: str) -> None:
    assert check_read_only(sql) is None, f"over-refused: {sql!r}"


REJECT_ESCAPED_BACKTICK_NAME = [
    # BigQuery decodes escapes inside a backtick name, so this resolves to EXTERNAL_QUERY while
    # this scan sees `EXTERNAL_QUE\u0052Y` and no denied function.
    r"SELECT `EXTERNAL_QUE\u0052Y`('demo.us.conn', 'SELECT 1')",
    r"SELECT `a\x62c` FROM t",
]


@pytest.mark.parametrize("sql", REJECT_ESCAPED_BACKTICK_NAME)
def test_rejects_a_backslash_inside_a_backtick_name(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"escaped backtick name NOT caught: {sql!r}"
    assert _lexer_remediation(sql) == _REMEDIATION["backtick_escape"]


@pytest.mark.parametrize(
    "sql",
    [
        # A combining mark (U+0301) continues the name `x\u0301`, so `$a$` after it opens nothing
        # on the engine and must open nothing here either, or the call between is hidden.
        "SELECT 1 AS x\u0301$a$, pg_sleep(9) AS y$a$",
        # Any non-ASCII letter continues a name too.
        "SELECT 1 AS \u00e9$a$, pg_sleep(9) AS y$a$",
    ],
)
def test_a_dollar_after_a_non_ascii_name_character_hides_nothing(sql: str) -> None:
    assert check_read_only(sql) is not None, f"call hidden behind a dollar after {sql[12]!r}"


REJECT_NESTED_COMMENT = [
    # Postgres nests block comments, so its comment runs past the first `*/` and the `'` this
    # scan opened a literal at is inside it: the DELETE runs on a stock Postgres.
    "SELECT 1 /* /* */ ' */ ; DELETE FROM t; --'",
    "SELECT 1 /* a /* b */ c */ FROM t",
    "SELECT 1 /* unterminated /* nested",
]


@pytest.mark.parametrize("sql", REJECT_NESTED_COMMENT)
def test_rejects_a_nested_block_comment(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"nested comment NOT caught: {sql!r}"
    assert _lexer_remediation(sql) == _REMEDIATION["nested_comment"]


REJECT_BACKTICK_HIDES_A_CALL = [
    # A `'` inside a backtick name opened a literal here that blanked the SLEEP MySQL runs.
    "SELECT `a'` , SLEEP(5) -- '",
    "SELECT `x'` FROM t; DELETE FROM t -- '",
]


@pytest.mark.parametrize("sql", REJECT_BACKTICK_HIDES_A_CALL)
def test_a_quote_inside_a_backtick_name_hides_nothing(sql: str) -> None:
    assert check_read_only(sql) is not None, f"backtick name hid a call: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # A backslash that moves no quote reads the same on every engine and must keep passing.
        r"SELECT a FROM t WHERE b LIKE 'a\_b'",
        r"SELECT REPLACE(a, '\n', ' ') FROM t",
        r"SELECT a FROM t WHERE p = 'C:\\'",          # even run before the close
        r"SELECT a FROM t WHERE p = 'C:\\path\\to'",
        r"SELECT a FROM t WHERE p = '\\\\' AND q = 'x'",
        r"SELECT E'tab\there' AS s",
        r"SELECT E'a\\' AS s",
        r"SELECT regexp_replace(a, '\s+', ' ') FROM t",
        r"SELECT a FROM t WHERE b = 'it''s \d'",
        r'SELECT "a\b" FROM t',
        # Doubled quotes stay fine; only three in a row are refused.
        "SELECT a FROM t WHERE b = 'it''s'",
        "SELECT '' AS empty, 'x' AS y",
        # Backtick names, as MySQL / BigQuery / Databricks write them.
        "SELECT `order id`, COUNT(*) FROM `proj.ds.orders` GROUP BY 1",
        "SELECT t.`a`, `b` FROM t",
        # A comment that merely mentions `*/` or `/` is not nested.
        "SELECT 1 /* a */ /* b */ FROM t",
        "SELECT 1 /* path a/b */ FROM t",
        # A name that ends in q is not Oracle's q-quote.
        "SELECT seq FROM t WHERE seq = 'x'",
        "SELECT q FROM t WHERE q = 'x'",
    ],
)
def test_unambiguous_quoting_still_passes(sql: str) -> None:
    assert check_read_only(sql) is None, f"unambiguous statement wrongly refused: {sql!r}"


REJECT_DOLLAR_INSIDE_A_NAME = [
    # `$` continues an identifier in Postgres, Redshift and Snowflake, so `x$a$` is one name and no
    # dollar quote opens. Reading `$a$` as an opener blanked the INSERT, which ran.
    "SELECT 1 AS x$a$; INSERT INTO sales.canary SELECT o.id::text FROM sales.orders o; "
    "SELECT 1 AS y$a$",
    "SELECT a$b$ FROM t WHERE 1 = 1 UNION SELECT pg_sleep(9) AS c$b$",
    "SELECT _$x$, pg_read_file('/etc/passwd') AS y$x$ FROM t",
    "SELECT 1$x$, pg_sleep(9) AS z$x$",
]


@pytest.mark.parametrize("sql", REJECT_DOLLAR_INSIDE_A_NAME)
def test_a_dollar_inside_a_name_opens_no_dollar_quote(sql: str) -> None:
    assert check_read_only(sql) is not None, f"`$` inside a name hid live SQL: {sql!r}"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a$b$ FROM t", "SELECT a$b$ FROM t"),
        ("SELECT 1 AS x$a$, 2 AS y$a$", "SELECT 1 AS x$a$, 2 AS y$a$"),
        ("SELECT $$x$$ AS s", "SELECT   AS s"),
        ("SELECT $t$ a ; b $t$ AS s", "SELECT   AS s"),
        ("SELECT f($t$x$t$)", "SELECT f( )"),
        ("SELECT 1,$t$x$t$", "SELECT 1,"),
    ],
)
def test_dollar_quotes_open_only_outside_a_name(sql: str, expected: str) -> None:
    assert _neutralize(sql).text == expected


REJECT_INNER_SEMICOLON = [
    # A `;` anywhere but the end is refused on the RAW text, before any lexing, so a lexer
    # mistake of any kind cannot smuggle a second statement.
    "SELECT a FROM t WHERE b = ';'",
    "SELECT a FROM t -- note; more\n",
    "SELECT a FROM t /* a; b */",
    'SELECT "a;b" FROM t',
    "SELECT $$;$$ AS s",
    "SELECT 1;\nSELECT 2",
    # Accepted before the raw rule, refused now: the `;` is inert on every reading, but the rule
    # does not read, which is the point of it.
    "SELECT 'a;b' FROM dual",
    "SELECT 1 /* ;DROP TABLE x; */ FROM dual",
    "SELECT 1 -- ; DROP TABLE x",
    "SELECT 1 -- 0;DROP\n",
    "SELECT a FROM t # note; more",
    # A comment after the final `;` is allowed only when nothing in it could close a string an
    # engine might still be inside. This one is the backslash bypass wearing a trailing comment.
    "SELECT 'p\\' ; -- x' ; DELETE FROM t; -- '\n",
    "SELECT 1; /* a; b */",
    "SELECT 1; /*! DROP TABLE t */",
    "SELECT 1; --x",
]


@pytest.mark.parametrize("sql", REJECT_INNER_SEMICOLON)
def test_a_semicolon_may_only_end_the_statement(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"inner `;` NOT caught: {sql!r}"
    assert refusal.remediation == _REMEDIATION["multi_statement"]
    assert "semicolon may only end the statement" in refusal.detail


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1;",
        "SELECT 1; ",
        "SELECT 1 ;\n",
        "SELECT 1",
        # Plain comments around the final `;`, as people paste statements from a BI tool.
        "SELECT 1; -- done",
        "SELECT 1;\n/* done */\n",
        "SELECT 1 -- every order\n;",
        "SELECT 1; -- a -- b",
        "SELECT 1; -- note --x",
        "SELECT 1; -- a --b --c",
    ],
)
def test_a_trailing_semicolon_still_passes(sql: str) -> None:
    assert check_read_only(sql) is None, sql


def test_many_comment_markers_are_peeled_in_linear_time() -> None:
    # Rebuilding the suffix's character set for every `--` made this quadratic: a statement just
    # under the length cap took ~1.8 s before reaching the database. Linear, it takes milliseconds.
    import time

    sql = "SELECT 1 -- " + "-- " * ((_MAX_SQL_CHARS - 20) // 3) + "'"
    assert len(sql) < _MAX_SQL_CHARS
    started = time.perf_counter()
    check_read_only(sql)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("sql", "hint"),
    [("SELECT a FROM t WHERE p = 'C:\\'", "CHR(92)"), ("SELECT a FROM t WHERE b = ''''", "CHR(39)")],
)
def test_the_two_known_over_refusals_say_how_to_rewrite(sql: str, hint: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None and hint in refusal.detail


REJECT_STACKED_KEYWORDS = [
    "SELECT 1; SET statement_timeout = 0; SELECT 2",
    "SELECT pg_sleep(10) FROM dual",
    "WITH x AS (LISTEN ch) SELECT 1",
    "SELECT 1 FROM (SELECT NOTIFY ch1, 'p' AS y) x",
]


@pytest.mark.parametrize("sql", REJECT_STACKED_KEYWORDS)
def test_red_team_stacked_keywords(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Stacked attack not blocked: {sql!r}"


def _every_reject_list(namespace: dict[str, Any]) -> list[Any]:
    """Every `REJECT_*` list in `namespace`, flattened, in declaration order.

    Built by scanning rather than hand-unioned. The union was 19 `*REJECT_…` lines a contributor had
    to remember to extend, and it is what makes the exhaustive-remediation claim in
    `test_ace035_read_only_refusal.py` true — so a new `REJECT_FOO` list left out of it did not fail
    anything, it silently shrank the corpus that file believes it is proving the contract over. The
    only existing guard was a `len > 120` floor, which a single dropped list of four vectors clears
    comfortably.

    `dict.fromkeys` de-duplicates while preserving order: a vector that legitimately appears in two
    categories should be parametrized once, and pytest ids must stay unique.
    """
    return list(
        dict.fromkeys(
            sql
            for name, value in namespace.items()
            if name.startswith("REJECT_") and name != "REJECT_CORPUS" and isinstance(value, list)
            for sql in value
        )
    )


# Every rejected statement in this file, in one list. `test_ace035_read_only_refusal.py` imports it
# to assert the refusal contract holds for the whole corpus rather than for a sampled few — so a new
# vector added to any list above is automatically held to the contract too. The over-length payload
# is appended there (it is generated, not a literal).
#
# Assembled here, at the bottom of the reject section, so it sees every list declared above it.
# `test_the_corpus_is_every_reject_list_in_this_module` re-runs the scan after the WHOLE module has
# loaded, which is what catches the remaining hole: a `REJECT_FOO` declared *below* this line.
REJECT_CORPUS: list[Any] = _every_reject_list(globals())


def test_the_corpus_is_every_reject_list_in_this_module() -> None:
    """The corpus, re-derived after the whole module has loaded, must equal the one assembled above.

    That is the only way to see a `REJECT_*` list declared BELOW the assembly line — the scan cannot
    include what does not exist yet, and a contributor appending a new category at the end of the
    file is the likeliest version of this mistake. A test rather than a lazy property because the
    corpus is consumed at import time by `@pytest.mark.parametrize`.
    """
    missing = [sql for sql in _every_reject_list(globals()) if sql not in REJECT_CORPUS]
    assert missing == [], (
        "a REJECT_* list is declared below REJECT_CORPUS and is not in it; move it above the "
        f"assembly line. Vectors dropped: {missing!r}"
    )


def test_the_corpus_scanner_finds_the_lists_and_only_the_lists() -> None:
    """The scanner is what makes the corpus self-maintaining, so it has to be shown working.

    Three shapes at once: a new `REJECT_*` list is picked up, a non-`REJECT_` list is not, and a
    duplicated vector appears once (pytest ids must stay unique).
    """
    scanned = _every_reject_list({
        "REJECT_ONE": ["a", "b"],
        "REJECT_TWO": ["b", "c"],  # 'b' is also in REJECT_ONE
        "ACCEPT_ONE": ["SELECT 1"],
        "REJECT_CORPUS": ["never"],  # the assembly itself is excluded by name
        "REJECT_NOT_A_LIST": "a",
    })
    assert scanned == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# False-positive guard — the analytics SQL an assistant emits every day. A regex
# regression that broke any of these would silently degrade every query.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # ----- Time-series rollups -----
        "SELECT date_trunc('day', created_at) AS d, COUNT(*) FROM orders GROUP BY d ORDER BY d DESC LIMIT 30",
        "SELECT date_trunc('month', o.created_at), SUM(o.amount) FROM orders o GROUP BY 1",
        "SELECT EXTRACT(YEAR FROM created_at) AS yr, EXTRACT(MONTH FROM created_at) AS mo, COUNT(*) FROM events GROUP BY yr, mo",
        "SELECT EXTRACT(DOW FROM o.created_at) AS dow, COUNT(*) FROM orders o GROUP BY dow",
        # ----- Conditional aggregation -----
        "SELECT SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) FROM invoices",
        "SELECT COUNT(*) FILTER (WHERE status = 'open') FROM tickets",
        "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status",
        # ----- Window functions -----
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) AS rn FROM orders",
        "SELECT id, RANK() OVER (ORDER BY revenue DESC) FROM accounts",
        "SELECT customer_id, amount, SUM(amount) OVER (PARTITION BY customer_id ORDER BY created_at ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative FROM orders",
        # ----- CTEs -----
        "WITH monthly AS (SELECT date_trunc('month', created_at) AS m, COUNT(*) AS n FROM orders GROUP BY 1) SELECT * FROM monthly ORDER BY m",
        "WITH t1 AS (SELECT id FROM users), t2 AS (SELECT id FROM accounts) SELECT * FROM t1 UNION ALL SELECT * FROM t2",
        # ----- Recursive CTE -----
        "WITH RECURSIVE org_tree(id, parent_id, depth) AS (SELECT id, parent_id, 0 FROM departments WHERE parent_id IS NULL UNION ALL SELECT d.id, d.parent_id, t.depth+1 FROM departments d JOIN org_tree t ON d.parent_id = t.id) SELECT * FROM org_tree",
        # ----- Multi-join -----
        "SELECT u.email, COUNT(o.id) FROM users u LEFT JOIN orders o ON u.id = o.customer_id GROUP BY u.email HAVING COUNT(o.id) > 5",
        "SELECT a.name, c.email FROM accounts a JOIN contacts c ON a.id = c.account_id LEFT JOIN opportunities o ON o.account_id = a.id",
        # ----- Set operations -----
        "SELECT id FROM users WHERE active UNION SELECT id FROM admins",
        "SELECT id FROM customers EXCEPT SELECT customer_id FROM churned_customers",
        "SELECT product_id FROM orders INTERSECT SELECT product_id FROM returns",
        # ----- String / array / JSON functions -----
        "SELECT REGEXP_REPLACE(email, '@.*', '') FROM users",
        "SELECT data->>'name' FROM events WHERE data ? 'id'",
        "SELECT array_agg(DISTINCT status) FROM tickets",
        "SELECT jsonb_array_length(items) FROM orders",
        # ----- Casts & arithmetic -----
        "SELECT CAST(amount AS DECIMAL(10,2)) FROM invoices",
        "SELECT amount::numeric * 1.18 AS amount_with_tax FROM orders",
        "SELECT NULLIF(email, '') FROM users",
        "SELECT COALESCE(phone, mobile, 'unknown') FROM contacts",
        # ----- Subqueries -----
        "SELECT * FROM users WHERE id IN (SELECT user_id FROM admin_users WHERE active)",
        "SELECT name, (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) AS order_count FROM users u",
        "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM orders WHERE orders.user_id = users.id)",
        # ----- Date/time functions -----
        "SELECT NOW(), CURRENT_DATE, CURRENT_TIMESTAMP",
        "SELECT created_at + INTERVAL '7 days' FROM events",
        "SELECT created_at AT TIME ZONE 'UTC' FROM events",
        # ----- Aliases that overlap reserved-substring patterns -----
        "SELECT u.created_at, u.updated_at, u.deleted_at FROM users u",
        "SELECT 1 AS dropped, 2 AS truncated FROM dual",
        "SELECT prepared_count, committed_at, rollback_total FROM stats",
        # ----- Comments mid-query (legit) -----
        "SELECT id /* primary key */, email FROM users",
        "SELECT id, /* date created */ created_at FROM users",
        "-- top-of-file comment\nSELECT 1",
        "SELECT 1 -- trailing comment",
        "/* block */ SELECT /* inline */ 1 /* end */",
        # ----- PG-quoted column names that are legit -----
        'SELECT "user_count" FROM stats',
        'SELECT "order date", "total amount" FROM legacy_orders',
        # ----- LATERAL joins -----
        "SELECT u.id, latest.created_at FROM users u, LATERAL (SELECT created_at FROM orders WHERE user_id = u.id ORDER BY created_at DESC LIMIT 1) latest",
        # ----- DISTINCT ON -----
        "SELECT DISTINCT ON (customer_id) customer_id, created_at, amount FROM orders ORDER BY customer_id, created_at DESC",
        # ----- generate_series -----
        "SELECT d::date FROM generate_series('2026-01-01'::date, '2026-12-31'::date, INTERVAL '1 day') AS d",
        # ----- Dollar-quoted string CONSTANTS (a value, not executable code). The
        # keywords/`;` inside are inert data; blocking these was an old false positive. -----
        "SELECT $$plain label$$ AS note FROM stats",
        "SELECT $tag$O'Brien$tag$ AS name",
        "SELECT $$multi\nline\ntext$$ AS body FROM docs",
        # ----- Positional parameters ($1, $2) are NOT dollar-quote openers -----
        "SELECT id, name FROM users WHERE id = $1",
        "SELECT * FROM orders WHERE customer_id = $1 AND status = $2",
        # ----- Names that overlap the cross-dialect function deny-list. Those entries are
        # bare words (`SLEEP`, `BENCHMARK`, `RELEASE_LOCK`, `REFLECT`), so they are the ones
        # most able to false-positive on an ordinary column, alias or table. `name(` is what
        # is matched, so a column never can — but an alias or a same-named user function
        # could, and these pin that the boundary is the call and not the word. -----
        "SELECT sleep_minutes, benchmark_score FROM sessions",
        "SELECT AVG(sleep) AS mean_sleep FROM sleep_study",
        "SELECT lock_count, release_count FROM lock_stats",
        "SELECT reflection_score FROM surveys",
        "SELECT system_id, external_query_id FROM job_runs",
        "SELECT load_file_path FROM ingest_log",
        # `OPENJSON` is ordinary SQL Server analytics and must survive the `OPENROWSET` deny —
        # which is why that section names its three functions instead of matching `OPEN\w+`.
        # `OPENXML` is the other one left out, and for a different reason: its document handle
        # can only come from an `EXEC sp_xml_preparedocument` the opening-keyword step refuses.
        "SELECT value FROM OPENJSON(@payload)",
        "SELECT x FROM OPENXML(@h, '/root', 1)",
    ],
)
def test_false_positive_guard_legitimate_analytics_sql(sql: str) -> None:
    assert check_read_only(sql) is None, f"FALSE POSITIVE — legit analytics SQL rejected: {sql!r}"


# ---------------------------------------------------------------------------
# Deferred-scope pins — behaviors intentionally NOT hardened in this pass, so a
# future change that adds them is a conscious decision (and updates this test).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # Bare `pg_catalog` / `information_schema` probing and environment-introspection
        # keywords/functions are NOT blocked by this gate (schema-scoping is a deferred
        # follow-up — see the plan). They currently PASS. When the follow-up lands, move
        # these into a reject test.
        "SELECT * FROM pg_tables",
        "SELECT * FROM information_schema.tables",
        "SELECT current_user",
        "SELECT current_database()",
    ],
)
def test_known_deferred_gaps_currently_pass(sql: str) -> None:
    assert check_read_only(sql) is None, (
        f"Expected this deferred-scope query to pass the read-only gate for now: {sql!r}"
    )


# ---------------------------------------------------------------------------
# Chokepoint enforcement — the guard is wired into execute_sql.py::main and is
# NOT bypassable via --no-safety (that flag only skips the semantic-model pass).
# This is the regression that closes the direct-`python -m execute_sql` gap.
# ---------------------------------------------------------------------------


def _run_executor(sql: str, tmp_path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", "nonexistent", "--sql", sql, *extra],
        capture_output=True,
        text=True,
        timeout=60,
        # Isolate artifacts dir so bootstrap() never touches the real home dir.
        env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(tmp_path)},
    )


@pytest.mark.parametrize("extra", [(), ("--no-safety",)])
def test_executor_blocks_dangerous_sql_even_with_no_safety(tmp_path, extra) -> None:
    """A write/DDL must be rejected by the executor BEFORE credentials are loaded,
    regardless of --no-safety. Proves the hard gate is at the shared chokepoint."""
    proc = _run_executor("DROP TABLE secrets", tmp_path, *extra)
    assert proc.returncode != 0, proc.stdout
    # The guard emits the contract Refusal as one JSON object on stderr.
    payload = None
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
    assert payload is not None, f"no JSON refusal on stderr; got: {proc.stderr!r}"
    assert payload["refusal"]["rule"] == RULE_READ_ONLY, payload


def test_executor_dangerous_function_blocked(tmp_path) -> None:
    proc = _run_executor("SELECT pg_read_file('/etc/passwd')", tmp_path)
    assert proc.returncode != 0
    assert json.loads(proc.stderr.strip())["refusal"]["rule"] == RULE_READ_ONLY


def test_executor_lets_valid_select_past_the_gate(tmp_path) -> None:
    """A valid SELECT is NOT rejected by the read-only gate — it proceeds to the
    credential step and fails there instead (proving the gate didn't block it)."""
    proc = _run_executor("SELECT 1", tmp_path)
    # It should fail (no such profile / credentials), but NOT with a refusal from the guard.
    assert '"refusal"' not in proc.stderr, (
        f"valid SELECT was wrongly blocked by the read-only gate: {proc.stderr!r}"
    )
