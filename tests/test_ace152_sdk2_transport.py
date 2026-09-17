"""The tool-call outcome policy on the HTTP transport, on both protocol eras (ACE-152).

SDK 2 changed what a raise inside a handler becomes: a generic internal error on 2026-07-28, and on
2025-06-18 a JSON-RPC error that carries `str(e)`. Neither is what a client should see, so
`build_server` decides every outcome itself (the F18 contract's policy):

- A hidden tool and an absent one both answer `-32602 Unknown tool: <name>`, byte for byte the same,
  so the answer is no oracle for what exists but is withheld. The stdio harness answers the same.
- A handler that raises, or an audit write that fails, answers `isError` with the fixed text
  `Error executing tool <name>`. The reason goes to the server log, never to the wire.
- Arguments are validated against `inputSchema` after the visibility check, with SDK 1.x's own
  `Input validation error: <msg>` text, and a refusal there writes no audit row.

Every claim runs through the real `create_app` on each era (see `mcp_eras`); the SDK is not faked.
The leak checks scan the raw response bytes and headers rather than a parsed field, so a reason
smuggled into any part of the response fails them.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import replace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

import mcp_http  # noqa: E402
import tools  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

from mcp_eras import ERAS, LEGACY, MODERN, base_headers, envelope, rpc  # noqa: E402

PUBLIC_BASE_URL = "https://demo.example.com"
MARKER = "marker-7f3a"
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"limit": {"type": "integer"}},
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    for name in (
        "AGAMI_SIGNING_SECRET",
        "AGAMI_DB_URL",
        "APP_DATABASE_URL",
        "AGAMI_REQUIRE_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    tools.set_statement_limits_provider(None)
    tools.set_injected_executor(None)


@pytest.fixture
def store_url(tmp_path, monkeypatch) -> str:
    """A served deployment: the audit write is load-bearing, so its failure fails the call."""
    url = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(url)
    store.run_migrations()
    store.close()
    monkeypatch.setenv("AGAMI_DB_URL", url)
    return url


def _app(handler=None, *, visibility=None, auth=None, register_probe: bool = True):
    """An app serving the core tools plus `probe`, narrowed by `visibility` the way a consumer would."""
    extra = None
    if register_probe:
        extra = {
            "probe": {
                "handler": handler or (lambda args: "ran"),
                "description": "probe",
                "inputSchema": PROBE_SCHEMA,
            }
        }
    adapters = replace(mcp_http.default_adapters(), tool_visibility=visibility)
    if auth is not None:
        adapters = replace(adapters, auth_provider=auth)
    return mcp_http.create_app(extra_tools=extra, adapters=adapters)


def _call(app, era: str, name: str = "probe", arguments: dict | None = None):
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        return rpc(client, era, "tools/call", {"name": name, "arguments": arguments or {}})


def _assert_nothing_leaked(response) -> None:
    assert MARKER.encode() not in response.content
    for key, value in response.headers.items():
        assert MARKER not in key and MARKER not in value


def _tool_calls(url: str) -> list[dict]:
    store = Store.connect(url)
    try:
        return store.query("SELECT * FROM tool_calls")
    finally:
        store.close()


def _raise(args: dict) -> str:
    raise RuntimeError(MARKER)


# --- a hidden tool is an absent one --------------------------------------------------------------


@pytest.mark.parametrize("arguments", [{}, {"limit": "ten"}], ids=["valid-args", "invalid-args"])
@pytest.mark.parametrize("era", ERAS)
def test_hidden_and_absent_are_byte_identical(era, arguments):
    """Invalid arguments too: validating before the visibility check would answer a hidden tool
    with its own schema's complaint, which an absent tool has no schema to produce."""
    hidden = _call(_app(visibility=lambda name: name != "probe"), era, arguments=arguments)
    absent = _call(_app(register_probe=False), era, arguments=arguments)

    assert envelope(hidden)["error"] == {"code": -32602, "message": "Unknown tool: probe"}
    assert hidden.status_code == absent.status_code
    assert hidden.headers.get("content-type") == absent.headers.get("content-type")
    assert hidden.content == absent.content


