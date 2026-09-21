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
import re
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

from mcp_eras import ERAS, LEGACY, rpc  # noqa: E402
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
