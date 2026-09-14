"""#258 — a table named without its schema still resolves on the Postgres-wire engines.

The client was served bare table names and wrote `FROM orders`; with the table in `sales_data` the
warehouse answered `relation "orders" does not exist` and the client retried. The executor now sets
`SET LOCAL search_path` to the model's schemas, on the statement's own transaction, when a bare name
means exactly one table.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import execute_sql
import pytest
from execute_sql import ExecResult


def _model(*tables: tuple[str, str | None]) -> SimpleNamespace:
    """A model with one area per schema-grouping, reduced to the attributes the helper reads."""
    return SimpleNamespace(
        subject_areas=[
            SimpleNamespace(
                tables_defined=[SimpleNamespace(name=n, schema_name=s) for n, s in tables]
            )
        ]
    )


# --- which schemas ---------------------------------------------------------------------------------


def test_schemas_come_from_the_model_in_first_seen_order():
    org = _model(("orders", "sales_data"), ("invoices", "finance"), ("refunds", "sales_data"))

    assert execute_sql._search_path_schemas(org) == ["sales_data", "finance"]


def test_a_name_declared_in_two_schemas_gets_no_path():
    """The path would pick whichever schema is listed first, silently. Keeping today's behaviour —
    the statement fails and names the relation — is the honest outcome."""
    org = _model(("orders", "sales_data"), ("ORDERS", "staging"))

    assert execute_sql._search_path_schemas(org) == []


def test_tables_without_a_schema_and_no_model_contribute_nothing():
    assert execute_sql._search_path_schemas(_model(("orders", None))) == []
    assert execute_sql._search_path_schemas(None) == []


def test_the_statement_quotes_each_schema_and_keeps_public():
    statement = execute_sql._search_path_statement(["sales_data", 'we"ird'])

    assert statement == 'SET LOCAL search_path TO "sales_data", "we""ird", public'
    assert execute_sql._search_path_statement(["public"]) == 'SET LOCAL search_path TO "public"'


# --- the Postgres path sets it on the statement's transaction --------------------------------------


class _Cursor:
    def __init__(self, log: list, name: str | None):
        self._log = log
        self.name = name
        self.description = [("c",)]
        self.itersize = 0

    def execute(self, sql: str, params=None) -> None:
        self._log.append(("execute", sql))

    def fetchmany(self, n: int):
        return [(1,)]

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _Connection:
    def __init__(self):
        self.log: list = []

    def cursor(self, name: str | None = None, **kwargs):
        self.log.append(("cursor", name))
        return _Cursor(self.log, name)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        self.log.append(("txn.enter",))
        return self

    def __exit__(self, *exc_info) -> bool:
        self.log.append(("txn.exit",))
        return False


_CREDS = {"host": "db.example", "port": "5432", "user": "u", "password": "p", "database": "shop"}


@pytest.fixture
def conn(monkeypatch):
    connection = _Connection()
    monkeypatch.setitem(sys.modules, "psycopg2", SimpleNamespace(connect=lambda **kw: connection))
    return connection


def _statements(conn: _Connection) -> list[str]:
    return [entry[1] for entry in conn.log if entry[0] == "execute"]


def test_postgres_sets_the_path_before_the_statement_in_the_same_transaction(conn):
    creds = {**_CREDS, execute_sql._SEARCH_PATH_KEY: ["sales_data"]}

    execute_sql._run_postgres(creds, "SELECT c FROM orders")

    statements = _statements(conn)
    path = statements.index('SET LOCAL search_path TO "sales_data", public')
    assert path < statements.index("SELECT c FROM orders")
    kinds = [entry[0] for entry in conn.log]
    assert "txn.exit" not in kinds[: kinds.index("execute") + len(statements)]


def test_postgres_without_schemas_sets_no_path(conn):
    execute_sql._run_postgres(dict(_CREDS), "SELECT c FROM orders")

    assert not any(s.startswith("SET LOCAL search_path") for s in _statements(conn))


# --- the chokepoint hands the schemas only to the engines that read them ---------------------------


def _guarded(monkeypatch, engine: str, org) -> dict:
    """Run `execute_guarded` with the gates stubbed to pass, returning the creds the executor got."""
    seen: dict = {}

    def _safety(sql, profile, area):
        execute_sql._guard_model.set(org)
        return sql, None

    monkeypatch.setattr(execute_sql, "_model_safety", _safety)
    monkeypatch.setattr(execute_sql, "_model_pass_disabled", lambda: False)
    monkeypatch.setattr(execute_sql, "_engine_mismatch", lambda profile, creds: None)
    monkeypatch.setattr(
        execute_sql, "_load_credentials", lambda profile, org_id="local": {"type": engine}
    )

    def _execute(sql, creds, *, profile):
        seen.update(creds)
        return ExecResult(columns=["c"], rows=[(1,)], truncated=False)

    execute_sql.execute_guarded(
        "SELECT c FROM orders", "shop", None, executor=SimpleNamespace(execute=_execute)
    )
    return seen


@pytest.mark.parametrize("engine", ["postgres", "redshift", "supabase"])
def test_the_postgres_wire_engines_receive_the_schemas(monkeypatch, engine):
    creds = _guarded(monkeypatch, engine, _model(("orders", "sales_data")))

    assert creds[execute_sql._SEARCH_PATH_KEY] == ["sales_data"]


def test_other_engines_and_ambiguous_models_receive_nothing(monkeypatch):
    assert execute_sql._SEARCH_PATH_KEY not in _guarded(
        monkeypatch, "snowflake", _model(("orders", "sales_data"))
    )
    assert execute_sql._SEARCH_PATH_KEY not in _guarded(
        monkeypatch, "postgres", _model(("orders", "sales_data"), ("orders", "staging"))
    )
