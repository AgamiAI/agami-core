"""
Tests for plugins/agami/scripts/mcp_server.py — the local stdio MCP server.

Two contracts anchor this file:

  1. **Read-only guarantee.** The server exposes SQL execution to an LLM, so
     `check_read_only` MUST reject anything that isn't a single SELECT /
     WITH...SELECT — including the Postgres `WITH ... DELETE` data-modifying-CTE
     edge and comment-hidden mutations.

  2. **Privacy invariant.** The server is part of the local-first story: it must
     contain NO network primitives. If you add a feature that needs the network,
     it does not belong in this file (that is the hosted product, by design).

The JSON-RPC handshake is exercised end-to-end via subprocess with no DB
required (initialize / tools/list / list_datasources).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
SERVER = SCRIPTS / "mcp_server.py"
sys.path.insert(0, str(SCRIPTS))

from mcp_server import (  # noqa: E402
    _classify_exit,
    check_read_only,
    resolve_artifacts_dir,
    resolve_profile,
)


# --- Read-only guard --------------------------------------------------------

def test_plain_select_is_allowed():
    assert check_read_only("SELECT 1") is None
    assert check_read_only("select count(*) from orders") is None


def test_with_select_is_allowed():
    assert check_read_only("WITH t AS (SELECT 1 AS x) SELECT x FROM t") is None


def test_trailing_semicolon_tolerated():
    assert check_read_only("SELECT 1;") is None


def test_leading_paren_select_allowed():
    assert check_read_only("(SELECT 1) UNION (SELECT 2)") is None


def test_ddl_rejected():
    for sql in ("DROP TABLE users", "ALTER TABLE t ADD c int", "TRUNCATE t", "CREATE TABLE t (id int)"):
        assert check_read_only(sql) is not None, sql


def test_dml_rejected():
    for sql in ("INSERT INTO t VALUES (1)", "UPDATE t SET x=1", "DELETE FROM t"):
        assert check_read_only(sql) is not None, sql


def test_multi_statement_rejected():
    assert check_read_only("SELECT 1; SELECT 2") is not None
    assert check_read_only("SELECT 1; DROP TABLE t") is not None


def test_data_modifying_cte_rejected():
    # Postgres allows `WITH x AS (...) DELETE ...` — this MUST be blocked even
    # though it starts with WITH.
    assert check_read_only("WITH x AS (SELECT id FROM t) DELETE FROM t USING x") is not None


def test_comment_hidden_mutation_rejected():
    # A mutation smuggled behind a comment-stripped multi-statement must fail.
    assert check_read_only("SELECT 1 -- harmless\n; DELETE FROM t") is not None


def test_identifier_with_keyword_substring_allowed():
    # Column names like created_date / updated_at / is_deleted contain DDL/DML
    # substrings but are NOT whole-word matches → must still be allowed.
    assert check_read_only("SELECT created_date, updated_at, is_deleted FROM t") is None


def test_empty_rejected():
    assert check_read_only("   ") is not None
    assert check_read_only("-- just a comment") is not None


# --- Resolution order -------------------------------------------------------

def test_resolve_profile_explicit_wins(monkeypatch):
    monkeypatch.setenv("AGAMI_PROFILE", "fromenv")
    assert resolve_profile("explicit") == "explicit"


def test_resolve_profile_env_over_config(monkeypatch):
    monkeypatch.setenv("AGAMI_PROFILE", "fromenv")
    assert resolve_profile() == "fromenv"


def test_resolve_artifacts_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    assert resolve_artifacts_dir() == tmp_path


# --- Exit-code classification ----------------------------------------------

def test_classify_exit_mapping():
    assert _classify_exit(3) == "driver_missing"
    assert _classify_exit(4) == "auth"
    assert _classify_exit(5) == "syntax"
    assert _classify_exit(2) == "dsn"
    assert _classify_exit(99) == "other"


# --- Privacy invariant ------------------------------------------------------

def test_server_makes_no_network_calls():
    """The local server must not import or use any network primitive."""
    src = SERVER.read_text()
    forbidden = ("import socket", "import http", "import urllib", "urllib.request",
                 "requests.", "httpx", "import ftplib", "smtplib", "websocket")
    hits = [tok for tok in forbidden if tok in src]
    assert not hits, f"mcp_server.py must stay network-free; found: {hits}"


# --- Protocol handshake (no DB required) ------------------------------------

def _rpc_exchange(messages: list[dict]) -> list[dict]:
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ},
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_initialize_and_tools_list():
    out = _rpc_exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    by_id = {m.get("id"): m for m in out}

    init = by_id[1]["result"]
    assert init["protocolVersion"] == "2025-06-18"  # echoes client's version
    assert init["serverInfo"]["name"] == "agami"

    tools = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert tools == {
        # core Ask-Agami-parity surface
        "list_datasources", "get_datasource_schema",
        "get_prompt_examples", "execute_sql", "log_feedback",
        # semantic-model traversal tools
        "list_subject_areas", "get_subject_area_bundle", "get_table_context",
        "identify_entity", "pre_flight_check",
    }


def test_execute_sql_rejects_mutation_over_protocol():
    out = _rpc_exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "execute_sql", "arguments": {"sql": "DELETE FROM users"}}},
    ])
    by_id = {m.get("id"): m for m in out}
    payload = json.loads(by_id[2]["result"]["content"][0]["text"])
    assert payload["error"]["kind"] == "permission"


def test_unknown_method_returns_error():
    out = _rpc_exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 9, "method": "does/not/exist"},
    ])
    by_id = {m.get("id"): m for m in out}
    assert by_id[9]["error"]["code"] == -32601
