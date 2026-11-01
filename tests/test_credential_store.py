"""`keyring:` references: a secret kept in the OS credential store, resolved when credentials load.

The library is an optional extra, so every test here works without it by swapping the module's
backend accessor for an in-memory fake; only the lazy-import test needs a real interpreter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import credential_store  # noqa: E402
import execute_sql  # noqa: E402


class _FakeBackend:
    """The three calls the module makes, backed by a dict."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, secret: str) -> None:
        self.data[(service, username)] = secret

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))


@pytest.fixture(autouse=True)
def _darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution is macOS/Windows only; pin it so the suite behaves the same on any CI runner."""
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    fake = _FakeBackend()
    monkeypatch.setattr(credential_store, "_backend", lambda: fake)
    return fake


def test_plain_value_unchanged_and_keyring_never_imported() -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import credential_store as c; "
        "assert c.resolve('hunter2', profile='demo') == 'hunter2'; "
        "assert 'keyring' not in sys.modules"
    )
    done = subprocess.run([sys.executable, "-c", code, str(PKG_SRC)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_put_then_resolve_round_trip(backend: _FakeBackend) -> None:
    ref = credential_store.put("demo", "s3cret")
    assert ref == "keyring:agami/demo"
    assert backend.data == {("agami", "demo"): "s3cret"}
    assert credential_store.resolve(ref, profile="demo") == "s3cret"


def test_missing_entry_raises_with_fix(backend: _FakeBackend) -> None:
    with pytest.raises(credential_store.CredentialStoreError) as exc:
        credential_store.resolve("keyring:agami/gone", profile="demo")
    assert "gone" in str(exc.value) and "reconnect" in str(exc.value)
    assert "\n" not in str(exc.value)


@pytest.mark.parametrize("ref", ["keyring:login/other", "keyring:agami", "keyring:agami/", "keyring:/demo"])
def test_foreign_or_malformed_reference_rejected(backend: _FakeBackend, ref: str) -> None:
    backend.data[("login", "other")] = "not-yours"
    with pytest.raises(credential_store.CredentialStoreError, match=r"keyring:agami/<profile>") as exc:
        credential_store.resolve(ref, profile="demo")
    assert "not-yours" not in str(exc.value) and "\n" not in str(exc.value)


def test_resolve_refused_off_macos_and_windows(backend: _FakeBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    backend.data[("agami", "demo")] = "s3cret"
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Linux")
    with pytest.raises(credential_store.CredentialStoreError, match="only on macOS and Windows"):
        credential_store.resolve("keyring:agami/demo", profile="demo")
    assert credential_store.resolve("plain", profile="demo") == "plain"


def test_backend_error_raises_with_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def get_password(self, service: str, username: str) -> str:
            raise OSError("locked")

    monkeypatch.setattr(credential_store, "_backend", lambda: Broken())
    with pytest.raises(credential_store.CredentialStoreError, match="reconnect"):
        credential_store.resolve("keyring:agami/demo", profile="demo")


def test_keyring_not_installed_raises_with_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # makes `import keyring` raise ImportError
    with pytest.raises(credential_store.CredentialStoreError, match=r"agami-core\[keyring\]"):
        credential_store.resolve("keyring:agami/demo", profile="demo")
    assert credential_store.supported() is False


def test_put_without_keyring_raises_with_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(credential_store.CredentialStoreError, match=r"agami-core\[keyring\]"):
        credential_store.put("demo", "x")


def test_put_backend_error_raises_with_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def set_password(self, service: str, username: str, secret: str) -> None:
            raise OSError("denied")

    monkeypatch.setattr(credential_store, "_backend", lambda: Broken())
    with pytest.raises(credential_store.CredentialStoreError, match="credential store"):
        credential_store.put("demo", "x")


@pytest.mark.parametrize("system,expected", [("Darwin", True), ("Windows", True), ("Linux", False)])
def test_supported_platforms(monkeypatch: pytest.MonkeyPatch, system: str, expected: bool) -> None:
    class Good:
        pass

    monkeypatch.setattr(credential_store.platform, "system", lambda: system)
    monkeypatch.setattr(credential_store, "_backend", lambda: Good())
    assert credential_store.supported() is expected


def test_supported_false_for_fail_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        pass

    Backend.__module__ = "keyring.backends.fail"
    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", lambda: Backend())
    assert credential_store.supported() is False


def test_supported_false_when_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise credential_store.CredentialStoreError("no keyring")

    monkeypatch.setattr(credential_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(credential_store, "_backend", boom)
    assert credential_store.supported() is False


def test_backend_returns_keyring_module(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setitem(sys.modules, "keyring", sentinel)
    assert credential_store._backend() is sentinel


def _creds_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    creds = tmp_path / "credentials"
    creds.write_text(f"[demo]\ntype = postgres\nhost = h\nuser = u\npassword = {password}\n")
    creds.chmod(0o600)
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", creds)
    for name in ("DATASOURCE_URL", "LOCAL_DATASOURCE_URL", "DATASOURCE_URL__DEMO", "LOCAL_DATASOURCE_URL__DEMO"):
        monkeypatch.delenv(name, raising=False)


def test_load_credentials_resolves_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: _FakeBackend) -> None:
    credential_store.put("demo", "s3cret")
    _creds_file(tmp_path, monkeypatch, "keyring:agami/demo")
    creds = execute_sql._load_credentials("demo")
    assert creds["password"] == "s3cret"
    assert creds["user"] == "u"


def test_load_credentials_ref_error_is_executor_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: _FakeBackend
) -> None:
    _creds_file(tmp_path, monkeypatch, "keyring:agami/demo")
    with pytest.raises(execute_sql.ExecutorError) as exc:
        execute_sql._load_credentials("demo")
    assert exc.value.code == 2
    assert "reconnect" in exc.value.msg
