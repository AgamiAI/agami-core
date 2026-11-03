"""The loopback password helper: a one-time page on 127.0.0.1 that puts the password in the OS store.

The helper runs in a thread against a temp artifacts dir and an in-memory keyring backend, so the
password's whole path (page -> helper -> credential store -> credentials file -> connection test) is
exercised without a real Keychain. The connection test uses a SQLite profile, so no network either.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "agami" / "scripts"
PKG_SRC = REPO / "packages" / "agami-core" / "src"
PAGE = REPO / "plugins" / "agami" / "shared" / "connect-helper-page.html"
for _p in (str(SCRIPTS), str(PKG_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import connect_helper  # noqa: E402
import credential_store  # noqa: E402
import execute_sql  # noqa: E402

SECRET = "not-a-real-password"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")


# What the OS-store check reads off keyring's active backend: its module and class name.
_Keychain = type("Keyring", (), {"__module__": "keyring.backends.macOS"})
_WinVault = type("WinVaultKeyring", (), {"__module__": "keyring.backends.Windows"})


class _FakeBackend:
    """The calls credential_store makes, backed by a dict. `gate` holds set_password until released."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def set_password(self, service: str, username: str, secret: str) -> None:
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        self.data[(service, username)] = secret

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        del self.data[(service, username)]

    def get_keyring(self) -> object:
        return (_WinVault if credential_store.platform.system() == "Windows" else _Keychain)()


