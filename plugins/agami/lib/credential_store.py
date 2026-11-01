"""Secrets in the OS credential store, referenced from the credentials file as `keyring:agami/<profile>`.

The file keeps the reference and the OS (Keychain on macOS, Credential Manager on Windows) keeps the
secret, so the password is never on disk in plain text. `keyring` is an optional extra
(`agami-core[keyring]`) and is imported only inside functions: a profile with no reference never
loads it, and the base install stays dependency-free.
"""

from __future__ import annotations

import platform
from typing import Any

REF_PREFIX = "keyring:"
SERVICE = "agami"
_PLATFORMS = ("Darwin", "Windows")  # elsewhere the backends are headless/unreliable, so plain file only


class CredentialStoreError(Exception):
    """A one-line, user-facing message: what happened and how to fix it."""


def _backend() -> Any:
    """The `keyring` module, imported on first use so the extra stays optional."""
    try:
        import keyring
    except ImportError as e:
        raise CredentialStoreError(
            "The OS credential store needs the keyring package; install it with: pip install 'agami-core[keyring]'."
        ) from e
    return keyring


def supported() -> bool:
    """True on macOS/Windows when `keyring` is installed and has a working (non-fail) backend."""
    if platform.system() not in _PLATFORMS:
        return False
    try:
        backend = _backend()
        active = backend.get_keyring() if hasattr(backend, "get_keyring") else backend
    except Exception:
        return False
    return not type(active).__module__.startswith("keyring.backends.fail")


def put(profile: str, secret: str) -> str:
    """Store `secret` for `profile` and return the reference to write into the credentials file."""
    try:
        _backend().set_password(SERVICE, profile, secret)
    except CredentialStoreError:
        raise
    except Exception as e:
        raise CredentialStoreError(
            f"Could not save the password for [{profile}] in the OS credential store; "
            "unlock it and reconnect, or keep the password in the credentials file instead."
        ) from e
    return f"{REF_PREFIX}{SERVICE}/{profile}"


def resolve(value: str, *, profile: str) -> str:
    """`value` unchanged unless it is a `keyring:` reference, in which case the stored secret."""
    if not value.startswith(REF_PREFIX):
        return value
    service, _, username = value[len(REF_PREFIX) :].partition("/")
    try:
        secret = _backend().get_password(service, username)
    except CredentialStoreError:
        raise
    except Exception as e:
        raise CredentialStoreError(
            f"Could not read [{value}] from the OS credential store for profile [{profile}]; "
            "unlock it and retry, or reconnect to store the password again."
        ) from e
    if secret is None:
        raise CredentialStoreError(
            f"No password found in the OS credential store for [{username}] (profile [{profile}]); "
            "reconnect to store it again."
        )
    return secret
