# Telemetry Payload — Privacy Allowlist

`agami` ships with all telemetry **off by default**. The `agami-init` skill asks once, in plain English, whether to enable it. The user can change their mind any time by editing `~/.agami/.config` or asking the skill to "turn off analytics".

This document is **the authoritative source of truth** for what gets sent. Both the client (skill) and the server (Cloudflare Worker at `services/telemetry-endpoint/`) enforce this allowlist independently — defense in depth.

## What we send (allowlist)

Telemetry payloads must contain ONLY these fields. Any other field causes the client to refuse to send (and the server to reject the event).

| Field | Type | Description | Example |
|---|---|---|---|
| `schema_version` | int | Always `1` for v1 | `1` |
| `event_type` | enum | One of: `install`, `connect`, `query`, `correction`, `chart`, `error`, `update_check` | `query` |
| `install_id` | UUIDv4 | Random per-install, generated on first opt-in. Never tied to a user. | `f47ac10b-58cc-4372-a567-0e02b2c3d479` |
| `db_type` | enum | One of: `postgres`, `redshift`, `mysql`, `snowflake`, `sqlite` | `postgres` |
| `os` | enum | One of: `darwin`, `linux`, `windows` | `darwin` |
| `host` | enum | One of: `claude-code-cli`, `claude-code-vscode`, `claude-code-cursor`, `claude-cowork` | `claude-code-cli` |
| `error_kind` | enum (optional) | Only when `event_type = error`. One of: `auth`, `dsn`, `network`, `permission`, `column_not_found`, `table_not_found`, `syntax`, `timeout`, `driver_missing`, `other` | `column_not_found` |
| `latency_p50_ms` | int (optional) | Median latency for the event in ms. Bucketed in 50ms increments. | `250` |
| `latency_p95_ms` | int (optional) | p95 latency in ms. Bucketed in 50ms increments. | `1100` |
| `tier` | enum (optional) | Which connection method handled the event: `cli` (native CLI), `duckdb` (DuckDB), `python` (Python driver). Field name is `tier` for compatibility with the v1.0 wire format. | `cli` |
| `client_version` | string | The skill version (from `plugin.json`) | `1.0.0` |
| `timestamp` | ISO8601 | UTC timestamp at event time | `2026-06-02T15:14:00Z` |

That's the entire surface. Eleven fields, all enums or numbers or UUIDs.

## What we never send

This list is exhaustive — if a field would fall into any of these buckets, it MUST NOT appear in a payload:

- Query text (the user's NL question or the generated SQL)
- Schema content (table names, column names, descriptions, sample data)
- Result rows or any subset thereof
- Database hostnames, IPs, ports, credentials
- File paths beyond `~/.agami/` (we never send paths at all in v1; this is a defense-in-depth statement)
- Email addresses, names, IPs, MAC addresses, machine IDs, hardware fingerprints
- Stack traces, error messages, log lines (only the classifier `error_kind` enum)
- Working-directory contents, environment variables, git history
- Anything from `~/.agami/credentials`, `~/.agami/<dbname>.yaml`, `~/.agami/<dbname>-examples.yaml`, `~/.agami/charts/*`, or `~/.agami/exports/*`

If a future feature wants to send something not on the allowlist, we **add a new field to the allowlist with explicit user consent** before shipping it. There is no `extras: { ... }` field. There is no `metadata`. There is no free-form string.

## Payload shape

```json
{
  "schema_version": 1,
  "events": [
    {
      "event_type": "query",
      "install_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
      "db_type": "postgres",
      "os": "darwin",
      "host": "claude-code-cli",
      "tier": "cli",
      "latency_p50_ms": 250,
      "latency_p95_ms": 1100,
      "client_version": "1.0.0",
      "timestamp": "2026-06-02T15:14:00Z"
    },
    {
      "event_type": "error",
      "install_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
      "db_type": "postgres",
      "os": "darwin",
      "host": "claude-code-cli",
      "tier": "cli",
      "error_kind": "column_not_found",
      "client_version": "1.0.0",
      "timestamp": "2026-06-02T15:14:30Z"
    }
  ]
}
```

A batch is at most 100 events. Server rejects larger batches.

## How the skill builds the payload

The `agami-query-database` skill (and any other skill that emits telemetry) follows this contract:

1. Read `~/.agami/.config` — if `analytics_consent != true`, do nothing.
2. Build the event object using only the fields in the table above. Hard-code the field list — do not iterate over a `dict` of "stuff to send".
3. Append the event to `~/.agami/.telemetry-queue.jsonl` (one JSON object per line).
4. Once a day (or on the next skill invocation if the last flush was >24h ago), POST the queue contents to `https://analytics.agami.ai/v1/events` via `curl`, then truncate the queue.

The daily-flush model means events buffer locally between sends — if the network is down, nothing is lost.

## How the test suite enforces the invariant

`tests/test_telemetry_privacy.py`:

1. Generates a sample payload using the skill's payload-building pseudo-code.
2. Asserts every key in the payload is in the allowlist above (no extras).
3. Asserts the payload values do not contain any banned strings (test plants the user's mock query/schema/path content into the environment and verifies none of it leaks into the payload).
4. Asserts that a deliberately constructed "bad" payload (with an extra field like `query_text`) is rejected by the same code path before sending.

## How the server enforces the invariant

The Cloudflare Worker at `services/telemetry-endpoint/` re-validates the payload server-side. Defense in depth: the skill could be tampered with (it's open source); the server still rejects anything off-allowlist.

## Plain-English opt-in dialog

The `agami-init` skill presents this to the user, verbatim:

> Help us improve agami by sending **anonymous usage stats**?
>
> What we send:
> - Counts of installs, queries, errors (no content)
> - Database type (postgres/mysql/sqlite), OS, which host (Claude Code / Cowork)
> - Latency percentiles
> - A random install ID — not tied to you
>
> What we never send:
> - Your queries, your schema, your data
> - Your hostname, paths, credentials
> - Anything we couldn't read out loud at a conference
>
> Your choice. You can change it any time. [Yes / No / Read more]

`Read more` opens [`docs/privacy.md`](../../../docs/privacy.md) which restates the allowlist and links here.
