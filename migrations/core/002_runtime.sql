-- agami-core runtime schema — what the server WRITES at query time (the ActivitySink target).
--
-- One row per query, written through the single execute_sql chokepoint (so a cross-datasource
-- question logs one row per single-datasource leg). Mirrors the local jsonl records exactly
-- (contracts.QueryExecutionRecord / FeedbackRecord), keyed by an app-minted id (no SERIAL).

CREATE TABLE query_executions (
    id         TEXT PRIMARY KEY,   -- minted by the server per query at runtime
    ts         TEXT NOT NULL,
    org_id     TEXT NOT NULL DEFAULT 'local',   -- the tenant this query ran for
    datasource TEXT,
    question   TEXT,
    sql        TEXT NOT NULL,
    row_count  INTEGER,
    source     TEXT
);
CREATE INDEX idx_query_executions_org ON query_executions (org_id);

CREATE TABLE feedback (
    id         TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    datasource TEXT,
    question   TEXT NOT NULL,
    rating     TEXT,
    notes      TEXT,
    source     TEXT
);
