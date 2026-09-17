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
    "zero-row.run.json",
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
    """Answer the named `sm` verbs with a fixed payload, or raise when the payload is an exception,
    or print the text of a `(text, exception)` pair and then raise; every other verb runs for real."""

    def main(argv):
        reply = replies.get(argv[0].replace("-", "_"))
        if reply is None:
            return REAL_SM(argv)
        if isinstance(reply, tuple):
            printed, reply = reply
            print(printed, end="")
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
    # The result itself, not only its header: Phase 1.5f takes `expected` from this file's one cell.
    count = sqlite3.connect(store["db"]).execute(sql).fetchone()[0]
    assert (got.dir / "statement.csv").read_text().splitlines() == ["n", str(count)]
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
    # A `:*` part is the ledger's mark for a file that is missing, empty or an error. A verb called
    # with a wrong argument writes one of those, and this is where it would show.
    assert [name for name in parts if name.endswith(":*")] == []
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


@pytest.mark.parametrize(
    "tail",
    [
        " -- every order not cancelled\n;",  # a comment, then the semicolon
        "; -- done",  # the semicolon, then a comment: wrapped verbatim this is two statements
        ";\n/* done */\n",
        "\n;\n",
    ],
    ids=["comment-then-semicolon", "semicolon-then-comment", "block-comment", "bare-semicolon"],
)
def test_a_trailing_comment_or_semicolon_never_costs_the_row_its_zero_row_check(row, tail):
    """The wrap must hold one statement and no comment that could swallow its tail. Before ACE-155's
    review the `; -- done` form wrapped to two statements, the guard refused the wrap on `read_only`,
    and the refusal was dropped: no `zero-row.run.json`, nothing said, the statement ran anyway."""
    got = row(f"SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled'{tail}")

    assert got.rc == 0 and got.run["status"] == "ok"
    zero = (got.dir / "zero-row.sql").read_text()
    assert zero.endswith("!= 'cancelled'\n) AS _agami_check WHERE 1=0")
    assert "--" not in zero and "/*" not in zero and ";" not in zero
    # The wrap ran, and its own outcome is on disk beside it.
    assert json.loads((got.dir / "zero-row.run.json").read_text())["status"] == "ok"
    assert got.seen[0] == zero
    assert _ledger(got.dir)["runs"]["verdict"] == "confirmed"


@pytest.mark.parametrize("tail", ["", ";", "; -- done"])
def test_a_semicolon_or_two_dashes_inside_a_value_is_not_read_as_the_end_of_the_statement(row, tail):
    """A `;` or a `--` inside a quoted value never ends a statement, so the wrap keeps it and the
    filter still means what the person wrote. Cutting there would make a sound statement a syntax
    error, and a syntax error at this step is graded as the person's own defect."""
    got = row(f"SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'a; -- b'{tail}")

    assert got.rc == 0 and got.run["status"] == "ok"
    assert (got.dir / "zero-row.sql").read_text().endswith(
        "WHERE o.status != 'a; -- b'\n) AS _agami_check WHERE 1=0"
    )


def test_what_the_zero_row_wrap_drops_off_the_end_and_what_it_must_not():
    """`_wrap_body` on its own: the wrap must hold one statement, and must never cut into one."""
    for statement, body in (
        ("SELECT 1", "SELECT 1"),
        ("  SELECT 1 ;\n ", "SELECT 1"),
        ("SELECT 1;;", "SELECT 1"),
        ("SELECT 1; -- done", "SELECT 1"),
        ("SELECT 1 -- done\n;", "SELECT 1"),
        ("SELECT 1;\n/* done */\n", "SELECT 1"),
        ("SELECT 1 /* a */ FROM t;", "SELECT 1 /* a */ FROM t"),
        ("SELECT 1 -- a\nFROM t;", "SELECT 1 -- a\nFROM t"),
        # A value or an identifier that holds what would otherwise end the statement.
        ("SELECT 'a; -- b';", "SELECT 'a; -- b'"),
        ("SELECT 'it''s; -- b';", "SELECT 'it''s; -- b'"),
        ('SELECT "a; -- b" FROM t;', 'SELECT "a; -- b" FROM t'),
        # Where a value ends cannot be read: drop the semicolon and no more, as it always did.
        ("SELECT 'a;", "SELECT 'a"),
        ("SELECT 'a\\' ; -- b", "SELECT 'a\\' ; -- b"),
        ("SELECT 'a\\' ;", "SELECT 'a\\'"),
    ):
        assert cs._wrap_body(statement) == body, statement


