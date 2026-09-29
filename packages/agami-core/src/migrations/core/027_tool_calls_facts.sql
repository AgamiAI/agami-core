-- What each tool call was sent, which names missed, and how much it sent back.
--
-- The log could not say how often the AI gets a call wrong. A wrong name that did not sink the whole
-- call was recorded as a plain success, most of what a call was sent was dropped, and the size of the
-- reply was nowhere. These five columns record those facts at the point where they are certain,
-- instead of leaving a later reader to reconstruct them from prose.
--
--   arguments          JSON {"values": {...}, "truncated": bool}: every parameter the client sent,
--                      as sent, except those with a column of their own (datasource, sql, raw_query,
--                      user_question, thread_id, correlation_id, basis, client_model, and example on
--                      execute_sql), so nothing is stored twice. NULL when only those were sent.
--   missed             JSON {"entries": [{"kind", "name", "did_you_mean"}], "truncated": bool}: each
--                      name the semantic model lacked, by kind (datasource, area, table, metric,
--                      misplaced_table, argument), with the names the reply offered instead.
--   miss_count         how many names missed, so a partial miss can be filtered without reading
--                      JSON. The true total even when `missed` was cut. NULL on a call with none.
--   result_chars       the exact length of the text the client received.
--   result_tokens_est  result_chars / 4, rounded up: the ratio the schema budget already uses. The
--                      exact count sits beside it so the estimate can be recomputed later.
--
-- `success` keeps its meaning. A call that answered with one name missing is still success=1, with
-- a non-zero miss_count; flipping it would change every existing error count in the activity views.
--
-- CALLER TEXT, BOUNDED BY THE WRITER. The parameters and the missed names are what the AI chose to
-- send. They are stored as bounded JSON data with control characters replaced, never interpolated
-- into a sentence, so a caller cannot grow a row past its bound, break a line in stored text, or make
-- its words read as the server's. `arguments` is at most 8,000 characters and `missed` at most 4,000,
-- and each says when it was cut. The one parameter that can carry a person's words, `query`, is the
-- user's question, which the row already keeps in `user_question`.
--
-- NULLABLE, AND NULL IS THE ORDINARY CASE on a row written before 027, on a call with no misses, and
-- (for the two sizes) on a call whose handler raised.
--
-- Forward-only and portable (SQLite + Postgres). No `IF NOT EXISTS` — SQLite's ALTER does not accept
-- it; re-run safety comes from the runner's applied-filename ledger. No index: nothing filters on
-- these yet, and the slices that will can add the one they need.
ALTER TABLE tool_calls ADD COLUMN arguments TEXT;
ALTER TABLE tool_calls ADD COLUMN missed TEXT;
ALTER TABLE tool_calls ADD COLUMN miss_count INTEGER;
ALTER TABLE tool_calls ADD COLUMN result_chars INTEGER;
ALTER TABLE tool_calls ADD COLUMN result_tokens_est INTEGER;
