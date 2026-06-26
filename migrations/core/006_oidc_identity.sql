-- Bind an OIDC user to a single identity provider + subject, so a second IdP can't be confused for
-- the first (an attacker with the same email at another provider must still fail the provider check).
-- oidc_provider is set at onboarding (which IdP the user signs in with); oidc_subject (the IdP's
-- immutable `sub`) is captured on first login and must match thereafter. Both NULL for a
-- password-only user. Plain ADD COLUMN is portable across SQLite and Postgres (no table rebuild).

ALTER TABLE users ADD COLUMN oidc_provider TEXT;
ALTER TABLE users ADD COLUMN oidc_subject TEXT;

-- Bind any pre-existing passwordless (OIDC-only) user to Google — the only provider that existed
-- before this binding — so the new provider-binding gate doesn't lock them out on upgrade. (A
-- password user has a non-NULL hash and is left untouched.)
UPDATE users SET oidc_provider = 'google' WHERE oidc_provider IS NULL AND password_hash IS NULL;