def test_a_wrap_the_guard_refuses_is_written_down_and_the_statement_is_still_checked(
    row, monkeypatch
):
    """The wrap is agami's statement, not the person's, so a refusal of it is this check not run.
    It must still reach disk: `SKILL.md` and `statement-check.md` both promise every execution and
    every refusal in this phase is written down."""
    real = cs.execute_sql.execute_guarded
    calls: list[str] = []

    def guarded(sql, profile, area, **kwargs):
        calls.append(sql)
        if len(calls) == 1:
            return SimpleNamespace(
                status="refused",
                failure=None,
                data=None,
                refusal=SimpleNamespace(
                    rule="read_only", detail="a value-free sentence", remediation="one sentence"
                ),
            )
        return real(sql, profile, area, **kwargs)

    monkeypatch.setattr(cs.execute_sql, "execute_guarded", guarded)
    got = row("SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled'")

    assert got.rc == 0
    assert json.loads((got.dir / "zero-row.run.json").read_text()) == {
        "status": "refused",
        "exit": 1,
        "kind": None,
        "rule": "read_only",
        "detail": "a value-free sentence",
        "remediation": "one sentence",
    }
    # The statement is checked on its own, and `run.json` carries its outcome, not the wrap's.
    assert got.run["status"] == "ok" and calls[1] == "SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled'"
    assert _ledger(got.dir)["runs"]["verdict"] == "confirmed"


# --- refusals are findings ------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,rule",
    [
        ("DELETE FROM orders", "read_only"),
        ("SELECT 1; DROP TABLE orders", "read_only"),
        # Reads the server's own metadata: the recon gate, which runs before anything too.
        ("SELECT version() AS v", "recon"),
    ],
)
def test_a_statement_that_writes_or_reads_the_servers_metadata_is_refused_and_nothing_runs_after_it(
    row, store, sql, rule
):
    before = sqlite3.connect(store["db"]).execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    got = row(sql)

    assert got.rc == 0
    assert got.run["status"] == "refused" and got.run["exit"] == 1 and got.run["rule"] == rule
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


# --- checking a row again -------------------------------------------------------------------


@pytest.mark.parametrize(
    "second,kinds,left",
    [
        # Refused at the read-only gate: nothing but the outcome.
        ("DELETE FROM orders", [], {"run.json"}),
        # Ended by the zero-row check: the wrap, the wrap's own outcome, and the row's.
        (
            "SELECT COUNT(*) AS n FROM orders",
            ["column_not_found"],
            {"zero-row.sql", "zero-row.run.json", "run.json"},
        ),
    ],
)
def test_checking_a_row_again_clears_every_file_the_last_statement_left(
    row, monkeypatch, second, kinds, left
):
    first = row(
        "SELECT COUNT(*) AS delivered FROM subscriptions s JOIN orders o ON o.customer_id = s.customer_id "
        "WHERE o.status = 'Delivered' AND o.status != 'cancelled'"
    )
    assert first.run["status"] == "ok"
    assert {"join-1.overlap.0.csv", "lit-1.exists_folded.csv", "probes.folded.plan.json"} <= _names(
        first.dir
    )
    # Files the session and Phase 2 write beside them are not the script's to clear.
    kept = {"question_fit.json", "agami.sql", "actual.csv"}
    for name in kept:
        (first.dir / name).write_text("kept")

    _failing(monkeypatch, kinds)
    got = row(second)

    assert _names(got.dir) == {"statement.sql"} | left | kept
    assert not [name for name in _ledger(got.dir) if name.startswith(("join", "literal"))]


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


