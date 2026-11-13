"""Snowflake's keys come from SHOW … KEYS, not information_schema.

Snowflake's information_schema has no key_column_usage view, so the generic key queries failed on every
table and introspection fell back to probing for a grain. Which columns form a key is only listed by
`SHOW PRIMARY KEYS` / `SHOW UNIQUE KEYS` / `SHOW IMPORTED KEYS`, whose output columns are fixed: the
dialect reshapes those rows into the canonical ones, and the read-only gate admits exactly those
commands, and only under `--build-profile`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402

SF = D.Snowflake()


def _pk(table: str, column: str, seq: int, name: str = "SYS_CONSTRAINT_1") -> dict:
    """One row of SHOW PRIMARY KEYS / SHOW UNIQUE KEYS, as Snowflake names its columns."""
    return {"created_on": "2026-01-01", "database_name": "DEMO", "schema_name": "SALES_DATA",
            "table_name": table, "column_name": column, "key_sequence": str(seq),
            "constraint_name": name, "rely": "false", "comment": ""}


def _fk(fk_schema: str, fk_table: str, fk_col: str, pk_table: str, pk_col: str, seq: int, name: str) -> dict:
    """One row of SHOW IMPORTED KEYS."""
    return {"created_on": "2026-01-01", "pk_database_name": "DEMO", "pk_schema_name": fk_schema,
            "pk_table_name": pk_table, "pk_column_name": pk_col, "fk_database_name": "DEMO",
            "fk_schema_name": fk_schema, "fk_table_name": fk_table, "fk_column_name": fk_col,
            "key_sequence": str(seq), "update_rule": "NO ACTION", "delete_rule": "NO ACTION",
            "fk_name": name, "pk_name": "SYS_CONSTRAINT_9", "deferrability": "NOT DEFERRABLE",
            "rely": "false", "comment": ""}


def _cols(*names: str) -> list[m.Column]:
    return [m.Column(name=n, type="integer") for n in names]


# --- the statements --------------------------------------------------------------------------------


def test_snowflake_reads_keys_with_show_commands_not_key_column_usage():
    statements = [SF.sql_primary_keys("SALES_DATA", "ORDERS"), SF.sql_unique_keys("SALES_DATA", "ORDERS"),
                  SF.sql_foreign_keys("SALES_DATA")]
    assert statements == ['SHOW PRIMARY KEYS IN TABLE "SALES_DATA"."ORDERS"',
                          'SHOW UNIQUE KEYS IN TABLE "SALES_DATA"."ORDERS"',
                          "SHOW IMPORTED KEYS IN DATABASE"]
    assert not any("key_column_usage" in s.lower() for s in statements)
    # the pre-#510 fallback is the generic information_schema query, which Snowflake cannot run either
    assert SF.sql_foreign_keys_unpaired("SALES_DATA") is None


def test_snowflake_does_not_enforce_foreign_keys():
    assert SF.fk_enforced is False


# --- the rows -------------------------------------------------------------------------------------


def test_a_composite_primary_key_is_read_in_key_order():
    rows = [_pk("ORDER_LINES", "LINE_NO", 2), _pk("ORDER_LINES", "ORDER_ID", 1)]
    seen: list[str] = []

    def run(sql: str) -> list[dict]:
        seen.append(sql)
        return rows if sql.startswith("SHOW PRIMARY KEYS") else []

    grain, mode = I._grain(SF, run, "SALES_DATA", "ORDER_LINES", _cols("ORDER_ID", "LINE_NO", "QTY"))
    assert (grain, mode) == (["ORDER_ID", "LINE_NO"], "catalog")
    assert seen == ['SHOW PRIMARY KEYS IN TABLE "SALES_DATA"."ORDER_LINES"']


def test_a_table_with_no_primary_key_still_probes_its_grain():
    def run(sql: str) -> list[dict]:
        if "COUNT(DISTINCT" in sql:
            return [{"total": "3", "distinct_count": "3", "null_count": "0"}]
        return []

    assert I._grain(SF, run, "SALES_DATA", "EVENTS", _cols("ID", "KIND")) == (["ID"], "probe")


def test_unique_keys_are_grouped_by_constraint_and_confirmed_on_the_data():
    rows = [_pk("CUSTOMERS", "B", 2, "UQ_AB"), _pk("CUSTOMERS", "EMAIL", 1, "UQ_EMAIL"),
            _pk("CUSTOMERS", "A", 1, "UQ_AB")]
    assert SF.unique_key_rows(rows) == [
        {"constraint_name": "UQ_AB", "column_name": "A"}, {"constraint_name": "UQ_AB", "column_name": "B"},
        {"constraint_name": "UQ_EMAIL", "column_name": "EMAIL"}]

    def run(sql: str) -> list[dict]:
        if sql.startswith("SHOW UNIQUE KEYS"):
            return rows
        return [{"total": "5", "distinct_count": "5", "null_count": "0"}]

    # unenforced, so only a single-column key the data bears out is kept
    keys = I._unique_keys(SF, run, "SALES_DATA", "CUSTOMERS", _cols("ID", "EMAIL", "A", "B"), ["ID"])
    assert keys == [["EMAIL"]]


def test_foreign_key_rows_are_canonical_and_confined_to_the_schema():
    rows = [_fk("SALES_DATA", "SHIPMENTS", "LINE_NO", "ORDER_LINES", "LINE_NO", 2, "FK_LINES"),
            _fk("SALES_DATA", "SHIPMENTS", "ORDER_ID", "ORDER_LINES", "ORDER_ID", 1, "FK_LINES"),
            _fk("OTHER", "SHIPMENTS", "ORDER_ID", "ORDERS", "ID", 1, "FK_ELSEWHERE")]
    assert SF.foreign_key_rows(rows, "SALES_DATA") == [
        {"from_table": "SHIPMENTS", "from_column": "ORDER_ID", "from_schema": "SALES_DATA",
         "to_table": "ORDER_LINES", "to_column": "ORDER_ID", "to_schema": "SALES_DATA",
         "constraint_name": "FK_LINES", "key_ordinal": "1"},
        {"from_table": "SHIPMENTS", "from_column": "LINE_NO", "from_schema": "SALES_DATA",
         "to_table": "ORDER_LINES", "to_column": "LINE_NO", "to_schema": "SALES_DATA",
         "constraint_name": "FK_LINES", "key_ordinal": "2"}]
    assert SF.foreign_key_rows(rows, "NONE_HERE") == []


@pytest.mark.parametrize("key", ["postgres", "mysql", "redshift", "databricks", "sqlserver", "oracle"])
def test_dialects_that_alias_their_key_rows_pass_them_through(key):
    rows = [{"column_name": "id", "constraint_name": "pk", "from_table": "t"}]
    d = D.get_dialect(key)
    assert d.primary_key_rows(rows) is rows
    assert d.unique_key_rows(rows) is rows
    assert d.foreign_key_rows(rows, "s") is rows


# --- the read-only gate ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    SF.sql_primary_keys("SALES_DATA", "ORDERS"),
    SF.sql_unique_keys("sales data", 'odd"name'),
    SF.sql_foreign_keys("SALES_DATA"),
])
def test_each_command_agami_builds_is_recognised(sql):
    assert D.is_catalog_command(sql)


@pytest.mark.parametrize("sql", [
    'SHOW PRIMARY KEYS IN TABLE "SALES_DATA"."ORDERS"; DROP TABLE "SALES_DATA"."ORDERS"',
    'SHOW PRIMARY KEYS IN TABLE "SALES_DATA"."ORDERS";',
    'SHOW PRIMARY KEYS IN TABLE "SALES_DATA"."OR"DERS"',
    "SHOW PRIMARY KEYS IN TABLE SALES_DATA.ORDERS",
    'SHOW PRIMARY KEYS IN TABLE "ORDERS"',
    "SHOW PRIMARY KEYS IN ACCOUNT",
    "SHOW IMPORTED KEYS IN DATABASE OTHER_DB",
    'SHOW PRIMARY KEYS IN TABLE "SALES_DATA"."ORDERS" ->> SELECT * FROM $1',
    "show imported keys in database",
    "SHOW GRANTS",
    "SHOW USERS",
    'SHOW PRIMARY KEYS IN TABLE "S"."' + "x" * 256 + '"',
    "",
])
def test_anything_else_is_not(sql):
    assert not D.is_catalog_command(sql)


class _Rows:
    """An executor that returns one SHOW row and records what reached it."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        self.seen.append(vetted_sql)
        return execute_sql.ExecResult(columns=["column_name", "key_sequence"], rows=[("ID", 1)])


