# Python helpers (skill-only)

The agami **library** — the unified executor (`execute_sql`), the MCP `TOOLS` harness
(`mcp_harness`), the `semantic_model` package, and `agami_paths` — lives in an installable
package at [`packages/agami-core/`](../../../packages/agami-core). Install it once:

```bash
pip install -e "packages/agami-core[model]"   # [model] pulls pydantic/sqlglot/pyyaml
```

This `scripts/` directory now holds **skill-only** helpers (rendering, setup/connect, reconcile,
parsing, the `sm` launcher) — they *import* the package; they are not library code.

## The runtime executor (now in the package)

The agami skill runs the Python-driver tier as a module against the installed package:

```bash
# Single-line SQL via --sql
python3 -m execute_sql --profile default --sql "SELECT COUNT(*) FROM orders"

# Multi-line / quote-heavy SQL via --sql-file (preferred for anything non-trivial)
python3 -m execute_sql --profile default --sql-file /tmp/agami-query.sql
```

It reads `<artifacts_dir>/local/credentials`, runs ONE SQL statement, and emits RFC 4180 CSV on
stdout (postgres / mysql / sqlite / …). Exit codes are documented in the module docstring; the
skill routes non-zero exits through the DB error classifier in `../shared/db_error_classifier.md`.

The local stdio MCP server (`agami serve`) is `python3 -m mcp_harness` — wired into Claude Desktop
by `setup_desktop_mcp.py` (below), which installs the package into the chosen interpreter and
registers `python -m mcp_harness`. See [`docs/mcp-server.md`](../../../docs/mcp-server.md).

## Skill-only helpers

| Script | What it does | Requires |
|---|---|---|
| `setup_desktop_mcp.py` | One-command wiring of the local MCP server into the Claude Desktop app (used by the `agami-serve` skill). Auto-detects the right Python interpreter, installs agami-core into it via `sm install`, and safely merges the entry into `claude_desktop_config.json` (timestamped backup + atomic write, preserving every other key). `--dry-run` previews. | stdlib (delegates install to `sm`) |
| `render_chart.py` | Substitutes `chart-template.html` placeholders programmatically. Used by the agami-query SKILL to produce HTML reports (Phase 4e). | stdlib only |
| `run_golden_eval.py` | Runs one golden dataset for a profile (used by the `agami-eval` skill): lists the profile's datasets with `--list`, else regenerates SQL per question, executes and scores each case, prints the verdicts failures-first as JSON, and writes the run artifact. Neither statement reaches stdout. | the agami-core package with the `model` extra (imports `semantic_model`) |
| `check_statement.py` | Checks one statement a person supplied, for `agami-reconcile` Phase 1.5: the read-only and recon gates, the zero-row check, the statement, the semantic-model verbs and every probe, each execution through `execute_sql`'s guarded chokepoint, and writes the row directory the part ledger reads. Never runs anything on a command-line tier. | the agami-core package with the `model` extra, and the database's Python driver |
| `connect_helper.py` | The agami pane's password step on macOS and Windows: serves `shared/connect-helper-page.html` once on `127.0.0.1` behind a one-time token, saves the password in the OS credential store, writes the profile with `password = keyring:agami/<profile>`, and runs `SELECT 1`. JSON lines on stdout. | stdlib; imports the agami-core package (`credential_store`, `execute_sql`) and the optional `keyring` extra |
| `approve_own_calls.py` | The plugin's `PreToolUse` hook (`../hooks/hooks.json`, started with `python3` from PATH): prints an "allow" decision only for a lone call of this plugin's `sm` whose verb and arguments are in its table of read-only verbs (`steps`, `progress`, `timing`, `summary`, `overview`, `areas`, `coverage`, `review-queue`, `review-items`, `model-tree`, `bundle`, `batch-id`, `wait-files`, each on a model folder directly under the artifacts folder, `--out`/`--file` only into the scratch folder), for a Write, Edit or Read of `<artifacts_dir>/local/tmp/agami-*.json\|txt\|tsv\|md`, and for a Read under the plugin root. Anything else prints nothing, so the normal permission prompt applies. It never denies. | stdlib only; imports nothing of the plugin |
| `say_gate.py` | The plugin's other `PreToolUse` hook, on `AskUserQuestion` only (`../hooks/hooks.json`, started the same way). The engine records the closing summary and the examples table in `<artifacts_dir>/local/tmp/say-pending.json` when it prints them (a name `approve_own_calls.py` never approves, so a subagent can't plant text there; an entry not shaped as the engine writes it is dropped). When a card is asked and one whose `say_id` this session's transcript shows has not been posted as visible text after it, the hook prints its text as `systemMessage` (Claude Code shows it to the person) and removes it, so it is shown once; several are shown in the order they were registered, in one message of at most 10,000 characters, the documented `systemMessage` limit (from the oldest, those that fit whole; the rest wait for the next card; a single text over it is cut at a whole line and ends with a fixed line saying the rest is in the model summary). Text the assistant posted is removed and not shown again. Its only output is `systemMessage`: it never makes a decision, so the card always goes ahead. When text of this session is not yet seen posted, it first waits, up to 3 s, for the card's own line to reach the transcript (written asynchronously), so text posted just before the card is not shown twice. Any doubt (a file it can't read or trust, a transcript it can't parse or that never shows the card, a change it can't save) prints nothing. Of two hook processes (two cards in one reply) only the one that creates the entry's `say-claim-<id>` file (O_EXCL) prints it, and every save re-reads the file and changes only the entries that run handled, so an entry registered meanwhile is kept. After one 3 s wait that timed out, an entry is marked `waited` and later cards decide at once. It reads only the pending file and the transcript, and writes only the pending file and the claim files beside it (removed after 30 minutes). | stdlib only; imports nothing of the plugin |
| `connect_resolve.py` · `setup_pgauth.py` · `promote_credentials.py` · `reconcile.py` · `parse_*.py` · `csv_to_sections.py` · `sm` (CLI launcher) · `build_duckdb_attach.py` (retired) | Connect/auth, model reconcile, parsing, and the `sm` launcher for `python -m semantic_model.cli`. | stdlib; some import the agami-core package |

## Install dependencies (only if you want the Python driver path)

```bash
pip install psycopg2-binary pymysql
```

That's it. No `agami` package on PyPI. The skill itself is just markdown + reference docs.

## Running a script directly

```bash
# Render a chart (single section)
python render_chart.py \
  --title "Top customers" \
  --summary "" \
  --section '{"title":"Top customers by spend","insights":"Carol Chen leads at $148.95.","chart_type":"bar","labels":["Carol Chen","Dave Davis","Bob Brown"],"datasets":[{"label":"Spend","data":[148.95,93.96,45.0]}],"table_headers":["Customer","Spend"],"table_rows":[["Carol Chen",148.95],["Dave Davis",93.96],["Bob Brown",45.0]]}' \
  --out <artifacts_dir>/local/charts/top-customers.html
```

These helpers work without any other agami code installed — they read `<artifacts_dir>/local/credentials` and `<artifacts_dir>/local/.config` themselves.
