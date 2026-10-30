"""`execute_sql --build-profile`: the local model-building mode (#523).

It allows exactly two things the model pass otherwise refuses, catalog reads and JSON/array table
functions over a column, and lets agami's own catalog queries call `pg_get_constraintdef`. Every other
check still runs. It is refused on a hosted server and in the child the MCP tool forks, and the MCP tool
has no way to ask for it, so what the tool accepts and refuses is unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as INTRO  # noqa: E402

PROFILE = "acme"


class _Rows:
    """An executor that returns one row and records what reached it."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        self.seen.append(vetted_sql)
        return execute_sql.ExecResult(columns=["x"], rows=[(1,)])


def _write_model(root: Path) -> None:
    """orders(id, status, payload) on Postgres, with `secret` excluded by the curator."""
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "Shop", "version": 1,
        "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
        "subject_areas": ["subject_areas/sales"]}))
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [
            {"name": "id", "type": "integer", "primary_key": True},
            {"name": "status", "type": "string"},
            {"name": "payload", "type": "json"},
            {"name": "secret", "type": "json", "review_state": "rejected"},
        ]}))


@pytest.fixture
def model(tmp_path, monkeypatch):
    _write_model(tmp_path / PROFILE)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", "postgresql://demo@localhost:5432/demo")
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv(execute_sql.SERVED_CHILD_ENV, raising=False)
    monkeypatch.setenv("AGAMI_GOVERNANCE_ENFORCED", "true")
    return tmp_path


def _run(sql: str, **kw):
    executor = _Rows()
    env = execute_sql.execute_guarded(sql, PROFILE, None, executor=executor, **kw)
    return env, executor


INFO_SCHEMA = "SELECT c.column_name FROM information_schema.columns c WHERE c.table_name = 'orders'"
JSONB_EACH = "SELECT kv.key, COUNT(*) AS n FROM orders o, jsonb_each(o.payload) AS kv GROUP BY kv.key"
LATERAL_JSONB = "SELECT kv.key FROM orders o CROSS JOIN LATERAL jsonb_each(o.payload) AS kv"
PG_CATALOG = ("SELECT con.conname FROM pg_catalog.pg_constraint con "
              "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid WHERE c.relname = 'orders'")


@pytest.mark.parametrize("sql", [
    INFO_SCHEMA, PG_CATALOG, JSONB_EACH, LATERAL_JSONB,
    # Bare names are fine where no model table is read, and an ORDER BY of the select's own alias.
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'orders'",
    "SELECT kv.key, COUNT(*) AS n FROM orders o, jsonb_each(o.payload) AS kv GROUP BY kv.key ORDER BY n",
])
def test_a_profiling_read_runs_under_build_profile_and_is_refused_without_it(model, sql):
    refused, executor = _run(sql)
    assert refused.status == "refused" and not executor.seen
    assert refused.refusal.rule in (guardrail.RULE_TABLE_SCOPE, guardrail.RULE_UNSCOPABLE)

    ran, executor = _run(sql, build_profile=True)
    assert ran.status == "ok", ran.refusal
    assert executor.seen == [sql]


