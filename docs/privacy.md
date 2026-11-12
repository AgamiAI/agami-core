# Privacy

**The short version:** used the normal way — as a skill inside Claude Code, or the local stdio server in Claude Desktop — agami makes no network calls of its own and has no telemetry. Your credentials, semantic model, query results, charts, and exports all stay in local files on your machine. (The self-hosted team server is a network service you deliberately stand up for your org — a separate, opt-in deployment, covered near the end.)

But agami works *through* an AI assistant — Claude Code, or Claude Desktop / claude.ai via the local MCP server — and that assistant is what turns your question into SQL. So the assistant sends the model provider (Anthropic, under its terms) the things any AI assistant would need: your question, the part of your semantic model it's reasoning over, the SQL, and the results it shows you. That's inherent to using an LLM — not something agami adds. This page is precise about who sees what.

---

## The local path makes no network calls

When you use agami locally — the skill scripts plus the on-machine library (the SQL executor and the stdio MCP server) — there is no outbound network call of any kind: no telemetry, no analytics, no install ping. `tests/test_privacy_no_network.py` enforces this: it scans every shipped skill script and the local library modules and fails the build if one gains a `curl` / `requests.post` / socket call. (It deliberately excludes the two **server** modules, `mcp_http` and `oidc` — the self-hosted team server *is* a network service by design; that's the opt-in deploy covered below, not the local path.)

One command sits outside that sentence, and it is worth naming rather than burying: running a **golden-dataset eval** starts your own already-installed AI client as a child process, once per question, to produce the SQL being graded. It is your client, your account and your existing trust decision — agami opens no connection itself and holds no key of its own. The child is sandboxed on purpose, and the two halves of that are worth separating because only one of them is a list:

- **It is given no tools to read anything with.** Every built-in tool is switched off by name (`--tools ""`), every MCP server the client would otherwise load is refused (`--strict-mcp-config`, with no config to load from), no user, project or local settings are read (`--setting-sources ""`), and it is started in an empty directory that holds no `CLAUDE.md` or `.mcp.json` of its own. The only thing it can work from is the question and the tables and columns inlined in its prompt.
- **It is given a built environment, not the inherited one** — a short allowlist of names that excludes every `AGAMI_*` name (including the one that would tell it where your artifacts directory is) and every `DATASOURCE_*` credential. `HOME` is on the list, because a client that cannot find its own configuration cannot start; your credentials file and your artifacts directory live under `HOME`, so what makes that harmless is the line above, not the allowlist. A process with no tool to open a file gets nothing from knowing where your home directory is.

Nothing in the query path does this; it happens only when you ask for an eval.

So none of this is ever collected, transmitted, or logged by the local agami path:

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

- **You decide what the model can see.** The semantic model is the only schema the LLM works from — never a live connection to your database. **Excluding a column is the control.** A column you leave out of the model is out of scope: any statement that names it is refused before it runs, and that is enforced rather than advisory.

  Marking a column `sensitive` is a *description*, not a gate. It is the model author saying "handle this carefully": the answer's receipt reports when a statement projected it, so you can see that it did. It does not stop the projection. Agami holds no access policy of its own and reads exactly as the connecting database role reads, so what a query can return is decided by that role's grants and by your warehouse's own masking policies — not by us. If a value must not come back, either leave the column out of the model or make sure the role cannot read it.

  One thing worth stating plainly, because it is the reason the flag is not a gate: excluding a column does not prevent *inference* about it through predicates or aggregates over the columns that remain. A filter on a column can answer a question about it one bit at a time. Only the warehouse's own controls, or not landing the data, close that.
- **Execution stays on your machine.** agami runs the generated SQL against your database locally. Your **database credentials and connection never go to the model** — only the model context above and the rows you'd see anyway.
- **Want nothing to leave at all?** Point your client — or a [self-hosted deploy](open-vs-hosted.md) — at a local or self-hosted model, and even the question and model context stay on your own infrastructure.

---

## What agami keeps local

Every byte agami reads or writes stays on your machine:

- **Credentials** (`<artifacts_dir>/local/credentials`) — chmod 600
- **Auth files** (`<artifacts_dir>/local/.pgpass`, `.mysql.cnf`, `.snowsql.cnf`) — chmod 600, written by `setup_pgauth.py`
- **Config** (`<artifacts_dir>/local/.config`) — `active_profile`, `tool_paths`, `reviewer_email`, `reviewer_role` (the artifacts-dir location lives in the `~/.config/agami/path` pointer)
- **Semantic model** (`datasource.yaml` + the `subject_areas/<area>/` tree under `<artifacts_dir>/<profile>/`; default `<artifacts_dir>` is `~/agami-artifacts/`)
- **Examples library** (`<artifacts_dir>/<profile>/examples.yaml`)
- **Datasource context** (`<artifacts_dir>/<profile>/datasource.md`) — your description of what the database represents, domain terminology
- **User memory** (`<artifacts_dir>/USER_MEMORY.md`) — your cross-database preferences
- **Query results** (everything the assistant shows you)
- **Query log** (`<artifacts_dir>/local/query_log.jsonl`) — your personal record of every query you ran
- **Charts** (`<artifacts_dir>/local/charts/<profile>/<ts>.html`)
- **CSV exports** (`<artifacts_dir>/local/exports/<profile>/<ts>.csv`)
- **Review + model-explorer + examples-validation dashboards** (`<artifacts_dir>/local/{review,model,examples-validation}/<profile>/<ts>.html`)
- **Snapshots** (`<artifacts_dir>/<profile>/.snapshots/<hash>/`) — immutable copies of past model versions for reproducibility
- **Curation log** (`<artifacts_dir>/<profile>/curation_log.jsonl`) — append-only audit trail of model edits (when, who, the prior and new value); `sm history` reads it
- **Corrections** (`<artifacts_dir>/<profile>/corrections.jsonl`) — append-only history of saved corrections

The skill never reads files outside those paths, with one carve-out: your DB tool's auth config (`~/.pg_service.conf`, `~/.snowsql/config`, etc.) is read when `setup_pgauth.py` materializes the auth files on first connect, with your permission.

---

## The password page listens on this computer only

On macOS and Windows, connecting a database from the agami pane asks for the password on a small page
in your browser instead of in the chat. `scripts/connect_helper.py` serves that page:

- **What it binds:** `127.0.0.1` only, on a random port, behind a one-time token of 43 random characters
  in the URL. Nothing off your machine can reach it. Any other path, a wrong token, a second submit, a
  request addressed to another host name, or a post from another page gets a 404. An idle or half-sent
  request is dropped after 10 seconds and cannot hold the helper open. The page loads no scripts, fonts, or files from anywhere.
- **For how long:** until the first password is submitted, or 5 minutes, whichever comes first. Then it
  stops listening and exits.
- **What it stores, and where:** the password goes into the OS credential store (Keychain on macOS,
  Credential Manager on Windows) under the service `agami` and the profile name. The credentials file
  gets the non-secret fields and a reference, `password = keyring:agami/<profile>`, never the password.
  The password is not written to the chat, the pane, a command line, a log, or any other file. If the
  connection test fails, the helper removes both the section it wrote and the stored password, so you
  can try again with the same profile name.
  The store is shared by every agami project on the computer, so if a password is already saved under
  that profile name, the helper refuses and asks for another name rather than overwrite it. To change
  a profile's saved password, run it with `--replace`; that works only for a profile whose section
  already reads `keyring:agami/<profile>`, and a failed connection puts the old password back.
- **What it calls:** after saving, it runs `SELECT 1` against your own database through `execute_sql`, the
  same local connection every query uses.

`tests/test_privacy_no_network.py` pins this: the helper and the page server below are the only shipped
scripts that listen, and both bind the loopback address. On Linux, and wherever no credential store is available, the helper refuses
and you use the manual credentials file step instead.

---

## Review pages save to a helper on this computer only

When you pick **Review on a page** (or **Mark them on a page** for the seed examples), the page you
mark decisions on sends them back by itself instead of asking you to copy and paste a block.
`scripts/serve_page.py --decisions` serves that page:

- **What it binds:** `127.0.0.1` only, on a random port, behind a one-time token of 43 random characters
  in the URL. It serves that one page and takes one post of decisions, at most 1 MiB of JSON, from the
  page itself. A wrong token or path, a request addressed to another host name, a post from another
  page or origin, a body that isn't a JSON object, a second post, and any post after the link expires
  are all refused. A post has 10 seconds in all to send its body, an idle connection is dropped
  after 10 seconds, and at most 8 connections are served at once. The page loads nothing from
  anywhere else; its content security policy runs only the page's own scripts (each allowed by its
  hash) and lets it talk only to the helper that served it.