class _Lines(io.TextIOBase):
    """A stdout stand-in the test can wait on, line by line."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._buf = ""
        self._cv = threading.Condition()

    def write(self, s: str) -> int:
        with self._cv:
            self._buf += s
            *done, self._buf = self._buf.split("\n")
            self.lines.extend(done)
            self._cv.notify_all()
        return len(s)

    def wait(self, n: int, timeout: float = 10) -> list[dict]:
        with self._cv:
            assert self._cv.wait_for(lambda: len(self.lines) >= n, timeout), self.lines
            return [json.loads(line) for line in self.lines[:n]]


@pytest.fixture
def art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "art"
    root.mkdir()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(root))
    for name in ("DATASOURCE_URL", "LOCAL_DATASOURCE_URL", "DATASOURCE_URL__DEMO", "LOCAL_DATASOURCE_URL__DEMO"):
        monkeypatch.delenv(name, raising=False)
    # The helper points execute_sql at this run's credentials file; put the module back afterwards.
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", execute_sql.CREDENTIALS_PATH)
    return root


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    fake = _FakeBackend()
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", lambda: fake)
    return fake


def _spec(tmp_path: Path, **over: object) -> dict:
    spec: dict = {"profile": "demo", "db_type": "sqlite", "fields": {"path": str(tmp_path / "demo.db")}}
    spec.update(over)
    return spec


def _start(spec: dict, *, expires_in: float = connect_helper.EXPIRES_IN) -> tuple[threading.Thread, _Lines]:
    out = _Lines()
    t = threading.Thread(target=connect_helper.run, args=(spec, out), kwargs={"expires_in": expires_in}, daemon=True)
    t.start()
    return t, out


def _post(url: str, password: str) -> int:
    data = urllib.parse.urlencode({"password": password}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, ""


def _creds(art: Path) -> Path:
    return art / "local" / "credentials"


def _secret_on_disk(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and SECRET.encode() in p.read_bytes()]


def test_prints_loopback_url_with_token(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    t, out = _start(_spec(tmp_path))
    first = out.wait(1)[0]
    url = urllib.parse.urlsplit(first["url"])
    assert url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
    token = url.path.strip("/")
    assert _TOKEN_RE.match(token), token
    assert url.path == f"/{token}/"
    assert first["expires_in"] == 300
    status, page = _get(first["url"])
    assert status == 200 and 'type="password"' in page and "demo" in page
    assert _post(first["url"], SECRET) == 200
    t.join(10)
    assert not t.is_alive()


def test_submit_stores_ref_not_secret(
    tmp_path: Path, art: Path, backend: _FakeBackend, capfd: pytest.CaptureFixture[str]
) -> None:
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    assert _post(url, SECRET) == 200
    t.join(10)
    final = out.wait(2)[1]
    assert final["status"] == "connected", final
    assert final["profile"] == "demo"
    creds = _creds(art).read_text(encoding="utf-8")
    assert "password = keyring:agami/demo" in creds
    assert "[demo]" in creds and "type = sqlite" in creds
    if os.name == "posix":
        assert _creds(art).stat().st_mode & 0o777 == 0o600
    assert backend.data == {("agami", "demo"): SECRET}
    assert _secret_on_disk(tmp_path) == []
    captured = capfd.readouterr()
    assert SECRET not in "\n".join(out.lines) + captured.out + captured.err


def test_connect_failure_reported_without_the_secret(
    tmp_path: Path, art: Path, backend: _FakeBackend, capfd: pytest.CaptureFixture[str]
) -> None:
    spec = _spec(tmp_path, fields={"path": str(tmp_path / "missing-dir" / "demo.db")})
    t, out = _start(spec)
    assert _post(out.wait(1)[0]["url"], SECRET) == 200
    t.join(10)
    final = out.wait(2)[1]
    assert final["status"] == "connect_failed" and final["detail"] and "\n" not in final["detail"]
    # Rolled back, so "Try again" with the same profile name is not a collision.
    assert not _creds(art).exists()
    assert backend.data == {}
    captured = capfd.readouterr()
    assert SECRET not in "\n".join(out.lines) + captured.out + captured.err
    assert _secret_on_disk(tmp_path) == []


def test_connect_failure_rolls_back_only_its_own_section(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    _creds(art).parent.mkdir(parents=True)
    before = "[acme]\ntype = sqlite\npath = /tmp/acme.db"  # no final newline
    _creds(art).write_text(before, encoding="utf-8")
    backend.data[("agami", "acme")] = "keep-me"
    t, out = _start(_spec(tmp_path, fields={"path": str(tmp_path / "missing-dir" / "demo.db")}))
    assert _post(out.wait(1)[0]["url"], SECRET) == 200
    t.join(10)
    assert out.wait(2)[1]["status"] == "connect_failed"
    assert _creds(art).read_text(encoding="utf-8") == before
    assert backend.data == {("agami", "acme"): "keep-me"}
    # And the same name now goes through.
    out2 = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out2, expires_in=1)
    assert json.loads(out2.lines[0])["status"] == "connected"


def test_rollback_leaves_a_file_someone_else_changed(tmp_path: Path, art: Path, monkeypatch) -> None:
    creds = _creds(art)

    def failing_test(profile: str) -> tuple[str, str]:
        with creds.open("a", encoding="utf-8") as fh:
            fh.write("\n[acme]\ntype = sqlite\n")  # another writer appended after the helper
        return "connect_failed", "The connection test failed."

    monkeypatch.setattr(connect_helper, "_connection_test", failing_test)
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    assert json.loads(out.lines[0])["status"] == "connect_failed"
    assert "[demo]" in creds.read_text(encoding="utf-8") and "[acme]" in creds.read_text(encoding="utf-8")


def test_rollback_that_cannot_delete_the_secret_says_so(tmp_path: Path, art: Path, monkeypatch) -> None:
    class Stuck(_FakeBackend):
        def delete_password(self, service: str, username: str) -> None:
            raise OSError("locked")

    fake = Stuck()
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", lambda: fake)
    t, out = _start(_spec(tmp_path, fields={"path": str(tmp_path / "missing-dir" / "demo.db")}))
    assert _post(out.wait(1)[0]["url"], SECRET) == 200
    t.join(10)
    final = out.wait(2)[1]
    assert final["status"] == "connect_failed" and "by hand" in final["detail"]
    assert SECRET not in json.dumps(final)


def test_wrong_token_404_and_nothing_stored(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    t, out = _start(_spec(tmp_path), expires_in=1.5)
    url = urllib.parse.urlsplit(out.wait(1)[0]["url"])
    base = f"http://127.0.0.1:{url.port}"
    wrong = base + "/" + "x" * 43 + "/"
    assert _post(wrong, SECRET) == 404
    assert _get(wrong)[0] == 404
    assert _get(base + "/")[0] == 404
    assert _post(base + url.path.rstrip("/"), SECRET) == 404  # the exact path only
    t.join(10)
    assert out.wait(2)[1] == {"status": "expired"}
    assert backend.data == {}
    assert not _creds(art).exists()


def test_second_submit_404(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    backend.gate = threading.Event()
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    assert _post(url, SECRET) == 200
    assert backend.entered.wait(10)  # the first submit is being stored; the server still answers
    assert _post(url, "another-password") == 404
    assert _get(url)[0] == 404
    backend.gate.set()
    t.join(10)
    assert out.wait(2)[1]["status"] == "connected"
    assert backend.data == {("agami", "demo"): SECRET}


def test_empty_password_is_not_a_submit(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    assert _post(url, "") == 400
    assert _post(url, SECRET) == 200
    t.join(10)
    assert out.wait(2)[1]["status"] == "connected"


@pytest.mark.parametrize("case", ["invalid_name", "existing_section"])
def test_invalid_or_existing_profile_refused_before_listening(
    tmp_path: Path, art: Path, backend: _FakeBackend, case: str
) -> None:
    spec = _spec(tmp_path)
    before = None
    if case == "invalid_name":
        spec["profile"] = "Acme Prod"
    else:
        _creds(art).parent.mkdir(parents=True)
        _creds(art).write_text("[demo]\ntype = sqlite\npath = /tmp/old.db\n", encoding="utf-8")
        before = _creds(art).read_text(encoding="utf-8")
    out = _Lines()
    assert connect_helper.run(spec, out, expires_in=1) == 0
    assert len(out.lines) == 1
    line = json.loads(out.lines[0])
    assert line["status"] == "refused" and "url" not in line and line["detail"]
    assert backend.data == {}
    if before is None:
        assert not _creds(art).exists()
    else:
        assert _creds(art).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "over",
    [
        {"fields": {"password": "x"}},
        {"fields": {"url": "postgresql://u@h/db"}},
        {"fields": {"host": "h\n[evil]"}},
        {"fields": {"Bad Key": "x"}},
        {"fields": {"port": True}},
        {"fields": {"database": "sales%2025"}},
        {"fields": {"host": "db.example.com #primary"}},
        {"fields": {"host": "db.example.com\t;x"}},
        {"fields": {"user": "#admin"}},
        {"fields": {"host": "h\r"}},
        {"fields": "not-a-dict"},
        {"db_type": "Postgres; x"},
        {"auth": "token"},
    ],
)
def test_bad_spec_refused(tmp_path: Path, art: Path, backend: _FakeBackend, over: dict) -> None:
    out = _Lines()
    connect_helper.run(_spec(tmp_path, **over), out, expires_in=1)
    assert [json.loads(x)["status"] for x in out.lines] == ["refused"]
    assert not _creds(art).exists()


def test_spec_that_is_not_an_object_refused(art: Path, backend: _FakeBackend) -> None:
    out = _Lines()
    connect_helper.run(["demo"], out, expires_in=1)
    assert json.loads(out.lines[0]) == {
        "status": "refused", "reason": "bad_spec", "profile": "", "detail": "The connection spec must be a JSON object."
    }


def test_unreadable_credentials_file_refused(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    _creds(art).parent.mkdir(parents=True)
    _creds(art).write_text("type = sqlite\n", encoding="utf-8")  # no [section] header
    out = _Lines()
    connect_helper.run(_spec(tmp_path), out, expires_in=1)
    assert json.loads(out.lines[0])["status"] == "refused"
    assert _creds(art).read_text(encoding="utf-8") == "type = sqlite\n"


def test_profile_added_while_the_page_was_open_refused(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    _creds(art).parent.mkdir(parents=True)
    _creds(art).write_text("[demo]\ntype = sqlite\npath = /tmp/other.db\n", encoding="utf-8")
    assert _post(url, SECRET) == 200
    t.join(10)
    assert out.wait(2)[1]["status"] == "refused"
    assert backend.data == {}
    assert "keyring" not in _creds(art).read_text(encoding="utf-8")


def test_write_failure_after_store_deletes_the_secret(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    _creds(art).mkdir(parents=True)  # the file cannot be written once the password is stored
    assert _post(url, SECRET) == 200
    t.join(10)
    final = out.wait(2)[1]
    assert final["status"] == "refused" and "credentials file" in final["detail"]
    assert backend.data == {}


def test_credentials_write_failure_refused(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    _creds(art).mkdir(parents=True)  # a directory where the file should be
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    line = json.loads(out.lines[0])
    assert line["status"] == "refused" and "credentials file" in line["detail"]


def test_a_refused_connection_test_is_connect_failed(tmp_path: Path, art: Path, monkeypatch) -> None:
    class Refused:
        status = "refused"
        refusal = type("R", (), {"detail": "The statement was refused."})()

    monkeypatch.setattr(execute_sql, "execute_guarded", lambda *a, **k: Refused())
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    assert json.loads(out.lines[0]) == {"status": "connect_failed", "profile": "demo", "detail": "The statement was refused."}


def test_not_supported_os_refuses(tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch) -> None:
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Linux")
    out = _Lines()
    connect_helper.run(_spec(tmp_path), out, expires_in=1)
    assert len(out.lines) == 1
    line = json.loads(out.lines[0])
    assert line["status"] == "refused" and "manual" in line["detail"]
    assert not _creds(art).exists()


def test_store_failure_refused_and_nothing_written(tmp_path: Path, art: Path, monkeypatch) -> None:
    class Broken(_FakeBackend):
        def set_password(self, service: str, username: str, secret: str) -> None:
            raise OSError(f"locked: {secret}")  # a backend that leaks the secret into its error

    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", lambda: Broken())
    t, out = _start(_spec(tmp_path))
    assert _post(out.wait(1)[0]["url"], SECRET) == 200
    t.join(10)
    final = out.wait(2)[1]
    assert final["status"] == "refused" and SECRET not in json.dumps(final)
    assert not _creds(art).exists()


def test_sso_profile_serves_no_page_and_stores_no_secret(
    tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch
) -> None:
    # No credential store is needed for a browser or cloud sign-in, so it works off macOS/Windows too.
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Linux")
    out = _Lines()
    assert connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1) == 0
    assert len(out.lines) == 1
    line = json.loads(out.lines[0])
    assert line["status"] == "connected" and "url" not in line
    assert backend.data == {}
    creds = _creds(art).read_text(encoding="utf-8")
    assert "[demo]" in creds and "password" not in creds and "auth" not in creds


def test_appends_after_existing_profiles(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    _creds(art).parent.mkdir(parents=True)
    _creds(art).write_text("[acme]\ntype = sqlite\npath = /tmp/acme.db", encoding="utf-8")  # no final newline
    _creds(art).chmod(0o644)
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    assert json.loads(out.lines[-1])["status"] == "connected"
    text = _creds(art).read_text(encoding="utf-8")
    assert text.startswith("[acme]\ntype = sqlite\npath = /tmp/acme.db\n")
    assert "[demo]" in text
    if os.name == "posix":
        assert _creds(art).stat().st_mode & 0o777 == 0o600


def test_sso_passes_the_stale_install_guard(tmp_path: Path, art: Path, monkeypatch) -> None:
    ok = type("Env", (), {"status": "ok"})()
    stale = type(sys)("execute_sql")  # an older agami-core: no credential_store, but it can run SELECT 1
    stale.BUILTIN_EXECUTOR = object()
    stale.execute_guarded = lambda *a, **k: ok
    monkeypatch.setitem(sys.modules, "execute_sql", stale)
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    assert json.loads(out.lines[0])["status"] == "connected"


def test_sso_on_an_install_that_cannot_run_the_test(tmp_path: Path, art: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "execute_sql", type(sys)("execute_sql"))  # no execute_guarded at all
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    line = json.loads(out.lines[0])
    assert line["status"] == "connect_failed" and "sm install" in line["detail"]
    assert not _creds(art).exists()


def test_stale_install_refused_before_listening(tmp_path: Path, art: Path, monkeypatch) -> None:
    stale = type(sys)("execute_sql")  # an older agami-core: an executor that never resolves references
    monkeypatch.setitem(sys.modules, "execute_sql", stale)
    out = _Lines()
    assert connect_helper.run(_spec(tmp_path), out, expires_in=1) == 0
    line = json.loads(out.lines[0])
    assert len(out.lines) == 1 and line["status"] == "refused" and "sm install" in line["detail"]
    assert not _creds(art).exists()


def test_no_credential_store_module_refused(tmp_path: Path, art: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "credential_store", None)  # `import credential_store` raises
    out = _Lines()
    connect_helper.run(_spec(tmp_path), out, expires_in=1)
    assert json.loads(out.lines[0])["status"] == "refused" and "sm install" in json.loads(out.lines[0])["detail"]


def test_stale_install_refused_from_the_command_line(tmp_path: Path) -> None:
    """The real shape: a pip-installed agami-core that predates credential_store shadows the plugin's lib/."""
    pkg = tmp_path / "site"
    pkg.mkdir()
    (pkg / "agami_paths.py").write_text(
        "from pathlib import Path\n"
        f"def artifacts_dir(): return Path({str(tmp_path / 'art')!r})\n"
        "def bootstrap(): return artifacts_dir()\n"
        "def local_dir(art=None): return artifacts_dir() / 'local'\n"
        "def credentials_path(art=None): return local_dir() / 'credentials'\n",
        encoding="utf-8",
    )
    (pkg / "execute_sql.py").write_text("CREDENTIALS_PATH = None\n", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec(tmp_path)), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(pkg)}
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "connect_helper.py"), "--spec", str(spec)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    lines = [json.loads(x) for x in r.stdout.splitlines()]
    assert len(lines) == 1 and lines[0]["status"] == "refused" and "sm install" in lines[0]["detail"]