@pytest.fixture
def local(tmp_path, monkeypatch):
    (tmp_path / "acme").mkdir()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("DATASOURCE_URL__ACME", "postgresql://demo@localhost:5432/demo")
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv(execute_sql.SERVED_CHILD_ENV, raising=False)
    return tmp_path


def _run(sql: str, **kw):
    executor = _Rows()
    return execute_sql.execute_guarded(sql, "acme", None, executor=executor, **kw), executor


def test_the_build_time_runner_sends_a_key_command_through_the_gate(local):
    sql = SF.sql_primary_keys("SALES_DATA", "ORDERS")
    env, executor = _run(sql, no_safety=True, build_profile=True)
    assert env.status == "ok", env.refusal
    assert executor.seen == [sql]


@pytest.mark.parametrize("kw", [{}, {"no_safety": True}])
def test_a_key_command_is_refused_without_the_build_profile(local, kw):
    env, executor = _run(SF.sql_foreign_keys("SALES_DATA"), **kw)
    assert env.status == "refused" and env.refusal.rule == guardrail.RULE_READ_ONLY
    assert not executor.seen


@pytest.mark.parametrize("sql", [
    'SHOW PRIMARY KEYS IN TABLE "SALES_DATA"."ORDERS"; DROP TABLE "SALES_DATA"."ORDERS"',
    "SHOW GRANTS",
    "SHOW PRIMARY KEYS IN ACCOUNT",
    "DELETE FROM orders",
])
def test_any_other_statement_is_still_refused_under_the_build_profile(local, sql):
    env, executor = _run(sql, no_safety=True, build_profile=True)
    assert env.status == "refused" and env.refusal.rule == guardrail.RULE_READ_ONLY
    assert not executor.seen


def test_a_hosted_server_never_admits_a_key_command(local, monkeypatch, tmp_path):
    from store import Store

    store = Store.connect(f"sqlite:///{tmp_path / 'app.db'}")
    store.run_migrations()
    store.close()
    monkeypatch.setenv("AGAMI_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    env, executor = _run(SF.sql_primary_keys("SALES_DATA", "ORDERS"), no_safety=True, build_profile=True)
    assert env.status == "refused" and env.refusal.rule == guardrail.RULE_READ_ONLY
    assert not executor.seen


def test_the_matcher_failing_to_load_keeps_the_gate_shut(local, monkeypatch):
    monkeypatch.setattr(D, "is_catalog_command", lambda sql: 1 / 0)
    env, _ = _run(SF.sql_primary_keys("SALES_DATA", "ORDERS"), no_safety=True, build_profile=True)
    assert env.status == "refused" and env.refusal.rule == guardrail.RULE_READ_ONLY
