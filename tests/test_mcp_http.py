"""The HTTP MCP transport — auth shim, OAuth discovery, and tools/list parity over HTTP.

Needs the [server] extra (the MCP SDK + ASGI stack); skipped cleanly without it.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

import mcp_http  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

BASE = "https://demo.example.com"
PRODUCT_TOOLS = {
    "list_datasources",
    "get_datasource_schema",
    "get_prompt_examples",
    "execute_sql",
    "log_feedback",
}


@pytest.fixture
def base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    return BASE


def test_public_base_url_is_required(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        mcp_http.public_base_url()


def test_discovery_advertises_public_base_url(base_url):
    c = TestClient(mcp_http.build_app())
    pr = c.get("/.well-known/oauth-protected-resource")
    assert pr.status_code == 200
    assert pr.json()["resource"] == f"{BASE}/mcp"
    assert pr.json()["authorization_servers"] == [BASE]
    # the path-suffixed variant the connector probes resolves to the same doc
    assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
    as_ = c.get("/.well-known/oauth-authorization-server")
    assert as_.status_code == 200 and as_.json()["issuer"] == BASE


def test_unauthenticated_request_gets_401_challenge(base_url):
    c = TestClient(mcp_http.build_app())
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert www.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource"' in www


def test_non_bearer_and_empty_tokens_are_rejected(base_url):
    c = TestClient(mcp_http.build_app())
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    # Only the Bearer scheme with a non-empty token counts as present.
    for authz in ("", "Bearer ", "Bearer    ", "Basic abc123", "token xyz"):
        r = c.post("/mcp", headers={"Authorization": authz}, json=body)
        assert r.status_code == 401, authz


def test_http_tools_list_is_the_same_five(base_url):
    """Authed end-to-end: initialize → tools/list over HTTP returns exactly the 5 product tools —
    the same surface stdio advertises (mirrored, not forked)."""
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
        assert init.status_code == 200
        sid = init.headers.get("mcp-session-id")
        h2 = {**headers, **({"mcp-session-id": sid} if sid else {})}
        c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        tl = c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tl.status_code == 200
        # the streamable-HTTP response may be SSE-framed; pull the JSON-RPC envelope out
        payload = json.loads(re.search(r"\{.*\}", tl.text, re.DOTALL).group(0))
        names = {t["name"] for t in payload["result"]["tools"]}
    assert names == PRODUCT_TOOLS
