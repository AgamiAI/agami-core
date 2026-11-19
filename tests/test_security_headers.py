"""The response headers every route class carries, and the sign-in pages' anti-framing policy.

Three claims, each proven over the real composed app:
- **HSTS on every response**, but only when the deployment's public URL is https. A loopback http
  deployment (local development) must not send it, or the browser would pin `localhost` to https.
- **Every HTML page** (the OAuth sign-in, the admin sign-in and console, the landing pages) carries a
  CSP with `frame-ancestors 'none'`, `X-Frame-Options`, `nosniff` and a `Referrer-Policy`, so none of
  them can be framed by a page someone else wrote.
- **The CSP still lets the pages run**: every inline script and inline handler core renders is
  covered by a hash in `script-src`, so a new inline script without a hash fails here rather than
  silently doing nothing in a browser.

Also: a route that set its own, stricter CSP keeps it, and the admin sign-in shows no password form
when a password could never sign the admin in.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("jwt")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import admin  # noqa: E402
import mcp_http  # noqa: E402
import security_headers  # noqa: E402
import user_store  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
LOCAL = "http://localhost:8000"
SECRET = "x" * 40  # throwaway HS256 key (>=32 bytes); not a real secret
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"

# One request per route class core serves: the landing page, both sign-in pages, a discovery
# document, a static asset, the OAuth registration endpoint and the bearer-gated MCP endpoint.
ROUTE_CLASSES = [
    ("GET", "/"),
    ("GET", "/oauth/authorize"),
    ("GET", "/admin/login"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/static/logo_h.svg"),
    ("POST", "/oauth/register"),
    ("POST", "/mcp"),
]

# The pages a person signs in on or administers from: the ones that must never be framable.
HTML_PAGES = ["/", "/oauth/authorize", "/admin/login", "/claim"]


def _env(tmp_path, monkeypatch, base: str, *, admin_password: str | None = ADMIN_PW) -> str:
    db_url = "sqlite://" + str(tmp_path / "headers.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", base)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    if admin_password is None:
        monkeypatch.delenv("AGAMI_ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", admin_password)
    for var in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    s = Store.connect(db_url)
    s.run_migrations()
    user_store.seed_admin_from_env(s)
    s.close()
    return db_url


def _send(client: TestClient, method: str, path: str):
    if method == "POST" and path == "/oauth/register":
        return client.post(
            path, json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
        )
    return client.request(method, path, follow_redirects=False)


@pytest.fixture
def https_client(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, BASE)
    return TestClient(mcp_http.build_app(), base_url=BASE)


# --- HSTS ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", ROUTE_CLASSES)
def test_every_route_class_sends_hsts_on_an_https_deployment(https_client, method, path):
    r = _send(https_client, method, path)
    assert r.headers.get("strict-transport-security") == "max-age=31536000"


@pytest.mark.parametrize("method,path", ROUTE_CLASSES)
def test_a_loopback_http_deployment_sends_no_hsts(tmp_path, monkeypatch, method, path):
    _env(tmp_path, monkeypatch, LOCAL)
    client = TestClient(mcp_http.build_app(), base_url=LOCAL)
    r = _send(client, method, path)
    assert "strict-transport-security" not in r.headers
    # The other headers do not depend on the scheme.
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_hsts_never_claims_subdomains_or_preload():
    # A self-hosted server cannot know what else lives under its hostname, so it must not pin it.
    assert "includesubdomains" not in security_headers.HSTS.lower()
    assert "preload" not in security_headers.HSTS.lower()


# --- the sign-in and admin pages -------------------------------------------------------------------


@pytest.mark.parametrize("path", HTML_PAGES)
def test_every_sign_in_and_landing_page_refuses_to_be_framed(https_client, path):
    r = https_client.get(path)
    assert r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_the_admin_console_refuses_to_be_framed(https_client):
    r = https_client.post(
        "/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW}, follow_redirects=False
    )
    assert r.status_code == 302
    page = https_client.get("/admin/model")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


def test_the_mcp_landing_for_a_browser_refuses_to_be_framed(https_client):
    r = https_client.post("/mcp", headers={"accept": "text/html"})
    assert r.status_code == 401
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_a_json_response_gets_nosniff_but_no_page_policy(https_client):
    r = https_client.get("/.well-known/oauth-authorization-server")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers


def test_the_page_policy_keeps_scripts_strict_and_leaves_form_targets_open():
    script_src = _directive(security_headers.PAGE_CSP, "script-src")
    assert "'unsafe-inline'" not in script_src and "*" not in script_src
    # The OAuth sign-in POST answers with a redirect to the connecting client's callback, and
    # browsers apply `form-action` to that redirect: restricting it would break every sign-in.
    assert "form-action" not in security_headers.PAGE_CSP
    for directive in ("default-src 'self'", "base-uri 'none'", "object-src 'none'"):
        assert directive in security_headers.PAGE_CSP


def _directive(csp: str, name: str) -> str:
    return next(d.strip() for d in csp.split(";") if d.strip().startswith(name + " "))


def _sha256(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def _assert_inline_code_is_allowed(html: str, csp: str) -> int:
    script_src = _directive(csp, "script-src")
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    handlers = re.findall(r'\son[a-z]+="([^"]*)"', html)
    for body in scripts + handlers:
        assert _sha256(body) in script_src, f"inline code has no hash in script-src: {body[:60]}"
    if handlers:
        assert "'unsafe-hashes'" in script_src
    return len(scripts) + len(handlers)


@pytest.mark.parametrize("path", HTML_PAGES)
def test_every_inline_script_on_a_served_page_is_allowed_by_its_policy(https_client, path):
    r = https_client.get(path)
    assert _assert_inline_code_is_allowed(r.text, r.headers["content-security-policy"]) > 0


def test_the_datasource_picker_handler_is_allowed_by_the_policy():
    # The admin model picker auto-submits through an inline handler, the one core renders.
    html = admin.datasource_picker_html("SALES_DATA", ["SALES_DATA", "MARKETING"])
    assert "onchange=" in html
    _assert_inline_code_is_allowed(html, security_headers.PAGE_CSP)


# --- an existing policy wins -----------------------------------------------------------------------


def _wrapped(response_factory, *, https: bool = True) -> TestClient:
    app = Starlette(
        routes=[Route("/", lambda request: response_factory())],
        middleware=[Middleware(security_headers.SecurityHeaders, https=https)],
    )
    return TestClient(app, base_url=BASE)


def test_a_route_with_its_own_policy_keeps_it_untouched():
    strict = "default-src 'none'; frame-ancestors 'none'"
    client = _wrapped(
        lambda: HTMLResponse(
            "<p>hi</p>",
            headers={"content-security-policy": strict, "referrer-policy": "no-referrer"},
        )
    )
    r = client.get("/")
    assert r.headers["content-security-policy"] == strict
    assert r.headers["referrer-policy"] == "no-referrer"
    # Not appended a second time either: one header, one value.
    assert r.headers.get_list("content-security-policy") == [strict]
    assert r.headers["strict-transport-security"] == "max-age=31536000"


def test_the_layer_adds_each_header_once():
    client = _wrapped(lambda: JSONResponse({"ok": True}))
    r = client.get("/")
    for name in ("strict-transport-security", "x-content-type-options", "referrer-policy"):
        assert len(r.headers.get_list(name)) == 1


# --- the admin sign-in's password form -------------------------------------------------------------


def _configure_google(monkeypatch):
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_SECRET", "test-client-secret")


def test_admin_login_shows_the_password_form_on_a_password_deployment(https_client):
    assert 'name="password"' in https_client.get("/admin/login").text


def test_admin_login_hides_the_password_form_when_a_password_cannot_sign_the_admin_in(
    tmp_path, monkeypatch
):
    # A provider deployment whose admin was seeded with no password: the form could only ever fail.
    db_url = _env(tmp_path, monkeypatch, BASE, admin_password=None)
    _configure_google(monkeypatch)
    monkeypatch.setenv("AGAMI_ADMIN_PROVIDER", "google")
    s = Store.connect(db_url)
    user_store.create_user(
        s, username=ADMIN_USER, password=None, email=ADMIN_USER, oidc_provider="google"
    )
    s.close()
    html = TestClient(mcp_http.build_app(), base_url=BASE).get("/admin/login").text
    assert 'name="password"' not in html
    assert "Continue with Google" in html


def test_admin_login_keeps_the_password_form_for_an_admin_who_has_one(tmp_path, monkeypatch):
    # A provider deployment whose admin also holds a password keeps it as the way in when the
    # provider is unavailable: the admin's password is accepted here even with a provider set.
    _env(tmp_path, monkeypatch, BASE)
    _configure_google(monkeypatch)
    html = TestClient(mcp_http.build_app(), base_url=BASE).get("/admin/login").text
    assert 'name="password"' in html


def test_admin_login_without_any_way_in_says_so_instead_of_a_blank_card():
    html = admin.admin_login_body_html(provider=None, password=False)
    assert 'name="password"' not in html
    assert "Password sign-in is off" in html
