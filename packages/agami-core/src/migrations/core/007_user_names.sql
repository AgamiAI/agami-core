-- Add the teammate's display name (first + last), captured when an admin onboards them.
--
-- The admin's "Add user" form collects email + first/last name; email stays the login identity
-- (a managed user's username is set to their email), so these two columns are display-only and
-- both nullable (a user can exist without a name set). Plain ADD COLUMN is portable across SQLite
-- and Postgres (no table rebuild).

ALTER TABLE users ADD COLUMN first_name TEXT;
ALTER TABLE users ADD COLUMN last_name TEXT;
