"""The tool-extension seam: tools.register + mcp_http.create_app(extra_tools, adapters).

Proves the seam is additive and no-op by default: create_app() == the historical build_app(),
extra tools merge over a COPY of TOOLS (execute_sql byte-identical, the global untouched), the
duplicate-name guard holds, and a passed Adapters(...) is used (OSS defaults when None).

Needs the [server] extra (MCP SDK + ASGI); skipped cleanly without it.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

import mcp_http  # noqa: E402
import tools  # noqa: E402
from oss_adapters import (  # noqa: E402
    FileActivitySink,
    NoopGovernancePolicy,
    PresenceAuthProvider,
    SingleTenantOrgResolver,
)
from ports import Adapters, Org  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

BASE = "https://demo.example.com"
PRODUCT_TOOLS = {"list_datasources", "get_datasource_schema", "get_prompt_examples", "execute_sql"}

_DEMO = {
    "handler": lambda args: "ok",
    "description": "a demo tool registered by a consumer",
    "inputSchema": {"type": "object", "additionalProperties": False},
}


def _auth_middleware_kwargs(app):
    """The kwargs the _AuthMiddleware was wired with (robust across Starlette's .kwargs/.options)."""
    for m in app.user_middleware:
        if m.cls is mcp_http._AuthMiddleware:
            return getattr(m, "kwargs", None) or getattr(m, "options", {})
    raise AssertionError("the auth middleware should be wired")


@pytest.fixture
def base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    # Deterministic OSS defaults: no ambient signing secret (presence auth, not JWT).
    monkeypatch.delenv("AGAMI_SIGNING_SECRET", raising=False)
    return BASE


# --- tools.register -------------------------------------------------------


def test_register_adds_a_tool(monkeypatch):
    monkeypatch.setattr(tools, "TOOLS", dict(tools.TOOLS))  # isolate the module global
    tools.register("demo_probe", lambda a: "ok", "demo", {"type": "object"})
    assert "demo_probe" in tools.TOOLS


def test_register_rejects_a_duplicate_name(monkeypatch):
    monkeypatch.setattr(tools, "TOOLS", dict(tools.TOOLS))
    with pytest.raises(ValueError, match="already registered"):
        tools.register("execute_sql", lambda a: "x", "dup", {})  # can't shadow a core tool


# --- create_app: the registry merge is additive + non-mutating ------------


def test_extra_tools_merge_keeps_execute_sql_byte_identical():
    before = json.dumps(tools.TOOLS["execute_sql"]["inputSchema"], sort_keys=True)
    merged = {**tools.TOOLS, "demo_probe": _DEMO}  # the exact op create_app performs
    after = json.dumps(merged["execute_sql"]["inputSchema"], sort_keys=True)
    assert before == after  # execute_sql schema untouched by the extension
    assert "demo_probe" in merged  # the extra tool is present
    assert PRODUCT_TOOLS <= set(merged)  # the four core tools remain


def test_create_app_does_not_mutate_the_global_registry(base_url):
    before = set(tools.TOOLS)
    mcp_http.create_app(extra_tools={"demo_probe": _DEMO})
    assert "demo_probe" not in tools.TOOLS  # create_app merges a COPY, never the global
    assert set(tools.TOOLS) == before  # a second create_app() would be clean


def test_create_app_extra_tools_none_matches_no_args(base_url):
    # extra_tools defaults to None (not a mutable {}); an explicit None must behave like no args.
    c = TestClient(mcp_http.create_app(extra_tools=None))
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401  # same auth challenge as build_app() / create_app()
    assert r.headers.get("www-authenticate", "").startswith("Bearer ")


def test_create_app_rejects_a_malformed_extra_tool(base_url):
    # A consumer entry missing a required field (or a non-callable handler) fails fast at
    # construction with a clear error — not later as a KeyError/500 inside tools/list or tools/call.
    with pytest.raises(ValueError, match="handler, description, inputSchema"):
        mcp_http.create_app(extra_tools={"bad": {"description": "no handler/schema"}})
    with pytest.raises(ValueError, match="must be callable"):
        mcp_http.create_app(
            extra_tools={"bad": {"handler": "x", "description": "d", "inputSchema": {}}}
        )


# --- create_app: adapter injection ----------------------------------------


def test_adapters_none_uses_the_oss_defaults(base_url):
    a = mcp_http.default_adapters()
    assert isinstance(a.org_resolver, SingleTenantOrgResolver)
    assert isinstance(a.auth_provider, PresenceAuthProvider)  # presence when no signing secret
    assert isinstance(a.activity_sink, FileActivitySink)
    assert isinstance(a.governance, NoopGovernancePolicy)


def test_create_app_uses_the_passed_adapters(base_url):
    resolver = SingleTenantOrgResolver(Org(id="sentinel"))
    auth = PresenceAuthProvider(subject="sentinel")
    adapters = Adapters(
        activity_sink=FileActivitySink(),
        org_resolver=resolver,
        auth_provider=auth,
        governance=NoopGovernancePolicy(),
    )
    kwargs = _auth_middleware_kwargs(mcp_http.create_app(adapters=adapters))
    assert kwargs["resolver"] is resolver  # the passed adapters are used at the composition root
    assert kwargs["auth"] is auth


# --- backwards-compat: build_app() is a thin create_app() wrapper ---------


def test_build_app_still_serves_the_same_auth_challenge(base_url):
    c = TestClient(mcp_http.build_app())
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401  # unchanged entrypoint behavior
    assert r.headers.get("www-authenticate", "").startswith("Bearer ")
