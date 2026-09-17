"""Client ID Metadata Documents — the one fetch a sign-in can cause, with NO real egress.

A client may identify itself with an https URL instead of a registered id; the server fetches that URL
to learn the client's redirect URIs. The URL is chosen by whoever starts the sign-in, so the fetch is
the dangerous part: every refusal here is a way an attacker could otherwise point the server at an
internal address, hold a worker open, or feed it an oversized body.

The two network seams are patched: `_resolve` (DNS) returns whatever address the test names, and
`_client` hands back an httpx client on a `MockTransport` that records every request. No test opens a
socket, and the deadline is patched small so no test sleeps.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import client_metadata  # noqa: E402
from client_metadata import ClientMetadataError  # noqa: E402

URL = "https://app.example.com/oauth/client.json"
# A global address, so the resolver check passes. Nothing ever connects to it: the transport is mocked.
PUBLIC_IP = "11.0.0.1"
PUBLIC_IP_2 = "11.0.0.2"


def _doc(**overrides) -> dict:
    doc = {
        "client_id": URL,
        "client_name": "Acme Assistant",
        "redirect_uris": ["http://127.0.0.1/callback", "https://app.example.com/callback"],
    }
    doc.update(overrides)
    return doc


def _served(status: int = 200, *, json_body: object = None, content: bytes = b"", **kwargs):
    """A response the way the network delivers one: an unread stream. A response built from `json=` or
    bytes is already read, which a real server's never is, and the raw reader refuses it."""
    if json_body is not None:
        content = httpx.Response(200, json=json_body).content
    return httpx.Response(status, content=iter([content]), **kwargs)


class _Net:
    """The patched network: what DNS answers, what the server answers, and what was asked of each."""

    def __init__(self) -> None:
        self.addresses = [PUBLIC_IP]
        self.resolves: list[tuple[str, int]] = []
        self.requests: list[httpx.Request] = []
        self.respond = lambda request: _served(json_body=_doc())

    def resolve(self, host: str, port: int) -> list[str]:
        self.resolves.append((host, port))
        return list(self.addresses)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


@pytest.fixture
def net(monkeypatch):
    client_metadata._cache.clear()
    n = _Net()
    monkeypatch.setattr(client_metadata, "_resolve", n.resolve)
    monkeypatch.setattr(
        client_metadata, "_client", lambda: httpx.Client(transport=httpx.MockTransport(n.handle))
    )
    monkeypatch.setattr(client_metadata, "_DEADLINE", 0.2)
    yield n
    client_metadata._cache.clear()


# --- the happy path ----------------------------------------------------------------------------------


def test_a_valid_document_yields_its_redirect_uris(net):
    assert client_metadata.redirect_uris(URL) == _doc()["redirect_uris"]


@pytest.mark.parametrize(
    ("client_id", "expected"),
    [
        (URL, True),
        ("http://app.example.com/client.json", True),  # routed here so it is REFUSED, not looked up
        ("3f2a9c", False),
        ("", False),
    ],
)
def test_is_metadata_client_id(client_id, expected):
    assert client_metadata.is_metadata_client_id(client_id) is expected


# --- the document itself -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    [
        _doc(client_id="https://other.example.com/oauth/client.json"),  # names a different client
        {k: v for k, v in _doc().items() if k != "client_name"},
        _doc(client_name=""),
        _doc(client_name=42),
        {k: v for k, v in _doc().items() if k != "redirect_uris"},
        _doc(redirect_uris=[]),
        _doc(redirect_uris="https://app.example.com/callback"),
        _doc(redirect_uris=["https://app.example.com/callback", 7]),
        # A public client has no secret; a document carrying one is not a public-client document.
        _doc(client_secret="not-a-real-secret"),
    ],
    ids=[
        "client_id-mismatch",
        "no-client_name",
        "blank-client_name",
        "non-string-client_name",
        "no-redirect_uris",
        "empty-redirect_uris",
        "string-redirect_uris",
        "non-string-redirect_uri",
        "client_secret",
    ],
)
def test_an_invalid_document_is_refused(net, doc):
    net.respond = lambda request: _served(json_body=doc)
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)


