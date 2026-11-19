"""The response headers every route carries, applied in one layer rather than per handler.

A header set inside each handler is one forgotten handler away from a page without it, and the pages
that matter most here are the ones a person types a password into or approves a connection on. So
the transport wraps the whole app once, and a new route inherits the policy without being told to.

What it adds, and to what:

- **`Strict-Transport-Security` on every response, when the public URL is https.** Without it the
  first request a person makes by typing the hostname goes out over plain http and can be answered by
  anyone on the path. Decided from `PUBLIC_BASE_URL` rather than the request's scheme: TLS usually
  ends at a proxy, so the request this process sees is http even when the person's browser is on
  https. A loopback http deployment (local development) gets none, because a browser that has seen
  HSTS for `localhost` refuses plain http to every local server on that name for a year.
- **`X-Content-Type-Options: nosniff` and `Referrer-Policy` on every response.** Neither changes what
  a correct client does, and both are cheaper to apply everywhere than to reason about per route.
- **`Content-Security-Policy` and `X-Frame-Options` on every HTML page.** The OAuth sign-in, the admin
  sign-in and the admin console are all pages whose clicks grant something; framed by someone else's
  page, those clicks can be steered. Keyed on the content type rather than a list of paths so a page
  added later cannot be left out.

**A header the route already set is never replaced.** A route that knows better, such as a stricter
policy on an app it serves, keeps its own; this layer only fills what is missing.
"""

from __future__ import annotations

import base64
import hashlib

import ui

#: One year: long enough to cover the gap between visits, and the value browsers' preload lists ask
#: for. **No `includeSubDomains` and no `preload`.** This server is self-hosted under a hostname its
#: operator chose, and it cannot know what else lives under that name; deployed at a bare domain,
#: `includeSubDomains` would pin every sibling site under it to https for a year, including any that
#: still serve plain http. An operator who wants either can add it at their proxy, where that is known.
HSTS = "max-age=31536000"


def _sha256(source: str) -> str:
    digest = hashlib.sha256(source.encode()).digest()
    return f"'sha256-{base64.b64encode(digest).decode()}'"


def _script_src() -> str:
    """`'self'` plus a hash for each inline script and handler core renders, and nothing else.

    Hashes rather than nonces because the inline code is a set of constants: a hash is computed once,
    while a nonce would have to reach every page builder on every request. `'unsafe-hashes'` is what
    lets a hash cover an inline event handler; it widens nothing beyond the hashes listed.
    """
    sources = ["'self'", *(_sha256(js) for js in ui.INLINE_SCRIPTS)]
    if ui.INLINE_HANDLERS:
        sources += ["'unsafe-hashes'", *(_sha256(js) for js in ui.INLINE_HANDLERS)]
    return " ".join(sources)


#: The policy for core's own pages.
#:
#: `style-src` needs `'unsafe-inline'`: every page carries its stylesheet inline and many elements a
#: `style=` attribute, and styles cannot run code, so `script-src` is the directive that keeps an
#: injected script from running and it stays strict. Images, fonts and the logo are all served from
#: `/static`, so nothing else needs an origin. **There is no `form-action`**: the OAuth sign-in answers
#: its POST with a redirect to the connecting client's callback, and browsers apply `form-action` to
#: that redirect, so restricting it would break every sign-in from every client.
PAGE_CSP = (
    "default-src 'self'; "
    f"script-src {_script_src()}; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

#: On every response. Bytes, because they go straight into the ASGI message.
_EVERY_RESPONSE = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)

#: On every HTML page. `X-Frame-Options` repeats `frame-ancestors` for browsers that predate it.
_HTML_PAGE = (
    (b"content-security-policy", PAGE_CSP.encode()),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeaders:
    """Pure ASGI, so a streamed response (the MCP endpoint) passes through without being buffered."""

    def __init__(self, app, *, https: bool) -> None:
        self.app = app
        self._always = ((b"strict-transport-security", HSTS.encode()),) if https else ()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = self._with_defaults(list(message.get("headers", [])))
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _with_defaults(self, headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        present = {name.lower() for name, _ in headers}
        content_type = next((v for n, v in headers if n.lower() == b"content-type"), b"")
        wanted = [*self._always, *_EVERY_RESPONSE]
        if content_type.lower().startswith(b"text/html"):
            wanted += _HTML_PAGE
        return headers + [(name, value) for name, value in wanted if name not in present]
