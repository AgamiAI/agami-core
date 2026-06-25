#!/usr/bin/env python3
"""agami serve --http — the HTTP MCP transport (streamable-HTTP).

Advertises the **same** shared `tools.TOOLS` registry as the stdio entrypoint, but over a network
endpoint a remote client (claude.ai) can reach — plus the OAuth-discovery surface and a bearer
auth shim. This is the network product: unlike the stdio harness it binds a port, so it carries
auth — an unauthenticated request gets a `401` + `WWW-Authenticate` challenge, which is what
triggers the client's OAuth flow. The authorize page + real identity are a later concern; here we
emit the challenge and require a bearer token's presence (the OSS `AuthProvider` default).

Requires the **[server]** extra (the MCP SDK + ASGI stack). `PUBLIC_BASE_URL` must be set
explicitly — it backs the discovery documents + the `WWW-Authenticate` resource URL and cannot be
reliably auto-detected behind a proxy/LB.

    PUBLIC_BASE_URL=https://your-host python -m mcp_http
"""

from __future__ import annotations

import contextlib
import os

from oss_adapters import PresenceAuthProvider
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from tools import SERVER_INSTRUCTIONS, SERVER_NAME, TOOLS, bootstrap_paths, server_version

# Bearer-presence is the OSS default; real identity providers are a later feature.
_AUTH = PresenceAuthProvider()


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


class _AuthMiddleware(BaseHTTPMiddleware):
    """Require a bearer token's presence; the OAuth-discovery endpoints stay open. Everything else
    401s without a token."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)
        authz = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        # Require the Bearer scheme specifically (not just any Authorization header), then a
        # non-empty token. Real token validation is a later feature; this is presence-only.
        if not authz.lower().startswith("bearer "):
            return _unauthenticated(public_base_url())
        if _AUTH.validate_token(authz[7:].strip()) is None:
            return _unauthenticated(public_base_url())
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
    """RFC 8414 — the authorization-server metadata. The authorize/token endpoints it names are a
    later feature; this transport only advertises them so the client can begin discovery."""
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

    routes = [
        Route("/.well-known/oauth-protected-resource", _protected_resource),
        Route("/.well-known/oauth-protected-resource/{rest:path}", _protected_resource),
        Route("/.well-known/oauth-authorization-server", _auth_server),
        Route("/.well-known/oauth-authorization-server/{rest:path}", _auth_server),
        Mount("/mcp", app=handle_mcp),
    ]
    return Starlette(routes=routes, middleware=[Middleware(_AuthMiddleware)], lifespan=lifespan)


def main() -> int:
    import uvicorn

    public_base_url()  # fail fast if unset
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_app(), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
