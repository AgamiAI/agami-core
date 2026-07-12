"""The guardrail audit trail — every execute_sql result (ok or refused) writes one row keyed by
the response Envelope's `audit_id`, at the shared chokepoint so BOTH surfaces are covered.

Also checks the jsonl fallback (no datastore) and that a logging failure never breaks the tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import tools  # noqa: E402
from store import Store  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "audit.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def _audit_rows(url):
    s = Store.connect(url)
    rows = s.query("SELECT * FROM guardrail_audit ORDER BY ts")
    s.close()
    return rows


class _FakeProc:
    """A successful executor run: RFC-4180 CSV on stdout (header + one row), clean stderr."""

    returncode = 0
    stdout = "n\n5\n"
    stderr = ""


def test_migration_creates_the_table(db):
    assert _audit_rows(db) == []  # table exists, empty


def test_refused_query_writes_a_refused_audit_row(db):
    # A write is rejected by the read-only gate before any subprocess — the refusal still audits.
    resp = json.loads(
        tools.tool_execute_sql(
            {"sql": "DELETE FROM t", "datasource": "sales", "correlation_id": "turn-1"}
        )
    )
    assert resp["status"] == "refused" and resp["refusal"]["kind"] == "permission"

    (row,) = _audit_rows(db)
    assert row["audit_id"] == resp["audit_id"]  # the row the Envelope points at
    assert row["status"] == "refused"
    assert row["refusal_kind"] == "permission"
    assert row["sql"] == "DELETE FROM t"
    assert row["datasource"] == "sales"
    assert row["correlation_id"] == "turn-1"
    assert row["row_count"] is None


def test_ok_query_writes_an_ok_audit_row(db, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tools, "_resolve_units", lambda *a: {})
    monkeypatch.setattr(tools, "_resolve_receipt", lambda *a: None)

    resp = json.loads(tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "sales"}))
    assert resp["status"] == "ok" and resp["data"]["row_count"] == 1

    (row,) = _audit_rows(db)
    assert row["audit_id"] == resp["audit_id"]
    assert row["status"] == "ok"
    assert row["refusal_kind"] is None
    assert row["sql"] == "SELECT n FROM t"
    assert row["row_count"] == 1


def test_jsonl_fallback_when_no_datastore(tmp_path, monkeypatch):
    # No AGAMI_DB_URL → the audit row lands in the local jsonl instead of the DB.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    log = tmp_path / "guardrail_audit.jsonl"
    monkeypatch.setattr(tools, "GUARDRAIL_AUDIT_LOG", log)

    resp = json.loads(tools.tool_execute_sql({"sql": "DROP TABLE t", "datasource": "sales"}))
    assert resp["status"] == "refused"

    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["audit_id"] == resp["audit_id"]
    assert rec["status"] == "refused" and rec["refusal_kind"] == "permission"


def test_audit_write_is_best_effort_and_never_raises(tmp_path, monkeypatch):
    # A failing write (here: the jsonl append blows up) is swallowed inside the recorder, so the
    # tool that calls it through _finish can never be broken by a logging failure.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(tools, "_append_jsonl", _boom)
    tools._record_guardrail_audit({"audit_id": "x", "ts": "t", "status": "ok"})  # must not raise
