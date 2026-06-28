-- Add the **turn** level to the tool-call log (ACE-015). ACE-008 captured `thread_id` (the whole
-- conversation) and the per-call `user_question`/`agent_query`; this adds `correlation_id` — the turn,
-- i.e. the ONE user question whose answer fanned out into several agent sub-queries. Self-reported by
-- Claude (the MCP protocol carries no turn boundary, so the server can't mint it), nullable and
-- best-effort like the other self-report columns; the Sessions view groups a session's calls by it and
-- degrades to ungrouped when absent. Portable (runs on SQLite + Postgres unchanged).
ALTER TABLE tool_calls ADD COLUMN correlation_id TEXT;
