-- Refresh tokens for the OAuth provider — long-lived, rotating, revocable renewal of the
-- short-lived access JWTs (RFC 6749 §6). Without this a client (claude.ai) had to redo the full
-- login every time the 1h access token expired; now it silently mints new access tokens.
--
-- Only the sha256 HASH of each token is stored (never the plaintext), so a DB read can't mint a
-- token. Rotation: each successful refresh revokes the presented token and issues a new one in the
-- same `family`; presenting an already-revoked token (a replay of a rotated/stolen token) revokes
-- the whole family — the OAuth 2.1 reuse-detection posture for public clients. Portable SQL (sqlite
-- + postgres), keyed by an app-minted value like the rest of the schema.

CREATE TABLE oauth_refresh_token (
    token_hash TEXT PRIMARY KEY,               -- sha256 hex of the opaque refresh token
    family     TEXT NOT NULL,                  -- rotation lineage; reuse of a revoked token kills it
    client_id  TEXT,                           -- the client the token was issued to (bound on refresh)
    username   TEXT NOT NULL,                  -- the principal the token renews
    expires_at TEXT NOT NULL,                  -- absolute idle-expiry; rotation carries it forward
    revoked    INTEGER NOT NULL DEFAULT 0,     -- set on rotation, logout, or family compromise
    created    TEXT NOT NULL
);

CREATE INDEX oauth_refresh_token_family ON oauth_refresh_token (family);
CREATE INDEX oauth_refresh_token_username ON oauth_refresh_token (username);