@pytest.mark.parametrize("kind", ["column_not_found", "table_not_found", "syntax"])
def test_the_zero_row_check_ends_the_row_on_the_persons_own_defect(row, monkeypatch, kind):
    calls = _failing(monkeypatch, [kind])
    got = row("SELECT COUNT(*) AS n FROM orders;")

    assert got.rc == 0 and len(calls) == 1
    assert (
        calls[0] == "SELECT 1 FROM (\nSELECT COUNT(*) AS n FROM orders\n) AS _agami_check WHERE 1=0"
    )
    assert got.run == {
        "status": "failed",
        "exit": cs.execute_sql.FAILURE_KIND_TO_EXIT[kind],
        "kind": kind,
        "rule": None,
        "detail": f"a value-free sentence about {kind}",
        "remediation": f"a value-free sentence about {kind}",
    }
    assert _names(got.dir) == {"statement.sql", "zero-row.sql", "zero-row.run.json", "run.json"}
    # The wrap's own record says the same thing the row's does; it is the wrap that failed.
    assert json.loads((got.dir / "zero-row.run.json").read_text()) == got.run
    runs = _ledger(got.dir)["runs"]
    assert runs["verdict"] == "query_defect" and runs["evidence"]["remediation"]


# Written out rather than read from the script, so dropping one from the script fails here.
@pytest.mark.parametrize("kind", ["auth", "dsn", "network", "permission", "driver_missing"])
@pytest.mark.parametrize("before", [0, 1], ids=["at-the-zero-row-check", "at-the-statement"])
def test_a_database_that_cannot_be_reached_as_configured_stops_the_run(
    row, monkeypatch, kind, before
):
    calls = _failing(monkeypatch, [None] * before + [kind])
    got = row("SELECT COUNT(*) AS n FROM orders")

    assert got.rc == 3 and len(calls) == before + 1
    assert got.run["status"] == "failed" and got.run["kind"] == kind
    # Nothing is probed over a connection that cannot open.
    assert (
        not (got.dir / "join-probes.json").exists() and not (got.dir / "probes.plan.json").exists()
    )


@pytest.mark.parametrize(
    "tail",
    ["", "; -- done"],
    ids=["plain", "trailing-comment"],
)
def test_a_semantic_model_declaring_another_engine_than_its_credentials_stops_the_run(
    row, monkeypatch, tail
):
    """The guard refuses every statement on that datasource until an operator fixes it.

    Both spellings stop at the zero-row check, on the first call. The `; -- done` form used to reach
    a second call, because the wrap was refused as two statements and that refusal was dropped;
    reaching the guard once is the stronger pin, so it replaces it."""
    monkeypatch.setenv(
        f"DATASOURCE_URL__{PROFILE.upper()}", "postgresql://reader@127.0.0.1:1/sales"
    )
    got = row(f"SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled'{tail}")

    assert got.rc == 3 and len(got.seen) == 1
    assert got.run["status"] == "refused" and got.run["rule"] == "engine_mismatch"
    assert json.loads((got.dir / "zero-row.run.json").read_text()) == got.run
    assert (
        not (got.dir / "join-probes.json").exists() and not (got.dir / "probes.plan.json").exists()
    )