@pytest.mark.parametrize(
    "response",
    [
        _served(content=b"<html>not json</html>"),
        _served(json_body=["a", "list"]),
    ],
    ids=["non-json", "non-object"],
)
def test_a_body_that_is_not_a_json_object_is_refused(net, response):
    net.respond = lambda request: response
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)


# --- the URL, refused before any lookup --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example.com/oauth/client.json",
        "https://user@app.example.com/oauth/client.json",
        "https://app.example.com/oauth/client.json#frag",
        "https://app.example.com",
        "https://app.example.com/oauth/../client.json",
        "https://app.example.com/./client.json",
        "https:///client.json",
        "https://app.example.com:notaport/client.json",
    ],
    ids=["http", "userinfo", "fragment", "no-path", "dot-dot", "dot", "no-host", "bad-port"],
)
def test_a_malformed_url_is_refused_without_resolving_or_fetching(net, url):
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(url)
    assert net.resolves == [] and net.requests == []


# --- where the fetch may go --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "100.64.0.1",
    ],
)
def test_a_non_global_address_is_refused_with_no_request(net, address):
    net.addresses = [address]
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    assert net.requests == []


def test_one_non_global_address_among_public_ones_is_refused(net):
    # Connecting to the first address alone would be safe here, but a name that resolves to an internal
    # address at all is not a public client's document host.
    net.addresses = [PUBLIC_IP, "127.0.0.1"]
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    assert net.requests == []


def test_a_name_that_does_not_resolve_is_refused(net, monkeypatch):
    def fail(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(client_metadata, "_resolve", fail)
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    net.addresses = []
    monkeypatch.setattr(client_metadata, "_resolve", net.resolve)
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    assert net.requests == []


def test_the_fetch_connects_to_the_address_it_checked(net, monkeypatch):
    # DNS rebinding: a name that answers a public address to the check and a loopback one to the
    # connection. The fetch must resolve ONCE and connect to what it checked, never re-resolve.
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])
    calls = []

    def rebinding(host, port):
        calls.append((host, port))
        return next(answers)

    monkeypatch.setattr(client_metadata, "_resolve", rebinding)
    net.respond = lambda request: _served(json_body=_doc(client_id=URL + "?v=1"))
    client_metadata.redirect_uris(URL + "?v=1")
    assert calls == [("app.example.com", 443)]
    (request,) = net.requests
    assert request.url.host == PUBLIC_IP and request.url.port in (None, 443)
    assert request.url.path == "/oauth/client.json" and request.url.query == b"v=1"
    # TLS and virtual hosting still see the name, so the certificate is verified against it.
    assert request.headers["host"] == "app.example.com"
    assert request.extensions["sni_hostname"] == "app.example.com"


def test_an_ipv6_address_is_bracketed_and_the_port_kept(net, monkeypatch):
    url = "https://app.example.com:8443/client.json"
    net.addresses = ["2606:4700::1"]
    net.respond = lambda request: _served(json_body=_doc(client_id=url))
    client_metadata.redirect_uris(url)
    (request,) = net.requests
    assert request.url.host == "2606:4700::1" and request.url.port == 8443
    assert net.resolves == [("app.example.com", 8443)]
    assert request.headers["host"] == "app.example.com:8443"


def test_the_first_checked_address_is_used(net):
    net.addresses = [PUBLIC_IP, PUBLIC_IP_2]
    client_metadata.redirect_uris(URL)
    assert net.requests[0].url.host == PUBLIC_IP


def test_the_request_carries_no_body_or_credentials(net):
    client_metadata.redirect_uris(URL)
    (request,) = net.requests
    assert request.method == "GET" and request.content == b""
    assert "authorization" not in request.headers and "cookie" not in request.headers


# --- what the fetch will accept back -----------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 500])
def test_a_non_200_including_a_redirect_is_refused_and_not_followed(net, status):
    net.respond = lambda request: _served(
        status, headers={"location": "https://app.example.com/elsewhere.json"}
    )
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    assert len(net.requests) == 1


