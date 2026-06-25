"""The shared TOOLS registry — one impl, both transports.

Both the stdio entrypoint (mcp_harness) and the HTTP entrypoint (mcp_http) import the SAME
`tools.TOOLS` object, so the surface can't drift. These assert the surface is exactly the 5
product tools, the dropped tools are gone, and the registry the two transports share is identical.
"""

from __future__ import annotations

import mcp_harness
import tools

PRODUCT_TOOLS = {
    "list_datasources",
    "get_datasource_schema",
    "get_prompt_examples",
    "execute_sql",
    "log_feedback",
}
# Subsumed by the smart get_datasource_schema / folded / internal / skill-operation — deliberately
# NOT on the MCP surface of either transport.
DROPPED_FROM_MCP = {
    "list_subject_areas",
    "get_subject_area_bundle",
    "get_table_context",
    "identify_entity",
    "pre_flight_check",
    "save_correction",
}


def test_surface_is_exactly_the_five_product_tools():
    assert set(tools.TOOLS) == PRODUCT_TOOLS


def test_dropped_tools_are_absent():
    assert DROPPED_FROM_MCP.isdisjoint(tools.TOOLS)


def test_both_transports_share_one_registry():
    # The strongest no-drift guarantee: it's literally the same object, not two copies.
    assert mcp_harness.TOOLS is tools.TOOLS


def test_every_tool_has_handler_and_input_schema():
    for name, meta in tools.TOOLS.items():
        assert callable(meta["handler"]), name
        assert (
            isinstance(meta["inputSchema"], dict) and meta["inputSchema"].get("type") == "object"
        ), name
