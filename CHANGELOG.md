# Changelog

All notable changes to **agami** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `version` in `.claude-plugin/marketplace.json` and `plugins/agami/.claude-plugin/plugin.json`
is the source of truth a host installs against — bumping it is what invalidates a
user's plugin cache (see [CONTRIBUTING.md](CONTRIBUTING.md)). Each released section
below corresponds to one such version.

## [0.3.0] — 2026-06-24

### Added

- **No-database sample (`/agami-connect sample`).** Ships *Acme Store*, a small
  local SQLite dataset (commerce + subscriptions) with a ready-made, signed-off
  semantic model. Goes from install to a governed, receipted answer in under a
  minute — no connection, no credentials, nothing leaving the machine. The
  bootstrap (Phase 0s) offers a fast copy-the-model path and a "watch it build
  live" rebuild path. Builds deterministically via the `sqlite3` CLI or a pure
  Python-stdlib fallback (no install required).
- **Model snapshots / `model_version`.** A model write now stamps a content-hashed
  snapshot under `<profile>/.snapshots/<hash>/`, so every answer's receipt pins a
  real `model_version` (previously `null` for all profiles) and old answers stay
  reproducible. New `sm snapshot <root>` CLI.
- **Deterministic interaction spine.** The mechanical parts of the skills are now
  scripts that emit a uniform `{ok, data, anomalies, needs_judgment}` contract, so
  the agent only makes judgment calls on genuine ambiguity:
  `connect_resolve.py` (one call resolves profile / credentials / interpreter +
  next-phase decision — fixes choosing a Python that can't connect),
  `parse_prune_block.py` (fixes a shell word-split that mangled table lists),
  `parse_model_feedback.py` (the dashboard back-channel), `csv_to_sections.py`
  (charts/tables get their numbers from the result CSV, not the model), and the
  `sm receipt` / `sm curate-gate` subcommands. See
  [`docs/design/determinism-refactor.md`](docs/design/determinism-refactor.md).

### Changed

- **Renamed LiteBi → agami-core.** The install identity is now `agami-core@agami`
  (marketplace `agami`, plugin `agami-core`); the version bump is breaking, so
  existing `agami@litebi` installs must re-add the marketplace to upgrade
  (`/plugin marketplace add AgamiAI/agami-core` → `/plugin install agami-core@agami`).
- **Repositioned around the trust layer.** README, marketplace, and plugin
  metadata now lead with the governance/trust stance ("the trust layer between AI
  and your data") instead of natural-language querying. Dropped the "BI" framing.
- **Quickstart leads with the sample** — the fastest path to a first governed
  answer, with the real-database flow following it.

### Security

- **Engine-level PII enforcement.** Raw projection of a column marked `sensitive`
  is refused in the shared executor (`runtime.check_sensitive_projection`, wired
  into `execute_sql.py`), so the same rule protects the Claude Code skill **and**
  the local MCP server. Aggregates, filters, joins, and `GROUP BY` over sensitive
  columns are still allowed — only raw per-row output is blocked.

## [0.2.2] — baseline

First version tracked in this changelog. Earlier history lives in the git log.

- The local-first **trust layer**: confidence + review state on every join,
  metric, and entity; single-reviewer sign-off; per-answer receipts (SQL, tables,
  relationships, metric definitions, freshness); a review dashboard.
- Schema introspection into a provider-portable, git-native YAML semantic model.
- NL→SQL generation and **local execution** across Postgres, Supabase, Redshift,
  MySQL, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino, DuckDB, and
  SQLite.
- Corrections with attribution, persisted to an `examples.yaml` few-shot library.
- An optional local **MCP server** (`agami serve`) for use from Claude Desktop and
  other clients — stdio, no auth, no network.
- Fan-trap / chasm-trap pre-flight that refuses to silently double-count.

[0.3.0]: https://github.com/AgamiAI/agami-core/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/AgamiAI/agami-core/releases/tag/v0.2.2
