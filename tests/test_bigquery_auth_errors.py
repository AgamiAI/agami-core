"""A BigQuery credential failure is classified by its type, wherever it surfaces (#491).

google-auth refreshes a token lazily, inside the first API call, so `bigquery.Client(...)` succeeds
with a stale Application Default Credential and the failure arrives from `client.query()`,
`job.result()` or a later page of rows. It used to leave as a code-5 execution error whose text no
classifier arm reads, so the caller was told `other` ("an unexpected error on the server") and went
looking for a server bug instead of re-credentialing.

Two things decide the kind, and neither is the message:

* a refusal of the credential itself (`RefreshError`, `UserAccessTokenError`, `OAuthError`) is
  `auth`;
* a refresh that failed on the way to Google (a `TransportError`, raised bare or wrapped in a
  `RefreshError`, or any refresh error google-auth marks `retryable`) is `network`, because nothing
  is wrong with the credential and re-credentialing would not help.

The kind the runner decides is final: a word in google-auth's text cannot move it.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

PKG_SRC = Path(__file__).resolve().parents[1] / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402

from google_auth_fake import (  # noqa: E402
    DefaultCredentialsError,
    OAuthError,
    ReauthFailError,
    RefreshError,
    TransportError,
    UserAccessTokenError,
    install_google_auth,
)

_REAUTH = (
    "Reauthentication is needed. Please run `gcloud auth application-default login` to "
    "reauthenticate."
)
_INVALID_GRANT = (
    "('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant'})"
)
# What google-auth's text can carry, and so what must never reach the caller: the ADC file's path
# and the account it belongs to.
_RAW = "/home/demo/.config/gcloud/application_default_credentials.json for you@example.com"


class ServiceUnavailable(Exception):
    """Stands in for `google.api_core.exceptions.ServiceUnavailable` on a later page."""


def _install_bigquery(monkeypatch, *, fail_at: str, error: Exception | None) -> None:
    """A fake google-cloud-bigquery that raises `error` at `fail_at`, or returns no result set
    when `fail_at` is "nowhere"."""

    class _Rows:
        schema = [] if fail_at == "nowhere" else [types.SimpleNamespace(name="n")]

        def __iter__(self):
            yield [1]
            raise error  # the next page's fetch fails

    class _Job:
        def result(self, max_results=None):
            if fail_at == "result":
                raise error
            return _Rows()

    class _Client:
        def __init__(self, **kwargs):
            if fail_at == "init":
                raise error

        def query(self, sql, job_config=None):
            if fail_at == "query":
                raise error
            return _Job()

    gcloud = types.ModuleType("google.cloud")
    gcloud.bigquery = types.SimpleNamespace(Client=_Client, QueryJobConfig=lambda **k: object())
    goauth = types.ModuleType("google.oauth2")
    goauth.service_account = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "google.cloud", gcloud)
    monkeypatch.setitem(sys.modules, "google.oauth2", goauth)
    install_google_auth(monkeypatch)


def _run(monkeypatch, *, fail_at: str, error: Exception) -> execute_sql.ExecutorError:
    _install_bigquery(monkeypatch, fail_at=fail_at, error=error)
    with pytest.raises(execute_sql.ExecutorError) as exc:
        execute_sql._run_bigquery({"project": "demo-project"}, "SELECT n FROM t")
    return exc.value


def _wrapped_transport_error() -> RefreshError:
    # How the Compute Engine credential reports a metadata-server timeout: the transport failure is
    # both the argument and the cause of the `RefreshError` it raises.
    cause = TransportError("Failed to retrieve metadata from the GCE metadata server: timed out")
    try:
        raise RefreshError(cause) from cause
    except RefreshError as e:
        return e


_SITES = ["query", "result", "rows"]


@pytest.mark.parametrize("fail_at", _SITES)
@pytest.mark.parametrize(
    "error",
    [
        RefreshError(_REAUTH),
        RefreshError(_INVALID_GRANT),
        ReauthFailError("Reauthentication failed."),
        UserAccessTokenError("Failed to obtain access token"),
        OAuthError("Error code invalid_client"),
    ],
    ids=["reauth", "invalid_grant", "reauth_failed", "gcloud_logged_out", "oauth"],
)
def test_a_refused_credential_is_auth(monkeypatch, fail_at: str, error: Exception) -> None:
    raised = _run(monkeypatch, fail_at=fail_at, error=error)

    assert raised.code == 4
    assert raised.kind == "auth"
    assert raised.msg.startswith("BigQuery credentials could not be refreshed")


@pytest.mark.parametrize("fail_at", _SITES)
@pytest.mark.parametrize(
    "make_error",
    [
        lambda: RefreshError("temporarily_unavailable", retryable=True),
        _wrapped_transport_error,
        lambda: TransportError("Connection aborted."),
    ],
    ids=["retryable_refresh", "wrapped_transport", "bare_transport"],
)
def test_a_refresh_that_never_reached_google_is_network(monkeypatch, fail_at: str, make_error) -> None:
    raised = _run(monkeypatch, fail_at=fail_at, error=make_error())

    assert raised.code == execute_sql.FAILURE_KIND_TO_EXIT["network"]
    assert raised.kind == "network"
    assert "transient" in raised.msg


def test_a_transport_error_only_in_the_args_is_still_network(monkeypatch) -> None:
    # Some google-auth paths pass the transport failure as the argument without chaining it.
    error = RefreshError(TransportError("Max retries exceeded"))

    assert _run(monkeypatch, fail_at="query", error=error).kind == "network"


def test_the_type_decides_and_the_text_does_not(monkeypatch) -> None:
    # This text reads as `dsn` to the classifier's needles. The error is a non-retryable refusal
    # with no transport cause, so it is `auth`, and the needles must not get a vote.
    error = RefreshError("Name or service not known")
    raised = _run(monkeypatch, fail_at="query", error=error)

    assert raised.kind == "auth"
    assert execute_sql._classify_db_error(raised.msg, raised.code) == "dsn"  # what text alone says


def test_a_missing_default_credential_fails_at_client_init(monkeypatch) -> None:
    # `bigquery.Client()` resolves Application Default Credentials in its constructor, so this is
    # the only place a missing one can surface.
    raised = _run(
        monkeypatch, fail_at="init", error=DefaultCredentialsError("default credentials not found")
    )

    assert raised.code == 4
    assert execute_sql._classify_db_error(raised.msg, raised.code) == "auth"


def test_an_execution_error_is_still_classified_by_its_text(monkeypatch) -> None:
    raised = _run(monkeypatch, fail_at="query", error=ValueError("Unrecognized name: nme"))

    assert raised.code == 5
    assert raised.kind is None
    assert execute_sql._classify_db_error(raised.msg, raised.code) == "column_not_found"


def test_a_later_page_failing_is_an_execution_error(monkeypatch) -> None:
    error = ServiceUnavailable("503 GET https://bigquery.googleapis.com/…: Service unavailable")
    raised = _run(monkeypatch, fail_at="rows", error=error)

    assert raised.code == 5
    assert raised.msg.startswith("BigQuery execution error")


def test_a_statement_with_no_result_set_still_returns_empty(monkeypatch) -> None:
    # The early return moved inside the try that now covers row iteration; it must still be one.
    _install_bigquery(monkeypatch, fail_at="nowhere", error=None)

    result = execute_sql._run_bigquery({"project": "demo-project"}, "SELECT n FROM t")

    assert (result.columns, result.rows, result.truncated) == ([], [], False)


# ---------------------------------------------------------------------------
# What the caller receives, through the chokepoint
# ---------------------------------------------------------------------------


def _guarded(monkeypatch, *, fail_at: str, error: Exception):
    _install_bigquery(monkeypatch, fail_at=fail_at, error=error)
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "bigquery", "project": "demo-project"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    return execute_sql.execute_guarded(
        "SELECT n FROM t", "acme", None, executor=execute_sql.BUILTIN_EXECUTOR
    )


@pytest.mark.parametrize(
    ("make_error", "kind"),
    [
        (lambda: RefreshError(f"Reauthentication is needed: {_RAW}"), "auth"),
        (lambda: UserAccessTokenError(f"gcloud failed for {_RAW}"), "auth"),
        (lambda: RefreshError(f"Name or service not known: {_RAW}"), "auth"),
        (lambda: RefreshError(f"server_error: {_RAW}", retryable=True), "network"),
        (lambda: RefreshError(TransportError(f"metadata timed out: {_RAW}")), "network"),
    ],
    ids=["reauth", "gcloud_logged_out", "dsn_text", "retryable", "wrapped_transport"],
)
@pytest.mark.parametrize("fail_at", _SITES)
def test_the_caller_gets_the_kind_and_a_fixed_message(monkeypatch, fail_at, make_error, kind) -> None:
    env = _guarded(monkeypatch, fail_at=fail_at, error=make_error())

    assert env.status == "failed"
    assert env.failure.kind == kind
    assert env.failure.message == execute_sql._ERROR_MESSAGES[kind]
    # The fork transport carries the kind as an exit code, so it must have one of its own.
    assert execute_sql.EXIT_TO_FAILURE_KIND[execute_sql.FAILURE_KIND_TO_EXIT[kind]] == kind
    for leaked in ("application_default_credentials", "you@example.com", "Reauthentication"):
        assert leaked not in env.failure.message
    # The raw text is kept for the operator, off the caller's channel.
    assert _RAW in execute_sql._last_error_detail.get()


def test_a_later_page_failing_is_not_logged_as_a_bug(monkeypatch, caplog) -> None:
    error = ServiceUnavailable(f"503 Service unavailable: {_RAW}")
    with caplog.at_level(logging.ERROR):
        env = _guarded(monkeypatch, fail_at="rows", error=error)

    assert env.status == "failed"
    assert env.failure.kind == "other"
    assert env.failure.message == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    assert _RAW not in env.failure.message
    assert not any("unhandled" in r.getMessage() for r in caplog.records)
