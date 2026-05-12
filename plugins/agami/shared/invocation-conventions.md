# Invocation conventions — read this BEFORE telling the user to invoke a skill

agami ships six skills, all prefixed `agami-` to avoid colliding with Claude Code's built-in slash commands (e.g. `/init`) and with other plugins:

| Skill | Slash command | Natural-language triggers (via `when_to_use`) |
|---|---|---|
| agami-connect | `/agami-connect` | "set up agami", "connect to my database", "introspect the schema", "reload the schema", "reintrospect", "add a new database" |
| agami-query-database | `/agami-query-database` | any data question — "how many", "show me", "top N", "trend over time", etc. |
| agami-review | `/agami-review` | "open the review dashboard", "review my model", "walk through the review queue" |
| agami-model | `/agami-model` | "open the model explorer", "show me the model", "exclude a table", "remove this column" |
| agami-save-correction | `/agami-save-correction` | "save this as a correction", "remember this", "use this SQL next time" |
| agami-reconcile | `/agami-reconcile` | "reconcile against my dashboard", "verify these numbers against agami" |

**`/agami-init` no longer exists** — its credential-setup flow was folded into `/agami-connect` Phase 0a. Users who haven't run setup before invoke `/agami-connect`; the skill detects missing credentials and runs the DB-type picker + writes `~/.agami/credentials.example` inline before introspecting.

## What works

| Form | Notes |
|---|---|
| `/agami-<skill>` (e.g. `/agami-connect`) | Works as a bare slash command across hosts. The `agami-` prefix is what makes them safe. |
| Plain natural language | Each skill's `when_to_use` carries trigger phrases. The model routes correctly without an explicit slash command. **Prefer this in chat** — it reads more naturally. |
| `@agami` at-mention (some hosts) | Some Claude Code hosts autocomplete `@agami` to the skill list. Don't assume it works on every host. |

## What does NOT work — never write these in user-facing text

The model often reaches for slash patterns it's seen on other plugins. None of these exist in users' installations:

- `/agami:connect`, `/agami:query-database`, etc. — colon-namespaced forms **do not exist**.
- `/init`, `/connect`, `/query-database`, etc. — bare forms without the `agami-` prefix. **`/init` collides with Claude Code's built-in `/init`** (which generates a CLAUDE.md), so we explicitly avoid it. The others are too generic and collide with other plugins.
- `/agami-init` — **deprecated and removed.** Its flow now lives at `/agami-connect` Phase 0a. If you see references in older docs, ignore them.
- `agami init`, `agami connect` (no slash, no @) — **do not exist** as commands.
- `@agami:init`, `@agami:connect` — colon-namespaced @-forms **do not exist**.

If you're tempted to write any of those, stop and re-read this doc.

## How to phrase guidance to the user

For most chat replies, **prefer natural-language phrasing over slash commands** — it reads better and the `when_to_use` matcher routes correctly.

| Instead of… | Say… |
|---|---|
| "Run `/agami-connect reintrospect`" | "Say 'reload the schema' and I'll re-introspect from your DB." |
| "Run `/agami-save-correction`" | "Say 'save this as a correction' and I'll add it to the examples library." |
| "Run `/agami-review`" | "Say 'open the review dashboard' to walk the queue." |
| "Type `/agami-query-database`" | Just answer the question directly. Slash commands are unnecessary. |

## When the model invents a new form

If you find yourself about to write `/init`, `/connect`, `/agami:connect`, `/agami connect`, or any other variation, stop. Either use the `agami-` prefix (e.g. `/agami-connect`) or use natural language. There is no third option.
