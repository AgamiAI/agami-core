"""The backend-portable store + migration runner (Slice A).

Exercised against SQLite (the CI/test backend); the same `Store` runs on Postgres in production via
the `?`→`%s` adaptation. Proves: connect (in-memory + file), portable execute/query → dict rows,
and an idempotent migration runner.
"""

from __future__ import annotations

from store import Store


def test_connect_in_memory_and_execute_query():
    s = Store.connect("sqlite://")
    assert s.dialect == "sqlite"
    s.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    s.execute("INSERT INTO t (a, b) VALUES (?, ?)", (1, "x"))
    s.commit()
    rows = s.query("SELECT a, b FROM t WHERE a = ?", (1,))
    assert rows == [{"a": 1, "b": "x"}]  # dict rows on every backend
    s.close()


def test_connect_file_url(tmp_path):
    url = "sqlite://" + str(tmp_path / "agami.db")  # → sqlite:///abs/path
    s = Store.connect(url)
    s.execute("CREATE TABLE t (a INTEGER)")
    s.commit()
    s.close()
    assert (tmp_path / "agami.db").exists()


def test_unsupported_scheme_rejected():
    import pytest

    with pytest.raises(ValueError, match="Unsupported"):
        Store.connect("mysql://h/db")


def test_param_adaptation_per_dialect():
    s = Store.connect("sqlite://")
    assert s._adapt("SELECT ?, ?") == "SELECT ?, ?"  # sqlite keeps ?
    s.dialect = "postgres"
    assert s._adapt("SELECT ?, ?") == "SELECT %s, %s"  # postgres wants %s


def test_run_migrations_is_idempotent(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_first.sql").write_text("CREATE TABLE alpha (id INTEGER PRIMARY KEY);")
    (mig / "002_second.sql").write_text("CREATE TABLE beta (id INTEGER PRIMARY KEY);")

    s = Store.connect("sqlite://")
    ran = s.run_migrations(mig)
    assert ran == ["001_first.sql", "002_second.sql"]
    # both tables exist + are tracked
    names = {
        r["name"]
        for r in s.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }
    assert {"alpha", "beta", "schema_migrations"} <= names
    assert {r["id"] for r in s.query("SELECT id FROM schema_migrations")} == {
        "001_first.sql",
        "002_second.sql",
    }

    # re-running applies nothing new
    assert s.run_migrations(mig) == []

    # a newly added migration is picked up on the next run
    (mig / "003_third.sql").write_text("CREATE TABLE gamma (id INTEGER PRIMARY KEY);")
    assert s.run_migrations(mig) == ["003_third.sql"]
    s.close()
