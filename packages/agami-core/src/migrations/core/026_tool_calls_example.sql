-- The example the client says it consulted before this `execute_sql` (#376).
--
-- On a hosted deployment a datasource with stored examples requires the call to name one example id
-- returned by `get_prompt_examples`, and whether the statement followed it. The id proves the client
-- looked at the examples; the use says whether one of them fit.
--
--   example_id   the id the client sent (checked to exist before the statement ran)
--   example_use  'followed' or 'shown_only' as sent — self-reported, never verified against the SQL
--   both NULL    any other tool, an execute_sql that sent neither, or a row written before 026
--
-- Kept apart from `basis` (020), which is optional free text nothing checks: these two are what the
-- gate decided on, so a reader can count how often the examples fit without parsing JSON.
--
-- Forward-only and portable (SQLite + Postgres). No `IF NOT EXISTS` — SQLite's ALTER does not accept
-- it; re-run safety comes from the runner's applied-filename ledger. No index: nothing filters on it.
ALTER TABLE tool_calls ADD COLUMN example_id TEXT;
ALTER TABLE tool_calls ADD COLUMN example_use TEXT;
