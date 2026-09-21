#!/usr/bin/env python3
"""agami serve --http — the HTTP MCP transport (streamable-HTTP).

Advertises the **same** shared `tools.TOOLS` registry as the stdio entrypoint, but over a network
endpoint a remote client (claude.ai) can reach — plus the OAuth-discovery surface and a bearer
auth shim. This is the network product: unlike the stdio harness it binds a port, so it carries
auth — an unauthenticated request gets a `401` + `WWW-Authenticate` challenge, which triggers the
client's OAuth flow against the authorize/token endpoints (see `oauth_server`). The issued JWT then
gates `/mcp`; with no signing secret configured the OSS bearer-presence default applies instead.

Requires the **[server]** extra (the MCP SDK + ASGI stack). `PUBLIC_BASE_URL` must be set
explicitly — it backs the discovery documents + the `WWW-Authenticate` resource URL and cannot be
reliably auto-detected behind a proxy/LB.

    PUBLIC_BASE_URL=https://your-host python -m mcp_http
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

import admin
import onboarding
import user_store
from async_offload import run_blocking
from execute_sql import BUILTIN_EXECUTOR
from oss_adapters import (
    FileActivitySink,
    PresenceAuthProvider,
    SingleTenantOrgResolver,
)
from ports import Adapters, AuthProvider, Org, OrgResolver
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from store import Store
from tools import (
    SERVER_NAME,
    TOOLS,
    _current_org_ctx,
    bootstrap_paths,
    has_statement_limits_provider,
    record_tool_call,
    require_thread_id,
    reset_typed_outcome,
    resolved_org_id,
    server_instructions,
    server_version,
    set_injected_executor,
    set_statement_limits_provider,
    thread_id_is_required,
    tool_description,
    typed_outcome_overrides,
)

if TYPE_CHECKING:
    # Imported where it is used, like the rest of the SDK, so importing this module needs no `mcp`.
    from mcp.server.transport_security import TransportSecuritySettings

_log = logging.getLogger(__name__)

# The authenticated user for the in-flight tool call. Set in `handle_mcp` (the raw-ASGI endpoint, which
# runs in the request's task) so it propagates into the MCP dispatch — the tool handler `_call_tool`
# only receives (name, arguments), and a contextvar set in the BaseHTTPMiddleware wouldn't reach it.
_actor_ctx: ContextVar[str | None] = ContextVar("agami_tool_actor", default=None)

# The CLIENT AUTHORIZATION behind the in-flight tool call — what tells two windows under one login apart.
# Set alongside `_actor_ctx` and for the same reason. None whenever the caller's auth has no session
# notion (presence auth, a hand-minted bearer): consumers must read that as "no session", not an error.
_session_ctx: ContextVar[str | None] = ContextVar("agami_tool_session", default=None)


def current_session_id() -> str | None:
    """The session id for the request being served, or None.

    Public because a consumer's tool handler needs it and the request never reaches the handler — only
    this contextvar does. Same shape as `tools.current_org_id`, so consumers don't have to reach into a
    private module attribute the way they otherwise would.
    """
    return _session_ctx.get()


def _principal_from_scope(scope: dict, auth: AuthProvider) -> object | None:
    """The authenticated caller for this /mcp request: the principal the auth middleware already validated
    (carried on the ASGI scope state), or — if that didn't propagate — re-validated from the bearer header
    on the scope (robust fallback). None under presence auth or if absent.

    ONE validation, from which every derived value is read. The subject and the session must describe the
    same caller, so deriving them from two independent `validate_token` calls would both double the work on
    the fallback path (a second signature check, or a second lookup for a DB-backed provider) and let them
    diverge if a provider's validation isn't deterministic.
    """
    principal = (scope.get("state") or {}).get("principal")
    if principal is not None:
        return principal
    for key, value in scope.get("headers", []):
        if key == b"authorization" and value[:7].lower() == b"bearer ":
            return auth.validate_token(value[7:].strip().decode("latin-1"))
    return None


def _actor_from_scope(scope: dict, auth: AuthProvider) -> str | None:
    """The authenticated user's subject for this /mcp request, or None. Thin wrapper over
    `_principal_from_scope` — `handle_mcp` resolves the principal once and reads both values off it, so this
    exists for callers that want only the subject."""
    return getattr(_principal_from_scope(scope, auth), "subject", None)


def _normalized_session(principal: object | None) -> str | None:
    """A principal's session id, or None — enforcing the `str | None` shape the contextvar promises.

    `AuthProvider` is a `@runtime_checkable` Protocol, so conformance is method presence only: a
    third-party provider's principal may expose no `session_id` at all, or a non-string one. Without this
    an unexpected type would reach `_session_ctx` and, through it, consumers using the value as a key.
    Same rule as `JwtAuthProvider.validate_token`'s read — non-string or blank is "no session"."""
    sid = getattr(principal, "session_id", None)
    return sid if isinstance(sid, str) and sid.strip() else None


def _session_from_scope(scope: dict, auth: AuthProvider) -> str | None:
    """The session id for this /mcp request, or None. Thin wrapper over `_principal_from_scope`, the mirror
    of `_actor_from_scope`."""
    return _normalized_session(_principal_from_scope(scope, auth))


def _org_id_from_scope(scope: dict) -> str | None:
    """The resolved org id for this /mcp request — the org the auth middleware attached to the ASGI scope
    state (`request.state.org`). None under presence auth / single-tenant, where the tool layer falls back
    to AGAMI_ORG_ID / 'local'. Keeps the per-process model cache tenant-safe (ACE-045): cache entries key on
    this, so under a multi-tenant resolver one org never gets another's cached model."""
    org = (scope.get("state") or {}).get("org")
    return getattr(org, "id", None)


# The brand assets (logo, provider icons, favicon) served at /static — packaged alongside this module.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_FAVICON_FILE = _STATIC_DIR / "logo_icon.png"


def _build_auth_provider() -> AuthProvider:
    """Pick the token validator. Presence (any non-empty bearer) is the local OSS default ONLY when
    no signing secret is configured *at all*. If `AGAMI_SIGNING_SECRET` is present — even empty or
    too weak — that signals intent to run real JWT auth, so we validate it now (fail fast at
    construction) rather than silently downgrade a misconfigured hosted deploy to presence."""
    if "AGAMI_SIGNING_SECRET" in os.environ:
        from oauth_server import JwtAuthProvider, _signing_secret

        _signing_secret()  # raises on an empty/weak secret — no insecure fallback on misconfig
        return JwtAuthProvider()
    return PresenceAuthProvider()


