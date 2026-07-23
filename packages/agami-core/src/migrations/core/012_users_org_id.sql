-- Tenant-scope the users table (F14 / ACE-057). The serving + runtime tables already carry `org_id`
-- (added earlier); `users` is the last per-customer table that didn't. Adding it here lets an
-- authorized-user roster ride along when a self-hosted deployment is lifted into hosted as one tenant.
--
-- Plain ADD COLUMN with a constant default is portable across SQLite + Postgres (no table rebuild) —
-- same shape as 007_user_names.sql. It DEFAULTs to the 'local' sentinel so existing rows land on it;
-- the minted uuid isn't known here (a static .sql migration can't generate one — SQLite has no uuid
-- function, and run_migrations applies static SQL only), so model_deploy runs a code backfill right
-- after migrations that moves every 'local' row (here and in the serving/runtime tables) onto the
-- resolved org_id.
--
-- Unlike the serving tables, `org_id` is NOT part of the primary key here: `users.id` (uuid4) is the
-- PK and logins resolve by the global `UNIQUE(username)` — correct for single-tenant (N=1). An indexed
-- non-PK column is the right shape; `username` stays globally unique (a per-org unique is a paid
-- multi-tenant concern, out of scope).

ALTER TABLE users ADD COLUMN org_id TEXT NOT NULL DEFAULT 'local';
CREATE INDEX idx_users_org ON users (org_id);
