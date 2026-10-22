"""An expired Application Default Credential on BigQuery is an `auth` failure (#491).

google-auth refreshes a token lazily, inside the first API call, so `bigquery.Client(...)` succeeds
with a stale ADC and the `RefreshError` arrives from `client.query()`, `job.result()` or a later page
of rows. It used to leave as a code-5 execution error whose text no classifier arm reads, so the
caller was told `other` — "an unexpected error on the server" — and looked for a server bug instead
of re-credentialing.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PKG_SRC = Path(__file__).resolve().parents[1] / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402


class RefreshError(Exception):
    pass


class DefaultCredentialsError(Exception):
    pass


_REAUTH = (
    "Reauthentication is needed. Please run `gcloud auth application-default login` to "
    "reauthenticate."
)


def _install_bigquery(monkeypatch, *, fail_at: str, error: Exception) -> None:
    """A fake google-cloud-bigquery whose token refresh fails at `fail_at`."""

    class _Rows:
        schema = [types.SimpleNamespace(name="n")]

        def __iter__(self):
            yield [1]
            raise error  # the next page's fetch is the call that refreshes the token

    class _Job:
        def result(self, max_results=None):
            if fail_at == "result":
                raise error
            return _Rows()

    class _Client:
        def __init__(self, **kwargs):
            pass

        def query(self, sql, job_config=None):
            if fail_at == "query":
                raise error
            return _Job()

    gcloud = types.ModuleType("google.cloud")
    gcloud.bigquery = types.SimpleNamespace(Client=_Client, QueryJobConfig=lambda **k: object())
    goauth = types.ModuleType("google.oauth2")
    goauth.service_account = types.SimpleNamespace()
    gauth = types.ModuleType("google.auth")
    gauth.exceptions = types.SimpleNamespace(
        RefreshError=RefreshError, DefaultCredentialsError=DefaultCredentialsError
    )
    monkeypatch.setitem(sys.modules, "google.cloud", gcloud)
    monkeypatch.setitem(sys.modules, "google.oauth2", goauth)
    monkeypatch.setitem(sys.modules, "google.auth", gauth)


@pytest.mark.parametrize("fail_at", ["query", "result", "rows"])
@pytest.mark.parametrize(
    "error",
    [
        RefreshError(_REAUTH),
        RefreshError(
            "('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant'})"
        ),
        DefaultCredentialsError("Your default credentials were not found."),
    ],
    ids=["reauth", "invalid_grant", "no_default_credentials"],
)
def test_a_lazy_credentials_failure_is_auth(monkeypatch, fail_at: str, error: Exception) -> None:
    _install_bigquery(monkeypatch, fail_at=fail_at, error=error)

    with pytest.raises(execute_sql.ExecutorError) as exc:
        execute_sql._run_bigquery({"project": "demo-project"}, "SELECT n FROM t")

    assert exc.value.code == 4
    assert execute_sql._classify_db_error(exc.value.msg, exc.value.code) == "auth"


def test_an_execution_error_is_still_not_auth(monkeypatch) -> None:
    # The new clause catches by TYPE, so an ordinary query failure keeps its own classification.
    _install_bigquery(monkeypatch, fail_at="query", error=ValueError("Unrecognized name: nme"))

    with pytest.raises(execute_sql.ExecutorError) as exc:
        execute_sql._run_bigquery({"project": "demo-project"}, "SELECT nme FROM t")

    assert exc.value.code == 5
    assert execute_sql._classify_db_error(exc.value.msg, exc.value.code) == "column_not_found"