@pytest.mark.parametrize(("sql", "rule"), [
    # The left-out column, however the statement reaches it.
    ("SELECT o.secret FROM orders o", guardrail.RULE_COLUMN_SCOPE),
    ("SELECT kv.key FROM orders o, jsonb_each(o.secret) AS kv", guardrail.RULE_COLUMN_SCOPE),
    ("SELECT kv.key FROM orders o CROSS JOIN LATERAL jsonb_each(o.secret) AS kv", guardrail.RULE_COLUMN_SCOPE),
    ("SELECT o.secret, c.column_name FROM orders o JOIN information_schema.columns c "
     "ON c.table_name = 'orders'", guardrail.RULE_COLUMN_SCOPE),
    # A column nobody declared.
    ("SELECT kv.key FROM orders o, jsonb_each(o.notes) AS kv", guardrail.RULE_COLUMN_SCOPE),
    # Undeclared user tables, and catalog tables that hold row values rather than structure.
    ("SELECT p.id FROM payroll p", guardrail.RULE_TABLE_SCOPE),
    ("SELECT s.most_common_vals FROM pg_catalog.pg_stats s", guardrail.RULE_TABLE_SCOPE),
    ("SELECT c.relname FROM pg_class c", guardrail.RULE_TABLE_SCOPE),
    # Sources that are not a function over a column.
    ("SELECT s.v FROM orders o, LATERAL (SELECT o.status AS v) s", guardrail.RULE_UNSCOPABLE),
    ("SELECT kv.key FROM orders o, jsonb_each((SELECT p.doc FROM payroll p)) AS kv", guardrail.RULE_UNSCOPABLE),
    ("SELECT g.n FROM generate_series(1, 3) AS g(n)", guardrail.RULE_UNSCOPABLE),
    ("SELECT o.id FROM orders o UNION ALL (VALUES (1))", guardrail.RULE_UNSCOPABLE),
    # A bare name beside a model table could resolve to the model table's left-out column from an
    # inner scope that lacks it, so every column there is qualified.
    ("SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM jsonb_each(o.payload) AS kv WHERE secret IS NULL)",
     guardrail.RULE_UNSCOPABLE),
    ("SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM information_schema.columns c WHERE secret IS NULL)",
     guardrail.RULE_UNSCOPABLE),
    ("SELECT o.id AS secret FROM orders o WHERE EXISTS "
     "(SELECT 1 FROM jsonb_each(o.payload) AS kv WHERE secret IS NULL)", guardrail.RULE_UNSCOPABLE),
    ("SELECT kv.key AS secret, COUNT(*) AS n FROM orders o, jsonb_each(o.payload) AS kv GROUP BY secret",
     guardrail.RULE_UNSCOPABLE),
    # The star ban still holds, over a catalog view too.
    ("SELECT * FROM information_schema.columns", guardrail.RULE_SELECT_STAR),
    # The always-on guard.
    ("DELETE FROM orders", guardrail.RULE_READ_ONLY),
    ("SELECT pg_sleep(1)", guardrail.RULE_READ_ONLY),
    ("SELECT version()", guardrail.RULE_RECON),
    ("SELECT 'orders'::regclass", guardrail.RULE_RECON),
])
def test_everything_else_is_still_refused_under_build_profile(model, sql, rule):
    env, executor = _run(sql, build_profile=True)
    assert env.status == "refused" and env.refusal.rule == rule, env
    assert not executor.seen


def test_the_length_cap_is_unchanged(model):
    sql = "SELECT o.id FROM orders o WHERE o.status = '" + "x" * 50_000 + "'"
    for kw in ({}, {"build_profile": True}, {"no_safety": True, "build_profile": True}):
        env, _ = _run(sql, **kw)
        assert env.status == "refused" and env.refusal.rule == guardrail.RULE_READ_ONLY
        assert "50000" in env.refusal.detail


def test_pg_get_constraintdef_runs_only_as_agami_s_own_catalog_query(model):
    builtin = D.PostgreSQL().sql_constraint_definitions("public")
    assert "pg_get_constraintdef" in builtin
    # Agami's own query, for any schema: the fingerprint ignores the literal.
    for schema in ("public", "sales"):
        env, executor = _run(D.PostgreSQL().sql_constraint_definitions(schema), build_profile=True)
        assert env.status == "ok", env.refusal
        assert executor.seen
    # The same query is refused without the build profile, with or without `--no-safety`.
    for kw in ({}, {"no_safety": True}):
        env, _ = _run(builtin, **kw)
        assert env.status == "refused" and env.refusal.rule == guardrail.RULE_RECON
    # A statement an agent wrote, or agami's query with anything changed, is refused.
    for sql in (
        "SELECT pg_get_constraintdef(con.oid) AS d FROM pg_catalog.pg_constraint con",
        builtin.replace("ORDER BY c.relname, con.conname", "AND 1 = 1 ORDER BY c.relname, con.conname"),
        builtin.replace("con.conname AS constraint_name", "con.conname AS constraint_name, version() AS v"),
        builtin.replace("pg_get_constraintdef(con.oid)", "pg_get_constraintdef(con.oid, true)"),
    ):
        for kw in ({"build_profile": True}, {"no_safety": True, "build_profile": True}):
            env, executor = _run(sql, **kw)
            assert env.status == "refused" and env.refusal.rule == guardrail.RULE_RECON, sql
            assert not executor.seen


