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
