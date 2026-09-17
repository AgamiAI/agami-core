"""Client ID Metadata Documents — learning a client's redirect URIs from the URL it identifies itself by.

An MCP client may skip registration and send an https URL as its `client_id`; the JSON document at that
URL lists the client's redirect URIs. So a sign-in by such a client makes the server fetch that public
URL. It is the hosted server's second deliberate outbound call (after the OIDC client), and it carries
no customer data: a plain GET with no body, no cookies and no credentials. It lives in its own module so
`oauth_server` stays free of network code.

The URL is chosen by whoever starts the sign-in, so the fetch is written against an attacker:
  - https only, with a host and a path, and no userinfo, fragment or dot segments;
  - the name is resolved once, every address must be globally routable, and the connection goes to
    the address that was checked (the name rides along as `Host` and TLS SNI, so the certificate is
    still verified against it) — a second lookup could answer an internal address;
  - no redirects, no proxy from the environment (a proxy would resolve the name itself), a 5 KB body
    cap enforced while streaming, and one 3 s deadline for the whole read.
A document that passes is cached in memory for an hour, under a cap, because the cache key is
attacker-chosen too. Failures are not cached.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from urllib.parse import SplitResult, urlsplit

import httpx

_MAX_BYTES = 5120
_DEADLINE = 3.0
_TTL = 3600
_CACHE_MAX = 256

# url -> (monotonic expiry, redirect URIs). Insertion-ordered, so the first key is the oldest entry.
_cache: dict[str, tuple[float, list[str]]] = {}
# Sign-ins run the fetch on worker threads, and eviction reads then deletes the oldest key.
_cache_lock = threading.Lock()


class ClientMetadataError(Exception):
    """The client's metadata document could not be fetched or is not valid; the sign-in is refused."""


def is_metadata_client_id(client_id: str) -> bool:
    """True when `client_id` is a URL. An `http` URL counts, so it is refused here rather than looked up
    as a registered id that happens to start with a scheme."""
    return client_id.startswith(("https://", "http://"))


def redirect_uris(client_id: str) -> list[str]:
    """The redirect URIs the document at `client_id` lists, from the cache or a fresh fetch.

    Blocking (DNS + HTTP) — call it through `run_blocking`. Raises `ClientMetadataError` on any failure."""
    with _cache_lock:
        hit = _cache.get(client_id)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    uris = _validate(client_id, _fetch(client_id))
    with _cache_lock:
        _cache.pop(client_id, None)
        if len(_cache) >= _CACHE_MAX:
            del _cache[next(iter(_cache))]
        _cache[client_id] = (time.monotonic() + _TTL, uris)
    return uris


def _check_url(url: str) -> SplitResult:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise ClientMetadataError("client_id is not a valid URL") from exc
    if parts.scheme != "https" or not parts.hostname or not parts.path:
        raise ClientMetadataError("client_id must be an https URL with a host and a path")
    if "#" in url or "@" in parts.netloc:
        raise ClientMetadataError("client_id must not carry a fragment or userinfo")
    if any(segment in (".", "..") for segment in parts.path.split("/")):
        raise ClientMetadataError("client_id must not contain dot segments")
    return parts


def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to. The network seam tests patch."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _is_global(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _client() -> httpx.Client:
    """The HTTP client for the fetch. The other network seam tests patch."""
    return httpx.Client(trust_env=False, follow_redirects=False, timeout=_DEADLINE)


def _fetch(url: str) -> dict:
    parts = _check_url(url)
    deadline = time.monotonic() + _DEADLINE
    host, port = parts.hostname, parts.port or 443
    try:
        addresses = _resolve(host, port)
    except OSError as exc:
        raise ClientMetadataError("client_id host does not resolve") from exc
    if not addresses or not all(_is_global(a) for a in addresses):
        raise ClientMetadataError("client_id host is not a public address")
    address = addresses[0]
    netloc = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    target = f"https://{netloc}{parts.path}" + (f"?{parts.query}" if parts.query else "")
    body = bytearray()
    try:
        with (
            _client() as client,
            client.stream(
                "GET",
                target,
                # identity: the cap counts bytes on the wire, and a compressed body could expand past it.
                headers={
                    "Host": parts.netloc,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": host},
            ) as response,
        ):
            if response.status_code != 200:
                raise ClientMetadataError(f"client metadata fetch returned {response.status_code}")
            for chunk in response.iter_raw():
                body += chunk
                if len(body) > _MAX_BYTES:
                    raise ClientMetadataError("client metadata document is too large")
                if time.monotonic() > deadline:
                    raise ClientMetadataError("client metadata fetch timed out")
    except httpx.HTTPError as exc:
        raise ClientMetadataError("client metadata fetch failed") from exc
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise ClientMetadataError("client metadata document is not JSON") from exc
    if not isinstance(doc, dict):
        raise ClientMetadataError("client metadata document is not a JSON object")
    return doc


def _validate(url: str, doc: dict) -> list[str]:
    # The document must name itself, or one client's document could vouch for another's id.
    if doc.get("client_id") != url:
        raise ClientMetadataError("client metadata client_id does not match its URL")
    name = doc.get("client_name")
    if not isinstance(name, str) or not name.strip():
        raise ClientMetadataError("client metadata has no client_name")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise ClientMetadataError("client metadata has no redirect_uris")
    # A metadata-document client is a public client: a secret published in a public document is no secret.
    if "client_secret" in doc:
        raise ClientMetadataError("client metadata must not carry a client_secret")
    return uris
