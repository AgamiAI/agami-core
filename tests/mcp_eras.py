"""Drive the real HTTP MCP transport on either protocol era (ACE-152).

The SDK routes each request by its `MCP-Protocol-Version` header, so one app serves two wire shapes:

- **2025-06-18** — the handshake era. A client runs `initialize` + `notifications/initialized`, then
  names the negotiated version in the header on every later request.
- **2026-07-28** — the per-request envelope. No handshake: every request carries the version header,
  `Mcp-Method` (and `Mcp-Name` for a named call), and the same version plus the client's
  capabilities under `params._meta`.

Shared by the ACE-152 test files so every transport claim runs on both eras through `create_app` +
`TestClient`, never a faked SDK: the behaviour under test is the SDK's own routing.
"""

from __future__ import annotations

import json
from typing import Any

LEGACY = "2025-06-18"
MODERN = "2026-07-28"
ERAS = (LEGACY, MODERN)

# The reserved `_meta` keys a 2026-07-28 request must carry. Spelled out rather than imported from
# `mcp_types`, so a test can build a request without importing SDK internals.
_PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
_CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"

# The methods whose request names a target, which a 2026-07-28 request repeats in `Mcp-Name`.
_NAME_BEARING = {"tools/call": "name", "prompts/get": "name"}


def base_headers(bearer: str = "present") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def rpc(
    client: Any,
    era: str,
    method: str,
    params: dict | None = None,
    *,
    rid: int = 2,
    bearer: str = "present",
    capabilities: dict | None = None,
) -> Any:
    """POST one JSON-RPC request on `era` and return the raw HTTP response.

    On 2025-06-18 this runs the handshake first, on the same client, so every request is preceded
    by what a real client of that era sends. The transport is stateless, so the handshake binds
    nothing; it is sent because skipping it is not what a client does.

    `capabilities` is what the client declares: in `initialize` on 2025-06-18, and in every
    request's `_meta` on 2026-07-28. None declares none, as before.
    """
    headers = base_headers(bearer)
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    params = dict(params or {})
    if era == LEGACY:
        _handshake(client, headers, capabilities or {})
        headers["MCP-Protocol-Version"] = LEGACY
    else:
        headers["MCP-Protocol-Version"] = era
        headers["Mcp-Method"] = method
        if method in _NAME_BEARING and _NAME_BEARING[method] in params:
            headers["Mcp-Name"] = params[_NAME_BEARING[method]]
        params["_meta"] = {
            **params.get("_meta", {}),
            _PROTOCOL_VERSION_META_KEY: era,
            _CLIENT_CAPABILITIES_META_KEY: capabilities or {},
        }
    if params:
        body["params"] = params
    return client.post("/mcp", headers=headers, json=body)


def _handshake(client: Any, headers: dict[str, str], capabilities: dict) -> None:
    init = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY,
                "capabilities": capabilities,
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    )
    assert init.status_code == 200, init.text
    client.post(
        "/mcp",
        headers={**headers, "MCP-Protocol-Version": LEGACY},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )


def envelope(response: Any) -> dict:
    """The JSON-RPC message in a response, whether it arrived plain or SSE-framed."""
    text = response.text.strip()
    if text.startswith("event:") or text.startswith("data:"):
        text = next(
            line[len("data:") :].strip() for line in text.splitlines() if line.startswith("data:")
        )
    return json.loads(text)
