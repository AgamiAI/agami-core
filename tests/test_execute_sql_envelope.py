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


def test_bare_operational_failure_is_classified_by_exit_code(monkeypatch):
    # Exit 5 = SQL execution error with no {"refusal"} line → classified as 'syntax' by exit code.
    env = _run(monkeypatch, _Proc(5, stderr='relation "x" does not exist'))
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == "syntax"  # _classify_exit(5)
    # Operational stderr surfaces as the reason (proper sanitization is handled separately).
    assert 'relation "x"' in env["refusal"]["reason"]
