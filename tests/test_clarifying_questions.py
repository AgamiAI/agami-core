"""A tool may ask the person a question, on a 2026-07-28 client that declared elicitation.

The first use is `get_datasource_schema` called with no `datasource` on an organization serving
several: instead of the AI guessing, the person is shown the served names and picks one. The server
keeps nothing between the question and the retry. It hands the client a sealed `requestState` and
reads it back, so the answer is honoured only from a state this server minted, for this caller, this
organization, this tool and these arguments, within ten minutes.

Every other client is answered exactly as before: a 2025-06-18 request, a modern request that did not
declare elicitation, the stdio harness, and any deployment with no `AGAMI_SIGNING_SECRET`.

The transport is driven through the real `create_app` and the SDK's own routing (see `mcp_eras`);
nothing about the sealing is faked. Datasource names are synthetic.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import replace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import mcp_http  # noqa: E402
import tools  # noqa: E402
from mcp.server import request_state  # noqa: E402
from ports import Org, Principal  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

from mcp_eras import LEGACY, MODERN, envelope, rpc  # noqa: E402
from test_datasource_routing import _write_profile  # noqa: E402

PUBLIC_BASE_URL = "https://demo.example.com"
SERVED = ["acme_crm", "acme_erp"]
SUBJECT = "you@example.com"
ORG = "org-acme"
BEARER = f"{SUBJECT}|{ORG}"
ELICIT = {"elicitation": {"form": {}}}
TOOL = "get_datasource_schema"
COLOUR = {"type": "object", "properties": {"colour": {"type": "string"}}, "required": ["colour"]}
CHOICE_SCHEMA = {
    "type": "object",
    "properties": {"datasource": {"type": "string", "enum": SERVED, "title": "Datasource"}},
    "required": ["datasource"],
}


class _BearerAuth:
    """`<subject>|<org>` as the bearer, so one test can speak as two callers."""

    def validate_token(self, token: str) -> Principal | None:
        return Principal(subject=token.split("|")[0]) if token else None


class _BearerOrgs:
    def resolve_org(self, ctx: object | None = None) -> Org:
        bearer = ctx.headers["authorization"][len("Bearer ") :]  # type: ignore[union-attr]
        return Org(id=bearer.split("|")[1])


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    # Generated here, never a literal: a secret in a test file is still a secret in the repo.
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", secrets.token_hex(32))
    for name in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE", "AGAMI_REQUIRE_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    _write_profile(tmp_path / "acme_crm")
    tools.bootstrap_paths()
    tools._sole_served_datasource.cache_clear()
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: list(SERVED))
    yield
    tools._sole_served_datasource.cache_clear()
    tools.set_statement_limits_provider(None)
    tools.set_injected_executor(None)


@pytest.fixture
def store_url(tmp_path, monkeypatch) -> str:
    url = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(url)
    store.run_migrations()
    store.close()
    monkeypatch.setenv("AGAMI_DB_URL", url)
    return url


def _tool_calls(url: str) -> list[dict]:
    store = Store.connect(url)
    try:
        return store.query("SELECT * FROM tool_calls ORDER BY ts")
    finally:
        store.close()


def _app(extra_tools: dict | None = None, *, visibility=None):
    adapters = replace(
        mcp_http.default_adapters(),
        auth_provider=_BearerAuth(),
        org_resolver=_BearerOrgs(),
        tool_visibility=visibility,
    )
    return mcp_http.create_app(extra_tools=extra_tools, adapters=adapters)


def _spied(answers: list) -> dict:
    """`get_datasource_schema` as served, recording the answer each call's handler was given."""

    def handler(args: dict) -> object:
        answers.append(tools.current_answer())
        return tools.tool_get_datasource_schema(args)

    return {TOOL: {**tools.TOOLS[TOOL], "handler": handler}}


def _call(
    client,
    *,
    era: str = MODERN,
    name: str = TOOL,
    arguments: dict | None = None,
    capabilities: dict | None = ELICIT,
    bearer: str = BEARER,
    **params,
) -> dict:
    body = {"name": name, "arguments": arguments or {}, **params}
    return envelope(rpc(client, era, "tools/call", body, bearer=bearer, capabilities=capabilities))


