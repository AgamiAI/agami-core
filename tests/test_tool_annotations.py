"""Read-only tools advertise the MCP `readOnlyHint` on both transports, and only they do; every other
tool states itself as a write, and every tool carries a title.

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
from tools import TOOLS, register, require_thread_id, tool_annotations, tool_title

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True}


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


def test_a_tool_without_the_flag_is_stated_as_a_write():
    """Opt-in, not inferred: the server cannot tell a write from a read except by the flag, so a tool
    without it is stated as one that may modify data, the caution a client would apply anyway. Claude's
    connector directory flags a tool that states neither hint."""
    assert tool_annotations(_consumer_tool()) == WRITE


@pytest.mark.parametrize("value", ["true", "false", 1, 1.0, None])
def test_only_the_literal_true_counts(value):
    """A config-sourced string or a truthy number must not promote a writing tool to unconfirmed."""
    assert tool_annotations({**_consumer_tool(), "read_only": value}) == WRITE


def test_the_flag_survives_the_thread_id_promotion():
    """`require_thread_id` rebuilds every entry it touches; the hint must not be lost on the way."""
    assert all(tool_annotations(meta) == READ_ONLY for meta in require_thread_id(TOOLS).values())


def test_register_can_opt_a_consumer_read_in_and_defaults_out():
    try:
        register("t_read", lambda a: "ok", "reads", {"type": "object"}, read_only=True)
        register("t_write", lambda a: "ok", "writes", {"type": "object"})
        assert tool_annotations(TOOLS["t_read"]) == READ_ONLY
        assert tool_annotations(TOOLS["t_write"]) == WRITE
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
    assert wire["annotations"] == {"title": "Run read-only SQL", **READ_ONLY}
    note = listed["log_note"].model_dump(by_alias=True, exclude_none=True)
    assert note["annotations"] == {"title": "Log note", **WRITE}


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
    assert all(
        t["annotations"] == {"title": tool_title(t["name"], TOOLS[t["name"]]), **READ_ONLY}
        for t in tools
    )


# --- Titles ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_core_tool_lists_a_title(name):
    """Claude's connector directory rejects a tool without a title. The core tools declare their
    own, so the listing never falls back to the name."""
    assert TOOLS[name].get("title")
    assert tool_annotations(TOOLS[name], name)["title"] == TOOLS[name]["title"]


def test_title_is_the_name_as_a_sentence_unless_declared():
    assert tool_title("list_datasources", {}) == "List datasources"
    assert (
        tool_title("list_datasources", {"title": "  List the datasources "})
        == "List the datasources"
    )
    assert tool_title("list_datasources", {"title": "   "}) == "List datasources"


def test_without_a_name_the_hints_carry_no_title():
    """Callers that only want the hints (and consumers' own tests) keep the old shape."""
    assert "title" not in tool_annotations(TOOLS["execute_sql"])