@pytest.mark.parametrize("era", ERAS)
def test_stdio_and_http_agree_on_unknown_tool(era):
    """REQ-002: one answer to a name nobody registered, whichever transport asked."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "no_such_tool"}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ},
    )
    replies = {m.get("id"): m for m in map(json.loads, proc.stdout.splitlines()) if m}
    assert 2 in replies, proc.stderr

    http = envelope(_call(_app(), era, name="no_such_tool"))

    assert http["error"] == replies[2]["error"]


@pytest.mark.parametrize("era", ERAS)
def test_the_predicate_sees_the_request_context(era):
    """REQ-023: the visibility predicate still runs inside the request on both eras, at both seams,
    so a consumer's predicate can read who is asking."""
    seen: list[str | None] = []

    class _Principal:
        subject = "jordan@example.com"
        session_id = None

    class _Auth:
        def validate_token(self, token):
            return _Principal() if (token or "").strip() else None

    def by_caller(name: str) -> bool:
        seen.append(mcp_http._actor_ctx.get())
        return True

    app = _app(visibility=by_caller, auth=_Auth())
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        listed = rpc(client, era, "tools/list")
        seen_at_list = list(seen)
        called = rpc(client, era, "tools/call", {"name": "probe", "arguments": {}})

    assert listed.status_code == 200 and called.status_code == 200
    assert seen_at_list and set(seen_at_list) == {"jordan@example.com"}
    assert len(seen) > len(seen_at_list) and set(seen) == {"jordan@example.com"}


# --- a crash answers the fixed text --------------------------------------------------------------


def _break_the_sink(monkeypatch) -> None:
    import model_store

    def _boom(self, record):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(model_store.DbActivitySink, "record_tool_call", _boom)


@pytest.mark.parametrize("cause", ["handler", "audit", "both"])
@pytest.mark.parametrize("era", ERAS)
def test_a_crash_answers_the_fixed_text(era, cause, store_url, monkeypatch):
    """A failed audit write fails the call (ACE-097), and answers exactly as a raising handler does:
    both are internals, and a driver's or a store's words are an enumeration channel."""
    if cause in ("audit", "both"):
        _break_the_sink(monkeypatch)
    handler = _raise if cause in ("handler", "both") else None

    response = _call(_app(handler), era)

    assert response.status_code == 200, response.text
    result = envelope(response)["result"]
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "Error executing tool probe"}]
    _assert_nothing_leaked(response)


@pytest.mark.parametrize("era", ERAS)
def test_a_crash_is_logged_once_with_its_traceback(era, store_url, caplog):
    """The reason leaves the wire, so the log is where an operator finds it."""
    caplog.set_level(logging.ERROR)

    _call(_app(_raise), era)

    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(records) == 1, [r.getMessage() for r in records]
    assert records[0].exc_info and MARKER in str(records[0].exc_info[1])


@pytest.mark.parametrize("era", ERAS)
def test_a_crash_is_recorded_for_the_operator(era, store_url):
    """The audit row keeps the reason the wire no longer carries, beside the failure it explains."""
    response = _call(_app(_raise), era)

    (row,) = [r for r in _tool_calls(store_url) if r["tool_name"] == "probe"]
    assert row["success"] == 0 and row["error_kind"] == "exception"
    assert MARKER in row["error_detail"]
    _assert_nothing_leaked(response)


@pytest.mark.parametrize("era", ERAS)
def test_a_raising_predicate_leaks_nothing(era):
    """A predicate that raises hides the tool, and its words stay in the log like any other crash."""

    def boom(name: str) -> bool:
        raise RuntimeError(MARKER)

    app = _app(visibility=boom)
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        listed = rpc(client, era, "tools/list")
        called = rpc(client, era, "tools/call", {"name": "probe", "arguments": {}})

    assert envelope(listed)["result"]["tools"] == []
    assert envelope(called)["error"] == {"code": -32602, "message": "Unknown tool: probe"}
    _assert_nothing_leaked(listed)
    _assert_nothing_leaked(called)


# --- argument validation -------------------------------------------------------------------------


@pytest.mark.parametrize("era", ERAS)
def test_invalid_arguments_get_1x_text(era):
    ran: list[dict] = []

    response = _call(_app(lambda args: ran.append(args) or "ran"), era, arguments={"limit": "ten"})

    assert response.status_code == 200, response.text
    result = envelope(response)["result"]
    assert result["isError"] is True
    assert result["content"] == [
        {"type": "text", "text": "Input validation error: 'ten' is not of type 'integer'"}
    ]
    assert ran == []