def _accept(name: object) -> dict:
    return {"datasource": {"action": "accept", "content": {"datasource": name}}}


def _todays_answer(actor: str | None = SUBJECT) -> str:
    """The `datasource_required` body exactly as it is built today, stamped the way HTTP stamps it."""
    return mcp_http._with_caller_identity(tools._choose_datasource_error(ORG, SERVED), actor)


def _text(message: dict) -> str:
    (block,) = message["result"]["content"]
    return block["text"]


# --- the sealing key ----------------------------------------------------------------------------


def test_the_sealing_key_is_derived_and_is_not_the_raw_secret(monkeypatch):
    secret = secrets.token_hex(32)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", secret)
    key = mcp_http._request_state_key()
    assert key is not None and len(key) == 32
    assert key != secret.encode() and key not in secret.encode()
    # Deterministic, or two instances sharing the secret could not read each other's state.
    assert mcp_http._request_state_key() == key


def test_no_signing_secret_means_no_key(monkeypatch):
    monkeypatch.delenv("AGAMI_SIGNING_SECRET")
    assert mcp_http._request_state_key() is None


def test_the_boundary_is_installed_only_with_a_key():
    def boundaries(server) -> list:
        return [m for m in server.middleware if isinstance(m, request_state.RequestStateBoundary)]

    assert boundaries(mcp_http.build_server()) == []
    assert len(boundaries(mcp_http.build_server(request_state_key=secrets.token_bytes(32)))) == 1


# --- the seam, for any tool author ----------------------------------------------------------------


def _asking_tool(seen: list) -> dict:
    """A consumer tool that asks one question whenever it may, and reports the answer it was given."""

    def handler(args: dict) -> object:
        seen.append((tools.can_ask(), tools.current_answer()))
        if tools.current_answer() is None and tools.can_ask():
            return tools.NeedsInput("colour", "Which colour?", COLOUR)
        return json.dumps({"answer": tools.current_answer()})

    return {"asker": {"handler": handler, "description": "asks", "inputSchema": {"type": "object"}}}


def test_a_tool_author_asks_and_gets_the_answer_back():
    seen: list = []
    with TestClient(_app(_asking_tool(seen)), base_url=PUBLIC_BASE_URL) as client:
        first = _call(client, name="asker")["result"]
        assert first["resultType"] == "input_required"
        request = first["inputRequests"]["colour"]
        assert request["method"] == "elicitation/create"
        assert request["params"]["message"] == "Which colour?"
        answer = {"colour": {"action": "accept", "content": {"colour": "teal"}}}
        second = _call(
            client, name="asker", inputResponses=answer, requestState=first["requestState"]
        )
    assert json.loads(_text(second))["answer"] == {
        "action": "accept",
        "content": {"colour": "teal"},
    }
    assert seen == [(True, None), (True, {"action": "accept", "content": {"colour": "teal"}})]


def test_the_state_on_the_wire_is_sealed_not_the_plaintext():
    with TestClient(_app(_asking_tool([])), base_url=PUBLIC_BASE_URL) as client:
        state = _call(client, name="asker")["result"]["requestState"]
    assert state.startswith("v1.") and "colour" not in state


@pytest.mark.parametrize("era", [LEGACY, MODERN])
def test_a_question_that_may_not_be_asked_never_leaves_unsealed(era, monkeypatch):
    """A tool that returns `NeedsInput` without checking `can_ask()` is answered as a crash: with no
    boundary, or on 2025-06-18, the state would go out as plaintext the client could rewrite."""
    monkeypatch.delenv("AGAMI_SIGNING_SECRET")

    def rude(args: dict) -> object:
        return tools.NeedsInput("colour", "Which colour?", COLOUR)

    extra = {"rude": {"handler": rude, "description": "asks", "inputSchema": {"type": "object"}}}
    with TestClient(_app(extra), base_url=PUBLIC_BASE_URL) as client:
        result = _call(client, era=era, name="rude")["result"]
    assert result.get("resultType", "complete") == "complete"
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Error executing tool rude"


def test_can_ask_is_false_and_there_is_no_answer_outside_a_call():
    assert tools.can_ask() is False
    assert tools.current_answer() is None