def _build_org_resolver() -> SingleTenantOrgResolver:
    """The OSS default tenancy: single-tenant, one configured org. The id is the F14 minted uuid
    resolved by `tools.resolved_org_id()` (AGAMI_ORG_ID env -> organization.yaml -> "local") — the SAME
    resolution the deploy stamp uses, so writes and reads agree. Multi-tenant is a future change at
    the *schema* layer plus an authz check — not a resolver swap, so the seam lives here now."""
    org_id = resolved_org_id()
    # Load-bearing for a hand-rolled DB-only deploy: log the resolved id + where it came from, so an
    # operator can see "org_id=local (default)" and realize organization.yaml/AGAMI_ORG_ID isn't reaching the
    # server (see F14's documented residual risk).
    source = (
        "env"
        if os.environ.get("AGAMI_ORG_ID", "").strip()
        else ("default" if org_id == "local" else "organization.yaml")
    )
    _log.info("single-tenant org_id=%s (source=%s)", org_id, source)
    return SingleTenantOrgResolver(Org(id=org_id))


def default_adapters() -> Adapters:
    """The OSS default adapters bundled for the composition root (env-driven auth + org). Used only by
    `create_app(adapters=None)` — the HTTP server's defaults. `create_app` wires `auth_provider` +
    `org_resolver` into the request path and registers `executor`; `activity_sink` is
    carried on the container for consumers.

    `executor=BUILTIN_EXECUTOR` (ACE-028) makes the HTTP server run execution **in-process** by default
    — no per-query subprocess fork, no CSV round-trip — behind the same guard (AH-012). The forking
    subprocess is the wrong shape for a long-running server; the local stdio path (`mcp_harness`) and
    the `python -m execute_sql` CLI never build these adapters, so they keep the subprocess isolation."""
    return Adapters(
        activity_sink=FileActivitySink(),
        org_resolver=_build_org_resolver(),
        auth_provider=_build_auth_provider(),
        executor=BUILTIN_EXECUTOR,
    )


def public_base_url() -> str:
    """The explicit public base URL. Required — discovery + redirect URIs are built from it and it
    can't be inferred behind a proxy/LB (the OAUTH_ISSUER_URL gotcha)."""
    url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "PUBLIC_BASE_URL must be set — it backs OAuth/MCP discovery + the WWW-Authenticate "
            "resource URL and can't be auto-detected behind a proxy/LB."
        )
    return url


def _resource_metadata_url(base: str) -> str:
    return f"{base}/.well-known/oauth-protected-resource"


def _unauthenticated(base: str, request: Request | None = None) -> Response:
    """401 + WWW-Authenticate pointing at the discovery doc — the challenge that starts OAuth.

    Content-negotiated: a browser (Accept: text/html) gets a branded "this is an MCP endpoint" page so
    a human who pastes the URL isn't met with raw JSON; claude.ai (JSON / event-stream Accept) gets the
    JSON body it expects. **Same 401 status + WWW-Authenticate header either way**, so the machine
    challenge that bootstraps OAuth is unchanged — only the body differs."""
    headers = {
        "WWW-Authenticate": f'Bearer resource_metadata="{_resource_metadata_url(base)}"',
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "WWW-Authenticate",
    }
    if request is not None and "text/html" in (request.headers.get("accept") or ""):
        return HTMLResponse(admin.mcp_landing_body_html(base), status_code=401, headers=headers)
    return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=headers)


# Only the OAuth-discovery endpoints are reachable unauthenticated (the client probes them before
# it has a token). Scoped to these exact prefixes — NOT a blanket `/.well-known/` skip — so the
# open surface is exactly the routes we serve, not "anything starting with /.well-known/".
_PUBLIC_PREFIXES = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
)


# The OAuth flow endpoints are pre-auth by definition — the user has no bearer token yet (they're
# obtaining one). They enforce their own validation (credential check, PKCE, single-use codes,
# redirect allow-listing), so they're public at the transport layer.
_OAUTH_PATHS = (
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/register",
    "/oauth/oidc/start",
    "/oauth/oidc/callback",
)


def _is_public_path(path: str) -> bool:
    """True for the surface reachable without an MCP bearer token: the OAuth discovery routes + flow
    endpoints, the static brand assets, the root landing, its favicon, and the `/admin/*` pages. Discovery and
    static use boundary matching (they have suffix/sub-path routes, and a bare `startswith` would let
    `/.well-known/oauth-protected-resource-x` or `/static-x` slip through); the OAuth + admin
    endpoints are matched *exactly* (only those exact paths are routed), so a future
    `/oauth/token/...` or `/admin/...` route can't inherit public access by accident.

    Static + admin are "public" only at the *bearer* layer: assets are genuinely public, and the
    admin pages run their OWN session-cookie auth (see `admin.current_admin`) — they are not
    unguarded, just guarded by a different credential than the MCP token."""
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES):
        return True
    if path == "/static" or path.startswith("/static/"):
        return True
    if path == "/" or path == "/favicon.ico":
        return True
    return path in _OAUTH_PATHS or path in admin.ADMIN_PATHS or path in onboarding.PUBLIC_PATHS


class _AuthMiddleware(BaseHTTPMiddleware):
    """Gate every request on a Bearer token via the configured `AuthProvider` (a real JWT validator
    in the hosted OAuth path, or bearer-presence locally); the discovery + OAuth-flow endpoints stay
    open. On a request that passes auth, resolve the single-tenant org and attach it to
    request.state.org — the explicit single-tenant contract + the multi-tenant seam."""

    def __init__(self, app, resolver: OrgResolver, auth: AuthProvider) -> None:
        super().__init__(app)
        self._resolver = resolver
        self._auth = auth

    async def dispatch(self, request: Request, call_next):
        if _is_public_path(request.url.path):
            return await call_next(request)
        authz = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        # Require the Bearer scheme specifically (not just any Authorization header), then hand the
        # token to the configured provider — a real JWT validator in the hosted OAuth path, or
        # bearer-presence locally.
        if not authz.lower().startswith("bearer "):
            return _unauthenticated(public_base_url(), request)
        principal = self._auth.validate_token(authz[7:].strip())
        if principal is None:
            return _unauthenticated(public_base_url(), request)
        # Carry the validated principal on the ASGI scope state so the /mcp endpoint can stamp the
        # activity log with the actor (the tool dispatch doesn't get the request).
        request.state.principal = principal
        # Resolve the org for this request. Single-tenant returns the one configured org regardless
        # of context; nothing downstream consumes it yet (tools key on `datasource`), so this asserts
        # the contract and reserves the seam — it does not add org-scoped behavior.
        try:
            request.state.org = self._resolver.resolve_org(request)
        except PermissionError:
            # A resolver may refuse a principal it cannot place. The refusal has to land as a clean 403,
            # not the 500 an uncaught raise would give (which leaks a traceback under debug). Authentication
            # already passed by here — this is "you are who you say, but you have no org", so 403, not 401.
            # The OSS resolver never raises, so this path is inert single-tenant.
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        return await call_next(request)


