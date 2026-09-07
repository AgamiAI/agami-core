"""the statement records which identity actually ran it.

`query_executions` said what ran, when, for whom and with what verdict, and never said who the
warehouse authenticated. For a consumer that runs each statement as the person who
asked, it is a different identity per row.

**The claim under test is that the value follows the CONNECTION, not the intent.** An implementation
that recorded the signed-in principal would pass a naive "an identity is recorded" check and be
wrong on the only day anybody reads the column — the day a misconfiguration ran a statement as
somebody nobody expected. So the tests here drive the executor's own report and assert the row
carries that, including on the paths where the statement did not succeed.

This ships the seam and the recorder. The criteria about a real connection's identity belong to
whichever executor supplies one — they cannot be proved here without a stub asserting our own
test double's behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pydantic")  # the contract records are pydantic

import tools  # noqa: E402
from contracts import QueryExecutionRecord  # noqa: E402
from execute_sql import (  # noqa: E402
    ExecResult,
    _last_executing_identity,
    report_executing_identity,
)
from model_store import DbActivitySink  # noqa: E402
from store import Store  # noqa: E402

_SRC = Path(__file__).resolve().parents[1] / "packages" / "agami-core" / "src"


def _fresh_db(tmp_path) -> str:
    url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def _record(url: str, **overrides) -> None:
    rec = {
        "id": "7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f",
        "ts": "2026-09-07T00:00:00Z",
        "profile": "main",
        "question": "how many orders?",
        "sql": "SELECT count(*) FROM orders",
        "row_count": 1,
        "source": "mcp_server",
        "status": "ok",
    }
    rec.update(overrides)
    tools._record_query(rec)


def _identities(url: str) -> list:
    s = Store.connect(url)
    rows = s.query("SELECT executing_identity FROM query_executions ORDER BY id")
    s.close()
    return [r["executing_identity"] for r in rows]


# --- the column, and what a null means -------------------------------------------------------------


def test_the_builtin_executor_reports_no_identity(tmp_path, monkeypatch):
    """The deliberate silence. Teaching the built-in executor to report would cost a round trip on
    every query in every self-hosted deployment to record the same shared account each time — so its
    rows stay null, and null is a claim: no executor reported one."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url)

    assert _identities(url) == [None]


def test_an_identity_reported_by_an_executor_reaches_the_row(tmp_path, monkeypatch):
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url, executing_identity="IAM:you@example.com")

    assert _identities(url) == ["IAM:you@example.com"]


def test_the_recorded_identity_follows_the_connection_not_the_intent(tmp_path, monkeypatch):
    """**The criterion that stops a plausible wrong implementation.** One that read the signed-in
    principal would satisfy "an identity is recorded" and be wrong exactly when it matters. Here the
    connection reports somebody the caller never named, and the row has to say what the connection
    said."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    # The question is asked by one person; the warehouse authenticated a different account.
    _record(url, question="asked by the analyst", executing_identity="warehouse_service_account")

    assert _identities(url) == ["warehouse_service_account"], (
        "the row recorded the intent rather than what the connection reported"
    )


# --- the outcome the column is most worth having for -----------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param({"status": "failed"}, id="the statement failed"),
        pytest.param(
            {"status": "refused", "reason": "resource_limit", "rule": "AGAMI-RESOURCE"},
            id="the statement was refused",
        ),
    ],
)
def test_a_statement_that_did_not_succeed_still_records_the_identity(tmp_path, monkeypatch, outcome):
    """A connection was opened and a statement ran as somebody; that it then failed is not a reason
    to forget who. "It ran as somebody and then it broke" is the question an operator is chasing, and
    a column that goes null on failure answers it for the wrong half of the traffic."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url, executing_identity="IAM:you@example.com", **outcome)

    assert _identities(url) == ["IAM:you@example.com"]


def test_an_executor_that_reports_nothing_changes_nothing(tmp_path, monkeypatch):
    """A driver that cannot answer costs nothing: the column is null, the row is otherwise complete,
    and nothing about the statement changed. Asserted over the whole row rather than the one column,
    because "does not change what it returns" is the half a column-only check would miss."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url, executing_identity=None)

    s = Store.connect(url)
    (row,) = s.query(
        "SELECT sql, row_count, status, executing_identity FROM query_executions", ()
    )
    s.close()
    assert row["executing_identity"] is None
    assert (row["sql"], row["row_count"], row["status"]) == (
        "SELECT count(*) FROM orders",
        1,
        "ok",
    )


# --- the seam, and the ContextVar behind it --------------------------------------------------------


def test_the_result_carries_an_identity_for_an_injected_executor():
    """The declared seam. `ExecResult` is what an injected `ports.Executor` returns, and the field is
    optional so every existing construction — the built-in engines included — keeps working."""
    assert ExecResult(columns=[], rows=[]).executing_identity is None
    assert ExecResult(columns=[], rows=[], executing_identity="alice").executing_identity == "alice"


def test_an_executor_reports_before_the_statement_can_fail():
    """Why the setter exists beside the result field. An executor that fails after connecting has no
    `ExecResult` to put the identity on, so it reports at connect time instead — which is the only
    shape that can satisfy the failure criterion above from a real executor."""
    report_executing_identity(None)
    report_executing_identity("IAM:you@example.com")

    assert _last_executing_identity.get() == "IAM:you@example.com"

    report_executing_identity(None)
    assert _last_executing_identity.get() is None, "reporting nothing must clear, not keep"


def test_the_column_is_written_by_the_insert_and_never_updated():
    """The row is append-only, and a column filled by a second write is what that stance exists to
    prevent. Asserted against the source rather than against behaviour, the way the key ledger's own
    guard is: the guarantee is that no such code path exists."""
    offenders = sorted(
        str(path.relative_to(_SRC))
        for pattern in ("*.py", "*.sql")
        for path in _SRC.rglob(pattern)
        if "UPDATE query_executions" in path.read_text(encoding="utf-8")
    )

    assert offenders == [], f"a second writer of the execution row appeared: {offenders}"


def test_the_record_defaults_the_field_so_an_older_caller_still_writes(tmp_path):
    """The `getattr` tolerance the recorder already gives `error_detail` and `model_version`: a
    caller still on the older record shape writes a NULL rather than raising."""
    url = _fresh_db(tmp_path)
    s = Store.connect(url)
    DbActivitySink(s).record_query_execution(
        QueryExecutionRecord(
            id="a" * 32,
            ts="2026-09-07T00:00:00Z",
            profile="main",
            sql="SELECT 1",
            row_count=1,
            source="mcp_server",
        )
    )
    rows = s.query("SELECT executing_identity FROM query_executions", ())
    s.close()

    assert [r["executing_identity"] for r in rows] == [None]
