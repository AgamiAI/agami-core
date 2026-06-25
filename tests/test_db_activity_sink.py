"""The DB write path — execute_sql/log_feedback log to the DB when AGAMI_DB_URL is set (Slice D).

`DbActivitySink` conforms to the `ports.ActivitySink` Protocol by shape (no inheritance) and is
backend-agnostic (one class — SQLite here, Postgres in prod). The local jsonl path is unchanged
when AGAMI_DB_URL is unset.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")  # the contract records are pydantic

import tools  # noqa: E402
from model_store import DbActivitySink  # noqa: E402
from ports import ActivitySink  # noqa: E402
from store import Store  # noqa: E402


def _fresh_db(tmp_path) -> str:
    url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def test_db_sink_conforms_to_activity_sink_port():
    # structural conformance — has the methods; verified via the runtime_checkable Protocol
    assert isinstance(DbActivitySink(Store.connect("sqlite://")), ActivitySink)


def test_record_query_writes_one_row(tmp_path, monkeypatch):
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    tools._record_query(
        {
            "ts": "2026-06-25T00:00:00Z",
            "profile": "main",
            "question": "how many orders?",
            "sql": "SELECT count(*) FROM orders",
            "row_count": 1,
            "source": "mcp_server",
        }
    )
    # a fresh connection (a "second instance") reads it
    s = Store.connect(url)
    rows = s.query("SELECT datasource, question, sql, row_count, source FROM query_executions")
    s.close()
    assert rows == [
        {
            "datasource": "main",
            "question": "how many orders?",
            "sql": "SELECT count(*) FROM orders",
            "row_count": 1,
            "source": "mcp_server",
        }
    ]


def test_log_feedback_writes_to_db(tmp_path, monkeypatch):
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    out = json.loads(
        tools.tool_log_feedback({"raw_query": "how many?", "rating": "good", "datasource": "main"})
    )
    assert out["ok"] is True and out["logged_to"] == "database"
    s = Store.connect(url)
    rows = s.query("SELECT datasource, question, rating FROM feedback")
    s.close()
    assert rows == [{"datasource": "main", "question": "how many?", "rating": "Good"}]


def test_record_query_is_best_effort_on_db_error(tmp_path, monkeypatch):
    # AGAMI_DB_URL points at a DB with NO migrations applied, so the INSERT into query_executions
    # fails. _record_query must swallow it — a logging failure can't break a successful query.
    url = "sqlite://" + str(tmp_path / "empty.db")
    Store.connect(url).close()  # create the file; no tables
    monkeypatch.setenv("AGAMI_DB_URL", url)
    tools._record_query(
        {  # must NOT raise
            "ts": "2026-06-25T00:00:00Z",
            "profile": "main",
            "question": "q",
            "sql": "SELECT 1",
            "row_count": 1,
            "source": "mcp_server",
        }
    )


def test_local_jsonl_path_unchanged_when_db_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    out = json.loads(tools.tool_log_feedback({"raw_query": "q", "rating": "bad"}))
    assert out["ok"] is True and out["logged_to"].endswith("feedback.jsonl")  # still the file
