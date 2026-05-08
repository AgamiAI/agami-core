# Privacy

The short version: **your data never leaves your machine.** `agami` is a Claude Code skill that runs locally — credentials, schema, query results, and corrections all live in `~/.agami/` on your laptop. We do not host, proxy, or process any of them.

This page documents:
- What we send if you opt into anonymous usage stats (and what we never send)
- How to opt in and out
- How we enforce the privacy invariant
- Where the receiving server lives + what defenses sit in front of it

For the canonical, machine-checked allowlist, see [`plugins/agami/shared/telemetry-payload.md`](../plugins/agami/shared/telemetry-payload.md). That doc and the test suite at [`tests/test_telemetry_privacy.py`](../tests/test_telemetry_privacy.py) are the source of truth — this page is the human-readable summary.

---

## What `agami` always keeps local

These never leave your machine, opt-in or not:

- **Credentials** (`~/.agami/credentials`)
- **Semantic model** (`~/.agami/<profile>/index.yaml` + `<profile>/<schema>.yaml`)
- **Examples library** (`~/.agami/<profile>/examples.yaml`)
- **Organization context** (`~/.agami/<profile>/ORGANIZATION.md`) — your description of what the database represents, domain terminology, etc.
- **User memory** (`~/.agami/USER_MEMORY.md`) — your cross-database preferences
- **Query results** (everything Claude shows you)
- **Query log** (`~/.agami/query_log.jsonl`) — your personal record of every query you ran
- **Charts** (`~/.agami/charts/<ts>.html`)
- **CSV exports** (`~/.agami/exports/<ts>.csv`)
- **Schema content** — table names, column names, descriptions, sample data
- **Hostnames, IPs, paths beyond `~/.agami/`**

There is no skill code that reads any of these and ships them anywhere. You can grep the source — every outbound `curl` in the SKILL.md files goes to either:
- `analytics.agami.ai/v1/events` (telemetry, opt-in only) — sends only the 11 allowlisted fields
- `api.hsforms.com/.../<form-id>` (HubSpot form, opt-in only) — sends an email address you typed

That's it.

---

## What we send if you opt into telemetry

The `init` skill asks once. **Default is off.** If you opt in, every event sent contains exactly these fields and nothing else:

| Field | What it is | Example |
|---|---|---|
| `schema_version` | Always `1` for v1 | `1` |
| `event_type` | `install`, `connect`, `query`, `correction`, `chart`, `error`, `update_check` | `query` |
| `install_id` | Random UUID generated on opt-in. Not tied to a user. | `f47ac10b-...` |
| `db_type` | `postgres` / `mysql` / `sqlite` | `postgres` |
| `os` | `darwin` / `linux` / `windows` | `darwin` |
| `host` | `claude-code-cli`, `claude-code-vscode`, `claude-code-cursor`, `claude-cowork` | `claude-code-cli` |
| `tier` | Which connection method ran the event: `cli` (native CLI), `duckdb`, `python` (Python driver). Field name is `tier` for compatibility with the v1.0 wire format. | `cli` |
| `error_kind` (only on errors) | One of nine categories like `auth`, `column_not_found`, `network`, `timeout` | `column_not_found` |
| `latency_p50_ms` (optional) | Median latency, bucketed in 50ms increments | `250` |
| `latency_p95_ms` (optional) | p95 latency, bucketed in 50ms increments | `1100` |
| `client_version` | The skill version | `1.0.0` |
| `timestamp` | UTC ISO8601 | `2026-05-06T15:14:00Z` |

Eleven fields, all enums or numbers or UUIDs. No free-form strings. No metadata bag. No "extras" field.

---

## What we never send (even if you opt in)

This list is exhaustive — if any future event would contain something in any of these categories, **the privacy invariant test fails, the build fails, the change does not ship**:

