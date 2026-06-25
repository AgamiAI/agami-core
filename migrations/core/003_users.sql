-- agami-core auth schema — per-user identity for the hosted server.
--
-- Flat access: there is deliberately NO role/permission column (roles are a paid tier). `status`
-- is the create/disable flag ('active' | 'disabled'); a disabled user cannot authenticate. The
-- password_hash is an argon2id PHC string (algo + cost params encoded inline) — never plaintext.
-- Keyed by an app-minted id (no SERIAL), like the runtime tables.

CREATE TABLE users (
    id            TEXT PRIMARY KEY,        -- minted by the server at create time (uuid4 hex)
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,           -- argon2id PHC string
    email         TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    created       TEXT NOT NULL
);