@pytest.mark.parametrize("era", ERAS)
def test_a_validation_refusal_writes_no_row(era, store_url):
    """As on SDK 1.x, where the SDK refused before the handler, and so before the audit write."""
    response = _call(_app(), era, arguments={"limit": "ten"})

    assert envelope(response)["result"]["isError"] is True
    assert _tool_calls(store_url) == []


# --- the host and origin allowlist ---------------------------------------------------------------


def _opening_request(client, era: str):
    """The first request a client of `era` sends: `initialize` on 2025-06-18, a plain `tools/list`
    on 2026-07-28. Sent raw on the handshake era, because `rpc` asserts the handshake succeeded and
    a refused host is exactly the case where it must not."""
    if era == MODERN:
        return rpc(client, era, "tools/list")
    return client.post(
        "/mcp",
        headers=base_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": era,
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    )


@pytest.mark.parametrize("era", ERAS)
def test_foreign_host_is_421(era):
    """DNS rebinding: a page on another name that resolves to this server must not reach /mcp."""
    with TestClient(_app(), base_url="https://elsewhere.example.net") as client:
        response = _opening_request(client, era)

    assert response.status_code == 421


@pytest.mark.parametrize("era", ERAS)
def test_matching_host_is_served(era):
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        response = _opening_request(client, era)

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("era", ERAS)
def test_loopback_serves_localhost_and_127(era, monkeypatch):
    """A developer's browser and tools spell loopback both ways, on whatever port they bound."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:18152")
    for base in ("http://localhost:18152", "http://127.0.0.1:18152", "http://127.0.0.1:9000"):
        with TestClient(_app(), base_url=base) as client:
            response = _opening_request(client, era)
        assert response.status_code == 200, (base, response.text)


def test_foreign_origin_is_403():
    """A browser page on another origin is refused even with the right Host. A server-to-server
    client sends no Origin and is unaffected; so is a page served from PUBLIC_BASE_URL itself."""

    def tools_list(client, origin: str):
        return client.post(
            "/mcp",
            headers={**base_headers(), "MCP-Protocol-Version": LEGACY, "Origin": origin},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        foreign = tools_list(client, "https://elsewhere.example.net")
        own = tools_list(client, PUBLIC_BASE_URL)

    assert foreign.status_code == 403
    assert own.status_code == 200, own.text


def test_well_known_ignores_the_allowlist():
    """REQ-003: only /mcp is checked. Discovery is what a client reads before it knows anything, and
    it already names PUBLIC_BASE_URL as the resource, whatever host it was asked on."""
    with TestClient(_app(), base_url="https://elsewhere.example.net") as client:
        response = client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert response.json()["resource"] == f"{PUBLIC_BASE_URL}/mcp"


# --- cache hints and discover --------------------------------------------------------------------


def test_cache_hints_on_the_modern_era():
    """Private, because the tool list is per caller: the visibility predicate and the per-org limits
    in execute_sql's description both depend on who asked."""
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        listed = envelope(rpc(client, MODERN, "tools/list"))["result"]
        discovered = envelope(rpc(client, MODERN, "server/discover"))["result"]

    for result in (listed, discovered):
        assert result["ttlMs"] == 60000
        assert result["cacheScope"] == "private"


def test_no_cache_fields_on_legacy():
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        listed = envelope(rpc(client, LEGACY, "tools/list"))["result"]

    assert "ttlMs" not in listed and "cacheScope" not in listed


def test_discover_carries_the_initialize_instructions():
    """A 2026-07-28 client never sends `initialize`, so `server/discover` is the only place it reads
    the instructions — including the PII rule and a consumer's appended ones."""
    extra = "Extra: call demo_probe when asked about widgets."
    app = mcp_http.create_app(extra_instructions=extra)
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        initialized = client.post(
            "/mcp",
            headers=base_headers(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LEGACY,
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        discovered = rpc(client, MODERN, "server/discover")

    instructions = envelope(initialized)["result"]["instructions"]
    assert extra in instructions
    assert envelope(discovered)["result"]["instructions"] == instructions
