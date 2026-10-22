"""A stand-in `google.auth.exceptions`, shared by the tests that fake google-cloud-bigquery.

`_run_bigquery` imports `google.auth.exceptions` before it builds a client, so every fake of the
BigQuery driver has to register it, and register it the same way. The classes copy the real
hierarchy and the one attribute the runner reads: `GoogleAuthError` takes a `retryable` keyword and
exposes it as `.retryable`, and every other class here subclasses it, as in google-auth 2.x.
"""

from __future__ import annotations

import sys
import types


class GoogleAuthError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self._retryable = kwargs.get("retryable", False)

    @property
    def retryable(self) -> bool:
        return self._retryable


class TransportError(GoogleAuthError):
    pass


class RefreshError(GoogleAuthError):
    pass


class ReauthFailError(RefreshError):
    pass


class UserAccessTokenError(GoogleAuthError):
    pass


class DefaultCredentialsError(GoogleAuthError):
    pass


class OAuthError(GoogleAuthError):
    pass


class TimeoutError(GoogleAuthError):  # noqa: A001 - google-auth's own name for it
    pass


def install_google_auth(monkeypatch) -> types.ModuleType:
    """Register `google.auth` and `google.auth.exceptions` in `sys.modules`, and return the latter."""
    exceptions = types.ModuleType("google.auth.exceptions")
    for cls in (
        GoogleAuthError,
        TransportError,
        RefreshError,
        ReauthFailError,
        UserAccessTokenError,
        DefaultCredentialsError,
        OAuthError,
        TimeoutError,
    ):
        setattr(exceptions, cls.__name__, cls)
    auth = types.ModuleType("google.auth")
    auth.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "google.auth.exceptions", exceptions)
    return exceptions
