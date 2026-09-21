"""The MCP App seam: a page beside a tool, an app-only tool, and a tool-result hook.

All three are optional and a consumer's to use; agami-core uses none of them. So the first claim is
the one that protects every deployment that never touches the seam: with no consumer, what a client
receives is byte for byte what it received before the seam existed. The fixtures under
`tests/fixtures/mcp_app_seam/` were captured over HTTP before any of this code was written, and this
file only ever reads them.

Every transport claim runs on both protocol eras through `create_app` + `TestClient`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("jwt")
pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

import mcp_http  # noqa: E402
import tools  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from mcp_eras import ERAS, LEGACY, envelope, rpc  # noqa: E402
from test_ace152_surface_snapshot import CALLS, PROFILE, served  # noqa: E402, F401

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mcp_app_seam"
PUBLIC_BASE_URL = "https://demo.example.com"

_VOLATILE = "<volatile>"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    for name in (
        "AGAMI_ORG_ID",
        "AGAMI_SIGNING_SECRET",
        "AGAMI_DB_URL",
        "APP_DATABASE_URL",
        "AGAMI_SQL_MAX_ROWS",
        "AGAMI_SQL_TIMEOUT_S",
        "AGAMI_REQUIRE_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    # `create_app` installs both process-wide; a later test must not inherit this file's.
    tools.set_statement_limits_provider(None)
    tools.set_injected_executor(None)


# --- no consumer: the served bytes are unchanged -------------------------------------------------

# Every method a client of either era may send about tools and resources. The ones an era does not
# serve are in the list too: their refusal is part of the surface, and a `resources/*` answer that
# changed from "method not found" would be the seam leaking into a deployment that never used it.
_SURFACE = (
    (
        "initialize",
        {
            "protocolVersion": LEGACY,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    ),
    ("server/discover", None),
    ("tools/list", None),
    ("resources/list", None),
    ("resources/read", {"uri": "ui://demo/widget.html"}),
    ("tools/call", {"name": "list_datasources", "arguments": {}}),
    *(
        ("tools/call", {"name": "execute_sql", "arguments": {"sql": sql, "datasource": PROFILE}})
        for sql in CALLS.values()
    ),
)

# The package version is stamped into `serverInfo`, and a release changes it without changing the
# surface. The envelope keys are the ones `test_ace152_surface_snapshot` masks, matched here in their
# escaped form because the envelope is a JSON string inside the response body.
_SERVER_VERSION = re.compile(r'("name":"agami","version":)"[^"]*"')
_ENVELOPE_VOLATILE = re.compile(
    r'(\\"(?:audit_id|execution_ms|elapsed_ms|executed_at|ts)\\": )(\\"[^\\"]*\\"|-?\d+(?:\.\d+)?)'
)


def _mask(body: str) -> str:
    body = _SERVER_VERSION.sub(rf'\1"{_VOLATILE}"', body)
    return _ENVELOPE_VOLATILE.sub(rf"\1{_VOLATILE}", body)


def no_consumer_surface(era: str) -> list[dict]:
    """Each surface request as a client of `era` receives it: the status and the raw body, masked."""
    from oauth_server import issue_jwt

    bearer = issue_jwt("jordan@example.com")
    answers = []
    with TestClient(mcp_http.create_app(), base_url=PUBLIC_BASE_URL) as client:
        for rid, (method, params) in enumerate(_SURFACE, start=2):
            response = rpc(client, era, method, params, rid=rid, bearer=bearer)
            answers.append(
                {"method": method, "status": response.status_code, "body": _mask(response.text)}
            )
    return answers


@pytest.mark.parametrize("era", ERAS)
def test_no_consumer_surface_is_byte_identical(served, era):  # noqa: F811 — the imported fixture
    expected = json.loads((FIXTURES / f"no_consumer_{era}.json").read_text())
    actual = no_consumer_surface(era)
    assert [a["method"] for a in actual] == [e["method"] for e in expected]
    for got, want in zip(actual, expected):
        assert got == want, got["method"]
    # Said outright rather than left implicit in the bytes: no consumer means no resources
    # capability and no extension advertised, on either era's capability-bearing answer.
    served_capabilities = [
        json.loads(a["body"])["result"]["capabilities"]
        for a in actual
        if a["method"] in ("initialize", "server/discover") and '"result"' in a["body"]
    ]
    assert len(served_capabilities) == 1  # the one the era serves
    assert "resources" not in served_capabilities[0]
    assert "extensions" not in served_capabilities[0]


# --- a consumer's page, app-only tool and result hook --------------------------------------------

PAGE = "ui://demo/widget.html"
OTHER_PAGE = "ui://demo/other.html"
HTML = "<!doctype html><title>demo</title><p>widget</p>"
SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
APP_MIME_TYPE = "text/html;profile=mcp-app"
EXTENSION_ID = "io.modelcontextprotocol/ui"


def _tool(handler=lambda args: "ran", **extra) -> dict:
    return {"handler": handler, "description": "probe", "inputSchema": dict(SCHEMA), **extra}


def _app(extra_tools: dict, *, visibility=None, hook=None, auth=None):
    """An app built the way a consumer builds one: extra tools, and the adapters it swaps."""
    swapped = {"tool_visibility": visibility, "tool_result_hook": hook, "auth_provider": auth}
    swapped = {k: v for k, v in swapped.items() if v is not None}
    adapters = replace(mcp_http.default_adapters(), **swapped) if swapped else None
    return mcp_http.create_app(extra_tools=extra_tools, adapters=adapters)


def _answer(app, era: str, method: str, params: dict | None = None, **kw) -> dict:
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        return envelope(rpc(client, era, method, params, **kw))


def _listed(app, era: str) -> dict[str, dict]:
    return {t["name"]: t for t in _answer(app, era, "tools/list")["result"]["tools"]}


def _capabilities(app, era: str) -> dict:
    if era == LEGACY:
        params = {
            "protocolVersion": LEGACY,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        }
        return _answer(app, era, "initialize", params)["result"]["capabilities"]
    return _answer(app, era, "server/discover")["result"]["capabilities"]


def _call(app, era: str, name: str, arguments: dict | None = None, **kw) -> dict:
    return _answer(app, era, "tools/call", {"name": name, "arguments": arguments or {}}, **kw)


@pytest.mark.parametrize("era", ERAS)
def test_a_page_is_listed_read_and_linked(era):
    sql_page = {"uri": "ui://demo/sql.html", "html": HTML}
    app = _app(
        {
            "demo_probe": _tool(page={"uri": PAGE, "html": HTML}),
            "execute_sql": {**tools.TOOLS["execute_sql"], "page": sql_page},
        }
    )
    listed = _listed(app, era)
    assert listed["demo_probe"]["_meta"] == {"ui": {"resourceUri": PAGE}}
    assert listed["demo_probe"]["inputSchema"] == SCHEMA
    # A page is linked beside a tool, never through it: the schema a client reads is the bytes it
    # read before, compared on the same era so the era's own key order is not what is measured.
    plain = _listed(_app({}), era)["execute_sql"]
    assert json.dumps(listed["execute_sql"]["inputSchema"]) == json.dumps(plain["inputSchema"])
    assert listed["execute_sql"]["_meta"] == {"ui": {"resourceUri": "ui://demo/sql.html"}}
    assert "_meta" not in listed["list_datasources"]

    resources = _answer(app, era, "resources/list")["result"]["resources"]
    assert sorted(resources, key=lambda r: r["uri"]) == [
        {"uri": "ui://demo/sql.html", "name": "ui://demo/sql.html", "mimeType": APP_MIME_TYPE},
        {"uri": PAGE, "name": PAGE, "mimeType": APP_MIME_TYPE},
    ]
    read = _answer(app, era, "resources/read", {"uri": PAGE})["result"]
    assert read["contents"] == [{"uri": PAGE, "mimeType": APP_MIME_TYPE, "text": HTML}]


@pytest.mark.parametrize("era", ERAS)
def test_a_page_advertises_resources_and_the_apps_extension(era):
    capabilities = _capabilities(_app({"demo_probe": _tool(page={"uri": PAGE, "html": HTML})}), era)
    assert "resources" in capabilities
    assert capabilities["extensions"] == {EXTENSION_ID: {}}
    assert "tools" in capabilities


@pytest.mark.parametrize(
    "page",
    [
        {"uri": PAGE, "html": HTML, "csp": {"connectDomains": ["https://demo.example.com"]}},
        {"uri": PAGE, "html": HTML, "domain": "demo.example.com"},
        {"uri": PAGE, "html": HTML, "permissions": {"camera": {}}},
        {"uri": PAGE, "html": HTML, "title": "unknown key"},
    ],
    ids=["csp", "domain", "permissions", "unknown-key"],
)
def test_a_page_naming_a_domain_is_refused(page):
    with pytest.raises(ValueError, match="'demo_probe'.*only uri and html"):
        _app({"demo_probe": _tool(page=page)})


@pytest.mark.parametrize(
    "extra, match",
    [
        ({"page": {"uri": "https://demo.example.com/w.html", "html": HTML}}, "ui://"),
        ({"page": {"uri": PAGE, "html": HTML.encode()}}, "html must be a str"),
        ({"app_only": "yes"}, "app_only must be a bool"),
    ],
    ids=["scheme", "html-type", "app-only-type"],
)
def test_a_malformed_page_or_app_only_is_refused_naming_the_tool(extra, match):
    with pytest.raises(ValueError, match=f"'demo_probe'.*{match}"):
        _app({"demo_probe": _tool(**extra)})


def test_one_uri_with_two_bodies_is_refused_naming_the_tool():
    with pytest.raises(ValueError, match="'demo_second'"):
        _app(
            {
                "demo_probe": _tool(page={"uri": PAGE, "html": HTML}),
                "demo_second": _tool(page={"uri": PAGE, "html": HTML + "<p>other</p>"}),
            }
        )


@pytest.mark.parametrize("era", ERAS)
def test_an_undeclared_uri_is_unknown(era):
    app = _app({"demo_probe": _tool(page={"uri": PAGE, "html": HTML})})
    error = _answer(app, era, "resources/read", {"uri": OTHER_PAGE})["error"]
    assert error["code"] == -32602
    assert error["message"] == f"Unknown resource: {OTHER_PAGE}"


def _read_raw(app, era: str, uri: str) -> tuple[int, str]:
    with TestClient(app, base_url=PUBLIC_BASE_URL) as client:
        response = rpc(client, era, "resources/read", {"uri": uri})
    return response.status_code, response.text


@pytest.mark.parametrize("era", ERAS)
def test_a_hidden_tools_page_is_absent_and_unreadable(era):
    """A hidden page answers as an absent one, byte for byte, or reading it is an oracle for it."""
    other = _tool(page={"uri": OTHER_PAGE, "html": HTML})
    hidden = _app(
        {"demo_probe": _tool(page={"uri": PAGE, "html": HTML}), "demo_other": other},
        visibility=lambda name: name != "demo_probe",
    )
    undeclared = _app({"demo_probe": _tool(), "demo_other": other})
    resources = _answer(hidden, era, "resources/list")["result"]["resources"]
    assert [r["uri"] for r in resources] == [OTHER_PAGE]
    assert _read_raw(hidden, era, PAGE) == _read_raw(undeclared, era, PAGE)
    assert "Unknown resource" in _read_raw(hidden, era, PAGE)[1]


@pytest.mark.parametrize("era", ERAS)
def test_a_shared_page_is_visible_if_any_tool_is(era):
    shared = {
        "demo_probe": _tool(page={"uri": PAGE, "html": HTML}),
        "demo_second": _tool(page={"uri": PAGE, "html": HTML}),
    }
    one_hidden = _app(shared, visibility=lambda name: name != "demo_probe")
    assert [
        r["uri"] for r in _answer(one_hidden, era, "resources/list")["result"]["resources"]
    ] == [PAGE]
    assert (
        _answer(one_hidden, era, "resources/read", {"uri": PAGE})["result"]["contents"][0]["text"]
        == HTML
    )
    both_hidden = _app(shared, visibility=lambda name: name not in shared)
    assert _answer(both_hidden, era, "resources/list")["result"]["resources"] == []
    assert "error" in _answer(both_hidden, era, "resources/read", {"uri": PAGE})


@pytest.mark.parametrize("era", ERAS)
def test_app_only_tool_is_marked_callable_and_still_hideable(era):
    tool = _tool(page={"uri": PAGE, "html": HTML}, app_only=True)
    app = _app({"demo_probe": tool})
    assert _listed(app, era)["demo_probe"]["_meta"] == {
        "ui": {"resourceUri": PAGE, "visibility": ["app"]}
    }
    # Marked, not enforced: any client may still call it, which is why its handler checks scope.
    assert _call(app, era, "demo_probe")["result"]["content"][0]["text"] == "ran"
    hidden = _app({"demo_probe": tool}, visibility=lambda name: name != "demo_probe")
    assert "demo_probe" not in _listed(hidden, era)
    assert "Unknown tool" in _call(hidden, era, "demo_probe")["error"]["message"]


def _widget(name, arguments, result_text):
    return {"tool": name, "chars": len(result_text)}


@pytest.mark.parametrize("era", ERAS)
def test_hook_output_is_structured_content_beside_identical_text(era):
    seen = []

    def hook(name, arguments, result_text):
        seen.append((name, arguments, result_text))
        return _widget(name, arguments, result_text)

    handler = lambda args: json.dumps({"ok": True})  # noqa: E731
    with_hook = _call(_app({"demo_probe": _tool(handler)}, hook=hook), era, "demo_probe")["result"]
    without = _call(_app({"demo_probe": _tool(handler)}), era, "demo_probe")["result"]
    assert with_hook["content"] == without["content"]
    assert "structuredContent" not in without
    text = with_hook["content"][0]["text"]
    assert with_hook["structuredContent"] == {"tool": "demo_probe", "chars": len(text)}
    assert seen == [("demo_probe", {}, text)]


@pytest.mark.parametrize("era", ERAS)
def test_hook_output_rides_beside_a_refused_execute_sql_envelope(served, era):  # noqa: F811
    from oauth_server import issue_jwt

    bearer = issue_jwt("jordan@example.com")
    arguments = {"sql": CALLS["refused"], "datasource": PROFILE}
    with_hook = _call(_app({}, hook=_widget), era, "execute_sql", arguments, bearer=bearer)[
        "result"
    ]
    without = _call(_app({}), era, "execute_sql", arguments, bearer=bearer)["result"]
    text = with_hook["content"][0]["text"]
    assert json.loads(text)["status"] == "refused"
    assert _mask(json.dumps(with_hook["content"])) == _mask(json.dumps(without["content"]))
    assert with_hook["structuredContent"] == {"tool": "execute_sql", "chars": len(text)}


def _hook_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "mcp_http" and r.levelno == logging.WARNING]


@pytest.mark.parametrize("era", ERAS)
def test_a_raising_hook_drops_structured_content_and_warns_once(era, caplog):
    def hook(name, arguments, result_text):
        raise RuntimeError("the hook broke")

    with caplog.at_level(logging.WARNING, logger="mcp_http"):
        result = _call(_app({"demo_probe": _tool()}, hook=hook), era, "demo_probe")["result"]
    assert result["content"][0]["text"] == "ran"
    assert not result.get("isError")
    assert "structuredContent" not in result
    assert len(_hook_warnings(caplog)) == 1


@pytest.mark.parametrize(
    "returned", [["not", "an", "object"], {"when": object()}], ids=["list", "unserializable"]
)
@pytest.mark.parametrize("era", ERAS)
def test_a_non_object_hook_return_is_dropped(era, returned, caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_http"):
        result = _call(_app({"demo_probe": _tool()}, hook=lambda *a: returned), era, "demo_probe")[
            "result"
        ]
    assert result["content"][0]["text"] == "ran"
    assert "structuredContent" not in result
    assert len(_hook_warnings(caplog)) == 1


@pytest.mark.parametrize("era", ERAS)
def test_a_hook_returning_none_adds_nothing_and_warns_nothing(era, caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_http"):
        result = _call(_app({"demo_probe": _tool()}, hook=lambda *a: None), era, "demo_probe")[
            "result"
        ]
    assert "structuredContent" not in result
    assert _hook_warnings(caplog) == []


@pytest.mark.parametrize("era", ERAS)
def test_the_hook_sees_the_request_context(era, monkeypatch):
    monkeypatch.setenv("AGAMI_ORG_ID", "acme")
    seen = {}

    class _Principal:
        subject = "you@example.com"
        session_id = "sess-42"

    class _Auth:
        def validate_token(self, token):
            return _Principal() if (token or "").strip() else None

    def hook(name, arguments, result_text):
        seen["actor"] = mcp_http._actor_ctx.get()
        seen["session"] = mcp_http.current_session_id()
        seen["org"] = tools._current_org_ctx.get()
        return None

    _call(_app({"demo_probe": _tool()}, hook=hook, auth=_Auth()), era, "demo_probe")
    assert seen == {"actor": "you@example.com", "session": "sess-42", "org": "acme"}


@pytest.mark.parametrize("era", ERAS)
def test_the_hook_does_not_run_on_unknown_hidden_invalid_or_crashed_calls(era):
    calls = []

    def crash(args):
        raise RuntimeError("handler broke")

    app = _app(
        {"demo_probe": _tool(), "demo_hidden": _tool(), "demo_crash": _tool(crash)},
        visibility=lambda name: name != "demo_hidden",
        hook=lambda *a: calls.append(a) or {"ran": True},
    )
    assert "error" in _call(app, era, "demo_missing")
    assert "error" in _call(app, era, "demo_hidden")
    invalid = _call(app, era, "demo_probe", {"unexpected": 1})["result"]
    assert invalid["isError"] and "structuredContent" not in invalid
    crashed = _call(app, era, "demo_crash")["result"]
    assert crashed["isError"] and "structuredContent" not in crashed
    assert calls == []
