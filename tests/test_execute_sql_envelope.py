"""tool_execute_sql assembles the response Envelope from the executor subprocess output — the
executor→tool refusal relay.

The `permission` refusal short-circuits BEFORE the subprocess (in the read-only pre-check), so this
is the only coverage of how a `{"refusal": ...}` stderr line (the model/scope/PII gates) — or a bare
operational failure — becomes a refused Envelope with the right `kind`. A regression in the stderr
parse would silently degrade every model-scope refusal to a generic error; these pin it.
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


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_db(monkeypatch, tmp_path):
    # The audit write in _finish is best-effort; point its jsonl fallback at a temp file and ensure
    # no DB is configured, so these stay hermetic.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.setattr(tools, "GUARDRAIL_AUDIT_LOG", tmp_path / "audit.jsonl")


def _run(monkeypatch, proc: _Proc) -> dict:
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: proc)
    return json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM t"}))


def test_stderr_refusal_line_becomes_a_refused_envelope(monkeypatch):
    stderr = json.dumps(
        {
            "refusal": {
                "kind": "table_out_of_scope",
                "reason": "table foo not in the model",
                "remediation": "add it to the model",
            }
        }
    )
    env = _run(monkeypatch, _Proc(1, stderr=stderr))
    assert env["status"] == "refused"
    assert env["refusal"] == {
        "kind": "table_out_of_scope",
        "reason": "table foo not in the model",
        "remediation": "add it to the model",
    }
    assert "data" not in env  # a refusal carries no data
    assert env["audit_id"]


def test_refusal_line_is_found_among_interleaved_notices(monkeypatch):
    # The executor prints `[agami] …` notices to stderr too; the parser must skip them and still
    # find the refusal line.
    stderr = "[agami] applied default_filters: deleted_at IS NULL\n" + json.dumps(
        {"refusal": {"kind": "sensitive_columns", "reason": "raw PII", "remediation": "aggregate"}}
    )
    env = _run(monkeypatch, _Proc(1, stderr=stderr))
    assert env["status"] == "refused" and env["refusal"]["kind"] == "sensitive_columns"


def test_resource_limit_refusal_discards_partial_stdout(monkeypatch):
    # The per-statement timeout emits a {"refusal": {"kind": "resource_limit"}} line from the
    # executor. Both transports (stdio + HTTP) funnel through tool_execute_sql, so this single relay
    # is the both-surfaces guarantee. Critically: a streaming engine may have flushed a partial CSV
    # (e.g. the header row) to stdout before the cancel — the relay must DISCARD it on the non-zero
    # exit, so a killed query is a refused Envelope with no data. Feed partial stdout to pin that.
    stderr = json.dumps(
        {
            "refusal": {
                "kind": "resource_limit",
                "reason": "the query exceeded the 30s statement timeout and was cancelled",
                "remediation": "Narrow the query, or raise AGAMI_SQL_TIMEOUT_S (currently 30s).",
            }
        }
    )
    env = _run(monkeypatch, _Proc(1, stdout="n\n1\n2\n", stderr=stderr))
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == "resource_limit"
    assert "data" not in env  # the partial CSV on stdout is discarded — no partial data leaks
    assert env["audit_id"]


def test_bare_operational_failure_is_classified_by_exit_code(monkeypatch):
    # Exit 5 = SQL execution error with no {"refusal"} line → classified as 'syntax' by exit code.
    env = _run(monkeypatch, _Proc(5, stderr='relation "x" does not exist'))
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == "syntax"  # _classify_exit(5)
    # Operational stderr surfaces as the reason (proper sanitization is handled separately).
    assert 'relation "x"' in env["refusal"]["reason"]
