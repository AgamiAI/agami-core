"""The serving + runtime schema migrations create cleanly on an empty DB (Slice B).

Runs the real migrations/core/*.sql against SQLite (the portable backend the tests use) and asserts
every per-object serving table + the runtime tables exist — the schema that backs the 5 tools.
"""

from __future__ import annotations

from store import Store

SERVING_TABLES = {
    "organization",
    "subject_area",
    "model_table",
    "metric",
    "entity",
    "relationship",
    "prompt_example",
    "memory",
    "model_version",
}
RUNTIME_TABLES = {"query_executions", "feedback"}


def _tables(s: Store) -> set[str]:
    return {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}


def test_real_migrations_create_all_tables_on_empty_db():
    s = Store.connect("sqlite://")
    ran = s.run_migrations()  # the real migrations/core dir
    assert "001_serving.sql" in ran and "002_runtime.sql" in ran
    tables = _tables(s)
    assert SERVING_TABLES <= tables
    assert RUNTIME_TABLES <= tables
    s.close()


def test_users_table_is_flat_no_role_column():
    # ACE-004: the users table exists with the identity columns and, crucially, NO role/permission
    # column — flat access is the open-core contract (roles are paid).
    s = Store.connect("sqlite://")
    s.run_migrations()
    assert "users" in _tables(s)
    cols = {r["name"] for r in s.query("PRAGMA table_info(users)")}
    assert {"id", "username", "password_hash", "email", "status", "created"} <= cols
    assert not (cols & {"role", "roles", "permission", "permissions"}), "flat access — no role column"
    s.close()


def test_migrations_are_idempotent_on_real_dir():
    s = Store.connect("sqlite://")
    s.run_migrations()
    assert s.run_migrations() == []  # nothing new the second time
    s.close()


def test_sizing_metadata_columns_present():
    # The smart get_datasource_schema sizing reads these; assert they exist so a schema change
    # can't silently drop them.
    s = Store.connect("sqlite://")
    s.run_migrations()
    sa_cols = {r["name"] for r in s.query("PRAGMA table_info(subject_area)")}
    assert "table_count" in sa_cols
    tbl_cols = {r["name"] for r in s.query("PRAGMA table_info(model_table)")}
    assert "est_row_count" in tbl_cols
    s.close()


def test_pk_area_columns_are_not_null_for_postgres_portability():
    # Postgres forbids NULL in a PRIMARY KEY column; SQLite would allow it. `area` is in the PK of
    # metric/entity/relationship, so it must be NOT NULL or the schema isn't Postgres-portable.
    s = Store.connect("sqlite://")
    s.run_migrations()
    for table in ("metric", "entity", "relationship"):
        area = next(r for r in s.query(f"PRAGMA table_info({table})") if r["name"] == "area")
        assert area["notnull"] == 1, f"{table}.area must be NOT NULL (it's in the PK)"
    s.close()
