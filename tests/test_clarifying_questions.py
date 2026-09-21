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

import base64
import io
import json
import secrets
import time
from contextlib import redirect_stdout
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import mcp_harness  # noqa: E402
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


# --- the question in get_datasource_schema ------------------------------------------------------


def test_a_modern_elicitation_client_is_asked_which_datasource():
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        result = _call(client)["result"]
    assert result["resultType"] == "input_required"
    assert isinstance(result["requestState"], str)
    (key, request), *rest = result["inputRequests"].items()
    assert rest == []
    assert key == "datasource"
    assert request["method"] == "elicitation/create"
    assert request["params"]["mode"] == "form"
    assert request["params"]["requestedSchema"] == CHOICE_SCHEMA


def test_an_accepted_choice_returns_what_naming_it_returns():
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        state = _call(client)["result"]["requestState"]
        answered = _call(client, inputResponses=_accept("acme_crm"), requestState=state)
        named = _call(client, arguments={"datasource": "acme_crm"})
    assert answered["result"]["resultType"] == "complete"
    assert _text(answered) == _text(named)
    assert json.JSONDecoder().raw_decode(_text(named))[0].get("error") is None


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_a_declined_or_cancelled_form_gets_todays_answer(action):
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        state = _call(client)["result"]["requestState"]
        answer = {"datasource": {"action": action}}
        retried = _call(client, inputResponses=answer, requestState=state)
    assert _text(retried) == _todays_answer()


@pytest.mark.parametrize(
    "era,capabilities",
    [(MODERN, {}), (LEGACY, ELICIT)],
    ids=["modern-no-elicitation", "legacy-declaring-elicitation"],
)
def test_a_client_that_cannot_be_asked_gets_todays_answer(era, capabilities):
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        message = _call(client, era=era, capabilities=capabilities)
    assert message["result"].get("resultType", "complete") == "complete"
    assert _text(message) == _todays_answer()


def test_stdio_gets_todays_answer():
    """The harness never sets `can_ask()`, whatever the request claims about the client."""
    params = {
        "name": TOOL,
        "arguments": {},
        "_meta": {"io.modelcontextprotocol/clientCapabilities": ELICIT},
        "inputResponses": _accept("acme_crm"),
        "requestState": "datasource",
    }
    out = io.StringIO()
    with redirect_stdout(out):
        mcp_harness._handle_tools_call(1, params)
    (block,) = json.loads(out.getvalue())["result"]["content"]
    assert block["text"] == tools._choose_datasource_error(ORG, SERVED)


def test_no_signing_secret_means_no_question(monkeypatch):
    monkeypatch.delenv("AGAMI_SIGNING_SECRET")
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        message = _call(client)
    assert message["result"].get("resultType", "complete") == "complete"
    assert _text(message) == _todays_answer()


def test_a_caller_with_no_subject_is_not_asked():
    # A state bound to no subject would open for every other caller without one.
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        message = _call(client, bearer="|acme")
    assert message["result"].get("resultType", "complete") == "complete"
    body = json.JSONDecoder().raw_decode(_text(message))[0]
    assert body["error"]["kind"] == "datasource_required"


def test_one_served_datasource_is_not_a_question(monkeypatch):
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: ["acme_crm"])
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        message = _call(client)
    assert message["result"]["resultType"] == "complete"
    assert json.JSONDecoder().raw_decode(_text(message))[0].get("error") is None


def test_an_unreachable_store_is_not_a_question(monkeypatch):
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: None)
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        message = _call(client)
    assert message["result"]["resultType"] == "complete"


# --- the sealed state is the control --------------------------------------------------------------


def _flip_one_byte(state: str) -> str:
    raw = bytearray(base64.urlsafe_b64decode(state[3:] + "=" * (-len(state[3:]) % 4)))
    raw[20] ^= 0x01
    return "v1." + base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")


def _clock(monkeypatch, offset: float) -> None:
    """Move only the request-state module's clock: the rest of the process keeps real time."""
    now = time.time
    monkeypatch.setattr(request_state, "time", SimpleNamespace(time=lambda: now() + offset))


_PROBE = {
    "probe": {
        "handler": lambda args: "ran",
        "description": "probe",
        "inputSchema": {"type": "object"},
    }
}


@pytest.mark.parametrize(
    "tamper",
    [
        "one-byte-flipped",
        "other-subject",
        "other-org",
        "other-tool",
        "other-arguments",
        "datasource-added",
        "expired",
        "minted-in-the-future",
        "not-a-string",
    ],
)
def test_a_tampered_or_stale_state_is_refused_and_no_answer_reaches_the_handler(
    tamper, monkeypatch
):
    answers: list = []
    with TestClient(_app({**_spied(answers), **_PROBE}), base_url=PUBLIC_BASE_URL) as client:
        if tamper == "minted-in-the-future":
            _clock(monkeypatch, 120)
        state = _call(client)["result"]["requestState"]
        if tamper == "minted-in-the-future":
            # Back to real time for the retry, which then sees an `iat` two minutes ahead.
            monkeypatch.setattr(request_state, "time", time)
        retry: dict = {"inputResponses": _accept("acme_crm"), "requestState": state}
        if tamper == "one-byte-flipped":
            retry["requestState"] = _flip_one_byte(state)
        elif tamper == "other-subject":
            retry["bearer"] = f"someone@example.com|{ORG}"
        elif tamper == "other-org":
            retry["bearer"] = f"{SUBJECT}|org-other"
        elif tamper == "other-tool":
            retry["name"] = "probe"
        elif tamper == "other-arguments":
            retry["arguments"] = {"mode": "full"}
        elif tamper == "datasource-added":
            retry["arguments"] = {"datasource": "acme_erp"}
        elif tamper == "expired":
            _clock(monkeypatch, 601)
        elif tamper == "not-a-string":
            retry["requestState"] = 12345
        message = _call(client, **retry)
    assert message["error"]["code"] == -32602
    # The first round ran with no answer; the refused retry never reached the handler at all.
    assert answers == [None]