def test_a_body_past_the_cap_is_refused(net):
    big = b" " * (client_metadata._MAX_BYTES + 1)
    net.respond = lambda request: _served(content=big)
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)


def test_a_streamed_body_is_cut_off_at_the_cap(net):
    # A body with no length that never ends: the reader must stop once it is past the cap, not read on.
    served = []

    def endless():
        while True:
            served.append(1)
            yield b" " * 1024

    net.respond = lambda request: httpx.Response(200, content=endless())
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    assert len(served) <= client_metadata._MAX_BYTES // 1024 + 1


def test_a_body_exactly_at_the_cap_is_read(net):
    body = httpx.Response(200, json=_doc()).content
    padded = body + b" " * (client_metadata._MAX_BYTES - len(body))
    net.respond = lambda request: _served(content=padded)
    assert client_metadata.redirect_uris(URL) == _doc()["redirect_uris"]


def test_a_slow_drip_body_is_cut_off_at_the_deadline(net, monkeypatch):
    monkeypatch.setattr(client_metadata, "_DEADLINE", 0.05)

    def drip():
        for _ in range(100):
            time.sleep(0.01)
            yield b" "

    net.respond = lambda request: httpx.Response(200, content=drip())
    started = time.monotonic()
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    assert time.monotonic() - started < 0.5


def test_a_transport_timeout_is_refused(net):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    net.respond = timeout
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)


# --- the real seams ----------------------------------------------------------------------------------


def test_the_real_client_ignores_proxy_env_and_redirects(monkeypatch):
    # With `trust_env` on, an ambient HTTPS_PROXY would route the fetch through the proxy, which then
    # resolves the name itself and the address check means nothing.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.com:8080")
    with httpx.Client(trust_env=True) as trusting:
        assert trusting._mounts  # the env really is set: a trusting client would mount a proxy
    with client_metadata._client() as client:
        assert not client._mounts
        assert client.follow_redirects is False


def test_the_real_resolver_returns_every_address(monkeypatch):
    seen = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        seen.append((host, port))
        return [(2, 1, 6, "", (PUBLIC_IP, port)), (10, 1, 6, "", ("::1", port, 0, 0))]

    monkeypatch.setattr(client_metadata.socket, "getaddrinfo", fake_getaddrinfo)
    assert client_metadata._resolve("app.example.com", 443) == [PUBLIC_IP, "::1"]
    assert seen == [("app.example.com", 443)]


# --- the cache ---------------------------------------------------------------------------------------


def test_a_second_sign_in_within_the_ttl_does_not_fetch(net):
    client_metadata.redirect_uris(URL)
    client_metadata.redirect_uris(URL)
    assert len(net.requests) == 1 and len(net.resolves) == 1


def test_an_expired_entry_is_fetched_again(net):
    client_metadata.redirect_uris(URL)
    expires_at, uris = client_metadata._cache[URL]
    client_metadata._cache[URL] = (time.monotonic() - 1, uris)
    client_metadata.redirect_uris(URL)
    assert len(net.requests) == 2


def test_a_failure_is_not_cached(net):
    net.respond = lambda request: _served(500)
    with pytest.raises(ClientMetadataError):
        client_metadata.redirect_uris(URL)
    assert URL not in client_metadata._cache
    net.respond = lambda request: _served(json_body=_doc())
    assert client_metadata.redirect_uris(URL) == _doc()["redirect_uris"]


def test_the_cache_is_capped_and_evicts_the_oldest(net, monkeypatch):
    # The key is attacker-chosen, so an uncapped cache is a memory-growth vector.
    monkeypatch.setattr(client_metadata, "_CACHE_MAX", 2)
    urls = [f"https://app.example.com/client-{i}.json" for i in range(3)]
    net.respond = lambda request: _served(
        json_body=_doc(client_id=f"https://app.example.com{request.url.path}")
    )
    for url in urls:
        client_metadata.redirect_uris(url)
    assert list(client_metadata._cache) == urls[1:]
