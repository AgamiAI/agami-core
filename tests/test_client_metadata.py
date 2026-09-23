"""Client ID Metadata Documents — the one fetch a sign-in can cause, with NO real egress.

A client may identify itself with an https URL instead of a registered id; the server fetches that URL
to learn the client's redirect URIs. The URL is chosen by whoever starts the sign-in, so the fetch is
the dangerous part: every refusal here is a way an attacker could otherwise point the server at an
internal address, hold a worker open, or feed it an oversized body.

The two network seams are patched: `_resolve` (DNS) returns whatever address the test names, and
`_client` hands back an httpx client on a `MockTransport` that records every request. The deadline is
patched small so no test waits long. One test opens a real socket, on 127.0.0.1, because only a real
connection shows whether a response trickled a byte at a time is cut off on the wall clock.
"""

from __future__ import annotations

import datetime
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import anyio
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


async def _chunks(*chunks: bytes):
    for chunk in chunks:
        yield chunk


def _served(status: int = 200, *, json_body: object = None, content: bytes = b"", **kwargs):
    """A response the way the network delivers one: an unread stream. A response built from `json=` or
    bytes is already read, which a real server's never is, and the raw reader refuses it."""
    if json_body is not None:
        content = httpx.Response(200, json=json_body).content
    return httpx.Response(status, content=_chunks(content), **kwargs)


def _redirect_uris(client_id: str) -> list[str]:
    return anyio.run(client_metadata.redirect_uris, client_id)


class _Net:
    """The patched network: what DNS answers, what the server answers, and what was asked of each."""

    def __init__(self) -> None:
        self.addresses = [PUBLIC_IP]
        self.resolves: list[tuple[str, int]] = []
        self.requests: list[httpx.Request] = []
        self.respond = lambda request: _served(json_body=_doc())

    async def resolve(self, host: str, port: int) -> list[str]:
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
        client_metadata,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(n.handle)),
    )
    monkeypatch.setattr(client_metadata, "_DEADLINE", 0.2)
    yield n
    client_metadata._cache.clear()


# --- the happy path ----------------------------------------------------------------------------------


def test_a_valid_document_yields_its_redirect_uris(net):
    assert _redirect_uris(URL) == _doc()["redirect_uris"]


@pytest.mark.parametrize(
    ("client_id", "expected"),
    [
        (URL, True),
        ("http://app.example.com/client.json", True),  # routed here so it is REFUSED, not looked up
        # A scheme is case-insensitive (RFC 3986 §3.1). Read as a registered id instead, one of these
        # would take a registered client's redirect fallbacks and never open the document at all.
        ("HTTPS://app.example.com/client.json", True),
        ("Http://app.example.com/client.json", True),
        ("3f2a9c", False),
        ("", False),
    ],
)
def test_is_metadata_client_id(client_id, expected):
    assert client_metadata.is_metadata_client_id(client_id) is expected


def test_a_client_id_naming_port_zero_is_refused(net):
    """`:0` parses, and reading it as "no port" would fetch on 443 — a document the id does not name."""
    with pytest.raises(client_metadata.ClientMetadataError, match="port 0"):
        _redirect_uris("https://app.example.com:0/client.json")


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
        _redirect_uris(URL)


@pytest.mark.parametrize(
    "response",
    [
        _served(content=b"<html>not json</html>"),
        _served(json_body=["a", "list"]),
        # Nested deeper than the parser recurses: a RecursionError on some Pythons, not a ValueError.
        _served(content=b"[" * client_metadata._MAX_BYTES),
    ],
    ids=["non-json", "non-object", "deeply-nested"],
)
def test_a_body_that_is_not_a_json_object_is_refused(net, response):
    net.respond = lambda request: response
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)


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
        "https://ünïcode.example.com/client.json",
        "https://" + "a" * 64 + ".example.com/client.json",
    ],
    ids=[
        "http",
        "userinfo",
        "fragment",
        "no-path",
        "dot-dot",
        "dot",
        "no-host",
        "bad-port",
        "non-ascii-host",
        "label-too-long",
    ],
)
def test_a_malformed_url_is_refused_without_resolving_or_fetching(net, url):
    with pytest.raises(ClientMetadataError):
        _redirect_uris(url)
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
        # Multicast ranges count as global, yet no document is served from one.
        "224.0.1.1",
        "ff0e::1",
        # IPv6 forms that carry an IPv4 address, which count as global whatever they embed: NAT64
        # (64:ff9b::/96) and IPv4-compatible (::/96). A NAT64 gateway delivers them to the IPv4 inside.
        "64:ff9b::a00:5",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::7f00:1",
        "::127.0.0.1",
        "::a00:5",
    ],
)
def test_a_non_global_address_is_refused_with_no_request(net, address):
    net.addresses = [address]
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert net.requests == []


