-- The admin **tool-call activity log** — one row per MCP tool call, written by the transport's
-- dispatch hook so the admin can see what ran, by whom.
--
-- Two tiers in one table. The **audit-grade** columns are what the server observes directly and are
-- always trustworthy: who (`actor`, the authenticated bearer subject), which `tool_name`, the
-- `datasource`, and for execute_sql the `sql` / `row_count` / `execution_ms` / `success`. The
-- **best-effort** columns (`user_question`, `agent_query`, `thread_id`) are self-reported by Claude —
-- the MCP protocol carries neither the user's question nor a conversation id, so these are nullable and
-- may be blank; the Sessions view groups on `thread_id` and degrades to ungrouped when it's absent.
--
-- Keyed by an app-minted id (uuid4 hex), like the other runtime tables. Portable CREATE TABLE (runs on
-- SQLite + Postgres unchanged); `success` is 0/1 (no portable boolean literal).

CREATE TABLE tool_calls (
    id            TEXT PRIMARY KEY,        -- minted by the server per call
    ts            TEXT NOT NULL,           -- UTC ISO8601
    org_id        TEXT NOT NULL DEFAULT 'local',  -- the tenant this call ran for
    actor         TEXT,                    -- the authenticated user (bearer sub); NULL under presence auth
    tool_name     TEXT NOT NULL,
    datasource    TEXT,
    sql           TEXT,                    -- execute_sql only
    row_count     INTEGER,                 -- execute_sql only
    execution_ms  INTEGER,
    success       INTEGER NOT NULL DEFAULT 1,
    error_kind    TEXT,
    source        TEXT,                    -- 'mcp_server'
    user_question TEXT,                    -- best-effort, self-reported (the user's verbatim question)
    agent_query   TEXT,                    -- best-effort, self-reported (the agent's framing of the query)
    thread_id     TEXT                     -- best-effort, self-reported (groups a conversation)
);

-- The admin views read newest-first and group by conversation, scoped to the operator's org; index
-- the three access paths.
CREATE INDEX idx_tool_calls_ts ON tool_calls (ts);
CREATE INDEX idx_tool_calls_thread ON tool_calls (thread_id);
CREATE INDEX idx_tool_calls_org ON tool_calls (org_id);
