# Invocation conventions — read this BEFORE telling the user to invoke a skill

agami ships four skills, all prefixed `agami-` to avoid colliding with Claude Code's built-in slash commands (e.g. `/init`) and with other plugins:

| Skill | Slash command | Natural-language triggers (via `when_to_use`) |
|---|---|---|
| agami-init | `/agami-init` | "set up agami", "configure agami", "switch profiles", "verify my agami install" |
| agami-connect | `/agami-connect` | "connect to my database", "introspect the schema", "reload the schema", "reintrospect" |
| agami-query-database | `/agami-query-database` | any data question — "how many", "show me", "top N", "trend over time", etc. |
| agami-save-correction | `/agami-save-correction` | "save this as a correction", "remember this", "use this SQL next time" |

## What works

| Form | Notes |
|---|---|
| `/agami-init`, `/agami-connect`, `/agami-query-database`, `/agami-save-correction` | All four work as bare slash commands across hosts. The `agami-` prefix is what makes them safe. |
| Plain natural language | Each skill's `when_to_use` field carries trigger phrases. The model routes correctly without an explicit slash command. **Prefer this for everything except agami-init** — it reads more naturally in chat. |
| `@agami` at-mention (some hosts) | Cowork / Desktop autocompletes `@agami` to the four-skill list. Don't assume it works on every host. |

## What does NOT work — never write these in user-facing text

The model often reaches for slash patterns it's seen on other plugins. None of these exist in users' installations:

- `/agami:init`, `/agami:connect`, `/agami:query-database`, `/agami:save-correction` — colon-namespaced forms **do not exist**.
- `/init`, `/connect`, `/query-database`, `/save-correction` — bare forms without the `agami-` prefix. **`/init` collides with Claude Code's built-in `/init`** (which generates a CLAUDE.md), so we explicitly avoid it. The other three names are too generic and would collide with other plugins.
- `agami init`, `agami connect` (no slash, no @) — **do not exist** as commands.
- `@agami:init`, `@agami:connect` — colon-namespaced @-forms **do not exist**.

If you're tempted to write any of those, stop and re-read this doc.

## How to phrase guidance to the user

For most chat replies, **prefer natural-language phrasing over slash commands** — it reads better and the `when_to_use` matcher routes correctly.

| Instead of… | Say… |
|---|---|
| "Run `/agami-connect reintrospect`" | "Say 'reload the schema' and I'll re-introspect from your DB." |
| "Run `/agami-save-correction`" | "Say 'save this as a correction' and I'll add it to the examples library." |
| "Type `/agami-query-database`" | Just answer the question directly. Slash commands are unnecessary. |

The exception: `/agami-init` is the natural way to say "start over from scratch", since "set up agami" can be ambiguous (do they mean init, or connect, or both?). Use `/agami-init` when init is the specific next step.

## When the model invents a new form

If you find yourself about to write `/init`, `/connect`, `/agami:init`, `/agami init`, or any other variation, stop. Either use the `agami-` prefix (e.g. `/agami-init`) or use natural language. There is no third option.
