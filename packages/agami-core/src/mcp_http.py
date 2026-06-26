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
import os

from oss_adapters import PresenceAuthProvider, SingleTenantOrgResolver
from ports import Org
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from tools import SERVER_INSTRUCTIONS, SERVER_NAME, TOOLS, bootstrap_paths, server_version


def _build_auth_provider():
    """Pick the token validator: the JWT provider when a signing secret is configured (the hosted
    OAuth path — only tokens this server issued are accepted), else bearer-presence (the no-secret
    local fallback, where any non-empty token passes)."""
    if os.environ.get("AGAMI_SIGNING_SECRET", "").strip():
        from oauth_server import JwtAuthProvider

        return JwtAuthProvider()
    return PresenceAuthProvider()


def _build_org_resolver() -> SingleTenantOrgResolver:
    """The OSS default tenancy: single-tenant, one configured org (id from AGAMI_ORG_ID, default
    "local"). Multi-tenant is a future change at the *schema* layer (rows key on datasource, not
    (org, datasource)) plus an authz check — not a resolver swap, so the seam lives here now."""
    org_id = os.environ.get("AGAMI_ORG_ID", "").strip() or "local"
    return SingleTenantOrgResolver(Org(id=org_id))


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


def _unauthenticated(base: str) -> JSONResponse:
    """401 + WWW-Authenticate pointing at the discovery doc — the challenge that starts OAuth."""
    return JSONResponse(
        {"error": "Not authenticated"},
        status_code=401,
        headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{_resource_metadata_url(base)}"',
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "WWW-Authenticate",
        },
    )


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
_OAUTH_PATHS = ("/oauth/authorize", "/oauth/token", "/oauth/register")


def _is_public_path(path: str) -> bool:
    """True for the discovery routes and the OAuth-flow endpoints — the surface reachable before a
    client has a token. Discovery uses boundary matching (it has `{rest:path}` suffix routes, and a
    bare `startswith` would let `/.well-known/oauth-protected-resource-x` skip auth); the OAuth
    endpoints are matched *exactly* (only those three paths are routed), so a future
    `/oauth/token/...` route can't inherit public access by accident."""
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES):
        return True
    return path in _OAUTH_PATHS


class _AuthMiddleware(BaseHTTPMiddleware):
    """Gate every request on a Bearer token via the configured `AuthProvider` (a real JWT validator
    in the hosted OAuth path, or bearer-presence locally); the discovery + OAuth-flow endpoints stay
    open. On a request that passes auth, resolve the single-tenant org and attach it to
    request.state.org — the explicit single-tenant contract + the multi-tenant seam."""

    def __init__(self, app, resolver: SingleTenantOrgResolver, auth) -> None:
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
            return _unauthenticated(public_base_url())
        if self._auth.validate_token(authz[7:].strip()) is None:
            return _unauthenticated(public_base_url())
        # Resolve the org for this request. Single-tenant returns the one configured org regardless
        # of context; nothing downstream consumes it yet (tools key on `datasource`), so this asserts
        # the contract and reserves the seam — it does not add org-scoped behavior.
        request.state.org = self._resolver.resolve_org(request)
        return await call_next(request)


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
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


def build_server():
    """A low-level MCP Server whose tool surface IS the shared registry — list_tools / call_tool
    read straight from `tools.TOOLS`, so HTTP advertises exactly what stdio does (no duplicate defs)."""
    import mcp.types as mt
    from mcp.server.lowlevel import Server

    server = Server(SERVER_NAME, version=server_version(), instructions=SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list:
        return [
            mt.Tool(name=name, description=meta["description"], inputSchema=meta["inputSchema"])
            for name, meta in TOOLS.items()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list:
        meta = TOOLS.get(name)
        if meta is None:
            raise ValueError(f"Unknown tool: {name}")
        return [mt.TextContent(type="text", text=meta["handler"](arguments or {}))]

    return server


def build_app() -> Starlette:
    """The ASGI app: the `.well-known` discovery routes + the streamable-HTTP MCP endpoint at /mcp,
    behind the bearer-presence auth middleware."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    # Fail fast at construction if PUBLIC_BASE_URL is unset — not per-request inside the middleware
    # (where the RuntimeError would surface as a 500, leaking a traceback under debug). Anything that
    # builds the app via --factory / an embedding harness gets a clear error up front.
    public_base_url()
    bootstrap_paths()
    session_manager = StreamableHTTPSessionManager(
        app=build_server(), json_response=True, stateless=True
    )

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with session_manager.run():
            yield

    from oauth_server import authorize, register, token

    routes = [
        Route("/.well-known/oauth-protected-resource", _protected_resource),
        Route("/.well-known/oauth-protected-resource/{rest:path}", _protected_resource),
        Route("/.well-known/oauth-authorization-server", _auth_server),
        Route("/.well-known/oauth-authorization-server/{rest:path}", _auth_server),
        Route("/oauth/authorize", authorize, methods=["GET", "POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/oauth/register", register, methods=["POST"]),
        Mount("/mcp", app=handle_mcp),
    ]
    middleware = [
        Middleware(_AuthMiddleware, resolver=_build_org_resolver(), auth=_build_auth_provider())
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def main() -> int:
    import uvicorn

    public_base_url()  # fail fast if unset
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_app(), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
