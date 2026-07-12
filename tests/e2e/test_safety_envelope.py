"""End-to-end: execute_sql returns ONE response Envelope on BOTH surfaces (stdio + HTTP).

A safety violation → status=refused + refusal{kind} + no data; a clean query → status=ok + data +
audit_id. The full adversarial safety corpus + the read-only-DB-role test are out of scope here;
this locks the cross-surface Envelope shape the shared contract promises — the same shape whether a
client connects over stdio or HTTP.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

PKG_SRC = Path(__file__).resolve().parents[2] / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import tools  # noqa: E402

BASE = "https://demo.example.com"


class _FakeOkProc:
    """A successful executor run: RFC-4180 CSV on stdout (header + one row), clean stderr."""

    returncode = 0
    stdout = "n\n5\n"
    stderr = ""


# --- transport drivers: each returns the execute_sql tool's Envelope (parsed) ----------------


def _stdio_execute_sql(sql: str) -> dict:
    """Drive execute_sql over the real stdio server (a subprocess), return the tool's Envelope."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "execute_sql", "arguments": {"sql": sql}},
        },
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in msgs)
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ},
    )
    by_id = {m.get("id"): m for m in (json.loads(x) for x in proc.stdout.splitlines() if x.strip())}
    return json.loads(by_id[2]["result"]["content"][0]["text"])


def _http_execute_sql(sql: str) -> dict:
    """Drive execute_sql over the real HTTP transport (in-process TestClient), return the Envelope."""
    import mcp_http
    from starlette.testclient import TestClient

    headers = {
        "Authorization": "Bearer present",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.build_app()) as c:
        init = c.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        sid = init.headers.get("mcp-session-id")
        h2 = {**headers, **({"mcp-session-id": sid} if sid else {})}
        c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = c.post(
            "/mcp",
            headers=h2,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "execute_sql", "arguments": {"sql": sql}},
            },
        )
    rpc = json.loads(re.search(r"\{.*\}", r.text, re.DOTALL).group(0))
    return json.loads(rpc["result"]["content"][0]["text"])


@pytest.fixture
def presence_auth(monkeypatch):
    """HTTP bearer-presence mode: PUBLIC_BASE_URL set, no signing secret → 'Bearer present' works."""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("AGAMI_SIGNING_SECRET", raising=False)


# --- the cross-surface contract ---------------------------------------------------------------


def _assert_refused(env: dict) -> None:
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == "permission"
    assert env["refusal"]["reason"]  # a human reason is present
    assert "data" not in env  # a refusal carries no data
    assert env["audit_id"]  # references the recorded trail


def test_write_is_refused_with_one_envelope_over_stdio():
    _assert_refused(_stdio_execute_sql("DELETE FROM users"))


def test_write_is_refused_with_one_envelope_over_http(presence_auth):
    _assert_refused(_http_execute_sql("DELETE FROM users"))


def test_clean_query_returns_ok_envelope_over_http(presence_auth, monkeypatch):
    # A governed query needs no live DB here — fake the guarded executor so the test stays hermetic.
    # The HTTP transport runs execution IN-PROCESS by default (ACE-028), so fake execute_guarded (not
    # the subprocess); the point is the Envelope shape the transport hands back, not the driver.
    import execute_sql

    monkeypatch.setattr(
        execute_sql,
        "execute_guarded",
        lambda *a, **k: execute_sql.ExecResult(columns=["n"], rows=[(1,)], truncated=False),
    )
    monkeypatch.setattr(tools, "_resolve_units", lambda *a: {})
    monkeypatch.setattr(tools, "_resolve_receipt", lambda *a: None)

    env = _http_execute_sql("SELECT n FROM t")
    assert env["status"] == "ok"
    assert env["data"]["row_count"] == 1  # the ExecuteSqlResult payload rides under `data`
    assert env["data"]["columns"] == ["n"]
    assert env["audit_id"]
    assert "refusal" not in env