def test_main_reads_the_spec_file(tmp_path: Path, art: Path, backend: _FakeBackend, capsys) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec(tmp_path, profile="Acme Prod")), encoding="utf-8")
    assert connect_helper.main(["--spec", str(spec)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert connect_helper.main(["--spec", str(tmp_path / "nope.json")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


_EXTERNAL = re.compile(r"""\b(?:src|href|action)\s*=\s*["']?\s*(?:[a-z]+:)?//""", re.IGNORECASE)


def test_page_has_no_external_resources() -> None:
    for html in (PAGE.read_text(encoding="utf-8"), connect_helper.render_page("demo", "form")):
        assert not _EXTERNAL.search(html)
        assert not re.search(r"https?://", re.sub(r'xmlns(?::\w+)?="[^"]*"', "", html))
        assert "<link" not in html.lower() and "<script" not in html.lower() and "@import" not in html
    page = connect_helper.render_page("demo", "form")
    assert "#2B38FC" in page and "<svg" in page and "{{" not in page
    assert "!" not in re.sub(r"<!--.*?-->|<!doctype html>", "", page, flags=re.S | re.I).replace("!important", "")


def _raw(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def _port_and_path(url: str) -> tuple[int, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.port or 0, parts.path


def test_idle_and_half_sent_connections_do_not_block_the_exit(
    tmp_path: Path, art: Path, backend: _FakeBackend, capfd: pytest.CaptureFixture[str]
) -> None:
    # The handler's own timeout (10 s) is left as shipped: the exit must not wait on it.
    t, out = _start(_spec(tmp_path), expires_in=1.5)
    port, path = _port_and_path(out.wait(1)[0]["url"])
    idle = socket.create_connection(("127.0.0.1", port))  # connects and never sends a byte
    half = socket.create_connection(("127.0.0.1", port))
    half.sendall(
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 8000\r\n\r\npassword=x".encode()
    )
    try:
        t.join(6)
        assert not t.is_alive(), "run() did not return with an idle connection open"
        assert out.wait(2)[1] == {"status": "expired"}
    finally:
        idle.close()
        half.close()
    assert backend.data == {} and not _creds(art).exists()
    assert "Traceback" not in capfd.readouterr().err


def test_a_half_sent_post_times_out_quietly(
    tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(connect_helper._Handler, "timeout", 0.5)
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    port, path = _port_and_path(url)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as half:
        half.sendall(
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 8000\r\n\r\npassword=x".encode()
        )
        assert half.recv(1024) == b""  # the server gave up on it and closed the connection
    assert "Traceback" not in capfd.readouterr().err
    assert _post(url, SECRET) == 200  # the page still works afterwards
    t.join(10)
    assert out.wait(2)[1]["status"] == "connected"


def test_wrong_host_or_origin_404(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    port, path = _port_and_path(url)
    body = "password=" + SECRET
    for host, origin in [
        ("localhost:%d" % port, None),                     # a rebinding name, not the address the URL gave
        ("127.0.0.1:%d" % port, "http://evil.example"),     # a page elsewhere posting to the helper
        ("127.0.0.1:%d" % port, "null"),
    ]:
        extra = f"Origin: {origin}\r\n" if origin else ""
        req = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\n{extra}Content-Type: application/x-www-form-urlencoded\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}")
        assert _raw(port, req.encode()).startswith(b"HTTP/1.0 404"), (host, origin)
    get = f"GET {path} HTTP/1.1\r\nHost: evil.example:{port}\r\nConnection: close\r\n\r\n"
    assert _raw(port, get.encode()).startswith(b"HTTP/1.0 404")
    assert backend.data == {}
    # The page's own origin is accepted.
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={"Origin": f"http://127.0.0.1:{port}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200
    t.join(10)
    assert out.wait(2)[1]["status"] == "connected"


def test_bad_content_length_404_without_a_traceback(
    tmp_path: Path, art: Path, backend: _FakeBackend, capfd: pytest.CaptureFixture[str]
) -> None:
    t, out = _start(_spec(tmp_path), expires_in=1.5)
    port, path = _port_and_path(out.wait(1)[0]["url"])
    req = f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: abc\r\nConnection: close\r\n\r\n"
    assert _raw(port, req.encode()).startswith(b"HTTP/1.0 404")
    t.join(10)
    assert out.wait(2)[1] == {"status": "expired"}
    assert "Traceback" not in capfd.readouterr().err


def test_rollback_that_cannot_edit_the_file_says_so(tmp_path: Path, art: Path, monkeypatch) -> None:
    def cannot(creds: Path, added: str) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(connect_helper, "_connection_test", lambda profile: ("connect_failed", "It failed."))
    monkeypatch.setattr(connect_helper, "_remove_section", cannot)
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    line = json.loads(out.lines[0])
    assert line["status"] == "connect_failed" and "Remove [demo] from the credentials file" in line["detail"]


def test_a_password_saved_by_another_project_is_refused_before_listening(
    tmp_path: Path, art: Path, backend: _FakeBackend
) -> None:
    backend.data[("agami", "demo")] = "another-projects-password"  # same name, another artifacts dir
    out = _Lines()
    assert connect_helper.run(_spec(tmp_path), out, expires_in=1) == 0
    assert len(out.lines) == 1
    line = json.loads(out.lines[0])
    assert line["status"] == "refused" and "url" not in line
    assert "already exists on this computer" in line["detail"] and "another-projects-password" not in line["detail"]
    assert backend.data == {("agami", "demo"): "another-projects-password"}
    assert not _creds(art).exists()


def test_a_password_saved_while_the_page_was_open_is_not_overwritten(
    tmp_path: Path, art: Path, backend: _FakeBackend
) -> None:
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    backend.data[("agami", "demo")] = "another-projects-password"
    assert _post(url, SECRET) == 200
    t.join(10)
    assert out.wait(2)[1]["status"] == "refused"
    assert backend.data == {("agami", "demo"): "another-projects-password"}
    assert not _creds(art).exists()


def test_an_unreadable_store_is_refused_before_listening(tmp_path: Path, art: Path, monkeypatch) -> None:
    class Locked(_FakeBackend):
        def get_password(self, service: str, username: str) -> str | None:
            raise OSError("locked")

    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", lambda: Locked())
    out = _Lines()
    connect_helper.run(_spec(tmp_path), out, expires_in=1)
    line = json.loads(out.lines[0])
    assert len(out.lines) == 1 and line["status"] == "refused" and "unlock" in line["detail"]


def test_sso_does_not_check_the_store(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    backend.data[("agami", "demo")] = "another-projects-password"
    out = _Lines()
    connect_helper.run(_spec(tmp_path, auth="sso"), out, expires_in=1)
    assert json.loads(out.lines[0])["status"] == "connected"
    assert backend.data == {("agami", "demo"): "another-projects-password"}


# --- Structured refusals: the pane branches on `reason`, never on the prose in `detail`. ---


def _only_line(spec: object) -> dict:
    out = _Lines()
    assert connect_helper.run(spec, out, expires_in=1) == 0
    assert len(out.lines) == 1, out.lines
    return json.loads(out.lines[0])


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"profile": "Acme Prod"}, "bad_name"),
        ({"profile": 7}, "bad_name"),
        ({"fields": {"password": "x"}}, "bad_field"),
        ({"fields": {"host": "db.example.com #primary"}}, "bad_field"),
        ({"fields": "not-a-dict"}, "bad_field"),
        ({"db_type": "Postgres; x"}, "bad_field"),
        ({"auth": "token"}, "bad_field"),
    ],
)
def test_a_bad_spec_names_its_reason(tmp_path: Path, art: Path, backend: _FakeBackend, over: dict, reason: str) -> None:
    line = _only_line(_spec(tmp_path, **over))
    assert line["status"] == "refused" and line["reason"] == reason and line["detail"]


def test_a_spec_that_is_not_an_object_is_bad_spec(art: Path, backend: _FakeBackend) -> None:
    assert _only_line(["demo"])["reason"] == "bad_spec"


def test_an_existing_section_is_name_taken(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    _creds(art).parent.mkdir(parents=True)
    _creds(art).write_text("[demo]\ntype = sqlite\n", encoding="utf-8")
    assert _only_line(_spec(tmp_path))["reason"] == "name_taken"


def test_a_section_added_while_the_page_was_open_is_name_taken(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    t, out = _start(_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    _creds(art).parent.mkdir(parents=True)
    _creds(art).write_text("[demo]\ntype = sqlite\n", encoding="utf-8")
    assert _post(url, SECRET) == 200
    t.join(10)
    assert out.wait(2)[1]["reason"] == "name_taken"


def test_an_unreadable_credentials_file_has_a_reason(tmp_path: Path, art: Path, backend: _FakeBackend) -> None:
    _creds(art).parent.mkdir(parents=True)
    _creds(art).write_text("type = sqlite\n", encoding="utf-8")
    assert _only_line(_spec(tmp_path))["reason"] == "unreadable_credentials"


def test_linux_is_unsupported_os(tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch) -> None:
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Linux")
    line = _only_line(_spec(tmp_path))
    assert line["reason"] == "unsupported_os" and "manual" in line["detail"]


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_no_keyring_package_is_no_store(tmp_path: Path, art: Path, monkeypatch, system: str) -> None:
    # The real shape before `sm install` adds the extra: macOS or Windows, and `import keyring` fails.
    def missing() -> object:
        raise credential_store.CredentialStoreError("The OS credential store needs the keyring package.")

    monkeypatch.setattr(credential_store.platform, "system", lambda: system)
    monkeypatch.setattr(credential_store, "_backend", missing)
    line = _only_line(_spec(tmp_path))
    assert line["status"] == "refused" and line["reason"] == "no_store" and line["detail"]
    assert not _creds(art).exists()


def test_a_fail_backend_is_no_store(tmp_path: Path, art: Path, monkeypatch) -> None:
    fail_cls = type("Keyring", (), {"__module__": "keyring.backends.fail"})
    module = type("M", (), {"get_keyring": staticmethod(lambda: fail_cls())})
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", lambda: module)
    assert _only_line(_spec(tmp_path))["reason"] == "no_store"


def test_a_stale_install_is_stale_install(tmp_path: Path, art: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "execute_sql", type(sys)("execute_sql"))
    assert _only_line(_spec(tmp_path))["reason"] == "stale_install"


@pytest.mark.parametrize(
    "system,how",
    [("Darwin", "Keychain Access, search agami"), ("Windows", "Credential Manager, Windows Credentials, agami")],
)
def test_a_saved_password_says_how_to_clear_it(
    tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch, system: str, how: str
) -> None:
    monkeypatch.setattr(credential_store.platform, "system", lambda: system)
    backend.data[("agami", "demo")] = "another-projects-password"
    line = _only_line(_spec(tmp_path))
    assert line["reason"] == "saved_password_exists" and how in line["detail"]
    assert "another-projects-password" not in json.dumps(line)


def test_a_store_that_cannot_be_read_has_a_reason(tmp_path: Path, art: Path, monkeypatch) -> None:
    class Locked(_FakeBackend):
        def get_password(self, service: str, username: str) -> str | None:
            raise OSError("locked")

    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", lambda: Locked())
    assert _only_line(_spec(tmp_path))["reason"] == "store_error"


# --- A missing database driver: say which package, and leave nothing behind. ---


def _pg_spec(tmp_path: Path) -> dict:
    return _spec(tmp_path, db_type="postgres", fields={"host": "your-cluster.example.com", "port": 5432, "user": "demo"})


def test_a_missing_driver_is_reported_before_listening(tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg2", None)  # `import psycopg2` raises
    line = _only_line(_pg_spec(tmp_path))
    assert line["status"] == "missing_driver" and line["package"] == "psycopg2-binary" and line["detail"]
    assert "url" not in line and not _creds(art).exists() and backend.data == {}


def test_a_driver_gone_by_the_submit_rolls_back(tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch) -> None:
    t, out = _start(_pg_spec(tmp_path))
    url = out.wait(1)[0]["url"]
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    assert _post(url, SECRET) == 200
    t.join(10)
    final = out.wait(2)[1]
    assert final["status"] == "missing_driver" and final["package"] == "psycopg2-binary"
    assert not _creds(art).exists() and backend.data == {}
    assert SECRET not in json.dumps(final)


@pytest.mark.parametrize("gone,package", [(("pytz",), "pytz"), (("duckdb", "pytz"), "duckdb pytz")])
def test_only_the_missing_driver_packages_are_named(
    tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch, gone: tuple, package: str
) -> None:
    monkeypatch.setitem(sys.modules, "duckdb", type(sys)("duckdb"))  # present unless the case removes it
    for mod in gone:
        monkeypatch.setitem(sys.modules, mod, None)
    line = _only_line(_spec(tmp_path, db_type="duckdb", auth="sso"))
    assert line["status"] == "missing_driver" and line["package"] == package


def test_the_driver_table_is_connect_resolves(tmp_path: Path) -> None:
    # One table of drivers in code: the helper asks connect_resolve, it does not keep a second copy.
    import connect_resolve

    assert connect_resolve.driver_packages("postgres") == {"psycopg2": "psycopg2-binary"}
    assert connect_resolve.driver_packages("sqlite") == {}
    assert "_DRIVER_MOD" not in (SCRIPTS / "connect_helper.py").read_text(encoding="utf-8")


# --- A run that ends without a final line leaves nothing behind. ---


def test_an_unexpected_error_rolls_back(tmp_path: Path, art: Path, backend: _FakeBackend, monkeypatch) -> None:
    def boom(profile: str) -> tuple[str, str]:
        raise RuntimeError("driver crashed")

    monkeypatch.setattr(connect_helper, "_connection_test", boom)
    t, out = _start(_spec(tmp_path))
    assert _post(out.wait(1)[0]["url"], SECRET) == 200
    t.join(10)
    final = out.wait(2)[1]
    assert final["status"] == "connect_failed" and final["detail"]
    assert not _creds(art).exists() and backend.data == {}


_SIGTERM_RUNNER = r"""
import json, sys, time
from pathlib import Path
sys.path[:0] = [{scripts!r}, {pkg!r}]
import connect_helper, credential_store

store = Path({store!r})

class FileBackend:
    def _load(self):
        return json.loads(store.read_text()) if store.exists() else {{}}
    def set_password(self, s, u, p):
        d = self._load(); d[u] = p; store.write_text(json.dumps(d))
    def get_password(self, s, u):
        return self._load().get(u)
    def delete_password(self, s, u):
        d = self._load(); d.pop(u); store.write_text(json.dumps(d))
    def get_keyring(self):
        return type("Keyring", (), {{"__module__": "keyring.backends.macOS"}})()

credential_store.platform.system = lambda: "Darwin"
credential_store._backend = lambda: FileBackend()

def stuck(profile):
    Path({ready!r}).write_text("x")
    time.sleep(60)

connect_helper._connection_test = stuck
sys.exit(connect_helper.main(["--spec", {spec!r}]))
"""


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM delivery to a child is POSIX-only")
def test_sigterm_after_storing_rolls_back(tmp_path: Path) -> None:
    import signal
    import time

    art_dir = tmp_path / "art"
    store, ready, spec = tmp_path / "store.json", tmp_path / "ready", tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec(tmp_path)), encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(
        _SIGTERM_RUNNER.format(scripts=str(SCRIPTS), pkg=str(PKG_SRC), store=str(store), ready=str(ready), spec=str(spec)),
        encoding="utf-8",
    )
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art_dir)}
    p = subprocess.Popen([sys.executable, str(runner)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        url = json.loads(p.stdout.readline())["url"]
        assert _post(url, SECRET) == 200
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline, "the helper never reached the connection test"
            time.sleep(0.05)
        assert json.loads(store.read_text())  # stored, and the section written
        assert (art_dir / "local" / "credentials").exists()
        p.send_signal(signal.SIGTERM)
        p.wait(20)
    finally:
        if p.poll() is None:
            p.kill()
    assert not (art_dir / "local" / "credentials").exists()
    assert json.loads(store.read_text()) == {}
    assert SECRET not in p.stdout.read() + p.stderr.read()
