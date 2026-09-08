"""The warehouse's own id for a statement, recorded where an engine mints one.

Some engines give every statement an internal id, and it is the key to their own logs — the plan,
the queue time, the bytes scanned. Recording it lets an operator follow a row into a system this
database does not own.

**It is a pointer, and this side treats it as opaque.** Nothing joins on it, parses it or fails
without it, and the tests here hold that line deliberately: they assert the value written is the
value reported, and never that it looks like anything in particular. A test that asserted a shape
would be this codebase inventing a claim about somebody else's identifier.

Which engines can answer is not decided here, and there is a test for that too — nothing in the
recorder or the schema knows which warehouses mint an id, so adding one is a matter for whoever owns
the connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pydantic")  # the contract records are pydantic

import tools  # noqa: E402
from contracts import QueryExecutionRecord  # noqa: E402
from execute_sql import (  # noqa: E402
    ExecResult,
    _last_warehouse_query_id,
    report_warehouse_query_id,
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
        "id": "3f9a1c047b2e4d819c55e0a2b6d7f118",
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


def _ids(url: str) -> list:
    s = Store.connect(url)
    rows = s.query("SELECT warehouse_query_id FROM query_executions ORDER BY id")
    s.close()
    return [r["warehouse_query_id"] for r in rows]


# --- the column, and what a null means -------------------------------------------------------------


def test_an_engine_that_mints_no_id_records_null(tmp_path, monkeypatch):
    """Most engines have no such id. Null is a claim — nothing was reported — rather than a gap, and
    it is the same null as an executor that chose not to ask."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url)

    assert _ids(url) == [None]


def test_an_id_an_executor_reports_reaches_the_row(tmp_path, monkeypatch):
    """Written down and handed over. Asserted as equality with what was reported and nothing more:
    the value is somebody else's identifier, and a test that checked its shape would be this
    codebase inventing a claim about it."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url, warehouse_query_id="a-warehouse-statement-id")

    assert _ids(url) == ["a-warehouse-statement-id"]


def test_an_opaque_id_is_stored_verbatim(tmp_path, monkeypatch):
    """Engines disagree about what an id even is — an integer on one, a uuid on another, a prefixed
    string on a third. Whatever arrives is what is written, because normalising it here would be a
    guess that makes the pointer stop resolving."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    for i, given in enumerate(("1234567890", "01af-3c22-9d10", "statement/2026/09/07#4")):
        _record(url, id=f"{i:032d}", warehouse_query_id=given)

    assert _ids(url) == ["1234567890", "01af-3c22-9d10", "statement/2026/09/07#4"]


# --- the outcomes an operator most wants to look up ------------------------------------------------


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
def test_a_statement_that_did_not_succeed_still_records_its_id(tmp_path, monkeypatch, outcome):
    """The row an operator most wants to follow into the warehouse's logs is the one that went
    wrong — that is where the reason lives. A column that goes null on failure is empty for exactly
    the traffic it was added for."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url, warehouse_query_id="a-warehouse-statement-id", **outcome)

    assert _ids(url) == ["a-warehouse-statement-id"]


def test_an_executor_that_reports_no_id_changes_nothing(tmp_path, monkeypatch):
    """A failure to obtain the id leaves the column null and does not change what the statement
    returned. Asserted over the whole row, because "changes nothing else" is the half a column-only
    check would miss."""
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    _record(url, warehouse_query_id=None)

    s = Store.connect(url)
    (row,) = s.query("SELECT sql, row_count, status, warehouse_query_id FROM query_executions", ())
    s.close()
    assert row["warehouse_query_id"] is None
    assert (row["sql"], row["row_count"], row["status"]) == (
        "SELECT count(*) FROM orders",
        1,
        "ok",
    )


# --- the seam, and what this side deliberately does not know ---------------------------------------


def test_the_result_carries_an_id_for_an_injected_executor():
    """The second field on the seam, in the shape of the first. Optional, so every existing
    construction keeps working."""
    assert ExecResult(columns=[], rows=[]).warehouse_query_id is None
    assert ExecResult(columns=[], rows=[], warehouse_query_id="x").warehouse_query_id == "x"


def test_an_executor_reports_the_id_itself():
    """Reported through a setter as well as through the result, so an executor can name the
    statement at the moment the answer is stable rather than only where a result exists."""
    report_warehouse_query_id(None)
    report_warehouse_query_id("a-warehouse-statement-id")

    assert _last_warehouse_query_id.get() == "a-warehouse-statement-id"

    report_warehouse_query_id(None)
    assert _last_warehouse_query_id.get() is None, "reporting nothing must clear, not keep"


def test_nothing_here_decides_which_engines_have_an_id():
    """**The property that keeps adding an engine free.** No database type, driver name or engine
    list appears in the recorder or the seam — an executor that can name its statement reports one,
    and one that cannot never calls. Asserted against the source, because the guarantee is that no
    such branch exists rather than that today's branches are correct."""
    for module in ("model_store.py", "contracts.py"):
        text = (_SRC / module).read_text(encoding="utf-8")
        assert "warehouse_query_id" in text, f"{module} should carry the field"
        for engine in ("redshift", "snowflake", "bigquery", "postgres", "mysql"):
            assert engine not in text.lower().split("warehouse_query_id")[1][:400], (
                f"{module} names {engine} near the field; which engines can answer is not decided here"
            )


def test_the_column_is_written_by_the_insert_and_never_updated():
    """Append-only, like every other column on this row. Asserted against the source: the guarantee
    is that no code path exists to fill it later."""
    offenders = sorted(
        str(path.relative_to(_SRC))
        for pattern in ("*.py", "*.sql")
        for path in _SRC.rglob(pattern)
        if "UPDATE query_executions" in path.read_text(encoding="utf-8")
    )

    assert offenders == [], f"a second writer of the execution row appeared: {offenders}"


def test_the_record_defaults_the_field_so_an_older_caller_still_writes(tmp_path):
    """The same tolerance every optional column on this record has: a caller on the older shape
    writes a NULL rather than raising."""
    url = _fresh_db(tmp_path)
    s = Store.connect(url)
    DbActivitySink(s).record_query_execution(
        QueryExecutionRecord(
            id="b" * 32,
            ts="2026-09-07T00:00:00Z",
            profile="main",
            sql="SELECT 1",
            row_count=1,
            source="mcp_server",
        )
    )
    rows = s.query("SELECT warehouse_query_id FROM query_executions", ())
    s.close()

    assert [r["warehouse_query_id"] for r in rows] == [None]
