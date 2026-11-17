"""A connection that can't be used stops introspection with its own message.

A live run with a credentials profile missing its `password` built all 34 tables, read no column
from any of them, and ended with "every name yielded zero columns. Check the --tables list (a
shell-quoting issue …)": every statement had failed with the real reason, and each table's reads
swallowed it as "no columns". A failure that is about the connection rather than the statement
(missing credentials, no driver, authentication, the network) fails every statement the same way,
so the first one now stops the run, with that message.

The same run read credentials from the artifacts folder a pointer file named, not the one
`--artifacts` gave: the runner now carries the folder it was given to execute_sql.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import introspect as INTRO  # noqa: E402

TABLES = ["sales.orders", "sales.customers", "sales.refunds"]


def _failing_runner(returncode: int, message: str):
    calls: list[str] = []

    def run(sql: str):
        calls.append(sql)
        raise INTRO.ExecuteSqlFailure(message, returncode)

    return run, calls


@pytest.mark.parametrize("code, message", [
    (2, "Credentials profile is missing required fields: ['password']."),
    (4, "password authentication failed for user \"reader\""),
    (10, "could not connect to server: Connection refused"),
])
def test_a_connection_failure_stops_the_run_with_its_own_message(tmp_path, code, message):
    run, calls = _failing_runner(code, message)
    with pytest.raises(INTRO.ConnectionUnusable) as err:
        INTRO.introspect("acme", "postgres", runner=run, artifacts_dir=tmp_path, tables=TABLES)
    assert message in str(err.value)
    assert "zero columns" not in str(err.value)
    # one statement reaches the database; the rest fail at once instead of each timing out in turn
    assert len(calls) == 1
    assert not (tmp_path / "acme" / "datasource.yaml").exists()


def test_a_statement_failure_still_drops_only_that_table(tmp_path):
    """A failure about one statement (a missing table) is that table's problem, not the run's."""
    def run(sql: str):
        raise INTRO.ExecuteSqlFailure('relation "sales.orders" does not exist', 8)

    with pytest.raises(RuntimeError) as err:
        INTRO.introspect("acme", "postgres", runner=run, artifacts_dir=tmp_path, tables=TABLES)
    assert not isinstance(err.value, INTRO.ConnectionUnusable)
    assert "zero columns" in str(err.value)


def test_the_runner_carries_the_artifacts_folder_it_was_given(tmp_path, monkeypatch):
    seen: dict = {}

    class _Proc:
        returncode, stdout, stderr = 0, "x\n1\n", ""

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        return _Proc()

    monkeypatch.setattr(INTRO.subprocess, "run", fake_run)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", "/somewhere/else")
    INTRO.make_execute_sql_runner("acme", artifacts_dir=tmp_path)("SELECT 1")
    assert seen["env"]["AGAMI_ARTIFACTS_DIR"] == str(tmp_path)


def test_without_an_artifacts_folder_the_runner_leaves_the_environment_alone(monkeypatch):
    seen: dict = {}

    class _Proc:
        returncode, stdout, stderr = 0, "x\n1\n", ""

    monkeypatch.setattr(INTRO.subprocess, "run", lambda cmd, **kw: seen.update(env=kw["env"]) or _Proc())
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", "/from/the/pointer")
    INTRO.make_execute_sql_runner("acme")("SELECT 1")
    assert seen["env"]["AGAMI_ARTIFACTS_DIR"] == "/from/the/pointer"


def test_sm_introspect_says_why_and_passes_its_artifacts_folder(tmp_path, monkeypatch, capsys):
    from semantic_model import cli

    given: dict = {}

    def fake_runner(profile, python=None, *, building_the_model=False, artifacts_dir=None):
        given["artifacts_dir"] = artifacts_dir
        return _failing_runner(2, "Credentials profile is missing required fields: ['password'].")[0]

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", fake_runner)
    rc = cli.main(["introspect", "--profile", "acme", "--db-type", "postgres",
                   "--artifacts", str(tmp_path), "--tables", *TABLES])
    err = capsys.readouterr().err
    assert rc == 2
    assert "missing required fields: ['password']" in err and "Traceback" not in err
    assert given["artifacts_dir"] == str(tmp_path)


def test_the_cli_and_introspection_share_one_list_of_connection_failures():
    from semantic_model import cli, failure_kinds

    assert cli._CONNECTION_KINDS is failure_kinds.CONNECTION_KINDS
    assert INTRO.CONNECTION_KINDS is failure_kinds.CONNECTION_KINDS


# A connection lost after the columns were read: relationship, placeholder and join probes each
# swallow their own failure, so without a last check the run wrote a model missing every join.
_SHOP = (
    "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, cust_ref INTEGER, amount REAL);"
    "INSERT INTO customers VALUES (1, 'a'), (2, 'b'), (3, 'c');"
    "INSERT INTO orders VALUES (1, 1, 2.0), (2, 2, 3.0), (3, 3, 1.0);"
)


def _shop_runner(tmp_path: Path, fail_from: str, code: int, *, once: bool = False):
    """A SQLite runner that fails from the first statement containing `fail_from` on: every later
    one too (a dead connection), or only that one (`once`, a statement's own error)."""
    import sqlite3

    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(_SHOP)
    con.close()
    state = {"failed": False}

    def run(sql: str) -> list[dict]:
        if (fail_from in sql and not (once and state["failed"])) or (state["failed"] and not once):
            state["failed"] = True
            raise INTRO.ExecuteSqlFailure("could not connect to server: Connection timed out"
                                          if code == 10 else "type mismatch in one probe", code)
        c = sqlite3.connect(db)
        try:
            cur = c.execute(sql)
            names = [d[0] for d in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            c.close()

    return run, state


@pytest.mark.parametrize("phase", ["AS from_table", "g.v AS v", "AS sampled"],
                         ids=["relationships", "placeholders", "join-candidates"])
def test_a_connection_lost_mid_build_stops_before_anything_is_written(tmp_path, phase):
    run, state = _shop_runner(tmp_path, phase, 10)
    out = tmp_path / "acme"
    with pytest.raises(INTRO.ConnectionUnusable) as err:
        INTRO.introspect("acme", "sqlite", runner=run, artifacts_dir=tmp_path, out_dir=out)
    assert state["failed"], f"the build never reached {phase!r}"
    assert "Connection timed out" in str(err.value)
    assert not (out / "datasource.yaml").exists()
    assert not (out / ".introspect" / "join_candidates.json").exists()


def test_one_probe_failing_on_its_own_statement_does_not_stop_the_build(tmp_path):
    run, state = _shop_runner(tmp_path, "AS sampled", 5, once=True)
    out = tmp_path / "acme"
    INTRO.introspect("acme", "sqlite", runner=run, artifacts_dir=tmp_path, out_dir=out)
    assert state["failed"]
    assert (out / "datasource.yaml").exists()


@pytest.mark.parametrize("tables", [None, TABLES], ids=["catalog", "allowlist"])
def test_discovery_stops_with_the_connection_message_not_the_table_list_advice(tables):
    run, calls = _failing_runner(4, "password authentication failed for user \"reader\"")
    with pytest.raises(INTRO.ConnectionUnusable) as err:
        INTRO.discover_inventory("acme", "postgres", runner=run, tables=tables)
    assert "password authentication failed" in str(err.value)
    assert "explicit" not in str(err.value)
    assert len(calls) == 1


def test_discovery_with_a_locked_catalog_still_gives_the_table_list_advice():
    run, _ = _failing_runner(9, "permission denied for schema information_schema")
    with pytest.raises(RuntimeError) as err:
        INTRO.discover_inventory("acme", "postgres", runner=run)
    assert not isinstance(err.value, INTRO.ConnectionUnusable)
    assert "explicit" in str(err.value)


def test_sm_discover_says_why_and_exits_2(tmp_path, monkeypatch, capsys):
    from semantic_model import cli

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", lambda *a, **kw: _failing_runner(
        10, "could not connect to server: Connection refused")[0])
    rc = cli.main(["discover", "--profile", "acme", "--db-type", "postgres",
                   "--artifacts", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "Connection refused" in err and "Traceback" not in err
    assert not (tmp_path / "acme" / ".introspect" / "inventory.json").exists()
