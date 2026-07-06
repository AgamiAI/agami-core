"""
Read-only / dangerous-SQL guard — the single source of truth for "is this SQL
safe to run against the user's database?".

This gate runs at the shared executor chokepoint (`execute_sql.py::main`) and as a
fail-fast pre-check in the MCP tool layer (`tools.check_read_only`), so the stdio
server, the HTTP/OAuth server, the agami-query skill, and cron are all protected
identically — not just whichever path happened to read a prose rule.

It is defense in depth at the application layer; the underlying connection is *also*
expected to run under a read-only role. Postgres / Redshift are the primary concern
(the dangerous functions below are Postgres server-side primitives), but the checks
are neutral enough to be safe across the other supported engines.

`check_read_only(sql)` returns `None` when the SQL is a single safe read-only
statement, else a short human-readable reason string. Callers decide how to wrap it
(the MCP tools attach `kind="permission"`).
"""

from __future__ import annotations

import re

# Hard cap on SQL length. Prevents a compromised client from POSTing a multi-MB
# SQL blob that takes the parser / planner / this gate down a slow path. Real
# analytics SQL fits in ~10KB; 50KB is conservative.
_MAX_SQL_CHARS = 50_000

# Match SQL comments (line `--` and block `/* */`).
# CRITICAL: callers MUST sub with a SINGLE SPACE, never with empty string.
# Replacing with empty welds adjacent tokens together (`SELECT/**/INTO` ->
# `SELECTINTO`) and defeats the `\b` word-boundary in every downstream regex.
#
# Line comments stop at `\r` OR `\n` — Postgres' scanner ends `--` at either.
# Stopping only at `\n` is exploitable: `SELECT 1 --x\r;DROP TABLE y\n` would be
# scrubbed to `SELECT 1 ` by a `[^\n]*`-only regex (the `\r;DROP...` is eaten as
# part of the comment), masking a multi-statement attack.
_SQL_COMMENT_RE = re.compile(r"--[^\r\n]*|/\*.*?\*/", re.DOTALL)

# Single-quoted string literals so `;` / keywords inside them don't trip the
# deny-list. PG / Redshift / Snowflake all support doubled-quote escaping inside
# literals. Double-quoted IDENTIFIERS are handled separately (the gate strips the
# quote chars via `.replace('"', '')` so `"pg_sleep"(10)` reduces to `pg_sleep(10)`
# and the dangerous-function regex catches it).
_SQL_SINGLE_QUOTED_RE = re.compile(r"'(?:''|[^'])*'")

# Allowed opening keyword. `WITH` covers CTEs whose final clause is a SELECT.
# Leading `(` / whitespace is tolerated so a parenthesized set operation —
# `(SELECT 1) UNION (SELECT 2)` — is still recognized as read-only.
_READ_ONLY_OPEN_RE = re.compile(r"^[\s(]*(?:SELECT|WITH)\b", re.IGNORECASE)

# Deny-list of statement-level keywords that must NOT appear anywhere in the
# stripped (comments + literals removed) SQL.
#   - DML/DDL: writes / schema changes.
#   - TCL: `COMMIT`/`ROLLBACK`/`SAVEPOINT`/`RELEASE` can escape a read-only
#     transaction (a known bypass class for SQL-execution servers).
#     `BEGIN`/`START`/`END` are omitted — the opening-keyword check already rejects
#     anything not starting with SELECT/WITH, and `END` is a false-positive
#     landmine (`CASE ... END`).
#   - Session: `SET`/`RESET`/`DISCARD` corrupt pooled connection state.
#   - Pub/sub + locking: `LISTEN`/`NOTIFY`/`LOCK` aren't analytics primitives.
#   - Prepared: `PREPARE`/`DEALLOCATE` are an alternative query-stacking path.
#   - `INTO`: `SELECT ... INTO new_table` is a write that starts with SELECT, so
#     the opening-keyword check passes it — deny `INTO` to close that write path.
_DML_DDL_KEYWORDS = "INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|CALL|VACUUM|REINDEX|CLUSTER|EXECUTE|INTO"
_TCL_KEYWORDS = "COMMIT|ROLLBACK|SAVEPOINT|RELEASE"
_SESSION_KEYWORDS = "RESET|DISCARD|SET"
_PUBSUB_LOCK_KEYWORDS = "LISTEN|NOTIFY|UNLISTEN|LOCK"
_PREPARED_KEYWORDS = "PREPARE|DEALLOCATE"
_DENY_KEYWORD_RE = re.compile(
    rf"\b({_DML_DDL_KEYWORDS}|{_TCL_KEYWORDS}|{_SESSION_KEYWORDS}|{_PUBSUB_LOCK_KEYWORDS}|{_PREPARED_KEYWORDS})\b",
    re.IGNORECASE,
)

# Row-level lock clauses inside an otherwise-valid SELECT. `FOR UPDATE`,
# `FOR SHARE`, `FOR NO KEY UPDATE`, `FOR KEY SHARE` — none belong in analytics.
_ROW_LOCK_RE = re.compile(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)

