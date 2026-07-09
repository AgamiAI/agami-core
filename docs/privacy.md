# Privacy

**The short version:** agami runs no server of its own, has no telemetry, and makes no network calls. Your credentials, semantic model, query results, charts, and exports all stay in local files on your machine.

But agami works *through* an AI assistant — Claude Code, or Claude Desktop / claude.ai via the local MCP server — and that assistant is what turns your question into SQL. So the assistant sends the model provider (Anthropic, under its terms) the things any AI assistant would need: your question, the part of your semantic model it's reasoning over, the SQL, and the results it shows you. That's inherent to using an LLM — not something agami adds. This page is precise about who sees what.

---

## agami itself sends nothing

There is no outbound network call in agami's own code — no telemetry, no analytics, no install ping, no agami server in the loop. You can grep the source: there is no `curl` / `requests.post` / socket call in any skill or script path, and a test (`tests/test_privacy_no_network.py`) fails the build if any script introduces one.

So none of this is ever collected, transmitted, or logged **by agami**:

- Your credentials, database hostnames, IPs, ports, tokens
- File paths, environment variables, working-directory contents, git history
- Email addresses, machine IDs, hardware fingerprints, stack traces, error logs
- Any usage counts or events — there is nothing to opt out of, because there's nothing there

---

## What your AI client sends to the model

Because agami writes SQL *with* an LLM, your AI client passes the model what it needs to do that. When you run agami inside Claude Code / Claude Desktop / claude.ai, that means Anthropic's API receives:

- your natural-language question,
- the slice of your **semantic model** it's working from — table and column names, descriptions, and metric definitions (the governed model agami built, not your whole database),
- the SQL agami generates, and
- the result rows the assistant shows you.

This is how every AI coding or analysis assistant works; agami neither adds nor removes it. What agami *does* do is keep that surface as small and controlled as possible:

- **You decide what the model can see.** The semantic model is the only schema the LLM works from — never a live connection to your database. Exclude any table or column you don't want queried, and mark columns `sensitive` so their raw values are never projected (they can still be counted or filtered).
- **Execution stays on your machine.** agami runs the generated SQL against your database locally. Your **database credentials and connection never go to the model** — only the model context above and the rows you'd see anyway.
- **Want nothing to leave at all?** Point your client — or a [self-hosted deploy](open-vs-hosted.md) — at a local or self-hosted model, and even the question and model context stay on your own infrastructure.

---

## What agami keeps local

Every byte agami reads or writes stays on your machine:

- **Credentials** (`<artifacts_dir>/local/credentials`) — chmod 600
- **Auth files** (`<artifacts_dir>/local/.pgpass`, `.mysql.cnf`, `.snowsql.cnf`) — chmod 600, written by `setup_pgauth.py`
- **Config** (`<artifacts_dir>/local/.config`) — `active_profile`, `tool_paths`, `reviewer_email`, `reviewer_role` (the artifacts-dir location lives in the `~/.config/agami/path` pointer)
- **Semantic model** (`org.yaml` + the `subject_areas/<area>/` tree under `<artifacts_dir>/<profile>/`; default `<artifacts_dir>` is `~/agami-artifacts/`)
- **Examples library** (`<artifacts_dir>/<profile>/examples.yaml`)
- **Organization context** (`<artifacts_dir>/<profile>/ORGANIZATION.md`) — your description of what the database represents, domain terminology
- **User memory** (`<artifacts_dir>/USER_MEMORY.md`) — your cross-database preferences
- **Query results** (everything the assistant shows you)
- **Query log** (`<artifacts_dir>/local/query_log.jsonl`) — your personal record of every query you ran
- **Charts** (`<artifacts_dir>/local/charts/<profile>/<ts>.html`)
- **CSV exports** (`<artifacts_dir>/local/exports/<profile>/<ts>.csv`)
- **Review + model-explorer + examples-validation dashboards** (`<artifacts_dir>/local/{review,model,examples-validation}/<profile>/<ts>.html`)
- **Snapshots** (`<artifacts_dir>/<profile>/.snapshots/<hash>/`) — immutable copies of past model versions for reproducibility
- **Curation log** (`<artifacts_dir>/<profile>/curation_log.jsonl`) — append-only audit trail of review actions
- **Corrections** (`<artifacts_dir>/<profile>/corrections.jsonl`) — append-only history of saved corrections

The skill never reads files outside those paths, with one carve-out: your DB tool's auth config (`~/.pg_service.conf`, `~/.snowsql/config`, etc.) is read when `setup_pgauth.py` materializes the auth files on first connect, with your permission.

---

## The GitHub-star prompt is not a network call

After your first successful query, `agami-query` asks once, in chat, whether you'd like to star the repo. It's just a prompt — choosing **Yes** runs `open https://github.com/AgamiAI/agami-core` (or the platform equivalent), handing that URL to your browser; your browser does the rest. agami sends nothing and never learns whether you actually starred (a star is public anyway). **Maybe later** and **Already starred** dismiss it for good. The choice is recorded in `<artifacts_dir>/local/.optins` so it doesn't repeat; to see it again, `rm <artifacts_dir>/local/.optins`.

---

## The optional local MCP server keeps the same guarantee

`agami serve` (`python -m mcp_harness`, in `packages/agami-core/src/mcp_harness.py`) lets you use agami from Claude Desktop. It changes nothing about the posture above:

- It speaks the MCP **stdio** transport — a child process of your AI client, reading/writing OS pipes. It **never binds a network port** and makes **no network call** of its own. `tests/test_mcp_harness.py` enforces this (the source is asserted to contain no socket/http/urllib/requests primitives).
- It reads only the local paths listed above and executes SQL locally via `execute_sql.py`. Only the rows you'd see anyway are returned to your client. (What that client then sends to the model is the same as the section above — it's still an LLM assistant.)
- It has **no authentication** because it needs none: the trust boundary is your OS user account. (Networked, authenticated, multi-user serving is the [self-hosted team server](open-vs-hosted.md).)

---

## No telemetry

agami has **no telemetry** — no usage counts, no events, no install ping, no opt-in or opt-out. Earlier 0.x builds kept a vestigial (never-deployed, never-invoked) telemetry sample client + endpoint in the repo as historical artifacts; those were removed entirely. There is no network call anywhere in agami's codebase, and `tests/test_privacy_no_network.py` fails the build if any script introduces one. If a future version ever added telemetry, it would be opt-in with a full privacy doc — never a silent re-enable.