def test_a_state_opens_on_a_second_instance_sharing_the_secret_and_not_on_a_third(monkeypatch):
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as a:
        state = _call(a)["result"]["requestState"]
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as b:
        shared = _call(b, inputResponses=_accept("acme_crm"), requestState=state)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", secrets.token_hex(32))
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as c:
        other = _call(c, inputResponses=_accept("acme_crm"), requestState=state)
    assert shared["result"]["resultType"] == "complete"
    assert json.JSONDecoder().raw_decode(_text(shared))[0].get("error") is None
    assert other["error"]["code"] == -32602


@pytest.mark.parametrize(
    "content",
    [{"datasource": "acme_hr"}, {"datasource": 5}, {"datasource": True}, {}, None],
    ids=["outside-the-list", "a-number", "a-bool", "empty", "absent"],
)
def test_an_accepted_answer_outside_the_served_list_never_resolves(content, monkeypatch):
    resolved: list = []
    real = tools._resolve_call_datasource
    monkeypatch.setattr(
        tools, "_resolve_call_datasource", lambda args: resolved.append(args) or real(args)
    )
    answer = {
        "datasource": {"action": "accept", **({} if content is None else {"content": content})}
    }
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        state = _call(client)["result"]["requestState"]
        retried = _call(client, inputResponses=answer, requestState=state)
    assert _text(retried) == _todays_answer()
    assert resolved == []


def test_both_rounds_write_a_tool_calls_row(store_url):
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        state = _call(client)["result"]["requestState"]
        _call(client, inputResponses=_accept("acme_crm"), requestState=state)
    asked, answered = _tool_calls(store_url)
    assert (asked["tool_name"], asked["success"], asked["error_kind"]) == (TOOL, 1, None)
    assert asked["row_count"] is None
    assert answered["tool_name"] == TOOL
    assert (answered["datasource"], answered["datasource_source"]) == ("acme_crm", "resolved")


def test_an_sdk_client_asks_answers_and_gets_the_schema():
    """End to end: the SDK's own client, on 2026-07-28, drives the question through its elicitation
    callback and its retry loop, over the real HTTP app."""
    import anyio
    import httpx2
    import mcp.types as mt
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    app = _app()
    asked: list = []

    async def choose(context, params):
        asked.append(params.requested_schema)
        return mt.ElicitResult(action="accept", content={"datasource": "acme_crm"})

    async def run() -> mt.CallToolResult:
        async with app.router.lifespan_context(app):
            http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url=PUBLIC_BASE_URL,
                headers={"Authorization": f"Bearer {BEARER}"},
            )
            async with http:
                transport = streamable_http_client(f"{PUBLIC_BASE_URL}/mcp", http_client=http)
                async with Client(transport, mode=MODERN, elicitation_callback=choose) as client:
                    return await client.call_tool(TOOL, {})

    result = anyio.run(run)
    assert asked == [CHOICE_SCHEMA]
    assert result.is_error is False
    body = json.JSONDecoder().raw_decode(result.content[0].text)[0]
    assert body.get("error") is None


def test_a_forged_state_answers_a_hidden_tool_and_an_absent_one_identically(store_url):
    forged = "v1." + base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    retry = {"name": "probe", "inputResponses": _accept("acme_crm"), "requestState": forged}
    with TestClient(_app(_PROBE, visibility=lambda n: n != "probe"), base_url=PUBLIC_BASE_URL) as c:
        hidden = rpc(
            c, MODERN, "tools/call", {"arguments": {}, **retry}, bearer=BEARER, capabilities=ELICIT
        )
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as c:
        absent = rpc(
            c, MODERN, "tools/call", {"arguments": {}, **retry}, bearer=BEARER, capabilities=ELICIT
        )
    assert envelope(hidden)["error"]["code"] == -32602
    assert hidden.status_code == absent.status_code
    assert hidden.content == absent.content
    # A refused call writes no row, hidden or absent alike, exactly as a refused call always has.
    assert _tool_calls(store_url) == []


def test_answers_without_a_state_or_under_another_key_are_ignored():
    with TestClient(_app(), base_url=PUBLIC_BASE_URL) as client:
        unsealed = _call(client, inputResponses=_accept("acme_crm"))
        state = _call(client)["result"]["requestState"]
        wrong_key = {"other": {"action": "accept", "content": {"datasource": "acme_crm"}}}
        misfiled = _call(client, inputResponses=wrong_key, requestState=state)
    # Neither is an answer, so the person is asked (again).
    assert unsealed["result"]["resultType"] == "input_required"
    assert misfiled["result"]["resultType"] == "input_required"
