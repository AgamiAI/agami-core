"""Read-only tools advertise the MCP `readOnlyHint`, and only they do.

A client's confirmation policy keys off tool annotations: Gemini Enterprise assumes an un-annotated
tool can mutate data and asks the person before each call, once per distinct argument set. Every core
tool reads, so each was costing a prompt per query. The hint is opt-in per registry entry
(`read_only: True`) so a consumer tool that writes keeps the client's default caution.

These tests hold the wire shape and the opt-in. Whether a given client honours the hint is its
policy, observed on that client rather than asserted here.
"""

from __future__ import annotations

import asyncio

import pytest
from tools import TOOLS, require_thread_id

types = pytest.importorskip("mcp.types")


def _listed(registry: dict | None = None) -> dict:
    from mcp_http import build_server

    server = build_server(registry)
    result = asyncio.run(server.request_handlers[types.ListToolsRequest](types.ListToolsRequest()))
    return {t.name: t for t in result.root.tools}


def _consumer_tool() -> dict:
    return {
        "handler": lambda args: "ok",
        "description": "Records a note.",
        "inputSchema": {"type": "object", "properties": {}},
    }


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_core_tool_is_listed_read_only(name):
    tool = _listed()[name]
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    # The spec defaults this one to True; left implicit it would contradict the read-only claim.
    assert tool.annotations.destructiveHint is False


def test_the_hint_is_on_the_wire_in_the_spec_field_names():
    tool = _listed()["execute_sql"]
    wire = tool.model_dump(by_alias=True, exclude_none=True)
    assert wire["annotations"] == {"readOnlyHint": True, "destructiveHint": False}


def test_a_tool_without_the_flag_advertises_no_annotations():
    """Opt-in, not inferred: a consumer tool that writes must keep the client's default caution, and
    the server has no way to tell a write from a read except the flag."""
    listed = _listed({**TOOLS, "log_note": _consumer_tool()})
    assert listed["log_note"].annotations is None
    assert listed["execute_sql"].annotations.readOnlyHint is True


def test_a_consumer_can_opt_a_read_in():
    listed = _listed({"list_notes": {**_consumer_tool(), "read_only": True}})
    assert listed["list_notes"].annotations.readOnlyHint is True


def test_the_flag_survives_the_thread_id_promotion():
    """`require_thread_id` rebuilds every entry it touches; the hint must not be lost on the way."""
    promoted = require_thread_id(TOOLS)
    assert all(meta.get("read_only") is True for meta in promoted.values())


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