def test_a_verb_that_raises_leaves_an_empty_file_the_ledger_reads_as_unchecked(
    row, monkeypatch, capsys
):
    _stub_sm(
        monkeypatch,
        # Half an answer, then the raise: the file must not read as a receipt.
        receipt=('{"tables": {"items": [', RuntimeError("near SELECT secret_column")),
        join_probes=ValueError("x"),
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


def test_the_drivers_own_words_never_reach_stderr(store, tmp_path):
    """Run as the skill runs it, in a process of its own. In process, pytest's log capture takes a
    record before it could reach stderr, so only a subprocess shows what the session would see."""
    row_dir = tmp_path / "rows" / "1"
    row_dir.mkdir(parents=True)
    # The guard passes it, and SQLite fails it while running it: an integer overflow.
    (row_dir / "statement.sql").write_text(
        "SELECT abs(-9223372036854775808) AS n FROM orders o WHERE o.status != 'cancelled'"
    )
    env = {
        **os.environ,
        "AGAMI_ARTIFACTS_DIR": str(store["base"]),
        f"DATASOURCE_URL__{PROFILE.upper()}": f"sqlite:///{store['db']}",
    }
    for key in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        env.pop(key, None)
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "check_statement.py"),
            "--profile",
            PROFILE,
            "--area",
            AREA,
            "--row-dir",
            str(row_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    run = json.loads((row_dir / "run.json").read_text())

    assert r.returncode == 0 and run["status"] == "failed" and run["kind"] == "other"
    assert r.stderr == ""
    # Said plainly, with nothing the database said, and no log that is not there.
    assert "could not classify" in run["detail"]
    assert "overflow" not in json.dumps(run) and "server log" not in json.dumps(run)


def test_a_break_in_agamis_own_code_is_named_on_stderr_and_in_run_json_without_its_text(
    row, monkeypatch, tmp_path, capsys
):
    # A credentials file configparser cannot read: the chokepoint's catch-all, not the database.
    creds = tmp_path / "credentials"
    creds.write_text("[demo]\ntype = sqlite\n[demo]\ntype = sqlite\n")
    creds.chmod(0o600)
    monkeypatch.delenv(f"DATASOURCE_URL__{PROFILE.upper()}")
    monkeypatch.setattr(cs.execute_sql, "CREDENTIALS_PATH", creds)
    got = row("SELECT COUNT(*) AS n FROM orders o WHERE o.status != 'cancelled'")

    assert got.rc == 0 and got.run["status"] == "failed" and got.run["kind"] == "other"
    assert "raised DuplicateSectionError" in got.run["detail"]
    err = capsys.readouterr().err
    assert (
        "check_statement: execute_sql: unhandled error in the guarded execution path "
        "(DuplicateSectionError)"
    ) in err
    # The error's own text names the file's path; neither channel carries it, or a log nobody has.
    said = err + json.dumps(got.run)
    assert str(creds) not in said and "already exists" not in said and "server log" not in said


def test_a_verb_that_returns_non_zero_leaves_an_empty_file_and_never_lands_its_payload(
    tmp_path, capsys
):
    """A verb can print a JSON error payload and return non-zero instead of raising: `cli.main`
    answers a root with no model that way, and four of these verbs return 2 the same way. Written to
    the part file, that payload reads as an answer, and this one names an absolute path."""
    out = tmp_path / "statement-receipt.json"
    out.write_text("left by an earlier check")
    no_model = tmp_path / "not-a-profile"
    no_model.mkdir()
    (tmp_path / "statement.sql").write_text("SELECT 1 AS n")

    cs._sm(out, "receipt", str(no_model), "--sql-file", str(tmp_path / "statement.sql"))

    assert out.read_text() == ""
    err = capsys.readouterr().err
    assert err == "check_statement: `sm receipt` returned 3\n"
    assert str(no_model) not in err and "no_model" not in err


def test_a_verb_called_wrongly_leaves_an_empty_file_instead_of_ending_the_row(tmp_path, capsys):
    """argparse ends a wrong call with `SystemExit`, which `except Exception` lets past: the script
    would die mid-row, some files written and some not, on a fault in its own call. It also writes
    the call's own arguments to stderr, where a `--sql-file` path would be."""
    out = tmp_path / "statement-prepare.json"
    out.write_text("left by an earlier check")

    cs._sm(out, "prepare")  # no root, no statement: argparse refuses the call

    assert out.read_text() == ""
    err = capsys.readouterr().err
    assert err == "check_statement: `sm prepare` raised SystemExit\n"
    assert "usage" not in err


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


def test_an_interpreter_with_no_agami_core_at_all_is_told_the_same_thing(tmp_path):
    """The bare directory: the two script files and nothing else, so the resolver finds no layout at
    all and raises ImportError itself. That raise used to sit outside the `try`, and left a traceback
    and exit 1 where this script's docstring, `statement-check.md` and `SKILL.md` all promise 2."""
    env = {**os.environ, "PYTHONPATH": ""}
    if (
        subprocess.run(
            [sys.executable, "-S", "-c", "import agami_paths"], env=env, capture_output=True
        ).returncode
        == 0
    ):
        pytest.skip("cannot hide the installed agami-core from a subprocess here")
    bare = tmp_path / "scripts"
    bare.mkdir()
    for name in ("check_statement.py", "_agami_lib.py"):
        shutil.copy(SCRIPTS / name, bare / name)
    r = subprocess.run(
        [sys.executable, "-S", str(bare / "check_statement.py"), "--help"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert r.returncode == 2 and 'sm" install' in r.stderr
    # The type, and no more: the resolver's own sentence is not relayed, and neither is a traceback,
    # because an import error's text can name an absolute path.
    assert r.stderr.strip().endswith("(ImportError)")
    assert "Traceback" not in r.stderr and "sync-lib" not in r.stderr
    assert str(tmp_path) not in r.stderr
