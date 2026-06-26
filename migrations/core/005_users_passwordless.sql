-- Allow passwordless users (OIDC social login) — an OIDC user has no password_hash.
--
-- SQLite has no ALTER COLUMN to drop NOT NULL, so rebuild the table (the portable way that runs on
-- both SQLite and Postgres as a single script): new table with a nullable password_hash, copy the
-- rows by explicit column list, drop + rename, then recreate the email index OIDC looks users up by.
-- The users table is recent and not yet deployed, so the copy is cheap.

CREATE TABLE users_new (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT,                     -- nullable now: NULL for an OIDC-only user
    email         TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    created       TEXT NOT NULL
);

-- Normalize existing emails (trim + lowercase) on the way over, matching how the app now stores
-- them — so the UNIQUE index below can't be tripped by a case/whitespace variant of an existing row.
INSERT INTO users_new (id, username, password_hash, email, status, created)
    SELECT id, username, password_hash, LOWER(TRIM(email)), status, created FROM users;

DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

-- UNIQUE so OIDC's email lookup resolves to exactly one user (NULLs stay distinct on both SQLite and
-- Postgres, so multiple password-only users with no email are fine). Emails are stored lowercased.
CREATE UNIQUE INDEX idx_users_email ON users (email);
