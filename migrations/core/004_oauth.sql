-- agami-core OAuth provider schema — the server's own authorize/token flow.
--
-- oauth_client: clients that registered (claude.ai via the minimal RFC-7591 endpoint). Minimal by
-- design — full DCR (client auth, rotation) is deferred. oauth_state: one row per issued
-- authorization code, bound to its PKCE challenge + redirect_uri + the authenticated username, with
-- a short expiry and a single-use flag. Keyed by app-minted values (no SERIAL), like the rest.

CREATE TABLE oauth_client (
    client_id     TEXT PRIMARY KEY,        -- minted by /oauth/register (uuid4 hex)
    redirect_uris TEXT,                     -- space-separated, as registered (may be empty)
    created       TEXT NOT NULL
);

CREATE TABLE oauth_state (
    code           TEXT PRIMARY KEY,        -- the authorization code (secrets.token_urlsafe)
    client_id      TEXT,
    redirect_uri   TEXT,
    code_challenge TEXT,                     -- PKCE S256 challenge; verified at token exchange
    username       TEXT NOT NULL,           -- the authenticated principal the code stands in for
    expires_at     TEXT NOT NULL,           -- short-lived; expired codes are rejected
    used           INTEGER NOT NULL DEFAULT 0,  -- single-use: set on first successful exchange
    created        TEXT NOT NULL
);
