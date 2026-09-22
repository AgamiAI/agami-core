"""Read-only tools advertise the MCP `readOnlyHint` on both transports, and only they do.

Why the hint exists, and the bar a tool must clear to carry it, is stated once at
`tools.tool_annotations`. These tests hold the wire shape, the opt-in, and that the two MCP servers
state the same hints. Whether a given client honours them is its policy, observed on that client
rather than asserted here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
from tools import TOOLS, register, require_thread_id, tool_annotations

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}


def _consumer_tool() -> dict:
    return {
        "handler": lambda args: "ok",
        "description": "Records a note.",
        "inputSchema": {"type": "object", "properties": {}},
    }


# --- The registry's claim -----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_core_tool_claims_read_only(name):
    assert tool_annotations(TOOLS[name]) == READ_ONLY


def test_a_tool_without_the_flag_advertises_nothing():
    """Opt-in, not inferred: the server cannot tell a write from a read except by the flag."""
    assert tool_annotations(_consumer_tool()) is None


@pytest.mark.parametrize("value", ["true", "false", 1, 1.0, None])
def test_only_the_literal_true_counts(value):
    """A config-sourced string or a truthy number must not promote a writing tool to unconfirmed."""
    assert tool_annotations({**_consumer_tool(), "read_only": value}) is None


def test_the_flag_survives_the_thread_id_promotion():
    """`require_thread_id` rebuilds every entry it touches; the hint must not be lost on the way."""
    assert all(tool_annotations(meta) == READ_ONLY for meta in require_thread_id(TOOLS).values())


def test_register_can_opt_a_consumer_read_in_and_defaults_out():
    try:
        register("t_read", lambda a: "ok", "reads", {"type": "object"}, read_only=True)
        register("t_write", lambda a: "ok", "writes", {"type": "object"})
        assert tool_annotations(TOOLS["t_read"]) == READ_ONLY
        assert tool_annotations(TOOLS["t_write"]) is None
    finally:
        TOOLS.pop("t_read", None)
        TOOLS.pop("t_write", None)


def test_register_refuses_a_non_bool_flag():
    with pytest.raises(ValueError, match="read_only must be a bool"):
        register("t_bad", lambda a: "ok", "reads", {"type": "object"}, read_only="true")
    assert "t_bad" not in TOOLS


def test_execute_sql_read_only_claim_is_backed_by_the_guard():
    """The hint is a promise about behaviour, not a label. `execute_sql` may carry it only because
    the guard refuses anything that is not a single SELECT before the statement runs."""
    from sql_guard import check_read_only

    assert check_read_only("SELECT 1") is None
    for statement in (
        "DELETE FROM t",
        "UPDATE t SET a = 1",
        "DROP TABLE t",
        "SELECT 1; DROP TABLE t",
    ):
        assert check_read_only(statement) is not None, statement


# --- HTTP transport ----------------------------------------------------------------------------


def _listed_over_http(registry: dict | None = None) -> dict:
    types = pytest.importorskip("mcp.types")
    from mcp_http import build_server

    server = build_server(registry)
    result = asyncio.run(server.request_handlers[types.ListToolsRequest](types.ListToolsRequest()))
    return {t.name: t for t in result.root.tools}


def test_http_lists_the_hint_in_the_spec_field_names():
    listed = _listed_over_http({**TOOLS, "log_note": _consumer_tool()})
    wire = listed["execute_sql"].model_dump(by_alias=True, exclude_none=True)
    assert wire["annotations"] == READ_ONLY
    assert listed["log_note"].annotations is None


def test_create_app_refuses_a_non_bool_flag(monkeypatch):
    pytest.importorskip("mcp")
    import mcp_http

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agami.example.com")
    with pytest.raises(ValueError, match="read_only must be a bool"):
        mcp_http.create_app(extra_tools={"w": {**_consumer_tool(), "read_only": "false"}})


# --- stdio transport ---------------------------------------------------------------------------


def test_stdio_lists_the_same_hints():
    """The harness `/agami-serve` wires into Claude Desktop builds its own tools/list; a hint the
    HTTP server advertises and stdio omits would leave Desktop prompting per read."""
    stdin = "".join(
        json.dumps(m) + "\n"
        for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    )
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ},
    )
    by_id = {m.get("id"): m for m in map(json.loads, filter(str.strip, proc.stdout.splitlines()))}
    tools = by_id[2]["result"]["tools"]
    assert {t["name"] for t in tools} == set(TOOLS)
    assert all(t["annotations"] == READ_ONLY for t in tools)