@pytest.mark.parametrize("address", ["64:ff9b::b00:1", "2606:4700::1"])
def test_a_public_address_is_fetched_including_one_behind_nat64(net, address):
    net.addresses = [address]
    assert _redirect_uris(URL) == _doc()["redirect_uris"]


def test_one_non_global_address_among_public_ones_is_refused(net):
    # Connecting to the first address alone would be safe here, but a name that resolves to an internal
    # address at all is not a public client's document host.
    net.addresses = [PUBLIC_IP, "127.0.0.1"]
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert net.requests == []


def test_a_name_that_does_not_resolve_is_refused(net, monkeypatch):
    async def fail(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(client_metadata, "_resolve", fail)
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    net.addresses = []
    monkeypatch.setattr(client_metadata, "_resolve", net.resolve)
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert net.requests == []


def test_a_name_the_resolver_cannot_encode_is_refused(net, monkeypatch):
    # The system resolver encodes the name itself and raises UnicodeError, not OSError, on one it cannot.
    async def unencodable(host, port):
        raise UnicodeError("label too long")

    monkeypatch.setattr(client_metadata, "_resolve", unencodable)
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert net.requests == []


def test_the_fetch_connects_to_the_address_it_checked(net, monkeypatch):
    # DNS rebinding: a name that answers a public address to the check and a loopback one to the
    # connection. The fetch must resolve ONCE and connect to what it checked, never re-resolve.
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])
    calls = []

    async def rebinding(host, port):
        calls.append((host, port))
        return next(answers)

    monkeypatch.setattr(client_metadata, "_resolve", rebinding)
    net.respond = lambda request: _served(json_body=_doc(client_id=URL + "?v=1"))
    _redirect_uris(URL + "?v=1")
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
    _redirect_uris(url)
    (request,) = net.requests
    assert request.url.host == "2606:4700::1" and request.url.port == 8443
    assert net.resolves == [("app.example.com", 8443)]
    assert request.headers["host"] == "app.example.com:8443"


def test_the_first_checked_address_is_used(net):
    net.addresses = [PUBLIC_IP, PUBLIC_IP_2]
    _redirect_uris(URL)
    assert net.requests[0].url.host == PUBLIC_IP


def test_the_request_carries_no_body_or_credentials(net):
    _redirect_uris(URL)
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
        _redirect_uris(URL)
    assert len(net.requests) == 1


def test_a_body_past_the_cap_is_refused(net):
    # A valid document, so only the cap can refuse it: one byte past the limit.
    body = httpx.Response(200, json=_doc()).content
    big = body + b" " * (client_metadata._MAX_BYTES + 1 - len(body))
    net.respond = lambda request: _served(content=big)
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)


def test_a_streamed_body_is_cut_off_at_the_cap(net):
    # A body with no length that never ends: the reader must stop once it is past the cap, not read on.
    served = []

    async def endless():
        while True:
            served.append(1)
            yield b" " * 1024

    net.respond = lambda request: httpx.Response(200, content=endless())
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert len(served) <= client_metadata._MAX_BYTES // 1024 + 1


def test_a_body_exactly_at_the_cap_is_read(net):
    body = httpx.Response(200, json=_doc()).content
    padded = body + b" " * (client_metadata._MAX_BYTES - len(body))
    net.respond = lambda request: _served(content=padded)
    assert _redirect_uris(URL) == _doc()["redirect_uris"]


def test_a_slow_drip_body_is_cut_off_at_the_deadline(net, monkeypatch):
    monkeypatch.setattr(client_metadata, "_DEADLINE", 0.05)

    async def drip():
        for _ in range(100):
            await anyio.sleep(0.01)
            yield b" "

    net.respond = lambda request: httpx.Response(200, content=drip())
    started = time.monotonic()
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert time.monotonic() - started < 0.5


