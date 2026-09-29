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

- **MCP App seam: a page beside a tool, an app-only tool, and a tool-result hook.** Three optional
  seams for a consumer that wants an MCP App rendered beside a tool's answer; agami-core uses none of
  them, and with none in use every byte a client of either protocol era receives is unchanged. An
  `extra_tools` entry may carry `"page": {"uri": "ui://…", "html": str}`, which puts
  `_meta.ui.resourceUri` on the tool and serves the page over `resources/list` and `resources/read`
  with the MCP App MIME type, and `"app_only": True`, which adds `_meta.ui.visibility: ["app"]`. A
  page declares only `uri` and `html`: `csp`, `domain`, `permissions` or any other key is refused at
  composition, naming the tool, so a page cannot ask a host for network access. A page is visible
  when any tool linking it is, under `Adapters.tool_visibility`, and a hidden page answers the same
  `Unknown resource` error as an undeclared one, byte for byte. The `resources` capability, and
  `capabilities.extensions["io.modelcontextprotocol/ui"]` on 2026-07-28, appear only when a page is
  declared. **App-only is a marking, not a control: any client can still call the tool, so its
  handler must check its own scope.** `Adapters.tool_result_hook(name, arguments, result_text)`
  returns an object added to the result's `_meta` beside the unchanged text (not `structuredContent`,
  which a client may give the model in place of the text); it runs off the event loop in the
  request's context, only on a result a handler returned, keys under the reserved
  `io.modelcontextprotocol/` prefix are dropped, and a hook that raises or returns anything but a JSON
  object is logged once and dropped without touching the call's outcome or its audit row. `None` (the OSS default) is byte-identical to prior behaviour.

### Security

- **A `column_scope` refusal lists what the tables it read DO declare (#386).** It said a column
  was not declared and not what the agent could use, so the agent guessed again: refused,
  guessed, refused. `remediation` now leads with the declared columns of the tables the refused
  statement reads — the same columns `get_datasource_schema` shows for them, up to 40 per table
  and 5 tables — and the closest listed name for each refused column, as a typo hint. This is a
  recorded amendment of the rule that a refusal never lists the declared surface; see SECURITY.md,
  "What a refusal may name", for the boundary and the residual.

