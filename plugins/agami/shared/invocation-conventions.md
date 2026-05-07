# Invocation conventions — read this BEFORE telling the user to invoke a skill

Across Claude Code hosts (CLI, VS Code, Cursor, Cowork, Desktop), agami's slash-command surfacing is **inconsistent**. The user can reliably type **`/init`** as a slash command. Everything else is unreliable as a slash command and must be invoked through natural-language phrasing instead, via the skills' `when_to_use` matching.

## What works

| Form | Reliability | Notes |
|---|---|---|
| `/init` (bare slash) | ✓ Works in every Claude Code host | The only confirmed-working slash command for agami. |
| Plain natural language ("how many orders…", "save this as a correction", "reload the schema") | ✓ Works everywhere | Each skill's `when_to_use` field carries trigger phrases. The model routes correctly. |
| `@agami` at-mention (some hosts) | partial | Cowork autocompletes `@agami` to a list of skills. Other hosts may treat it as plain text. Don't assume it works. |

## What does NOT work — never write these in user-facing text

The model often reaches for slash-command patterns it's seen elsewhere. Override that reflex. **None of the following exist in users' installations**, and asking the user to type them produces a confusing dead-end:

- `/agami:init` (colon-prefixed plugin-scoped form) — **does not exist**
- `/agami:connect`, `/agami:query-database`, `/agami:save-correction` — **do not exist**
- `/agami init`, `agami:init`, `agami init` (bare) — **do not exist**
- `/connect`, `/query-database`, `/save-correction` — surfaced inconsistently; assume **no**
- `@agami:init`, `@agami:connect` — **do not exist**

If you're tempted to write any of those, stop and re-read this doc. The user has tested every form; only `/init` (bare) is reliable as a slash command. Everything else uses natural language.

## How to phrase guidance to the user

When the user asks a question that another agami skill would answer better, **never tell them to type a slash command**. Instead, phrase it as a natural-language instruction the skill's `when_to_use` will catch:

| Instead of… | Say… |
|---|---|
| "Run `/agami:connect reintrospect`" | "Tell me 'reload the schema' and I'll re-introspect from your DB." |
| "Run `/connect`" | "Say 'introspect my database' and I'll connect and seed examples." |
| "Type `/save-correction`" | "Say 'save this as a correction' and I'll add it to the examples library." |
| "Run `/agami:init`" | "Run `/init`" (bare slash works) — or "say 'set up agami'." |

## The one exception

`/init` IS a working slash command. Tell users to type `/init` directly when init is the right next step. Don't write it as `/agami:init` or `/agami init` — just `/init`.

## When the model invents a new form

If you find yourself about to suggest a slash command that isn't `/init`, you've drifted. Check this doc. Use natural language.