def test_the_built_in_list_failing_to_load_keeps_the_gate_shut(model, monkeypatch):
    monkeypatch.setattr(D, "catalog_function_queries", lambda: 1 / 0)
    env, _ = _run(D.PostgreSQL().sql_constraint_definitions("public"), build_profile=True)
    assert env.status == "refused" and env.refusal.rule == guardrail.RULE_RECON


def test_a_hosted_server_never_gets_the_build_profile_even_in_process(model, monkeypatch, tmp_path):
    from store import Store

    store = Store.connect(f"sqlite:///{tmp_path / 'app.db'}")
    store.run_migrations()
    store.close()
    monkeypatch.setenv("AGAMI_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    for sql in (INFO_SCHEMA, JSONB_EACH):
        env, executor = _run(sql, build_profile=True)
        assert env.status == "refused" and not executor.seen
    env, _ = _run(D.PostgreSQL().sql_constraint_definitions("public"), build_profile=True)
    assert env.status == "refused" and env.refusal.rule == guardrail.RULE_RECON


# --- the command line ---------------------------------------------------------------------------


def _main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", PROFILE, *argv])
    return execute_sql.main()


def test_the_flag_reaches_the_guard_from_a_local_command_line(model, monkeypatch, capsys):
    executor = _Rows()
    monkeypatch.setattr(execute_sql, "BUILTIN_EXECUTOR", executor)
    assert _main(monkeypatch, "--sql", INFO_SCHEMA) == 1
    assert json.loads(capsys.readouterr().err)["refusal"]["rule"] == guardrail.RULE_TABLE_SCOPE
    assert _main(monkeypatch, "--build-profile", "--sql", INFO_SCHEMA) == 0
    assert executor.seen == [INFO_SCHEMA]


@pytest.mark.parametrize("marker", ["hosted", "served_child"])
def test_the_flag_is_refused_on_a_hosted_server_and_in_the_mcp_child(model, monkeypatch, capsys, marker):
    executor = _Rows()
    monkeypatch.setattr(execute_sql, "BUILTIN_EXECUTOR", executor)
    if marker == "hosted":
        monkeypatch.setenv("AGAMI_DB_URL", "sqlite:///unused.db")
    else:
        monkeypatch.setenv(execute_sql.SERVED_CHILD_ENV, "1")
    for argv in (("--sql", INFO_SCHEMA), ("--batch", str(model / "plan.json"))):
        assert _main(monkeypatch, "--build-profile", *argv) == 2
        assert "--build-profile" in capsys.readouterr().err
    assert not executor.seen and not (model / "plan.json.manifest.json").exists()


# --- the MCP tool -------------------------------------------------------------------------------


def test_the_mcp_tool_cannot_ask_for_the_build_profile():
    source = (PKG_SRC / "tools.py").read_text(encoding="utf-8")
    assert "build-profile" not in source and "build_profile" not in source
    assert tools._pass_child_env()[execute_sql.SERVED_CHILD_ENV] == "1"


@pytest.mark.parametrize("path", ["forked", "in_process"])
@pytest.mark.parametrize("sql", [INFO_SCHEMA, JSONB_EACH, D.PostgreSQL().sql_constraint_definitions("public")])
def test_the_mcp_tool_refuses_profiling_reads_as_it_always_did(model, path, sql):
    """The tool's answer to these is the one it gave before #523, whatever the call carries."""
    args = {"sql": sql, "datasource": PROFILE, "build_profile": True, "build-profile": True}
    if path == "in_process":
        tools.set_injected_executor(_Rows())
    try:
        body = json.loads(tools.tool_execute_sql(args))
    finally:
        tools.set_injected_executor(None)
    assert body["status"] == "refused"
    assert body["refusal"]["rule"] in (guardrail.RULE_TABLE_SCOPE, guardrail.RULE_UNSCOPABLE,
                                       guardrail.RULE_RECON)


def test_a_statement_spelled_as_the_flag_is_not_a_flag(model):
    body = json.loads(tools.tool_execute_sql({"sql": "--build-profile", "datasource": PROFILE}))
    assert body["status"] == "refused" and body["refusal"]["rule"] == guardrail.RULE_READ_ONLY


# --- the build-time runner ----------------------------------------------------------------------


@pytest.mark.parametrize("hosted", [False, True])
def test_the_build_time_runner_keeps_no_safety_and_adds_the_build_profile_locally(monkeypatch, hosted):
    import subprocess

    seen: list[list[str]] = []

    class _Done:
        returncode, stdout, stderr = 0, "x\n1\n", ""

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.append(cmd)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    if hosted:
        monkeypatch.setenv("AGAMI_DB_URL", "sqlite:///unused.db")
    else:
        monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    INTRO.make_execute_sql_runner(PROFILE, building_the_model=True)("SELECT 1")
    INTRO.make_execute_sql_runner(PROFILE)("SELECT 1")
    build, answer = seen
    assert "--no-safety" in build and ("--build-profile" in build) is not hosted
    assert "--no-safety" not in answer and "--build-profile" not in answer


@pytest.mark.parametrize("sql", [
    "SELECT o.id FROM orders o JOIN orders p USING (id)",
    "SELECT o.id FROM orders o NATURAL JOIN orders p",
    "SELECT o.id FROM orders o NATURAL LEFT JOIN orders p",
    "SELECT o.id, c.column_name FROM orders o JOIN information_schema.columns c USING (status)",
])
def test_a_using_or_natural_join_is_refused_under_build_profile(model, sql):
    refused, executor = _run(sql, build_profile=True)
    assert refused.status == "refused" and not executor.seen
    assert refused.refusal.rule == guardrail.RULE_UNSCOPABLE and "explicit ON" in refused.refusal.remediation


def test_a_using_join_is_unchanged_without_build_profile_and_an_explicit_on_runs_with_it(model):
    using = "SELECT o.id FROM orders o JOIN orders p USING (id)"
    assert _run(using)[0].status == "ok"
    on = "SELECT o.id FROM orders o JOIN orders p ON p.id = o.id"
    assert _run(on, build_profile=True)[0].status == "ok"


@pytest.mark.parametrize("sql", [
    # An ordinary quoted table whose name only starts like BigQuery's catalog spelling.
    'SELECT p.secret FROM public."information_schema.payroll" p',
    # information_schema views that hold row values or statement text.
    "SELECT s.histogram FROM information_schema.column_statistics s",
    "SELECT p.info FROM information_schema.processlist p",
    "SELECT t.trx_query FROM information_schema.innodb_trx t",
])
def test_a_catalog_name_that_is_not_a_structure_view_is_refused_under_build_profile(model, sql):
    refused, executor = _run(sql, build_profile=True)
    assert refused.status == "refused" and not executor.seen
    assert refused.refusal.rule == guardrail.RULE_TABLE_SCOPE


@pytest.mark.parametrize(("sql", "dialect", "allowed"), [
    ("SELECT t.table_name FROM ds.INFORMATION_SCHEMA.TABLES t", "bigquery", True),
    ("SELECT t.table_name FROM `proj.ds.INFORMATION_SCHEMA.COLUMNS` t", "bigquery", True),
    ("SELECT j.query FROM proj.ds.INFORMATION_SCHEMA.JOBS j", "bigquery", False),
    ('SELECT p.secret FROM public."information_schema.payroll" p', "postgres", False),
    ('SELECT p.secret FROM "information_schema.tables" p', "sqlite", False),
    ("SELECT t.name FROM main.information_schema.tables t", "sqlite", False),
    ("SELECT t.table_name FROM information_schema.tables t", "postgres", True),
    ("SELECT t.table_name FROM information_schema.tables t", "mysql", True),
])
def test_only_bigquery_spells_a_catalog_view_as_one_dotted_name(sql, dialect, allowed):
    import sqlglot
    from semantic_model import runtime
    from sqlglot import exp

    tbl = sqlglot.parse_one(sql, read=dialect).find(exp.Table)
    assert runtime._is_catalog_relation(tbl, dialect) is allowed


@pytest.mark.parametrize("sql", [
    # On Postgres a quoted name keeps its case, so these are ordinary schemas, not the catalog.
    'SELECT p.secret FROM "INFORMATION_SCHEMA".tables p',
    'SELECT p.secret FROM information_schema."TABLES" p',
    'SELECT p.secret FROM "PG_CATALOG".pg_class p',
])
def test_a_quoted_lookalike_of_a_catalog_schema_is_refused_under_build_profile(model, sql):
    refused, executor = _run(sql, build_profile=True)
    assert refused.status == "refused" and not executor.seen
    assert refused.refusal.rule == guardrail.RULE_TABLE_SCOPE


@pytest.mark.parametrize(("sql", "dialect", "allowed"), [
    ('SELECT t.table_name FROM "information_schema"."tables" t', "postgres", True),
    ('SELECT t.table_name FROM "INFORMATION_SCHEMA".tables t', "postgres", False),
    ('SELECT t.table_name FROM "INFORMATION_SCHEMA"."COLUMNS" t', "snowflake", True),
    ("SELECT t.table_name FROM information_schema.columns t", "snowflake", True),
    ('SELECT t.table_name FROM "information_schema".columns t', "snowflake", False),
    ("SELECT t.table_name FROM INFORMATION_SCHEMA.COLUMNS t", "tsql", True),
    ("SELECT t.table_name FROM information_schema.columns t", "duckdb", True),
    # Oracle has no information_schema, and an engine nobody resolved is trusted with none.
    ("SELECT c.secret FROM information_schema.columns c", "oracle", False),
    ("SELECT c.secret FROM information_schema.columns c", None, False),
])
def test_a_catalog_schema_matches_only_as_the_engine_spells_it(sql, dialect, allowed):
    import sqlglot
    from semantic_model import runtime
    from sqlglot import exp

    tbl = sqlglot.parse_one(sql, read=dialect).find(exp.Table)
    assert runtime._is_catalog_relation(tbl, dialect) is allowed


def test_order_by_a_name_that_only_case_folds_to_an_alias_reads_the_column(model):
    # Postgres resolves `secret` to orders.secret here, not to the output "SECRET".
    sql = 'SELECT o.id AS "SECRET" FROM orders o, jsonb_each(o.payload) kv ORDER BY secret LIMIT 1'
    refused, executor = _run(sql, build_profile=True)
    assert refused.status == "refused" and not executor.seen
    assert refused.refusal.rule == guardrail.RULE_UNSCOPABLE

    same = 'SELECT kv.key, COUNT(*) AS "n" FROM orders o, jsonb_each(o.payload) AS kv GROUP BY kv.key ORDER BY n'
    ran, executor = _run(same, build_profile=True)
    assert ran.status == "ok", ran.refusal


# ------------------------------------------------- beside table identity (#337, merged with #523)


@pytest.mark.parametrize("sql", [
    "SELECT t.table_name FROM demo..information_schema.tables t",
    "SELECT o.id FROM demo..orders o",
    # A catalog view in another database lists that database's structure.
    "SELECT t.table_name FROM other_db.information_schema.tables t",
    "SELECT c.relname FROM other_db.pg_catalog.pg_class c",
])
def test_table_identity_refusals_hold_under_build_profile(model, sql):
    refused, executor = _run(sql, build_profile=True)
    assert refused.status == "refused" and not executor.seen
    assert refused.refusal.rule == guardrail.RULE_TABLE_SCOPE


def test_a_catalog_view_in_the_connections_own_database_runs_under_build_profile(model):
    ran, _ = _run("SELECT t.table_name FROM demo.information_schema.tables t", build_profile=True)
    assert ran.status == "ok", ran.refusal


@pytest.mark.parametrize(("engine", "sql", "connection", "refused"), [
    ("Snowflake", "SELECT t.table_name FROM other_db.information_schema.tables t", "ANALYTICS", True),
    ("Snowflake", "SELECT t.table_name FROM analytics.information_schema.tables t", "ANALYTICS", False),
    ("Snowflake", "SELECT t.table_name FROM analytics.information_schema.tables t", None, True),
    ("BigQuery", "SELECT t.table_name FROM `other-project.ds.INFORMATION_SCHEMA.TABLES` t", "acme-project", True),
    ("BigQuery", "SELECT t.table_name FROM `acme-project.ds.INFORMATION_SCHEMA.TABLES` t", "acme-project", False),
    ("BigQuery", "SELECT t.table_name FROM ds.INFORMATION_SCHEMA.TABLES t", None, False),
])
def test_the_catalog_check_judges_a_catalog_views_database(engine, sql, connection, refused):
    from semantic_model import models as m
    from semantic_model import runtime

    table = m.Table(name="orders", schema="sales", storage_connection="c", grain=["id"],
                    description="orders", columns=[m.Column(name="id", type="integer")])
    org = m.Datasource(datasource="acme",
                       storage_connections=[m.StorageConnection(name="c", storage_type=engine)],
                       subject_areas=[m.SubjectArea(name="sales", tables_defined=[table])])
    res = runtime.check_catalog_scope(sql, org, connection)
    assert (res is not None) is refused
    assert res is None or res.rule == guardrail.RULE_TABLE_SCOPE


def test_the_default_schema_spelling_is_never_a_catalog_relation():
    import sqlglot
    from semantic_model import runtime
    from sqlglot import exp

    tbl = sqlglot.parse_one("SELECT name FROM db..sqlite_master", read="sqlite").find(exp.Table)
    assert runtime._is_catalog_relation(tbl, "sqlite") is False


@pytest.mark.parametrize("dialect", [
    "postgres", "snowflake", "oracle", "duckdb", "tsql", "bigquery", "sqlite", "databricks", "trino",
])
def test_the_catalog_name_rule_agrees_with_the_table_name_rule(dialect):
    """`_engine_name` (catalog views, ORDER BY aliases) and `_part_names` (declared tables) agree on a
    name stored in the engine's own folded case, which is how every catalog name is stored."""
    from semantic_model import runtime
    from sqlglot import exp

    stored = "ORDERS" if dialect in ("snowflake", "oracle") else "orders"
    want = runtime._engine_name(exp.Identifier(this=stored, quoted=True), dialect)
    for spelling in ("orders", "ORDERS", "Orders"):
        for quoted in (False, True):
            ident = exp.Identifier(this=spelling, quoted=quoted)
            by_table_rule = (spelling.lower() == stored.lower()
                             and runtime._part_names(ident, stored, dialect))
            assert (runtime._engine_name(ident, dialect) == want) is by_table_rule, (spelling, quoted)


# ------------------------------------------------- aliases that differ only in case


@pytest.mark.parametrize("sql", [
    # Postgres keeps a quoted alias's case: `c` is orders and `"C"` the catalog view, either order.
    'SELECT c.secret FROM orders c CROSS JOIN information_schema.columns AS "C"',
    'SELECT c.secret FROM information_schema.columns AS "C" CROSS JOIN orders c',
    # An unaliased catalog view registers under its own name too.
    "SELECT columns.secret FROM orders columns CROSS JOIN information_schema.columns",
    # Fail closed: which source the alias names is not known, so even a declared column is refused.
    'SELECT c.id FROM orders c CROSS JOIN information_schema.columns AS "C"',
])
def test_an_alias_shared_apart_from_case_is_refused_under_build_profile(model, sql):
    refused, executor = _run(sql, build_profile=True)
    assert refused.status == "refused" and not executor.seen
    assert refused.refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert "differ only in case" in refused.refusal.remediation


def test_distinct_aliases_beside_a_catalog_view_still_run_under_build_profile(model):
    sql = "SELECT o.id, c.column_name FROM orders o CROSS JOIN information_schema.columns AS c"
    ran, executor = _run(sql, build_profile=True)
    assert ran.status == "ok", ran.refusal
    assert executor.seen == [sql]


def _org(engine: str):
    """orders(id, tags) and customers(id, secret), with orders' `secret` left out of the model."""
    from semantic_model import models as m

    def table(name: str, *cols: str):
        return m.Table(name=name, schema="sales", storage_connection="c", grain=["id"], description=name,
                       columns=[m.Column(name=c, type="array" if c == "tags" else "string") for c in cols])

    return m.Datasource(datasource="acme",
                        storage_connections=[m.StorageConnection(name="c", storage_type=engine)],
                        subject_areas=[m.SubjectArea(name="sales", tables_defined=[
                            table("orders", "id", "tags"), table("customers", "id", "secret")])])


def test_an_alias_two_model_tables_share_apart_from_case_is_judged_against_both():
    from semantic_model import runtime

    org = _org("PostgreSQL")
    # `c` is orders, which leaves `secret` out; customers declaring it must not let it through.
    for sql in ('SELECT c.secret FROM orders c JOIN customers "C" ON c.id = "C".id',
                'SELECT c.secret FROM customers "C" JOIN orders c ON c.id = "C".id'):
        res = runtime.check_column_scope(sql, org)
        assert res is not None and res.rule == guardrail.RULE_COLUMN_SCOPE, sql
    # Declared on both, so whichever the alias names, it is declared.
    assert runtime.check_column_scope('SELECT c.id FROM orders c JOIN customers "C" ON c.id = "C".id', org) is None


# ------------------------------------------------- BigQuery UNNEST value and offset aliases


def _gates(sql: str, org, **kw):
    from semantic_model import runtime

    for gate in (runtime.check_scopable, runtime.check_table_scope):
        res = gate(sql, org, **kw)
        if res is not None:
            return res
    return runtime.check_column_scope(sql, org)


@pytest.mark.parametrize("sql", [
    "SELECT tag FROM sales.orders o, UNNEST(o.tags) AS tag",
    "SELECT tag, i FROM sales.orders o, UNNEST(o.tags) AS tag WITH OFFSET AS i ORDER BY i",
    "SELECT o.id, tag FROM sales.orders o CROSS JOIN UNNEST(o.tags) AS tag WITH OFFSET AS i WHERE i = 0",
])
def test_a_bigquery_unnest_value_and_offset_alias_run_under_build_profile(sql):
    org = _org("BigQuery")
    assert _gates(sql, org, build_profile=True) is None
    res = _gates(sql, org)
    assert res is not None and res.rule == guardrail.RULE_UNSCOPABLE


@pytest.mark.parametrize(("sql", "rule"), [
    # A name no source of that SELECT binds still has to be qualified.
    ("SELECT o.id FROM sales.orders o, UNNEST(o.tags) AS tag WHERE secret IS NULL", guardrail.RULE_UNSCOPABLE),
    ("SELECT secret FROM sales.orders o, UNNEST(o.tags) AS tag", guardrail.RULE_UNSCOPABLE),
    # Bound in the outer SELECT only, so the inner `tag` is not the unnest's.
    ("SELECT o.id FROM sales.orders o, UNNEST(o.tags) AS tag "
     "WHERE EXISTS (SELECT 1 FROM sales.customers c WHERE tag = c.id)", guardrail.RULE_UNSCOPABLE),
    ("SELECT tag FROM sales.orders o, UNNEST(o.secret) AS tag", guardrail.RULE_COLUMN_SCOPE),
])
def test_a_bigquery_unnest_does_not_open_anything_else_under_build_profile(sql, rule):
    res = _gates(sql, _org("BigQuery"), build_profile=True)
    assert res is not None and res.rule == rule
