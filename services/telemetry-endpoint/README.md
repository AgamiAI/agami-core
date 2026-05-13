# agami telemetry endpoint — VESTIGIAL, not deployed

> Telemetry was removed from the agami runtime in the 0.x line. See [`docs/privacy.md`](../../docs/privacy.md) for the current stance: there is no outbound network call from skill code, period. This Worker is preserved as a historical artifact — it is **not** deployed against any active hostname, and no skill posts to it. The hostnames and URLs below are illustrative of the old design.

Cloudflare Worker that *would* receive anonymous usage events POSTed by the agami OSS skill running on users' machines.

## What it does (when running)

- Accepts POST `https://analytics.agami.ai/v1/events`
- Validates the payload **server-side** against the 11-field allowlist (formerly documented in `plugins/agami/shared/telemetry-payload.md`, now removed — the authoritative copy lives inline in [`plugins/agami/scripts/sample_send_telemetry.py`](../../plugins/agami/scripts/sample_send_telemetry.py)). Defense in depth — the open-source skill could be modified to send extra fields; the server still rejects them.
- Rate-limits 100 req/min per IP via the Cloudflare-managed rate-limiter binding
- Writes accepted events to R2 (one JSONL object per UTC day at `events/YYYY-MM-DD.jsonl`)

## Deploy

```bash
cd services/telemetry-endpoint
npm install
npx wrangler login
npx wrangler r2 bucket create agami-telemetry
npx wrangler deploy
```

Then in the Cloudflare dashboard for the `agami.ai` zone:
- **Bot Fight Mode**: enabled
- **Custom domain**: bound to the worker route in `wrangler.toml` (`analytics.agami.ai/v1/events`)

## Aggregation (out-of-band)

A separate scheduled job reads the daily JSONL files into DuckDB and applies outlier-aware aggregation for DAU / MAU dashboards. Anomalous-volume `install_id`s (e.g., one ID sending > 1000 events/day) are filtered from cohort counts but kept in the raw log.

```sql
-- Sample DAU query (in DuckDB, against the JSONL files)
WITH per_install_daily AS (
  SELECT
    install_id,
    DATE_TRUNC('day', timestamp::timestamp) AS day,
    COUNT(*) AS events
  FROM read_json_auto('events/*.jsonl', format='newline_delimited')
  GROUP BY install_id, day
),
filtered AS (
  -- Drop install_ids with > p99 daily event volume (likely automated/abusive)
  SELECT *
  FROM per_install_daily
  WHERE events <= (SELECT QUANTILE_CONT(events, 0.99) FROM per_install_daily)
)
SELECT day, COUNT(DISTINCT install_id) AS dau
FROM filtered
GROUP BY day
ORDER BY day;
```

## What this endpoint never sees

Per the 11-field allowlist (inline in [`sample_send_telemetry.py`](../../plugins/agami/scripts/sample_send_telemetry.py)):

- No query text (NL or SQL)
- No schema or column names
- No result rows
- No hostnames, IPs, paths
- No PII

If the client sends any of those, the request is rejected with HTTP 400 before it reaches R2.

## Layered abuse defenses

| Layer | What it does |
|---|---|
| Cloudflare bot fight mode | Drops obvious automated traffic at the edge (zone-level) |
| Per-IP rate limit (binding) | 100 req/min/IP — tighter than a real user would ever need |
| Body size cap (64 KB) | Rejects oversized POSTs before parsing |
| Schema validation | Strict allowlist + UUID + ISO timestamp regex; one bad field → 400 |
| Schema version check | Rejects future / unknown versions cleanly |
| Outlier-aware aggregation | Anomalous install_ids excluded from DAU/MAU even if they bypass the rate limit |

## Testing locally

```bash
npm run dev    # starts a local instance on :8787

# happy path
curl -sS -X POST http://localhost:8787/v1/events \
  -H "content-type: application/json" \
  -d '{"schema_version":1,"events":[{"event_type":"query","install_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","db_type":"postgres","os":"darwin","host":"claude-code-cli","tier":"cli","client_version":"1.0.0","timestamp":"2026-06-02T15:14:00Z"}]}'

# disallowed extra field — should 400
curl -sS -X POST http://localhost:8787/v1/events \
  -H "content-type: application/json" \
  -d '{"schema_version":1,"events":[{"event_type":"query","query_text":"SELECT 1","install_id":"f47ac10b-58cc-4372-a567-0e02b2c3d479","db_type":"postgres","os":"darwin","host":"claude-code-cli","tier":"cli","client_version":"1.0.0","timestamp":"2026-06-02T15:14:00Z"}]}'
```

## When to bump `schema_version`

If you add a field to the allowlist, bump `schema_version` to `2` in:
1. [`plugins/agami/scripts/sample_send_telemetry.py`](../../plugins/agami/scripts/sample_send_telemetry.py) (the client + authoritative allowlist)
2. [`tests/test_telemetry_privacy.py`](../../tests/test_telemetry_privacy.py) (the test)
3. `src/worker.ts` (this server — `SUPPORTED_SCHEMA_VERSION`)

Old clients on `schema_version: 1` will then 400 — that's deliberate. Decide your migration window before bumping.
