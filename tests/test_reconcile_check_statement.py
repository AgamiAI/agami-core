"""`check_statement.py`: reconcile's Phase 1.5 checks a supplied statement in code, and every
statement it sends, the zero-row check, the statement and each probe, goes through
`execute_sql.execute_guarded` (ACE-155).

Most of these run against the sample store the plugin ships, through the real guard, the real
semantic-model verbs and a real SQLite file, then read the row directory back through
`reconcile.py ledger`: the files are only right if the ledger grades them the way it graded the
files the session used to write by hand. The rest stub the chokepoint where a database would have to
misbehave on cue.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
SAMPLE = REPO_ROOT / "plugins" / "agami" / "samples" / "store"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SAMPLE))

import build_sample  # noqa: E402
import check_statement as cs  # noqa: E402
import reconcile  # noqa: E402

PROFILE = "demo"
AREA = "agami-example"
REAL_SM = cs.sm_cli.main

# The files a checked row holds before any probe: the statement, its outcome, and the five verbs.
BASE_FILES = {
    "statement.sql",
    "run.json",
    "zero-row.sql",
    "statement-prepare.json",
    "statement.csv",
    "statement-receipt.json",
    "mentions.json",
    "join-probes.json",
    "filter-values.plan.json",
    "filter-values.judge.json",
}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    base = tmp_path_factory.mktemp("check-statement")
    db = base / "store.db"
    build_sample.build(db, prefer_cli=False)
    shutil.copytree(SAMPLE / "model", base / PROFILE)
    return {"base": base, "db": db}


@pytest.fixture
def row(store, tmp_path, monkeypatch):
    """A wired profile, a spy on the chokepoint, and a way to write a row and check it."""
    for key in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(store["base"]))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{store['db']}")
    # `main` quiets the executor's loggers for the life of its process; this one is shared.
    for name in ("execute_sql", "execute_sql.raw"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "propagate", logger.propagate)
        monkeypatch.setattr(logger, "handlers", list(logger.handlers))
    seen: list[str] = []
    real = cs.execute_sql.execute_guarded

    def spy(sql, profile, area, **kwargs):
        seen.append(sql)
        assert (
            profile == PROFILE
            and area == AREA
            and kwargs["executor"] is cs.execute_sql.BUILTIN_EXECUTOR
        )
        assert not kwargs.get("no_safety")
        return real(sql, profile, area, **kwargs)

    monkeypatch.setattr(cs.execute_sql, "execute_guarded", spy)

    def check(sql: str) -> SimpleNamespace:
        row_dir = tmp_path / "rows" / "1"
        row_dir.mkdir(parents=True, exist_ok=True)
        (row_dir / "statement.sql").write_text(sql)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cs.main(["--profile", PROFILE, "--area", AREA, "--row-dir", str(row_dir)])
        return SimpleNamespace(
            rc=rc,
            dir=row_dir,
            out=out.getvalue(),
            seen=seen,
            run=json.loads((row_dir / "run.json").read_text()),
        )

    return check


def _ledger(row_dir: Path) -> dict[str, dict]:
    (row_dir / "question_fit.json").write_text(json.dumps({"fit": "plausible", "reason": None}))
    return {part["part"]: part for part in reconcile.ledger(row_dir)["rows"]}


def _names(row_dir: Path) -> set[str]:
    return {p.name for p in row_dir.iterdir()}


def _stub_sm(monkeypatch, **replies):
    """Answer the named `sm` verbs with a fixed payload, or raise when the payload is an exception;
    every other verb runs for real."""

    def main(argv):
        reply = replies.get(argv[0].replace("-", "_"))
        if reply is None:
            return REAL_SM(argv)
        if isinstance(reply, Exception):
            raise reply
        print(json.dumps(reply))
        return 0

    monkeypatch.setattr(cs.sm_cli, "main", main)


# --- a statement that runs ------------------------------------------------------------------


def test_a_select_runs_every_step_through_the_guard_and_the_ledger_reads_what_it_wrote(
    row, store, capsys
):
    sql = "SELECT COUNT(*) AS n FROM subscriptions s JOIN orders o ON o.customer_id = s.customer_id WHERE o.status != 'cancelled'"
    got = row(sql)

    assert got.rc == 0
    assert got.run == {
        "status": "ok",
        "exit": 0,
        "kind": None,
        "rule": None,
        "detail": None,
        "remediation": None,
    }
    assert (got.dir / "statement.csv").read_text().splitlines()[0] == "n"
    # One plan of every probe the verbs emitted, named as the part ledger lists them.
    plan = json.loads((got.dir / "probes.plan.json").read_text())
    ids = {entry["id"] for entry in plan}
    assert ids == {
        "join-1.overlap.0",
        "join-1.overlap.1",
        "join-1.dropped_rows",
        "cardinality.orders.customer_id",
        "cardinality.subscriptions.customer_id",
        "orders.status.distinct",
        "lit-1.exists",
    }
    probe_files = {f"{i}{ext}" for i in ids for ext in (".sql", ".csv", ".run.json")}
    assert _names(got.dir) == BASE_FILES | probe_files | {
        "probes.plan.json",
        "probes.plan.json.manifest.json",
    }
    manifest = json.loads((got.dir / "probes.plan.json.manifest.json").read_text())
    assert manifest["total"] == manifest["ok"] == 7
    # Every execution was the chokepoint's: the zero-row wrap, the statement, and each probe on its own.
    probes = {(got.dir / f"{i}.sql").read_text() for i in ids}
    assert len(got.seen) == 9 and set(got.seen[2:]) == probes
    assert got.seen[0].startswith("SELECT 1 FROM (\n") and got.seen[1] == sql
    # No `exists` counted zero, so no near-miss probe ran.
    assert not (got.dir / "lit-1.exists_folded.csv").exists()
    # The summary names outcomes and never the statement; nothing reached stderr.
    assert json.loads(got.out) == {
        "row_dir": str(got.dir),
        "probes": 7,
        "probes_ok": 7,
        "run": got.run,
    }
    assert "SELECT" not in got.out and capsys.readouterr().err == ""
    # The ledger grades from these files exactly as it graded the hand-written ones.
    parts = _ledger(got.dir)
    assert parts["runs"]["verdict"] == "confirmed" and parts["scope"]["verdict"] == "confirmed"
    assert parts["join:orders-subscriptions"]["verdict"] == "model_gap"
    assert parts["join_key:orders-subscriptions"]["verdict"] == "confirmed"
    assert parts["dropped_rows:orders-subscriptions"]["verdict"] == "noted"
    assert parts["values_declared:orders.status"]["verdict"] == "confirmed"
    # This phase keeps its own record; the query log is the AI's.
    assert not list(store["base"].rglob("query_log.jsonl"))


def test_a_value_no_row_holds_gets_its_near_miss_probe_and_no_other_value_does(row):
    got = row(
        "SELECT COUNT(*) AS delivered FROM orders o WHERE o.status = 'Delivered' AND o.status != 'cancelled'"
    )

    assert got.rc == 0 and got.run["status"] == "ok"
    assert (got.dir / "lit-1.exists.csv").read_text().splitlines() == ["n", "0"]
    folded = json.loads((got.dir / "probes.folded.plan.json").read_text())
    assert [entry["id"] for entry in folded] == ["lit-1.exists_folded"]
    assert (got.dir / "lit-1.exists_folded.run.json").exists() and (
        got.dir / "probes.folded.plan.json.manifest.json"
    ).exists()
    assert not (got.dir / "lit-2.exists_folded.csv").exists()
    assert got.seen[-1] == (got.dir / "lit-1.exists_folded.sql").read_text()
    lit = _ledger(got.dir)["literal:orders.status=Delivered"]
    assert lit["verdict"] == "query_defect" and lit["evidence"]["near_miss"] == "delivered"


def test_a_trailing_line_comment_does_not_swallow_the_zero_row_wrapper(row):
    got = row(
        "SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled' -- every order not cancelled\n;"
    )
    assert got.rc == 0 and got.run["status"] == "ok"
    assert (got.dir / "zero-row.sql").read_text().endswith("cancelled\n) AS _agami_check WHERE 1=0")


# --- refusals are findings ------------------------------------------------------------------


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT 1; DROP TABLE orders"])
def test_a_statement_that_writes_is_refused_by_the_guards_own_gate_and_nothing_runs_after_it(
    row, store, sql
):
    before = sqlite3.connect(store["db"]).execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    got = row(sql)

    assert got.rc == 0
    assert (
        got.run["status"] == "refused" and got.run["exit"] == 1 and got.run["rule"] == "read_only"
    )
    assert got.run["remediation"]
    assert got.seen == [] and _names(got.dir) == {"statement.sql", "run.json"}
    assert (
        sqlite3.connect(store["db"]).execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before
    )
    assert _ledger(got.dir)["runs"]["verdict"] == "unresolved"


def test_a_scope_refusal_is_written_down_and_grades_as_a_gap_in_the_semantic_model(row):
    got = row("SELECT COUNT(*) AS n FROM orders o WHERE o.nonexistent = 1")

    assert got.rc == 0
    assert (
        got.run["status"] == "refused"
        and got.run["rule"] == "column_scope"
        and got.run["remediation"]
    )
    assert (got.dir / "statement.csv").read_bytes() == b""
    parts = _ledger(got.dir)
    assert parts["scope"]["verdict"] == "model_gap" and parts["scope"]["kind"] == "scope"
    assert parts["runs"]["verdict"] == "unresolved"


def test_a_probe_the_guard_refuses_leaves_an_empty_csv_and_its_run_record(row, monkeypatch):
    _stub_sm(
        monkeypatch,
        join_probes={
            "joins": [
                {
                    "id": "join-1",
                    "probes": {
                        "overlap": [
                            {"sql": "SELECT * FROM orders"},
                            {"sql": "SELECT COUNT(*) AS matched FROM orders"},
                        ]
                    },
                    "dropped_rows_probe": None,
                }
            ],
            "cardinality": {"orders.id": None},
            "joins_written": 1,
            "unreadable": None,
        },
    )
    got = row("SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled'")

    assert got.rc == 0 and got.run["status"] == "ok"
    assert "SELECT * FROM orders" in got.seen  # it reached the guard, and the guard said no
    assert (got.dir / "join-1.overlap.0.csv").read_bytes() == b""
    refused = json.loads((got.dir / "join-1.overlap.0.run.json").read_text())
    assert refused["status"] == "refused" and refused["rule"] == "select_star"
    assert (got.dir / "join-1.overlap.1.csv").read_text().splitlines()[0] == "matched"
    assert not any(name.startswith("cardinality.orders.id") for name in _names(got.dir))
    summary = json.loads(got.out)
    assert summary["probes"] == 4 and summary["probes_ok"] == 3


# --- failures -------------------------------------------------------------------------------


def _failing(monkeypatch, kinds: list[str | None]) -> list[str]:
    """Stub the chokepoint: the n-th call fails with the n-th kind, or runs for real on None."""
    calls: list[str] = []
    real = cs.execute_sql.execute_guarded

    def guarded(sql, profile, area, **kwargs):
        calls.append(sql)
        kind = kinds[len(calls) - 1] if len(calls) <= len(kinds) else None
        if kind is None:
            return real(sql, profile, area, **kwargs)
        return SimpleNamespace(
            status="failed",
            refusal=None,
            data=None,
            failure=SimpleNamespace(kind=kind, message=f"a value-free sentence about {kind}"),
        )

    monkeypatch.setattr(cs.execute_sql, "execute_guarded", guarded)
    return calls


def test_the_zero_row_check_ends_the_row_on_the_persons_own_defect(row, monkeypatch):
    calls = _failing(monkeypatch, ["column_not_found"])
    got = row("SELECT COUNT(*) AS n FROM orders;")

    assert got.rc == 0 and len(calls) == 1
    assert (
        calls[0] == "SELECT 1 FROM (\nSELECT COUNT(*) AS n FROM orders\n) AS _agami_check WHERE 1=0"
    )
    assert got.run == {
        "status": "failed",
        "exit": 7,
        "kind": "column_not_found",
        "rule": None,
        "detail": "a value-free sentence about column_not_found",
        "remediation": "a value-free sentence about column_not_found",
    }
    assert _names(got.dir) == {"statement.sql", "zero-row.sql", "run.json"}
    runs = _ledger(got.dir)["runs"]
    assert runs["verdict"] == "query_defect" and runs["evidence"]["remediation"]


@pytest.mark.parametrize("kinds,calls_made", [(["auth"], 1), ([None, "driver_missing"], 2)])
def test_a_database_that_cannot_be_reached_as_configured_stops_the_run(
    row, monkeypatch, kinds, calls_made
):
    calls = _failing(monkeypatch, kinds)
    got = row("SELECT COUNT(*) AS n FROM orders")

    assert got.rc == 3 and len(calls) == calls_made
    assert got.run["status"] == "failed" and got.run["kind"] == kinds[-1]
    # Nothing is probed over a connection that cannot open.
    assert (
        not (got.dir / "join-probes.json").exists() and not (got.dir / "probes.plan.json").exists()
    )


def test_a_verb_that_raises_leaves_an_empty_file_the_ledger_reads_as_unchecked(
    row, monkeypatch, capsys
):
    _stub_sm(
        monkeypatch, receipt=RuntimeError("near SELECT secret_column"), join_probes=ValueError("x")
    )
    got = row("SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled'")

    assert got.rc == 0 and got.run["status"] == "ok"
    assert (got.dir / "statement-receipt.json").read_text() == ""
    err = capsys.readouterr().err
    assert "`sm receipt` raised RuntimeError" in err and "secret_column" not in err
    parts = _ledger(got.dir)
    assert (
        parts["receipt:*"]["verdict"] == "unresolved" and parts["join:*"]["verdict"] == "unresolved"
    )


def test_a_row_with_no_readable_statement_cannot_start(tmp_path, capsys):
    assert cs.main(["--profile", PROFILE, "--area", AREA, "--row-dir", str(tmp_path)]) == 2
    assert "no readable statement.sql" in capsys.readouterr().err


def test_an_exists_file_counts_zero_only_when_it_holds_a_zero(tmp_path):
    path = tmp_path / "lit-1.exists.csv"
    assert cs._counted_zero(path) is False  # never ran
    for text, zero in (
        ("", False),
        ("n\n", False),
        ("n\nabc\n", False),
        ("n\n3\n", False),
        ("N\n0.0\n", True),
    ):
        path.write_text(text)
        assert cs._counted_zero(path) is zero, text


def test_an_interpreter_without_the_semantic_model_package_is_told_how_to_get_one(tmp_path):
    """The marketplace layout with no pip install: the bundled lib ships a stub `semantic_model`,
    so the script stops before it could check anything and says what to run."""
    env = {**os.environ, "PYTHONPATH": ""}
    if (
        subprocess.run(
            [sys.executable, "-S", "-c", "import agami_paths"], env=env, capture_output=True
        ).returncode
        == 0
    ):
        pytest.skip("cannot hide the installed agami-core from a subprocess here")
    shutil.copytree(SCRIPTS, tmp_path / "scripts")
    shutil.copytree(REPO_ROOT / "plugins" / "agami" / "lib", tmp_path / "lib")
    r = subprocess.run(
        [sys.executable, "-S", str(tmp_path / "scripts" / "check_statement.py"), "--help"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2 and 'sm" install' in r.stderr
