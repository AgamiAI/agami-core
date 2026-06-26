-- Bind an OIDC user to a single identity provider + subject, so a second IdP can't be confused for
-- the first (an attacker with the same email at another provider must still fail the provider check).
-- oidc_provider is set at onboarding (which IdP the user signs in with); oidc_subject (the IdP's
-- immutable `sub`) is captured on first login and must match thereafter. Both NULL for a
-- password-only user. Plain ADD COLUMN is portable across SQLite and Postgres (no table rebuild).

ALTER TABLE users ADD COLUMN oidc_provider TEXT;
ALTER TABLE users ADD COLUMN oidc_subject TEXT;