- Query text (the NL question or the generated SQL)
- Schema content (table names, column names, descriptions, sample data)
- Result rows or any subset thereof
- Database hostnames, IPs, ports, credentials
- File paths beyond `~/.agami/` (we don't actually send any path; this is defense-in-depth wording)
- Email addresses, names, IPs, MAC addresses, machine IDs, hardware fingerprints
- Stack traces, log lines, error messages (only the `error_kind` enum value)
- Working directory contents, environment variables, git history
- Anything from `~/.agami/credentials`, `~/.agami/<dbname>.yaml`, `~/.agami/<dbname>-examples.yaml`, `~/.agami/charts/*`, `~/.agami/exports/*`, or `~/.agami/query_log.jsonl`

If a future feature wants to add a field, we add it explicitly to the allowlist with user consent before shipping. There is no `extras: { ... }` slot.

---

## How to opt in

The `init` skill prompts you once during first-run, in plain English:

> **Help us improve agami by sending anonymous usage stats?**
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
> Your choice. You can change it any time.

Pick `Yes` or `No`. The choice is recorded in `~/.agami/.config`.

## How to opt out

If you previously said yes:

```bash
# Just edit the file
$EDITOR ~/.agami/.config
# Change "analytics_consent": true to "analytics_consent": false
```

Or in any agami skill conversation:

```
@agami turn off analytics
```

The next time you flush, the queue is dropped. No cleanup is required on the server side — your `install_id` stops appearing in events.

To re-enable:

```
@agami turn on analytics
```

---

## How we enforce the privacy invariant

Three layers of enforcement:

### 1. Documented allowlist

[`plugins/agami/shared/telemetry-payload.md`](../plugins/agami/shared/telemetry-payload.md) is the source of truth. Both the client and the server import from this list (semantically — they each have their own copy that must match).

### 2. Test suite

[`tests/test_telemetry_privacy.py`](../tests/test_telemetry_privacy.py) plants 12 categories of sensitive data into a fake `~/.agami/` and the environment, then builds a payload via the same code path the skill uses. It asserts:

- No field outside the allowlist appears
- None of the 12 planted strings appear (query text, hostnames, paths, PII, etc.)
- The payload-builder function rejects bad input rather than silently coercing it

The test runs on every PR. If it fails, the change does not merge.

### 3. Server-side re-validation

The Cloudflare Worker at [`services/telemetry-endpoint/`](../services/telemetry-endpoint/) re-validates every payload independently. Even if the open-source skill is tampered with to ship extra fields, the server still rejects them. This is defense-in-depth: a determined adversary running their own modified copy can't poison the analytics pipeline with PII.

---

## Where the receiving server lives

`https://analytics.agami.ai/v1/events` — a Cloudflare Worker, source code at [`services/telemetry-endpoint/src/worker.ts`](../services/telemetry-endpoint/src/worker.ts).

Layered abuse defenses:

| Layer | What |
|---|---|
| Cloudflare Bot Fight Mode | Edge-level filter on automated traffic, zone-wide |
| Per-IP rate limit | 100 req/min/IP via the rate-limiter binding |
| Body size cap | 64 KB max — a 100-event batch is ~25 KB |
| Schema validation | Every field validated against the allowlist + UUID regex + ISO8601 regex |
| Schema-version check | Future versions cleanly rejected with a 400 |
| Outlier-aware aggregation | Anomalous-volume install_ids are filtered from DAU/MAU even if they slip past the rate limit |

Storage: accepted events go into one R2 (Cloudflare object storage) JSONL file per UTC day at `events/YYYY-MM-DD.jsonl`. Aggregation runs out-of-band in a separate scheduled job.

We do not log IP addresses alongside `install_id`. IPs hit the rate limiter and Cloudflare edge logs (which we do not export to long-term storage); the JSONL events do not contain them.

---

## Compliance notes

- **GDPR**: `install_id` is a random UUID generated locally and not tied to a user identity. It can be considered pseudonymous data. You can rotate yours by deleting `~/.agami/.config` and re-running `@agami init` — a fresh ID is generated.
- **Data retention**: events are kept in R2 for 365 days, then auto-deleted via the bucket lifecycle policy.
- **Deletion requests**: if you want a specific `install_id`'s events deleted, email `skills@agami.ai` with the ID. We can't otherwise identify you because we don't have anything else.

---

## Email opt-in (separate from telemetry)

After your first successful query, the `query-database` skill asks once whether you want occasional product-update emails. **Default is skip.** This is a separate question from the analytics opt-in — you can opt into one and not the other.

If you opt in with an email, the skill POSTs to a HubSpot form at `api.hsforms.com/submissions/v3/integration/submit/<HUB_ID>/<FORM_GUID>` with:

- `email`
- `utm_source` (always `skill_install`)
- `host_preference` (`claude-code-cli` / etc.)
- `signup_timestamp`

Your email lives in HubSpot under our standard subscriber list (subject to HubSpot's privacy policy). Unsubscribe at any time via the link in any email we send. We never sell or share email addresses.

State persists at `~/.agami/.optins`. To re-prompt (e.g., to change your email), delete that file and ask any agami skill a question.
