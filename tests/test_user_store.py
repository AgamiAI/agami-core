"""User store + password authentication.

SQLite-backed (the portable backend the gate runs on). Proves: argon2id hashing (never plaintext),
correct/wrong verify, the disabled-status gate, the env-seeded admin (idempotent), the rehash
upgrade path, and that a listing never leaks the hash.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("argon2")  # the password path needs the [server]-extra argon2-cffi

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import user_store  # noqa: E402
from store import Store  # noqa: E402


def _store() -> Store:
    s = Store.connect("sqlite://")
    s.run_migrations()
    return s


def test_create_then_authenticate_round_trip():
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw", email="you@example.com")
    assert user_store.authenticate(s, "admin", "s3cret-pw") is not None
    s.close()


def test_wrong_password_fails():
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw")
    assert user_store.authenticate(s, "admin", "wrong") is None
    assert user_store.authenticate(s, "nobody", "s3cret-pw") is None  # unknown user
    s.close()


def test_empty_password_is_rejected_at_create():
    s = _store()
    with pytest.raises(ValueError):
        user_store.create_user(s, "admin", "")
    assert user_store.list_users(s) == []  # nothing created
    s.close()


def test_passwordless_user_cannot_password_login():
    # An OIDC-only user has no password_hash and must never be loginable via the password path.
    s = _store()
    user_store.create_user(s, "oidc-user", password=None, email="you@example.com")
    assert user_store.get_user(s, "oidc-user")["password_hash"] is None
    assert user_store.authenticate(s, "oidc-user", "") is None
    assert user_store.authenticate(s, "oidc-user", "anything") is None
    s.close()


def test_get_user_by_email_normalizes_case_and_whitespace():
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw", email="  You@Example.com  ")
    # stored trimmed + lowercased; lookup normalizes the same, so casing/whitespace all resolve
    assert user_store.get_user_by_email(s, "you@example.com")["username"] == "admin"
    assert user_store.get_user_by_email(s, " YOU@EXAMPLE.COM ")["username"] == "admin"
    assert user_store.get_user_by_email(s, "missing@example.com") is None
    assert user_store.get_user_by_email(s, "   ") is None
    s.close()


def test_duplicate_email_is_rejected():
    # The UNIQUE email index makes OIDC's lookup one-to-one — a second user with the same email
    # (any casing) can't be created.
    import sqlite3

    s = _store()
    user_store.create_user(s, "alice", password=None, email="you@example.com")
    with pytest.raises(sqlite3.IntegrityError):
        user_store.create_user(s, "bob", password=None, email="YOU@example.com")
    s.close()


def test_authenticate_runs_a_verify_even_for_unknown_user(monkeypatch):
    # The anti-enumeration guard: a missing username still runs a verify (against the dummy hash),
    # so the call can't be distinguished by "did verify run". We assert the spy fired.
    s = _store()
    calls: list[str] = []
    real = user_store.verify_password
    monkeypatch.setattr(
        user_store, "verify_password", lambda h, p: (calls.append(h), real(h, p))[1]
    )
    assert user_store.authenticate(s, "ghost", "whatever") is None
    assert calls == [user_store._DUMMY_HASH]  # verified against the dummy, not skipped
    s.close()


def test_password_is_argon2id_and_never_plaintext():
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw")
    stored = user_store.get_user(s, "admin")["password_hash"]
    assert stored.startswith("$argon2id$")
    assert "s3cret-pw" not in stored
    s.close()


def test_disabled_status_blocks_login():
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw")
    user_store.set_status(s, "admin", "disabled")
    assert user_store.authenticate(s, "admin", "s3cret-pw") is None  # right pw, but disabled
    user_store.set_status(s, "admin", "active")
    assert user_store.authenticate(s, "admin", "s3cret-pw") is not None
    s.close()


def test_authenticate_returns_principal_with_username_subject():
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw")
    principal = user_store.authenticate(s, "admin", "s3cret-pw")
    assert principal is not None and principal.subject == "admin"
    s.close()


def test_list_users_never_exposes_the_hash():
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw")
    rows = user_store.list_users(s)
    assert rows and "password_hash" not in rows[0]
    s.close()


def test_seed_admin_from_env_creates_and_is_idempotent(monkeypatch):
    s = _store()
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", "seed-pw")
    assert user_store.seed_admin_from_env(s) == "admin"
    assert user_store.authenticate(s, "admin", "seed-pw") is not None
    # second run is a no-op (no duplicate, no clobber)
    assert user_store.seed_admin_from_env(s) is None
    assert len(user_store.list_users(s)) == 1
    s.close()


def test_seed_admin_noop_when_env_unset(monkeypatch):
    s = _store()
    for var in ("AGAMI_ADMIN_USERNAME", "AGAMI_ADMIN_PASSWORD", "AGAMI_ADMIN_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    assert user_store.seed_admin_from_env(s) is None
    assert user_store.list_users(s) == []
    s.close()


def test_seed_admin_provider_only_is_passwordless_and_named(monkeypatch):
    s = _store()
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", "Owner@Example.com")
    monkeypatch.delenv("AGAMI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("AGAMI_ADMIN_PROVIDER", "google")
    monkeypatch.setenv("AGAMI_ADMIN_FIRST_NAME", "Alex")
    monkeypatch.setenv("AGAMI_ADMIN_LAST_NAME", "Kim")
    assert user_store.seed_admin_from_env(s) == "owner@example.com"  # email is the identity, normalized
    row = user_store.get_user(s, "owner@example.com")
    assert row["oidc_provider"] == "google" and row["password_hash"] is None
    assert row["first_name"] == "Alex" and row["last_name"] == "Kim"
    # passwordless ⇒ no password login
    assert user_store.authenticate(s, "owner@example.com", "anything") is None
    s.close()


def test_seed_admin_requires_at_least_one_credential(monkeypatch):
    s = _store()
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", "owner@example.com")
    monkeypatch.delenv("AGAMI_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("AGAMI_ADMIN_PROVIDER", raising=False)
    assert user_store.seed_admin_from_env(s) is None  # email alone is not a credential
    assert user_store.list_users(s) == []
    s.close()


def test_seed_admin_ignores_an_unknown_provider(monkeypatch):
    s = _store()
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", "owner@example.com")
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", "seed-pw")
    monkeypatch.setenv("AGAMI_ADMIN_PROVIDER", "bogus")  # typo / unsupported → ignored, not pinned
    assert user_store.seed_admin_from_env(s) == "owner@example.com"
    assert user_store.get_user(s, "owner@example.com")["oidc_provider"] is None
    s.close()


def test_seed_admin_hybrid_password_and_provider(monkeypatch):
    s = _store()
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", "owner@example.com")
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", "seed-pw")
    monkeypatch.setenv("AGAMI_ADMIN_PROVIDER", "microsoft")
    assert user_store.seed_admin_from_env(s) == "owner@example.com"
    row = user_store.get_user(s, "owner@example.com")
    assert row["oidc_provider"] == "microsoft" and row["password_hash"] is not None
    assert user_store.authenticate(s, "owner@example.com", "seed-pw") is not None  # password still works
    s.close()


def test_seed_admin_whitespace_password_is_treated_as_unset(monkeypatch):
    s = _store()
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", "owner@example.com")
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", "   ")  # whitespace-only ⇒ unset, must not crash
    monkeypatch.setenv("AGAMI_ADMIN_PROVIDER", "google")
    assert user_store.seed_admin_from_env(s) == "owner@example.com"
    row = user_store.get_user(s, "owner@example.com")
    assert row["password_hash"] is None and row["oidc_provider"] == "google"
    s.close()


def test_seed_admin_is_idempotent_against_a_prior_mixed_case_username(monkeypatch):
    s = _store()
    # Simulate a legacy seed: an admin row created with the verbatim (mixed-case) username.
    user_store.create_user(
        s, username="Owner@Example.com", password="old-pw", email="owner@example.com"
    )
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", "Owner@Example.com")
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", "seed-pw")
    monkeypatch.delenv("AGAMI_ADMIN_PROVIDER", raising=False)
    assert user_store.seed_admin_from_env(s) is None  # already present ⇒ no duplicate admin row
    assert len(user_store.list_users(s)) == 1
    s.close()


def test_needs_rehash_upgrade_path(monkeypatch):
    # Simulate a stored hash made with a weaker profile: authenticate should re-store an upgraded
    # hash in place (the cross-tier cost-bump path) without changing the verified password.
    s = _store()
    user_store.create_user(s, "admin", "s3cret-pw")
    before = user_store.get_user(s, "admin")["password_hash"]
    # user_store imported needs_rehash by name, so patch the binding it actually calls.
    monkeypatch.setattr(user_store, "needs_rehash", lambda stored: True)
    assert user_store.authenticate(s, "admin", "s3cret-pw") is not None
    after = user_store.get_user(s, "admin")["password_hash"]
    assert after != before and after.startswith("$argon2id$")
    # the upgraded hash still verifies the same password
    assert user_store.authenticate(s, "admin", "s3cret-pw") is not None
    s.close()


def test_create_with_oidc_fields_and_bind_subject_no_clobber():
    s = _store()
    user_store.create_user(
        s, "alice", password=None, email="you@example.com", oidc_provider="google"
    )
    row = user_store.get_user(s, "alice")
    assert row["oidc_provider"] == "google" and row["oidc_subject"] is None
    # first bind sets it
    user_store.bind_oidc_subject(s, "alice", "sub-1")
    assert user_store.get_user(s, "alice")["oidc_subject"] == "sub-1"
    # a second bind is a no-op (the WHERE oidc_subject IS NULL guard) — can't rebind
    user_store.bind_oidc_subject(s, "alice", "sub-2")
    assert user_store.get_user(s, "alice")["oidc_subject"] == "sub-1"
    s.close()
