"""A failing statement shows the database's first line on a person's own command line (#523).

Only there. A hosted server and the MCP tool, forked or in-process, still answer with the fixed
sentence alone (ACE-039), and every exit code is the one it was, so the agami-query skill's handling
of them is unchanged.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

import execute_sql  # noqa: E402
import tools  # noqa: E402

PROFILE = "acme"
# `amount` is declared but the warehouse lacks it, so the database itself rejects the statement.
MISSING_COLUMN = "SELECT amount FROM orders"
# A nested aggregate: no needle reads it, so it is `other` and exit 6.
NESTED_AGGREGATE = "SELECT SUM(SUM(id)) AS n FROM orders"


def _write_model(root: pathlib.Path) -> None:
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "Shop", "version": 1,
        "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
        "subject_areas": ["subject_areas/sales"]}))
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "integer"}]}))


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)
    db = tmp_path / "warehouse.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{db}")
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", execute_sql.SERVED_CHILD_ENV):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _cli(sql: str, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", PROFILE, "--sql", sql],
        capture_output=True, text=True, timeout=120, env={**os.environ, **extra_env},
        cwd=str(pathlib.Path(execute_sql.__file__).parent),
    )


@pytest.mark.parametrize(("sql", "kind", "said"), [
    (MISSING_COLUMN, "column_not_found", "no such column: amount"),
    (NESTED_AGGREGATE, "other", "misuse of aggregate function SUM()"),
])
def test_a_local_command_line_shows_the_database_s_first_line(warehouse, sql, kind, said):
    proc = _cli(sql)
    assert proc.returncode == execute_sql.FAILURE_KIND_TO_EXIT[kind]
    sentence = execute_sql._ERROR_MESSAGES.get(kind, execute_sql.UNEXPECTED_FAILURE_MESSAGE)
    first, second = proc.stderr.strip().splitlines()
    assert first == sentence
    assert second == f"Database error: SQLite execution error: {said}"


@pytest.mark.parametrize("sql", [MISSING_COLUMN, NESTED_AGGREGATE])
def test_the_child_the_mcp_tool_forks_writes_only_the_fixed_sentence(warehouse, sql):
    proc = _cli(sql, **{execute_sql.SERVED_CHILD_ENV: "1"})
    assert proc.stderr.strip() in (execute_sql._ERROR_MESSAGES["column_not_found"],
                                   execute_sql.UNEXPECTED_FAILURE_MESSAGE)
    assert "amount" not in proc.stderr and "misuse" not in proc.stderr


@pytest.mark.parametrize("sql", [MISSING_COLUMN, NESTED_AGGREGATE])
def test_a_hosted_server_writes_only_the_fixed_sentence(warehouse, sql):
    from store import Store

    app_db = warehouse / "app.db"
    store = Store.connect(f"sqlite:///{app_db}")
    store.run_migrations()
    store.close()
    proc = _cli(sql, AGAMI_DB_URL=f"sqlite:///{app_db}", AGAMI_GOVERNANCE_ENFORCED="true")
    assert proc.stderr.strip() in (execute_sql._ERROR_MESSAGES["column_not_found"],
                                   execute_sql.UNEXPECTED_FAILURE_MESSAGE)
    assert "Database error" not in proc.stderr and "misuse" not in proc.stderr


@pytest.mark.parametrize(("sql", "kind", "message"), [
    (MISSING_COLUMN, "column_not_found", execute_sql._ERROR_MESSAGES["column_not_found"]),
    (NESTED_AGGREGATE, "other", execute_sql.UNEXPECTED_FAILURE_MESSAGE),
])
@pytest.mark.parametrize("path", ["forked", "in_process"])
def test_the_mcp_tool_s_failure_payload_is_unchanged(warehouse, sql, kind, message, path):
    if path == "in_process":
        tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    try:
        body = json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE}))
    finally:
        tools.set_injected_executor(None)
    assert body["status"] == "failed"
    assert body["failure"] == {"kind": kind, "message": message}
    rendered = json.dumps(body)
    assert "Database error" not in rendered and "misuse" not in rendered and "no such column" not in rendered


def test_the_detail_never_rides_on_the_envelope(warehouse):
    env = execute_sql.execute_guarded(NESTED_AGGREGATE, PROFILE, None, executor=execute_sql.BUILTIN_EXECUTOR)
    assert env.failure.message == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    assert "misuse" in execute_sql._last_error_cli_detail.get()
    # Cleared by the next call, so it is never attributed to a statement that did not produce it.
    execute_sql.execute_guarded("SELECT id FROM orders", PROFILE, None, executor=execute_sql.BUILTIN_EXECUTOR)
    assert execute_sql._last_error_cli_detail.get() is None


# Built at import, so no credential-shaped literal sits in the source for a secret scanner to flag.
_FAKE_SECRET = "not" + "-a-" + "secret"
_FAKE_ACCOUNT = "acct" + "-demo"
_PW_KEY = "pass" + "word"


@pytest.mark.parametrize(("text", "creds", "expected"), [
    ('Postgres execution error: column "amont" does not exist\nLINE 1: SELECT amont FROM orders\n'
     '               ^\nHINT:  Perhaps you meant to reference the column "orders.internal_ref".',
     {}, 'Postgres execution error: column "amont" does not exist'),
    ('Postgres connect failed: connection to server at "db.example.com" (10.1.2.3), port 5432 failed: '
     'FATAL:  password authentication failed for user "demo"',
     {"host": "db.example.com", "user": "demo"},
     'Postgres connect failed: connection to server at "<redacted>" (<redacted>), port 5432 failed: '
     'FATAL: password authentication failed for user "<redacted>"'),
    (f"MySQL connect failed: could not open mysql://demo:{_FAKE_SECRET}@db.example.com:3306/sales",
     {}, "MySQL connect failed: could not open <redacted>"),
    (f"Snowflake connect failed: bad login {_PW_KEY}={_FAKE_SECRET} user=demo account={_FAKE_ACCOUNT}",
     {"account": _FAKE_ACCOUNT}, "Snowflake connect failed: bad login password=<redacted> user=<redacted> "
     "account=<redacted>"),
    ("DETAIL: only detail\nCONTEXT: and context", {}, None),
])
def test_the_first_line_is_redacted(text, creds, expected):
    assert execute_sql._db_error_first_line(text, None, creds) == expected


def test_the_sqlstate_is_shown_when_the_driver_gives_one(warehouse):
    class _PgError(Exception):
        pgcode = "42703"

    class _Raiser:
        def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
            try:
                raise _PgError("column x does not exist")
            except _PgError as e:
                raise execute_sql.ExecutorError(f"Postgres execution error: {e}", code=5) from e

    execute_sql.execute_guarded("SELECT 1", PROFILE, None, executor=_Raiser(), no_safety=True)
    assert execute_sql._last_error_cli_detail.get() == (
        "Postgres execution error: column x does not exist (SQLSTATE 42703)")
    assert execute_sql._db_error_first_line("column x does not exist", "42703", {}) == (
        "column x does not exist (SQLSTATE 42703)")