class _NormalizeMcpSlash:
    """Rewrite the exact path ``/mcp`` → ``/mcp/`` before routing.

    Starlette's ``Mount("/mcp", …)`` answers a request to the bare ``/mcp`` (no trailing slash) with a
    307 redirect to ``/mcp/``. claude.ai posts the connector URL ``{base}/mcp`` and does **not** follow
    that redirect, so the connector errors right after login. ``handle_mcp`` ignores the sub-path, so
    normalizing here is loss-free. Pure ASGI (not ``BaseHTTPMiddleware``) so it edits the scope path
    with no request-body buffering; only the exact ``/mcp`` is touched — ``/mcp/…`` and every other
    route pass through untouched. Placed outermost so it runs before auth + routing; the fix lives in
    the app, so it also covers the Caddy-less ``cloud-run`` profile (no proxy rewrite needed).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.app(scope, receive, send)


async def _protected_resource(request: Request) -> JSONResponse:
    """RFC 9728 — tells the client where the authorization server is."""
    base = public_base_url()
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def _auth_server(request: Request) -> JSONResponse:
    """RFC 8414 — the authorization-server metadata advertising the authorize/token/register
    endpoints (served by `oauth_server`) so the client can run the OAuth flow."""
    base = public_base_url()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _with_caller_identity(result_text: str, actor: str | None) -> str:
    """Stamp the authenticated caller's identity onto a tool result that is a JSON object, so the
    model resolving "my"/"me" in the next turn has somewhere to read who is asking (ACE-118) — the
    audit log was the only consumer of `_actor_ctx` before this.

    Best-effort and additive ONLY: some existing test tools (and any third-party consumer's tool)
    return a bare string, not JSON, and this must never corrupt that. A body whose top level is not
    a JSON object — a bare string, or a JSON array — always returns `result_text` unchanged; an
    array has no field to add without changing the shape its consumer reads. With no actor, a body that also carries
    no `caller_identity` key returns unchanged too — but one that DOES carry that key is still
    reserialized with it stripped; see the overwrite-guarantee paragraph below.

    Handles a JSON object followed by trailing non-JSON text — `tool_get_datasource_schema` builds
    its response this way, a JSON payload with a "## Domain context" / "## USER_MEMORY.md" markdown
    suffix appended after it — by parsing only the leading JSON object and re-appending whatever
    followed it, untouched.

    **`tool_get_prompt_examples`'s local file-serving branch (no DB configured) is NOT handled, and
    this is not merely deferred scope — it is a mode where the gap cannot matter.** That branch
    returns a whole area's curated library as one YAML/Markdown document with no leading JSON
    object at all, so there is no prefix here to stamp. But "no `AGAMI_DB_URL`" is exactly the
    condition for the local single-player transport (`mcp_harness.py`), which has no authentication
    at all, by design — its own docs state the trust boundary is the OS user account, because there
    is nothing to authenticate to. `_actor_ctx` is only ever set inside this module's own
    `handle_mcp`, which that transport never reaches. So the one branch this can't stamp is also
    the one branch where `actor` is always `None` regardless — there is no real identity being
    withheld from the model here, on either surface a self-hosted or hosted deployment actually
    uses (both require a database, which is exactly what routes `get_prompt_examples` away from
    this branch and onto the one that already carries identity correctly). Restructuring this
    branch's response shape to carry a field that would always be empty is not a fix (ACE-118
    review).

    `caller_identity` is a reserved, server-injected field: it is ALWAYS overwritten with the true
    authenticated value, even when the tool's own JSON body already carries that key — deferring to
    an existing value would let any tool whose own domain data happens to use this field name spoof
    the asker to the model, which is instructed to trust it unconditionally (ACE-118 review).

    **The overwrite guarantee holds even with no actor.** An unresolved principal means there is
    nothing to SET the field to, but a tool-supplied `caller_identity` still has to be REMOVED —
    returning the body unchanged would let that same spoofed key through precisely when there is no
    real identity to contradict it, which is the worst case for it to survive (Copilot review).
    """
    try:
        body = json.loads(result_text)
        suffix = ""
    except (TypeError, ValueError):
        try:
            body, end = json.JSONDecoder().raw_decode(result_text)
            suffix = result_text[end:]
        except (TypeError, ValueError):
            return result_text
    if not isinstance(body, dict):
        return result_text
    if actor is None:
        if "caller_identity" not in body:
            return result_text
        body = {k: v for k, v in body.items() if k != "caller_identity"}
    else:
        body["caller_identity"] = actor
    return json.dumps(body, indent=2) + suffix


def _declared_pages(registry: dict) -> dict[str, dict]:
    """Every `ui://` page the registry declares, by URI: its HTML and the tools that link to it.

    Checked here, in `build_server`, because `create_app` builds its server at construction: one
    chokepoint, so a malformed declaration fails composition naming its tool on both entry points,
    not later as a 500 inside `resources/read`."""
    pages: dict[str, dict] = {}
    for tool_name, meta in registry.items():
        if not isinstance(meta.get("app_only", False), bool):
            raise ValueError(f"extra tool {tool_name!r} app_only must be a bool")
        page = meta.get("page")
        if page is None:
            continue
        # Exactly these two keys. A page that could name a CSP relaxation, a domain or a device
        # permission could ask the host to let it reach the network; without them it runs in the
        # host's default sandbox, and the HTML itself need not be read to know that.
        if not isinstance(page, dict) or set(page) != {"uri", "html"}:
            raise ValueError(
                f"extra tool {tool_name!r} page may declare only uri and html "
                "(no csp, domain or permissions)"
            )
        uri, html = page["uri"], page["html"]
        if not isinstance(uri, str) or not uri.startswith("ui://"):
            raise ValueError(f"extra tool {tool_name!r} page uri must use the ui:// scheme")
        if not isinstance(html, str):
            raise ValueError(f"extra tool {tool_name!r} page html must be a str")
        # Two tools may share a page; they may not disagree about what it is, or which body a
        # client got would depend on registry order.
        declared = pages.setdefault(uri, {"html": html, "tools": []})
        if declared["html"] != html:
            raise ValueError(
                f"extra tool {tool_name!r} page {uri!r} differs from the html another tool "
                "declared for it"
            )
        declared["tools"].append(tool_name)
    return pages


def build_server(
    registry: dict | None = None,
    extra_instructions: str | None = None,
    visibility: Callable[[str], bool] | None = None,
    result_hook: Callable[[str, Mapping[str, Any], str], Mapping[str, Any] | None] | None = None,
):
    """A low-level MCP Server whose tool surface IS the given registry — list_tools / call_tool read
    from it, so HTTP advertises exactly what stdio does (no duplicate defs). Defaults to the shared
    `tools.TOOLS`; `create_app` passes a merged copy (base + a consumer's extra tools).

    `extra_instructions` is APPENDED to `server_instructions()` (never replaces it) and surfaced to the
    model in the MCP `initialize` and `server/discover` results — append-only so a consumer can add guidance but can't drop
    the base protocol's safety directives (e.g. the receipt-reporting rules). None = no-op.

    `visibility(tool_name) -> bool` narrows the surface PER REQUEST. None (the default) is exactly
    today's behaviour: the whole registry, listed and callable. It exists because a registry assembled
    once at composition time cannot answer "may THIS caller see this tool", and for a consumer running a
    server-side agent that is the difference between a control and a suggestion: the model decides what
    to call, so withholding the tool is the control and "it shouldn't call that" is not.

    An empty hook by design — this module decides nothing about who may see what. The predicate is the
    consumer's, and it reads the request context itself: this callable runs inside the request's task
    (see `handle_mcp`), so `_actor_ctx` / `_session_ctx` / `tools._current_org_ctx` are all live in it.

    SUBTRACTIVE ONLY. It filters the one shared registry; it never adds, renames, or reshapes a tool. A
    surviving tool's description and inputSchema pass through untouched, so a consumer cannot fork the
    surface into a private variant under cover of "visibility".
    """
    import jsonschema
    import mcp.types as mt
    import referencing
    from mcp.server import Server, ServerRequestContext
    from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
    from mcp.server.caching import CacheHint
    from mcp.shared.exceptions import MCPError

    registry = TOOLS if registry is None else registry
    # Applied to whatever registry is being served, consumer tools included: a conversation is
    # reconstructed from ALL of a turn's calls, so a tool exempt from the requirement would punch a
    # hole in exactly the grouping this enforces. No-op unless AGAMI_REQUIRE_THREAD_ID is set; see
    # `tools.require_thread_id` for why that gate exists and why the default is off.
    #
    # Before `_visible`, because this reshapes a schema while that one only filters names — running
    # it after would mean a hidden tool still had its schema rewritten, which is work for nothing.
    if thread_id_is_required():
        registry = require_thread_id(registry)
    pages = _declared_pages(registry)

    # One validator per tool, built once. `jsonschema.validate` re-checks the schema against its
    # metaschema on every call, which is ~2ms of pure repetition, and raises SchemaError mid-call for a
    # broken schema — an exception that is not a ValidationError and that SDK 2 would send as `str(e)`
    # on 2025-06-18. Checked here instead, so a consumer's malformed schema fails the build with its
    # name, the way `create_app` refuses any other malformed extra tool.
    #
    # An empty `Registry`, not jsonschema's default: the default one FETCHES a remote `$ref` with
    # `urlopen`. A tool schema is no licence for the server to reach the network, and an unresolvable
    # `$ref` then raises at call time, where `_on_call_tool` answers it as a crash.
    validators = {}
    for tool_name, meta in registry.items():
        schema = meta["inputSchema"]
        validator_cls = jsonschema.validators.validator_for(schema)
        try:
            validator_cls.check_schema(schema)
        except jsonschema.SchemaError as e:
            raise ValueError(
                f"tool {tool_name!r} inputSchema is not a valid JSON Schema: {e.message}"
            ) from e
        validators[tool_name] = validator_cls(schema, registry=referencing.Registry())

    def _visible(name: str) -> bool:
        """Applied at BOTH seams. Listing alone would leave an unlisted tool callable by name, which is
        worse than either control on its own — the surface would look narrowed while remaining open."""
        if visibility is None:
            return True
        try:
            return bool(visibility(name))
        except Exception:
            # A consumer's predicate that raises must not be an accidental grant. Refuse the tool and
            # keep serving; the alternative (propagating) turns one bad classification into a dead
            # transport for every caller.
            _log.exception("tool visibility predicate failed for %r; hiding the tool", name)
            return False

    # Appended, never replacing: the instructions carry the PII output rule, so replace-semantics
    # would let a consumer silently drop a safety directive. Resolved per build_server rather than
    # read off a module constant, so a hosted deployment does not ship the local privacy claim.
    instructions = server_instructions()
    if extra_instructions:
        instructions = f"{instructions}\n{extra_instructions}"

    def _ui_meta(entry: dict) -> dict | None:
        # From the registry entry, never from the visibility predicate: the link beside a tool is
        # part of what the consumer registered, and the schema a client reads is untouched by it.
        ui: dict = {}
        if entry.get("page") is not None:
            ui["resourceUri"] = entry["page"]["uri"]
        if entry.get("app_only"):
            ui["visibility"] = ["app"]
        return {"ui": ui} if ui else None

    def _described(names: list[str]) -> list:
        return [
            # `tool_description` states execute_sql's limits for THIS caller's organisation (#329).
            # That is core describing its own tool per request, not the subtractive-only hook above
            # reshaping one: a consumer's description passes through it untouched.
            mt.Tool(
                name=name,
                description=tool_description(name, registry[name]["description"]),
                input_schema=registry[name]["inputSchema"],
                meta=_ui_meta(registry[name]),
            )
            for name in names
        ]

    async def _on_list_tools(
        ctx: ServerRequestContext, params: mt.PaginatedRequestParams | None
    ) -> mt.ListToolsResult:
        # The visibility predicate runs HERE, in the request task, before any hop: that is the context
        # its contract promises a consumer, who may read request-task state from it. Only the
        # descriptions go off the loop, for ACE-048's reason: the limits provider is the consumer's
        # code and usually a database read, and on the loop one slow read would stall every in-flight
        # request. `run_blocking` copies the request context, so the organisation is still set in the
        # worker. Without a provider nothing here blocks, so the hop would be a thread per listing for
        # nothing.
        names = [name for name in registry if _visible(name)]
        if not has_statement_limits_provider():
            return mt.ListToolsResult(tools=_described(names))
        return mt.ListToolsResult(tools=await run_blocking(_described, names))

    def _page_visible(page: dict) -> bool:
        # A page is as visible as the most visible tool that links it. Checked on the loop, in the
        # request task, as `_on_list_tools` checks tools, so the predicate sees the same context.
        return any(_visible(tool_name) for tool_name in page["tools"])

    async def _on_list_resources(
        ctx: ServerRequestContext, params: mt.PaginatedRequestParams | None
    ) -> mt.ListResourcesResult:
        return mt.ListResourcesResult(
            resources=[
                mt.Resource(uri=uri, name=uri, mime_type=APP_MIME_TYPE)
                for uri, page in pages.items()
                if _page_visible(page)
            ]
        )

    async def _on_read_resource(
        ctx: ServerRequestContext, params: mt.ReadResourceRequestParams
    ) -> mt.ReadResourceResult:
        page = pages.get(params.uri)
        # A hidden page answers as an undeclared one, byte for byte, for the reason a hidden tool
        # answers `Unknown tool`: a different answer would say the page exists but is withheld.
        if page is None or not _page_visible(page):
            raise MCPError(code=mt.INVALID_PARAMS, message=f"Unknown resource: {params.uri}")
        return mt.ReadResourceResult(
            contents=[
                mt.TextResourceContents(uri=params.uri, mime_type=APP_MIME_TYPE, text=page["html"])
            ]
        )

    def _crashed(name: str) -> mt.CallToolResult:
        """The one answer to a call that raised, whatever raised. The exception's own words are an
        enumeration channel — a driver's error can name a column the caller never sent (migration
        016 records exactly such a hint) — so they go to the log, and the client learns only that
        the call failed."""
        return mt.CallToolResult(
            content=[mt.TextContent(type="text", text=f"Error executing tool {name}")],
            is_error=True,
        )

    def _structured(name: str, arguments: dict, result_text: str) -> dict | None:
        """The result hook's object for this call, or None. Never raises: the hook adds output, so
        its failure must not become the call's failure — the text it would have sat beside is
        already a complete answer."""
        try:
            returned = result_hook(name, arguments, result_text)
            if returned is None:
                return None
            if not isinstance(returned, Mapping):
                raise TypeError(f"returned {type(returned).__name__}, not an object")
            structured = dict(returned)
            # Checked here, not left to the SDK: a value JSON cannot carry would fail while the
            # answer is being written, after this call has already been recorded as a success.
            json.dumps(structured)
        except Exception:
            _log.warning(
                "tool %r: the result hook failed; answered without structuredContent",
                name,
                exc_info=True,
            )
            return None
        return structured

    async def _on_call_tool(
        ctx: ServerRequestContext, params: mt.CallToolRequestParams
    ) -> mt.CallToolResult:
        # `or {}` as SDK 1.x did before anything else saw them, so the schema check, the handler and
        # the audit row all read the same value they always have.
        name, arguments = params.name, params.arguments or {}
        meta = registry.get(name)
        # A hidden tool answers as an ABSENT one, not as a refused one: the same `Unknown tool` a typo
        # gets. Distinguishing them would turn the list into an oracle — a caller could enumerate what
        # exists but is withheld, which is the fact hiding it was meant to keep. Raised as the SDK's
        # own protocol error, `-32602`, which is what the stdio harness answers too (REQ-002): any
        # other exception type becomes a generic internal error on 2026-07-28, which would make a
        # hidden tool read as a crash.
        if meta is None or not _visible(name):
            raise MCPError(code=mt.INVALID_PARAMS, message=f"Unknown tool: {name}")
        # SDK 1.x checked the arguments against `inputSchema` before the handler ran; SDK 2's
        # low-level Server does not, and `AGAMI_REQUIRE_THREAD_ID` is enforced by nothing but the
        # schema (`tools.require_thread_id`). Rebuilt here with 1.x's exact text, so a client sees the
        # refusal it always did. AFTER the visibility check, unlike 1.x, whose check ran first: a
        # hidden tool must answer `Unknown tool` whatever its arguments, not its own schema's
        # complaint. Outside the recorded scope, so a refusal here writes no row, as it never did.
        #
        # `best_match`, as `jsonschema.validate` picks, so the message names the same error it did.
        # Anything else raised while validating (an unresolvable `$ref`) is the schema's fault, not
        # the caller's: logged, and answered as a crash, so the reason stays off the wire.
        try:
            error = jsonschema.exceptions.best_match(validators[name].iter_errors(arguments))
        except Exception:
            _log.exception("tool %r: validating the arguments failed", name)
            return _crashed(name)
        if error is not None:
            return mt.CallToolResult(
                content=[
                    mt.TextContent(type="text", text=f"Input validation error: {error.message}")
                ],
                is_error=True,
            )
        try:
            return await _recorded_call(name, arguments, meta)
        except Exception:
            # Only a failure while recording the call reaches here — the audit write, or reading or
            # resetting the typed outcome around it: `_recorded_call` answers a raising handler
            # itself. The call still fails (ACE-097), and it fails the same way a crash does, because
            # SDK 2 would otherwise send `str(e)` on 2025-06-18, and a store's error text says as
            # much about the deployment as a driver's does. Logged here even when the handler also
            # raised and was logged below: two failures, one record each.
            _log.exception(
                "tool %r: recording the call failed; the call is answered as failed", name
            )
            return _crashed(name)

    async def _recorded_call(name: str, arguments: dict, meta: dict) -> mt.CallToolResult:
        # Record every tool call to the admin activity log — timed, attributed to the authenticated
        # actor, never allowed to break the tool (logging is best-effort + double-guarded).
        started = time.monotonic()
        result_text = None
        crash: Exception | None = None
        handler_ctx = contextvars.copy_context()
        # Cleared inside the context we own, before the handler can run. `copy_context()`
        # copies whatever is current, so a verdict published by an earlier call would be
        # inherited here and read back as THIS tool's outcome — and a tool that never
        # reaches `execute_guarded` has nothing else to clear it.
        handler_ctx.run(reset_typed_outcome)
        try:
            # Run the tool handler OFF the event loop. The heavy handlers block for the whole query —
            # execute_sql runs it under the executor's own bounds (the per-statement budget, plus the
            # supervisor's slack on the fork path) and hits the warehouse — so on the loop a single
            # slow query would freeze every other in-flight request. This completes ACE-048 (which
            # off-loaded the KDF/OIDC/audit calls but left the handler on the loop).
            # `run_blocking` (anyio.to_thread) copies the request context into the worker thread, so
            # the org-scoped model cache (ACE-045, read via `_current_org_ctx`) stays tenant-correct.
            # Run the handler inside a context THIS frame owns, so the classified outcome it
            # publishes can be read back afterwards (ACE-098). `run_blocking` hands the worker a
            # copy of the current context, and a `ContextVar.set` inside a copy is invisible here —
            # verified, not assumed. Giving the handler a `Context` object we hold is what makes the
            # verdict readable at the audit write below, instead of it having to `json.loads` the
            # body this call is about to return.
            #
            # The caller-identity stamp (ACE-118) is folded into THIS SAME offloaded call rather than
            # applied as a separate synchronous step after `run_blocking` returns. `_with_caller_identity`
            # does its own `json.loads`/`json.dumps` round-trip over the full result body, which is exactly
            # the kind of blocking work ACE-048 moved the handler off the loop to avoid — doing it
            # synchronously here would silently reintroduce that regression for every call. `actor` is read
            # on the loop (same as the audit write below) and closed over, so the worker thread does no
            # contextvar reads of its own.
            actor = _actor_ctx.get()

            def _run_and_stamp() -> str:
                return _with_caller_identity(meta["handler"](arguments), actor)

            result_text = await run_blocking(handler_ctx.run, _run_and_stamp)
            structured = None
            if result_hook is not None:
                # Off the loop for the handler's reason — the hook is the consumer's code — and in
                # the handler's own context, so the actor, session and organisation are all live.
                structured = await run_blocking(
                    handler_ctx.run, _structured, name, arguments, result_text
                )
            # None is the field's default, so without a hook the answer is built exactly as before.
            return mt.CallToolResult(
                content=[mt.TextContent(type="text", text=result_text)],
                structured_content=structured,
            )
        except Exception as exc:
            # Logged here, once, with its traceback: this is the only place the reason is kept for
            # an operator, now that it no longer reaches the client. (If the audit write then fails
            # too, `_on_call_tool` logs that as its own record.)
            crash = exc
            _log.exception("tool %r raised", name)
            return _crashed(name)
        finally:
            # The per-call audit write opens a fresh Store + INSERT + close; run it off the event loop so
            # it doesn't add DB latency to every tool call on the loop (ACE-048). `_actor_ctx.get()` is read
            # here (on the loop) and passed in.
            #
            # NOT wrapped (ACE-097). This `try` held an `except Exception: pass`, the second of the
            # two swallows that made a lost audit row invisible: it caught whatever
            # `_record_tool_call` raised, and `_record_tool_call` swallowed internally so it never
            # raised anything for this to catch. Each made the other unobservable, which is why
            # removing either alone changed nothing and neither was ever removed.
            #
            # `record_tool_call` decides what a failure means, by deployment: served, it raises and
            # the call fails; local, it warns and returns. That decision belongs there, beside the
            # write, rather than being pre-empted here by a transport that cannot tell the two
            # deployments apart. Raising from a `finally` replaces the handler's own result, which is
            # the intended outcome: a call whose record was lost must not read as a success. The
            # caller, `_on_call_tool`, turns that raise into the same failed answer a crash gets.
            await run_blocking(
                record_tool_call,
                name=name,
                arguments=arguments,
                result_text=result_text,
                execution_ms=int((time.monotonic() - started) * 1000),
                actor=_actor_ctx.get(),
                raised=crash is not None,
                # The reason the client no longer gets (ACE-152), kept for the operator instead.
                error_detail=f"{type(crash).__name__}: {crash}" if crash is not None else None,
                # The classified outcome, when the handler produced one (ACE-098). Empty for every
                # tool that does not speak the Envelope, which means "derive it the way you always
                # have" — so those tools keep the body parse and nothing about them changes.
                **typed_outcome_overrides(handler_ctx),
            )

    # A minute, and private. Private because both lists are per caller: the visibility predicate and
    # execute_sql's per-organisation limits decide what `tools/list` says, so a result cached for one
    # authorization must not be served to another. A minute because a deployment's tool surface
    # changes on a restart, not per request, and a client re-listing on every turn is pure cost.
    # `server/discover` carries the instructions a 2026-07-28 client reads in place of `initialize`;
    # the SDK's default handler already returns `instructions` below, so it needs only the hint.
    # Sent on 2026-07-28 only: the 2025-06-18 results have no fields for it.
    hint = CacheHint(ttl_ms=60_000, scope="private")
    cache_hints = {"tools/list": hint, "server/discover": hint}
    # The resource handlers are registered only when a page is declared, because the SDK advertises
    # the `resources` capability from whether they are: with no page, a client sees exactly the
    # server it saw before pages existed, "method not found" included. Pages follow the tools'
    # visibility, so their answers are per caller too, and get the same private hint.
    if pages:
        cache_hints |= {"resources/list": hint, "resources/read": hint}
    server = Server(
        SERVER_NAME,
        version=server_version(),
        instructions=instructions,
        cache_hints=cache_hints,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources if pages else None,
        on_read_resource=_on_read_resource if pages else None,
    )
    if pages:
        server.extensions[EXTENSION_ID] = {}
    return server


def _is_loopback(base: str) -> bool:
    """Whether this is a local address, where plain http is safe and is the only thing that works.

    **Not a relaxation of the TLS rule — the reason for that rule does not apply here.** It exists
    because a browser drops a `Secure` cookie sent over http, which would silently break the admin
    session. Browsers make a specific exception for loopback: `http://localhost` is a *secure
    context* by the W3C definition, so `Secure` cookies are kept and everything the rule protects
    still holds.

    It is also the only thing that works. Identity providers permit a plain-http redirect **only**
    on loopback, for exactly the same reason, so a developer signing in against a real provider from
    their own machine has no https option to choose instead.

    The host is parsed rather than matched as a prefix: `http://localhost.example.com` is somebody
    else's domain, and `startswith("http://localhost")` would have said yes to it.
    """
    from urllib.parse import urlsplit

    if not base.startswith("http://"):
        return False
    return urlsplit(base).hostname in ("localhost", "127.0.0.1", "::1")


def _transport_security(base: str) -> "TransportSecuritySettings":
    """The Host and Origin values `/mcp` accepts, derived from `PUBLIC_BASE_URL` (ACE-152).

    DNS rebinding: a page on some other name that resolves to this server would otherwise reach
    `/mcp` from a victim's browser. The SDK refuses a foreign `Host` with 421 and a foreign `Origin`
    with 403; an absent `Origin` passes, so a server-to-server client (claude.ai) is unaffected, while
    a browser client served from any origin but `PUBLIC_BASE_URL` is refused.

    The allowed host is the base URL's own host, the one name the discovery documents already
    publish. On a loopback base URL both spellings of loopback are also allowed, on any port: a
    developer's browser, curl and a port-forward do not agree on which name they send, and nothing
    else can resolve to a loopback address.

    Normalised, not copied from the netloc, because the SDK compares by exact string while a client
    sends the host lowercased and usually without the default port. `https://Demo.Example.com` or
    `https://demo.example.com:443` copied verbatim would answer 421 to every call from every client.
    For the same reason, at the default port both spellings are allowed, `host` and `host:443`: they
    are one origin, and which one arrives is the client's choice rather than the operator's.
    """
    from urllib.parse import urlsplit

    from mcp.server.transport_security import TransportSecuritySettings

    parts = urlsplit(base)
    scheme = parts.scheme.lower()
    # `hostname` is already lowercased and unbracketed; an IPv6 literal needs its brackets back to
    # be a Host value again.
    hostname = parts.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = {"http": 80, "https": 443}.get(scheme)
    if parts.port is None or parts.port == default_port:
        hosts = [hostname, f"{hostname}:{default_port}"]
    else:
        hosts = [f"{hostname}:{parts.port}"]
    origins = [f"{scheme}://{host}" for host in hosts]
    # Loopback is decided by the host alone, not `_is_loopback`: that one is true only over plain http,
    # because it answers the TLS-cookie rule, while `https://localhost` (local TLS) is just as much the
    # developer's own machine, and without the aliases `127.0.0.1` would answer 421 there.
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        for name in ("localhost", "127.0.0.1", "[::1]"):
            hosts += [name, f"{name}:*"]
            origins += [f"{scheme}://{name}", f"{scheme}://{name}:*"]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def create_app(
    extra_tools: dict | None = None,
    adapters: Adapters | None = None,
    extra_instructions: str | None = None,
) -> Starlette:
    """The ASGI app + the composition factory: the `.well-known` discovery routes + the
    streamable-HTTP MCP endpoint at /mcp, behind the auth middleware. Merges `extra_tools` over a
    COPY of the shared TOOLS (never mutating the global) and wires the `adapters` into the request
    path (auth + org resolution; OSS defaults when None). `create_app()` with no args == the
    historical `build_app()` behavior.

    Reusing an existing tool name in `extra_tools` overrides that tool in this app's registry copy —
    intentional at the composition root (the caller opts in explicitly). `tools.register` is the
    guarded path that refuses a duplicate name.

    `extra_instructions` is APPENDED to the base MCP instructions and surfaced to the model via the
    MCP `initialize` and `server/discover` results (never replaces the base protocol — see
    `build_server`). None = no-op."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    # Fail fast at construction if PUBLIC_BASE_URL is unset — not per-request inside the middleware
    # (where the RuntimeError would surface as a 500, leaking a traceback under debug). Anything that
    # builds the app via --factory / an embedding harness gets a clear error up front.
    base = public_base_url()
    # TLS is mandatory: claude.ai's OAuth and the Secure admin session cookie both require https. A
    # plain-http PUBLIC_BASE_URL would silently break the admin login (the browser drops a Secure
    # cookie), so fail fast with a clear message instead. (Set this to the public https URL even when
    # TLS terminates at a proxy — the browser↔proxy hop is what must be https.)
    if not base.startswith("https://") and not _is_loopback(base):
        raise RuntimeError(
            "PUBLIC_BASE_URL must be https:// (OAuth + the Secure admin cookie need TLS). "
            "http://localhost is the one exception, for local development."
        )
    bootstrap_paths()
    adapters = adapters or default_adapters()
    auth_provider = adapters.auth_provider
    # AH-012: register the composition-root executor (None = the default subprocess path). Behind the
    # shared guard in `tool_execute_sql`; a hosted consumer injects a pooled/RBAC/tunnel executor here.
    set_injected_executor(adapters.executor)
    # #329: register the per-organisation statement-limits provider the same way, and unconditionally
    # for the same reason — the adapters are the composition root, so an app built without one must not
    # inherit a provider an earlier app in the same process installed.
    set_statement_limits_provider(getattr(adapters, "statement_limits", None))
    # Validate consumer-supplied tools up front so a malformed entry fails at construction with a
    # clear error, not later as a KeyError/500 inside tools/list or tools/call.
    for tool_name, meta in (extra_tools or {}).items():
        if not isinstance(meta, dict) or not {"handler", "description", "inputSchema"} <= set(meta):
            raise ValueError(
                f"extra tool {tool_name!r} must be a dict with handler, description, inputSchema"
            )
        if not callable(meta["handler"]):
            raise ValueError(f"extra tool {tool_name!r} handler must be callable")
    # Merge the consumer's extra tools over a COPY of TOOLS — the module global is never mutated.
    registry = {**TOOLS, **(extra_tools or {})}
    session_manager = StreamableHTTPSessionManager(
        # `tool_visibility` is read from the adapters rather than taken as a `create_app` parameter, so it
        # rides the seam a consumer already swaps (`replace(default_adapters(), …)`) instead of adding a
        # second, parallel way to configure the surface. None on the OSS defaults ⇒ unchanged behaviour.
        app=build_server(
            registry,
            extra_instructions=extra_instructions,
            visibility=getattr(adapters, "tool_visibility", None),
            result_hook=getattr(adapters, "tool_result_hook", None),
        ),
        json_response=True,
        stateless=True,
        # Checked on `/mcp` only: the discovery and OAuth routes answer whatever host asked, because
        # they are what a client reads before it knows the server's name.
        security_settings=_transport_security(base),
    )

    async def handle_mcp(scope, receive, send):
        # Set the actor + resolved org for this request's tool calls, then run the MCP dispatch in the same
        # task so the contextvars reach `_call_tool` and the per-process model cache. Prefer what the auth
        # middleware attached to the scope state; the actor falls back to re-validating the bearer.
        # Resolve the caller ONCE: the subject and the session must describe the same principal, and on the
        # fallback path two lookups would mean validating the same bearer twice.
        principal = _principal_from_scope(scope, auth_provider)
        token = _actor_ctx.set(getattr(principal, "subject", None))
        org_token = _current_org_ctx.set(_org_id_from_scope(scope))
        session_token = _session_ctx.set(_normalized_session(principal))
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _actor_ctx.reset(token)
            _current_org_ctx.reset(org_token)
            _session_ctx.reset(session_token)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        # Say the posture out loud before serving anything (ACE-101). `AGAMI_GOVERNANCE_ENFORCED`
        # defaults OFF, so a server can be brought up with table scope, column scope, the star ban and
        # the engine-mismatch check all inert, which is a supported deployment and a surprising one.
        # The only other place it is visible is the absence of a refusal, and nobody reads an absence.
        #
        # Here rather than in `create_app`'s body because this runs when the process SERVES, while the
        # body also runs for every embedding harness and every test that builds an app. Once per
        # worker process, not once per deployment: `main()` runs uvicorn with `WORKERS=N` and uvicorn
        # re-invokes the factory in each child, so N workers legitimately log N lines.
        from execute_sql import _model_pass_disabled_by_env

        if _model_pass_disabled_by_env():
            # The SAME predicate every enforcement site reads, not a second spelling of it. An earlier
            # draft dropped the `_hosted()` half and so announced "the gates are off" on a file-mode
            # server where they were fully on. A warning that is wrong in the safe direction is still
            # a warning an operator learns to discount, which is the one thing this line cannot afford.
            #
            # The `_by_env` reader rather than the pinned one, and that distinction is the point of
            # its existing: this runs ONCE at startup, before any request, so there is no call for it
            # to be scoped to. Reading the pinned value would make a startup statement depend on
            # request-scoped state, which is None in a served process but need not be in an embedding
            # harness that ran a query before building an app. Found by driving the boot path after a
            # query in one process, where the stale pin made this line fire on all four postures.
            #
            # `is not enabled` rather than `is not set`: the variable is also not enabled when it IS
            # set to `false`, `0`, or a typo like `ture`, and telling an operator who typed something
            # that they typed nothing sends them to check env plumbing instead of their own value.
            _log.warning(
                "AGAMI_GOVERNANCE_ENFORCED is not enabled (value: %r): the semantic-model pass is "
                "OFF, so table scope, column scope, the SELECT * ban and the engine-mismatch check "
                "will not run. This server can read any table and any column the connecting role is "
                "granted, including columns excluded from the model, and can enumerate the schema "
                "through catalog relations. Read-only, dangerous-function and resource-bound "
                "protection are unaffected.",
                os.environ.get("AGAMI_GOVERNANCE_ENFORCED", ""),
            )
        # Heal the schema before serving: apply any pending migrations so freshly-deployed code never hits
        # an old DB shape (a column a migration adds, selected before it's applied, 500s the admin). This
        # is fail-closed — a failing migration propagates and aborts startup; a half-migrated DB never
        # serves. File-mode (no DB configured) has nothing to migrate.
        store = Store.from_env()
        if store is not None:
            try:
                applied = store.run_migrations()  # fail-closed: a bad migration aborts startup
                if applied:
                    _log.info("applied migrations: %s", ", ".join(applied))
                # Seed the configured admin (AGAMI_ADMIN_*) so a fresh deploy has someone who can sign in —
                # nothing else creates it. Create-if-absent + idempotent. BEST-EFFORT, unlike migrations: when
                # several instances boot together they can race on the admin INSERT (a UNIQUE violation); the
                # admin is seeded either way, so log + roll back + continue rather than aborting startup.
                try:
                    if user_store.seed_admin_from_env(store):
                        _log.info("seeded the configured admin")  # not the email — no PII in logs
                    store.commit()
                except Exception:  # noqa: BLE001 — a concurrent boot won the seed; not fatal
                    store.rollback()
                    _log.warning(
                        "admin seed skipped (already seeded or a concurrent boot won the race)"
                    )
            finally:
                store.close()
        async with session_manager.run():
            yield

    from oauth_server import authorize, oidc_callback, oidc_start, register, token

    async def _root(request: Request) -> Response:
        """The bare base URL in a browser → a branded landing (connector URL + admin link), not a 404."""
        return HTMLResponse(admin.landing_body_html(public_base_url()))

    async def _favicon(request: Request) -> Response:
        """The conventional icon path, answered from the same PNG the pages link.

        Routed because without it the path is not merely absent but *gated*: the bearer middleware
        challenges anything off its public list before routing can 404 it, so a client asking what
        this server looks like got a 401 and an auth prompt. That matters beyond tidiness — an MCP
        client showing a server in a connector list has no credentials to offer for an icon, and a
        deployment that redirects `/` elsewhere leaves this the only path left to ask on."""
        return FileResponse(_FAVICON_FILE, media_type="image/png")

    routes = [
        Route("/", _root, methods=["GET"]),
        Route("/favicon.ico", _favicon, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", _protected_resource),
        Route("/.well-known/oauth-protected-resource/{rest:path}", _protected_resource),
        Route("/.well-known/oauth-authorization-server", _auth_server),
        Route("/.well-known/oauth-authorization-server/{rest:path}", _auth_server),
        Route("/oauth/authorize", authorize, methods=["GET", "POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/oidc/start", oidc_start, methods=["GET"]),
        Route("/oauth/oidc/callback", oidc_callback, methods=["GET"]),
        *admin.routes(),
        *onboarding.routes(),
        Mount("/static", app=StaticFiles(directory=_STATIC_DIR), name="static"),
        Mount("/mcp", app=handle_mcp),
    ]
    middleware = [
        # Outermost: normalize the bare `/mcp` → `/mcp/` before routing so Starlette's Mount doesn't
        # 307-redirect it (claude.ai posts `{base}/mcp` and won't follow the redirect). See _NormalizeMcpSlash.
        Middleware(_NormalizeMcpSlash),
        Middleware(_AuthMiddleware, resolver=adapters.org_resolver, auth=auth_provider),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def build_app() -> Starlette:
    """Backwards-compatible entrypoint — `create_app()` with the OSS defaults and no extra tools, so
    the existing `python -m mcp_http` / `main()` path is unchanged."""
    return create_app()


def main() -> int:
    import uvicorn

    public_base_url()  # fail fast if unset
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    # Bind via the import-string factory (not a built instance) so `WORKERS=N` can fork N worker processes —
    # uvicorn re-imports `build_app` in each worker (ACE-048). Multi-worker is safe: session state is stateless
    # JWT + Postgres, boot migrations are guarded by a pg advisory lock (store.run_migrations), and the admin
    # seed tolerates a concurrent-boot race (lifespan). WORKERS defaults to 1 (unchanged single-process behaviour).
    workers = int(os.environ.get("WORKERS", "1"))
    uvicorn.run("mcp_http:build_app", factory=True, host=host, port=port, workers=workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