# Dangerous function calls — these read server files, execute OS commands via
# `COPY ... FROM PROGRAM` (when callable), drain server-side IO, sleep to burn
# worker time, kill other backends, mutate session state via the function path
# that bypasses the `SET` keyword deny, hold session-survival advisory locks, or
# execute a nested SQL string passed as a function arg (the `query_to_xml(text)`
# family). Match against `name(` so identifiers sharing a prefix aren't matched.
_DANGEROUS_FN_RE = re.compile(
    r"\b("
    # Time wasters / DoS
    r"pg_sleep|pg_sleep_for|pg_sleep_until|"
    # Server-side file I/O
    r"pg_read_file|pg_read_binary_file|pg_read_server_files|pg_write_server_files|"
    r"pg_ls_dir|pg_stat_file|pg_ls_logdir|pg_ls_waldir|pg_ls_tmpdir|"
    # Large objects — full set including legacy `loread`/`lowrite` and the
    # open/seek/tell/close API that lets an attacker chain `lo_open` -> `loread`
    # to read arbitrary LO content without using `lo_export`.
    r"lo_export|lo_import|lo_create|lo_unlink|lo_get|lo_put|lo_from_bytea|"
    r"lo_open|lo_read|lo_write|lo_close|lo_lseek|lo_lseek64|lo_tell|lo_tell64|lo_truncate|lo_truncate64|"
    r"loread|lowrite|"
    # Remote SQL execution — `dblink\w*` catches every variant.
    r"dblink\w*|"
    # Shell out via COPY
    r"copy_program|"
    # Backend / process control
    r"pg_terminate_backend|pg_cancel_backend|pg_reload_conf|"
    r"pg_rotate_logfile|pg_logfile_rotate|"
    # Session-state mutation that bypasses the `SET` keyword deny.
    r"set_config|current_setting|"
    # Session-survival advisory locks — survive connection return and can DoS.
    r"pg_advisory_lock|pg_advisory_xact_lock|"
    r"pg_advisory_unlock|pg_advisory_unlock_all|"
    # Nested-SQL execution via XML/JSON conversion — these execute the SQL passed
    # as a string argument server-side, bypassing the outer gate.
    r"query_to_xml|query_to_xmlschema|query_to_json|cursor_to_xml"
    r")\s*\(",
    re.IGNORECASE,
)


def check_read_only(sql: str | None) -> str | None:
    """Return None if `sql` is a single safe read-only statement, else a reason string.

    Rejection ladder (each step has its own message so the caller can correct):
      0. Empty SQL
      1. SQL longer than `_MAX_SQL_CHARS`
      2. Multi-statement (any `;` outside literals/comments, except one trailing `;`)
      3. Doesn't open with SELECT or WITH (leading `(` tolerated)
      4. Contains a forbidden keyword (DML/DDL/TCL/session/pub-sub/lock/prepared/INTO)
      5. Contains a row-level lock clause (`FOR UPDATE` etc.)
      6. Calls a dangerous function (`pg_sleep`, `pg_read_file`, `dblink`, ...)
    """
    if not sql or not sql.strip():
        return "empty statement"

    if len(sql) > _MAX_SQL_CHARS:
        return (
            f"SQL is {len(sql)} characters; the guard caps at {_MAX_SQL_CHARS}. "
            "Real analytics SQL fits well under this."
        )

    # CRITICAL: strip string literals BEFORE comments. The reverse order is
    # exploitable — `SELECT '--'; DROP TABLE x` has `--` inside a literal, but the
    # comment regex would otherwise eat from the `--` through end-of-line (taking
    # the injected `; DROP ...` with it) and the multi-statement check would pass.
    # Replacing literals first kills any embedded `--` / `/*` before the comment
    # regex runs.
    #
    # Then strip PG-quoted identifier delimiters (`"`) so `"pg_sleep"(10)` reduces
    # to `pg_sleep(10)` and the dangerous-function regex catches it. The trade-off:
    # a pathological column named `"a;b"` ends up as `a;b` and trips the
    # multi-statement check — a deliberate hardening choice.
    stripped = _SQL_COMMENT_RE.sub(" ", _SQL_SINGLE_QUOTED_RE.sub("''", sql))
    stripped = stripped.replace('"', "").strip()

    # Allow exactly one trailing `;`. Any other `;` indicates a second statement —
    # the classic statement-stacking bypass (`COMMIT; DROP SCHEMA public CASCADE`).
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if not stripped:
        return "empty statement"
    if ";" in stripped:
        return "multiple statements are not allowed — send one SELECT"

    if not _READ_ONLY_OPEN_RE.match(stripped):
        head = stripped.lstrip("(").split(None, 1)
        head = head[0].upper() if head else "?"
        return f"only SELECT / WITH...SELECT is allowed (statement starts with {head})"

    deny = _DENY_KEYWORD_RE.search(stripped)
    if deny:
        return (
            f"keyword '{deny.group(1).upper()}' is not allowed — send a single "
            "SELECT / WITH...SELECT (no DML, DDL, transaction control, session-state, "
            "or prepared statements)"
        )

    if _ROW_LOCK_RE.search(stripped):
        return "row-level lock clauses (FOR UPDATE / FOR SHARE / ...) are not allowed"

    fn = _DANGEROUS_FN_RE.search(stripped)
    if fn:
        return (
            f"function `{fn.group(1)}` is not allowed — server-file / OS / "
            "process-control / sleep / remote-SQL functions are blocked"
        )
    return None
