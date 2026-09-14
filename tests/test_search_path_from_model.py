"""#258 — a table named without its schema still resolves on the Postgres-wire engines.

The client was served bare table names and wrote `FROM orders`; with the table in `sales_data` the
warehouse answered `relation "orders" does not exist` and the client retried. The executor now sets
`SET LOCAL search_path` to the model's schema, on the statement's own transaction — but only when
the model's tables live in ONE schema, because with two or more a bare name can silently resolve to
a table the model does not declare.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import execute_sql
import pytest
from execute_sql import ExecResult


def _model(*tables: tuple[str, str | None]) -> SimpleNamespace:
    """A one-area model reduced to the attributes the helper reads: (table name, schema) pairs."""
    return SimpleNamespace(
        subject_areas=[
            SimpleNamespace(
                tables_defined=[SimpleNamespace(name=n, schema_name=s) for n, s in tables]
            )
        ]
    )


# --- which schemas ---------------------------------------------------------------------------------


def test_a_model_in_one_schema_gets_that_schema():
    org = _model(("orders", "sales_data"), ("refunds", "sales_data"), ("regions", "public"))

    assert execute_sql._search_path_schemas(org) == ["sales_data"]


def test_a_model_spanning_two_schemas_gets_no_path():
    """With `finance` listed before `sales_data`, a bare `orders` would resolve to an undeclared
    `finance.orders` if the warehouse has one — the wrong data, silently, under a receipt naming the
    model's table. The model cannot rule that out, so no path is set and a bare name fails as before."""
    org = _model(("invoices", "finance"), ("orders", "sales_data"))

    assert execute_sql._search_path_schemas(org) == []


def test_tables_without_a_schema_no_model_and_public_only_contribute_nothing():
    assert execute_sql._search_path_schemas(_model(("orders", None))) == []
    assert execute_sql._search_path_schemas(_model(("orders", "public"))) == []
    assert execute_sql._search_path_schemas(None) == []


def test_a_schema_name_with_a_control_character_gets_no_path():
    """A NUL makes the driver raise on every statement for the profile; refuse to build the path."""
    assert execute_sql._search_path_schemas(_model(("orders", "sales\x00data"))) == []


def test_the_statement_quotes_the_schema_and_keeps_public():
    assert (
        execute_sql._search_path_statement(['we"ird'])
        == 'SET LOCAL search_path TO "we""ird", public'
    )


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


def test_postgres_sets_the_path_before_the_statement_in_the_same_transaction(conn):
    creds = {**_CREDS, execute_sql._SEARCH_PATH_KEY: ["sales_data"]}

    execute_sql._run_postgres(creds, "SELECT c FROM orders")

    log = conn.log
    entered = log.index(("txn.enter",))
    path = log.index(("execute", 'SET LOCAL search_path TO "sales_data", public'))
    statement = log.index(("execute", "SELECT c FROM orders"))
    exited = log.index(("txn.exit",))
    assert entered < path < statement < exited


def test_postgres_without_schemas_sets_no_path(conn):
    execute_sql._run_postgres(dict(_CREDS), "SELECT c FROM orders")

    assert not any(e[0] == "execute" and e[1].startswith("SET LOCAL search_path") for e in conn.log)


# --- the chokepoint hands the schema only to the engines that read it ------------------------------


def _guarded(monkeypatch, engine: str, org) -> dict:
    """Run `execute_guarded` with the gates stubbed to pass, returning the creds the executor got."""
    seen: dict = {}

    def _safety(sql, profile, area):
        if org is not None:
            execute_sql._guard_model.set(org)
        return sql, None

    monkeypatch.setattr(execute_sql, "_model_safety", _safety)
    monkeypatch.setattr(execute_sql, "_model_pass_disabled", lambda: False)
    monkeypatch.setattr(execute_sql, "_engine_mismatch", lambda profile, creds: None)
    monkeypatch.setattr(
        execute_sql, "_load_credentials", lambda profile, org_id="local": {"type": engine}
    )

    def _execute(sql, creds, *, profile):
        seen.clear()
        seen.update(creds)
        return ExecResult(columns=["c"], rows=[(1,)], truncated=False)

    execute_sql.execute_guarded(
        "SELECT c FROM orders", "shop", None, executor=SimpleNamespace(execute=_execute)
    )
    return seen


@pytest.mark.parametrize("engine", ["postgres", "redshift", "supabase"])
def test_the_postgres_wire_engines_receive_the_schema(monkeypatch, engine):
    creds = _guarded(monkeypatch, engine, _model(("orders", "sales_data")))

    assert creds[execute_sql._SEARCH_PATH_KEY] == ["sales_data"]


def test_other_engines_and_multi_schema_models_receive_nothing(monkeypatch):
    assert execute_sql._SEARCH_PATH_KEY not in _guarded(
        monkeypatch, "snowflake", _model(("orders", "sales_data"))
    )
    assert execute_sql._SEARCH_PATH_KEY not in _guarded(
        monkeypatch, "postgres", _model(("orders", "sales_data"), ("invoices", "finance"))
    )


def test_a_previous_call_s_model_does_not_reach_the_next_one(monkeypatch):
    """`execute_guarded` clears the published model on entry; a call whose pass publishes nothing must
    not inherit the last call's schema."""
    _guarded(monkeypatch, "postgres", _model(("orders", "sales_data")))

    assert execute_sql._SEARCH_PATH_KEY not in _guarded(monkeypatch, "postgres", None)
