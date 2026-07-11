-- ACE-050: index query_executions by timestamp. The query log is RETAINED (never pruned — it's the
-- user's history and the paid tier's eval fuel), so it must stay fast to read newest-first as it
-- grows. Mirrors idx_tool_calls_ts on the activity log. IF NOT EXISTS: this index is added to an
-- already-shipped table, so guard against a deployment that created it out of band.

CREATE INDEX IF NOT EXISTS idx_query_executions_ts ON query_executions (ts);
