"""An expired BigQuery credential is reported as `auth`, not as an unexpected server error.

With Application Default Credentials, `bigquery.Client(...)` succeeds on a stale credential
because google-auth refreshes the token lazily. The refresh — and its failure — happens inside
`client.query()` / `job.result()`, on the execution path, where every other exception is an
execution error. Classified from its text, google-auth's "Reauthentication is needed" matched no
rule and came out as `other`, which sends a caller looking for a server bug instead of telling the
operator to re-credential.

No network: the google modules are fakes, so the classes the executor checks against are the
ones these tests raise.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402

PROFILE = "acme"
_REAUTH_TEXT = (
    "Reauthentication is needed. Please run `gcloud auth application-default login` to "
    "reauthenticate. demo-secret-detail"
)


class GoogleAuthError(Exception):
    pass


class RefreshError(GoogleAuthError):
    pass


class DefaultCredentialsError(GoogleAuthError):
    pass


class TransportError(GoogleAuthError):
    pass


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install_google(monkeypatch, *, raise_on: str, error: BaseException) -> None:
    """Fake google-cloud-bigquery whose `query` or `result` raises `error`."""

    class _Job:
        def result(self, max_results=None):
            if raise_on == "result":
                raise error
            raise AssertionError("the test expected the failure before rows were read")

    class _Client:
        def __init__(self, **kwargs):
            pass

        def query(self, sql, job_config=None):
            if raise_on == "query":
                raise error
            return _Job()

    bigquery = _module("google.cloud.bigquery", Client=_Client, QueryJobConfig=lambda **k: object())
    service_account = _module("google.oauth2.service_account")
    exceptions = _module(
        "google.auth.exceptions",
        GoogleAuthError=GoogleAuthError,
        RefreshError=RefreshError,
        DefaultCredentialsError=DefaultCredentialsError,
        TransportError=TransportError,
    )
    google_auth = _module("google.auth", exceptions=exceptions)
    google = _module("google", auth=google_auth)
    google.__path__ = []  # a package, so `from google.auth import exceptions` resolves
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", google_auth)
    monkeypatch.setitem(sys.modules, "google.auth.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "google.cloud", _module("google.cloud", bigquery=bigquery))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery)
    monkeypatch.setitem(sys.modules, "google.oauth2", _module("google.oauth2",
                                                              service_account=service_account))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", service_account)


class _BigQueryAdapter:
    """Runs the real `_run_bigquery` against the fake modules, through the guarded path."""

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        return execute_sql._run_bigquery({"project": "your-project"}, vetted_sql)


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    db = tmp_path / "warehouse.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{db}")
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    return tmp_path


def _run(monkeypatch, *, raise_on: str, error: BaseException):
    _install_google(monkeypatch, raise_on=raise_on, error=error)
    return execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None, executor=_BigQueryAdapter(), no_safety=True
    )


@pytest.mark.parametrize("raise_on", ["query", "result"])
@pytest.mark.parametrize(
    "error",
    [
        RefreshError(_REAUTH_TEXT),
        RefreshError("('invalid_grant: Token has been expired or revoked.', demo-secret-detail)"),
        DefaultCredentialsError("Could not automatically determine credentials. demo-secret-detail"),
    ],
    ids=["reauth", "invalid_grant", "no_default_credentials"],
)
def test_a_credential_that_cannot_be_refreshed_is_auth(monkeypatch, warehouse, raise_on, error):
    env = _run(monkeypatch, raise_on=raise_on, error=error)
    assert env.status == "failed"
    assert env.failure.kind == "auth"
    assert env.failure.message == execute_sql._ERROR_MESSAGES["auth"]
    # The google text stays in the server log; the caller gets the fixed sentence only.
    assert "demo-secret-detail" not in env.failure.message
    assert "gcloud" not in env.failure.message


def test_the_executor_raises_the_auth_exit_code_for_a_refresh_failure(monkeypatch):
    _install_google(monkeypatch, raise_on="query", error=RefreshError(_REAUTH_TEXT))
    with pytest.raises(execute_sql.ExecutorError) as exc:
        execute_sql._run_bigquery({"project": "your-project"}, "SELECT 1")
    assert exc.value.code == execute_sql.FAILURE_KIND_TO_EXIT["auth"]


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        # A google-auth error that is not a credential failure: the token endpoint was unreachable.
        # Matching the base class would wrongly tell the operator to re-credential.
        (TransportError("the token endpoint could not be reached"), "other"),
        (Exception("400 Unrecognized name: ssn at [1:8]"), "column_not_found"),
        (Exception("Access Denied: Table p: User does not have bigquery.tables.get permission"),
         "permission"),
        (Exception("something no rule in the classifier reads"), "other"),
    ],
    ids=["transport", "unrecognized_name", "access_denied", "unread"],
)
def test_other_bigquery_failures_classify_as_before(monkeypatch, warehouse, error, kind):
    env = _run(monkeypatch, raise_on="result", error=error)
    assert env.status == "failed"
    assert env.failure.kind == kind


def test_without_google_auth_installed_an_execution_failure_is_unchanged(monkeypatch):
    """The check imports google-auth lazily and tolerates its absence, as the fakes elsewhere
    in the suite install a `google` package with no `auth` module."""
    _install_google(monkeypatch, raise_on="query", error=Exception("boom"))
    monkeypatch.setitem(sys.modules, "google.auth", None)
    monkeypatch.setitem(sys.modules, "google.auth.exceptions", None)
    with pytest.raises(execute_sql.ExecutorError) as exc:
        execute_sql._run_bigquery({"project": "your-project"}, "SELECT 1")
    assert exc.value.code == 5
