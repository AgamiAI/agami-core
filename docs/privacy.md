# Privacy

The short version: **your data never leaves your machine.** `agami` is a Claude Code skill that runs locally — credentials, schema, query results, and corrections all live in `~/.agami/` and `~/agami-artifacts/` on your laptop. There is no agami server in the loop, no telemetry, no opt-in, no opt-out. The runtime is silent.

This page documents what stays on your machine, what we'd never send anywhere even hypothetically, and how the one outbound interaction (the post-install GitHub-star ask) works.

---

## What `agami` keeps local

Every byte agami reads or writes stays on your machine:

- **Credentials** (`~/.agami/credentials`) — chmod 600
- **Auth files** (`~/.agami/.pgpass`, `.mysql.cnf`, `.snowsql.cnf`) — chmod 600, written by `setup_pgauth.py`
- **Config** (`~/.agami/.config`) — `active_profile`, `artifacts_dir`, `tool_paths`, `reviewer_email`, `reviewer_role`
- **Semantic model** (`<artifacts_dir>/<profile>/index.yaml` + `<schema>/<table>.yaml` files; default `<artifacts_dir>` is `~/agami-artifacts/`)
- **Examples library** (`<artifacts_dir>/<profile>/examples.yaml`)
- **Organization context** (`<artifacts_dir>/<profile>/ORGANIZATION.md`) — your description of what the database represents, domain terminology
- **User memory** (`<artifacts_dir>/USER_MEMORY.md`) — your cross-database preferences
- **Query results** (everything Claude shows you)
- **Query log** (`~/.agami/query_log.jsonl`) — your personal record of every query you ran
- **Charts** (`~/.agami/charts/<profile>/<ts>.html`)
- **CSV exports** (`~/.agami/exports/<profile>/<ts>.csv`)
- **Review + model-explorer + examples-validation dashboards** (`~/.agami/{review,model,examples-validation}/<profile>/<ts>.html`)
- **Snapshots** (`<artifacts_dir>/<profile>/.snapshots/<hash>/`) — immutable copies of past model versions for reproducibility
- **Curation log** (`<artifacts_dir>/<profile>/curation_log.jsonl`) — append-only audit trail of review actions
- **Corrections** (`<artifacts_dir>/<profile>/corrections.jsonl`) — append-only history of saved corrections

The skill never reads files outside those paths, with one carve-out: your DB tool's auth config (`~/.pg_service.conf`, `~/.snowsql/config`, etc.) is read when `setup_pgauth.py` materializes the auth files on first connect, with your permission.

---

## What agami never sends anywhere

There is no outbound network call from the skill code, period. To be explicit, the following categories are never collected, transmitted, or logged by agami:

- Query text (the NL question or the generated SQL)
- Schema content (table names, column names, descriptions, sample data)
- Result rows or any subset thereof
- Database hostnames, IPs, ports, credentials
- File paths beyond `~/.agami/` and `<artifacts_dir>/`
- Email addresses, names, IPs, MAC addresses, machine IDs, hardware fingerprints
- Stack traces, log lines, error messages
- Working directory contents, environment variables, git history
- Anything from `~/.agami/credentials`, the artifacts dir, charts, exports, or the query log

You can grep the source — there is no `curl` / `requests.post` / network call in any skill code path. The validator suite enforces this invariant: any change that would introduce a network call to a non-allowlisted host fails the build.

---

## The one outbound interaction: GitHub-star ask

After your first successful query, the `agami-query-database` skill asks once whether you want to star the repo on GitHub. This is a chat-side `AskUserQuestion` modal with three options:

- **Yes — open GitHub now** — runs `open https://github.com/AgamiAI/LiteBi` (or platform equivalent), which hands the URL to your OS. Your browser handles it from there.
- **Maybe later** — closes the prompt; we never ask again.
- **Already starred — thank you!** — closes the prompt; we never ask again.

Nothing about your response leaves your machine. agami has no signal-collection on the GitHub side — we don't observe whether you actually star, and a star is public information anyway. The decision is recorded in `~/.agami/.optins` so the ask doesn't repeat. To re-prompt: `rm ~/.agami/.optins` and ask any agami skill a question.

That's the entire outbound surface: opening one well-known URL in your browser when you click "Yes." No background network calls, no opt-in telemetry, no analytics events.

---

## The optional local MCP server keeps the same guarantee

`agami serve` (`plugins/agami/scripts/mcp_server.py`) lets you use agami from
Claude Code / Claude Desktop. It changes nothing about this privacy posture:

- It speaks the MCP **stdio** transport — a child process of your AI client,
  reading/writing OS pipes. It **never binds a network port** and makes **no
  network call**. `tests/test_mcp_server.py` enforces this (the source is
  asserted to contain no socket/http/urllib/requests primitives).
- It reads only the same local paths listed above and executes SQL locally via
  `execute_sql.py`. Only the rows you'd see anyway are returned to your client.
- It has **no authentication** because it needs none: the trust boundary is your
  OS user account. (Networked, authenticated, multi-user serving is the hosted
  product — see [open-vs-hosted.md](open-vs-hosted.md).)

---

## Vestigial telemetry code (preserved, not invoked)

Early designs of agami had an opt-in telemetry path that sent anonymous usage counts to a hosted endpoint. That path was removed from the runtime in the 0.x line. The implementation code remains in the repo as historical artifacts:

- `plugins/agami/scripts/sample_send_telemetry.py` — sample client (not invoked by any skill)
- `tests/test_telemetry_privacy.py` — allowlist tests (asserts the sample script can't include unauthorized fields)
- `services/telemetry-endpoint/` — Cloudflare Worker that would receive events

None of these runs in the normal agami flow. The Worker isn't deployed against any active hostname; the sample script isn't called from any skill; the test just pins the shape of the legacy spec. If a future agami version re-introduces telemetry, it would be opt-in with the same allowlist discipline and a full privacy doc — not a silent re-enable.
