# Changelog

All notable changes to **agami** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `version` in `.claude-plugin/marketplace.json` and `plugins/agami/.claude-plugin/plugin.json`
is the source of truth a host installs against — bumping it is what invalidates a
user's plugin cache (see [CONTRIBUTING.md](CONTRIBUTING.md)). Each released section
below corresponds to one such version.

## [Unreleased]

### Added

- **On protocol 2026-07-28, `get_datasource_schema` asks the person which datasource they meant.**
  When `datasource` is omitted on an organization serving several, a client that declared form
  elicitation gets an input-required result with one form listing the served names, and the retry
  carrying the choice returns that datasource's schema. A declined or cancelled form, a 2025-06-18
  request, a client without elicitation, stdio, and a server with no `AGAMI_SIGNING_SECRET` all get
  the `datasource_required` answer, unchanged. The question's `requestState` is sealed under a key
  derived from `AGAMI_SIGNING_SECRET` and bound to the caller, organization, tool, arguments and a
  ten-minute expiry; anything else is refused with `-32602`. A state opens on any instance sharing
  the secret, so nothing is kept between the rounds, and both rounds write a `tool_calls` row. Tool
  authors can ask the same way: return `tools.NeedsInput(key, message, schema)` when
  `tools.can_ask()` is true, and read the reply from `tools.current_answer()`.

### Security

- **A `column_scope` refusal lists what the tables it read DO declare (#386).** It said a column
  was not declared and not what the agent could use, so the agent guessed again: refused,
  guessed, refused. `remediation` now leads with the declared columns of the tables the refused
  statement reads — the same columns `get_datasource_schema` shows for them, up to 40 per table
  and 5 tables — and the closest listed name for each refused column, as a typo hint. This is a
  recorded amendment of the rule that a refusal never lists the declared surface; see SECURITY.md,
  "What a refusal may name", for the boundary and the residual.

