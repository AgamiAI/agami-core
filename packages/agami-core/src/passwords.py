"""Password hashing — argon2id, the only place credentials are turned into a stored secret.

We never hand-roll the KDF: hashing goes through `argon2-cffi` (the OWASP-first argon2id, a
`[server]`-extra dependency — never in the local install). The stored value is a PHC string
(`$argon2id$v=19$m=...,t=...,p=...$salt$hash`) that encodes the algorithm and every cost parameter
inline, so it verifies in any language and a future backend can read the same column unchanged.

`verify_password` returns a bool (never raises on a wrong password) and is timing-safe — argon2's
verify compares in constant time, so a caller can't distinguish "no such user" from "wrong password"
by timing as long as both paths run a verify (the user store does).
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# One shared hasher — its parameters define the cost. Defaults are argon2-cffi's recommended profile;
# `needs_rehash` lets us raise them later and upgrade existing hashes on next login (the cross-tier
# cost-bump path) without a migration.
_PH = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a plaintext password to an argon2id PHC string. The salt is generated per call."""
    return _PH.hash(plain)


def verify_password(stored: str, plain: str) -> bool:
    """True iff `plain` matches the stored argon2id hash. Returns False (never raises) on a mismatch
    or a malformed stored hash, so callers branch on a bool, not an exception."""
    try:
        return _PH.verify(stored, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    """True if the stored hash was made with weaker parameters than the current profile — the caller
    re-hashes on the next successful login to upgrade it in place."""
    return _PH.check_needs_rehash(stored)