- **For how long:** until the page is saved, or 30 minutes, whichever comes first. Then it stops
  listening and exits.
- **What it writes:** the decisions, to one file under `<artifacts_dir>/local/tmp/` (readable by you
  only, written through a fresh temporary file, so a link planted beside it is never followed).
  Nothing else. The decisions are then checked in the chat before anything is applied: the page and
  model they name must be the ones agami opened (`parse_model_feedback.py --expect-profile`,
  `parse_examples_decisions.py`), and the sign-off identity is always your saved one, never one the
  page sent. The helper applies nothing: agami reads that file in the chat and applies it
  with the same commands a card uses (`sm curate`, `sm add-example`, `sm remove-example`), which
  validate the model and record each change, with your saved sign-off identity, in
  `curation_log.jsonl`. The file is deleted once applied.
- **What it calls:** nothing. It makes no connection of its own.

Pages that only show something (the full view of a chart, say) are served by the same script
without `--decisions`: every post to them is refused, and their banner says nothing on them is saved.
`tests/test_privacy_no_network.py` pins both listeners (this one and the password page) to the
loopback address, and `tests/test_serve_page.py` tests each refusal.

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

## The self-hosted team server (opt-in) — Early access (in testing)

If you [deploy the team server](../deploy/README.md) so your org can share one model, that server *is* a network service by design — it serves your model to your team over HTTPS. (This path is **early access** — usable today, newer than the local single-player experience; [feedback via a GitHub issue](https://github.com/AgamiAI/agami-core/issues) is welcome.) Your data still stays in your environment: it holds only the semantic model (never a live database connection), runs SQL locally against your own warehouse, and is **zero-egress by default**. The one thing that reaches out is **single sign-on**, if you turn it on: to verify a login the server calls Google/Microsoft (the identity provider). Leave SSO off — the default — and it makes no outbound call of its own.

---

## No telemetry

agami has **no telemetry** — no usage counts, no events, no install ping, no opt-in or opt-out. Earlier 0.x builds kept a vestigial (never-deployed, never-invoked) telemetry sample client + endpoint in the repo as historical artifacts; those were removed entirely. There is no network **client** anywhere in agami's codebase — nothing it ships opens a socket, and `tests/test_privacy_no_network.py` fails the build if any script introduces one. (The one thing that reaches outward is the golden-dataset eval described above, and it does so by starting *your* AI client as a child process, not by calling anything itself.) If a future version ever added telemetry, it would be opt-in with a full privacy doc — never a silent re-enable.
