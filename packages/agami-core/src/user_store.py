"""The `users` store + the password credential check — per-user identity for the hosted server.

This is the credential authenticator the OAuth authorize page calls: `authenticate` turns a
username/password into a `Principal` (the identity the token endpoint then mints a JWT for). It is
NOT the bearer-token `AuthProvider` (that validates the issued token — a later, separate adapter).
Flat access only: there is no role column (roles are paid).

Thin SQL over the portable `Store` (same shape as `model_store.py`), so it runs on SQLite (CI) and
Postgres (prod) unchanged. Passwords never touch this module in plaintext beyond the moment they're
hashed/verified via `passwords.py`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from passwords import hash_password, needs_rehash, verify_password
from ports import Principal
from store import Store

_ACTIVE = "active"

# A throwaway argon2id hash that no password verifies against. `authenticate` verifies against it on
# the user-absent path so every code path pays the same KDF cost — without it, a missing username
# returns far faster than a wrong password and leaks which usernames exist (an enumeration oracle).
_DUMMY_HASH = hash_password(uuid4().hex)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_user(
    store: Store,
    username: str,
    password: str,
    email: str | None = None,
    status: str = _ACTIVE,
) -> str:
    """Create a user with an argon2id-hashed password; returns the minted id. Raises if the username
    already exists (the UNIQUE constraint) — callers that want create-if-absent check first."""
    if not password:
        raise ValueError("password must not be empty")
    user_id = uuid4().hex
    store.execute(
        "INSERT INTO users (id, username, password_hash, email, status, created) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, hash_password(password), email, status, _now_iso()),
    )
    store.commit()
    return user_id


def get_user(store: Store, username: str) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM users WHERE username = ?", (username,))
    return rows[0] if rows else None


def list_users(store: Store) -> list[dict[str, Any]]:
    """Identity rows only — never selects `password_hash` (so a listing can't leak the secret)."""
    return store.query("SELECT id, username, email, status, created FROM users ORDER BY username")


def set_status(store: Store, username: str, status: str) -> None:
    store.execute("UPDATE users SET status = ? WHERE username = ?", (status, username))
    store.commit()


def authenticate(store: Store, username: str, password: str) -> Principal | None:
    """The credential check: an active user whose password verifies → a `Principal`, else None.

    A disabled user fails even with the right password. Every path runs exactly one argon2 verify
    (against a dummy hash when the user is absent/ineligible), so response time never reveals whether
    a username exists — closing a timing-enumeration oracle. On success, opportunistically upgrade
    the stored hash if the cost profile has risen (the cross-tier cost-bump path)."""
    user = get_user(store, username)
    stored_hash = user["password_hash"] if user else _DUMMY_HASH
    verified = verify_password(stored_hash, password)
    if user is None or user["status"] != _ACTIVE or not verified:
        return None
    if needs_rehash(user["password_hash"]):
        try:
            store.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(password), username),
            )
            store.commit()
        except Exception:
            # Best-effort upgrade — a DB hiccup on the opportunistic rehash must not fail an
            # otherwise-valid login.
            pass
    return Principal(subject=username)


def seed_admin_from_env(store: Store) -> str | None:
    """Seed the initial admin from AGAMI_ADMIN_USERNAME / AGAMI_ADMIN_PASSWORD if both are set.

    Create-if-absent and idempotent: a redeploy never duplicates the admin or clobbers a
    password the admin has since changed. Returns the username seeded, or None if unset/already
    present. (Rotating the seed password is out of scope — that's a later reset path.)"""
    username = os.environ.get("AGAMI_ADMIN_USERNAME", "").strip()
    password = os.environ.get("AGAMI_ADMIN_PASSWORD", "")
    if not username or not password:
        return None
    if get_user(store, username) is not None:
        return None
    create_user(store, username, password, status=_ACTIVE)
    return username