def _self_signed(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A certificate for `name` that is its own CA, so a client trusting it verifies the name for real."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


@pytest.fixture
def trickling_server(tmp_path, monkeypatch):
    """A TLS server on 127.0.0.1 that sends its response headers one byte every 50 ms, giving up after
    a second. Every byte arrives well inside httpx's per-read timeout, so only a wall-clock deadline
    ends the fetch sooner. The real client is used; only its CA bundle is swapped for this server's."""
    import certifi

    cert_file, key_file = _self_signed(tmp_path, "app.example.com")
    monkeypatch.setattr(certifi, "where", lambda: str(cert_file))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(2)
    stop = threading.Event()

    def serve():
        try:
            conn = context.wrap_socket(listener.accept()[0], server_side=True)
            conn.recv(4096)
        except OSError:
            return
        with conn:
            gives_up = time.monotonic() + 1.0
            for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + b"X-Pad: a" * 8:
                if stop.is_set() or time.monotonic() > gives_up:
                    return
                time.sleep(0.05)
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    thread.join()
    listener.close()


def test_headers_trickled_past_the_deadline_are_refused_on_time(monkeypatch, trickling_server):
    client_metadata._cache.clear()
    monkeypatch.setattr(client_metadata, "_DEADLINE", 0.2)

    async def loopback(host, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(client_metadata, "_resolve", loopback)
    # The test server can only be local; the address rule has its own tests above.
    monkeypatch.setattr(client_metadata, "_is_public", lambda address: True)
    started = time.monotonic()
    with pytest.raises(ClientMetadataError):
        _redirect_uris(f"https://app.example.com:{trickling_server}/client.json")
    assert time.monotonic() - started < 0.5


def test_a_slow_lookup_counts_against_the_deadline(net, monkeypatch):
    async def slow(host, port):
        await anyio.sleep(1)
        return [PUBLIC_IP]

    monkeypatch.setattr(client_metadata, "_resolve", slow)
    started = time.monotonic()
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert time.monotonic() - started < 0.5
    assert net.requests == []


def test_a_transport_timeout_is_refused(net):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    net.respond = timeout
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)


# --- the real seams ----------------------------------------------------------------------------------


def test_the_real_client_ignores_proxy_env_and_redirects(monkeypatch):
    # With `trust_env` on, an ambient HTTPS_PROXY would route the fetch through the proxy, which then
    # resolves the name itself and the address check means nothing.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.com:8080")
    with httpx.Client(trust_env=True) as trusting:
        assert trusting._mounts  # the env really is set: a trusting client would mount a proxy
    client = client_metadata._client()
    assert isinstance(client, httpx.AsyncClient)
    assert not client._mounts
    assert client.follow_redirects is False
    anyio.run(client.aclose)


def test_the_real_resolver_returns_every_address(monkeypatch):
    seen = []

    async def fake_getaddrinfo(host, port, *args, **kwargs):
        seen.append((host, port))
        return [(2, 1, 6, "", (PUBLIC_IP, port)), (10, 1, 6, "", ("::1", port, 0, 0))]

    monkeypatch.setattr(client_metadata.anyio, "getaddrinfo", fake_getaddrinfo)
    assert anyio.run(client_metadata._resolve, "app.example.com", 443) == [PUBLIC_IP, "::1"]
    assert seen == [("app.example.com", 443)]


# --- the cache ---------------------------------------------------------------------------------------


def test_a_second_sign_in_within_the_ttl_does_not_fetch(net):
    _redirect_uris(URL)
    _redirect_uris(URL)
    assert len(net.requests) == 1 and len(net.resolves) == 1


def test_an_expired_entry_is_fetched_again(net):
    _redirect_uris(URL)
    expires_at, uris = client_metadata._cache[URL]
    client_metadata._cache[URL] = (time.monotonic() - 1, uris)
    _redirect_uris(URL)
    assert len(net.requests) == 2


def test_a_failure_is_not_cached(net):
    net.respond = lambda request: _served(500)
    with pytest.raises(ClientMetadataError):
        _redirect_uris(URL)
    assert URL not in client_metadata._cache
    net.respond = lambda request: _served(json_body=_doc())
    assert _redirect_uris(URL) == _doc()["redirect_uris"]


def test_the_cache_is_capped_and_evicts_the_oldest(net, monkeypatch):
    # The key is attacker-chosen, so an uncapped cache is a memory-growth vector.
    monkeypatch.setattr(client_metadata, "_CACHE_MAX", 2)
    urls = [f"https://app.example.com/client-{i}.json" for i in range(3)]
    net.respond = lambda request: _served(
        json_body=_doc(client_id=f"https://app.example.com{request.url.path}")
    )
    for url in urls:
        _redirect_uris(url)
    assert list(client_metadata._cache) == urls[1:]
