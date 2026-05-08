# `~/.agami/USER_MEMORY.md` — User memory format

Free-form Markdown file holding **cross-database user preferences and policies** that should apply no matter which profile the user connects to. Every agami skill loads this file on each invocation and applies what's in it to SQL generation, formatting, and follow-up suggestions.

This is **separate** from the auto-memory file at `~/.claude/projects/<workspace>/memory/MEMORY.md` (Claude Code's auto-memory, which is host-managed and project-scoped). USER_MEMORY.md is **agami-managed**, lives alongside `credentials` and the per-profile directories, and persists across hosts (CLI, Cowork, Desktop) the same way credentials do.

USER_MEMORY.md is also **separate from `~/.agami/<profile>/ORGANIZATION.md`**:

| File | Scope | Examples |
|---|---|---|
| `~/.agami/USER_MEMORY.md` | **Cross-database** preferences (one global file) | "default time window: last 30 days", "exclude test users with email matching @example.com", "show currency as EUR" |
| `~/.agami/<profile>/ORGANIZATION.md` | **Per-database** domain context (one per profile) | "MRR = monthly recurring revenue", "active user = signed in within 30 days", "fiscal year starts October" |

The skill loads both on every query. USER_MEMORY answers *how should I display / filter results, no matter which database*; ORGANIZATION.md answers *what does the data mean for this specific database*. They don't overlap.

## What goes in here (USER_MEMORY)

- **Default filters** the user always wants applied across every database (e.g. "exclude test users where email matches `%@example.com`")
- **Display preferences** (currency formatting, date format, "always show top 10 not top 5")
- **Hard avoids that apply broadly** (don't query rows where `is_test = true`)

If the preference is database-specific (e.g. "in this finance DB, always join orders to invoices"), it belongs in the OSI model or in `ORGANIZATION.md`, not here.

## What does NOT go in here

- **Connection details** → `~/.agami/credentials`
- **Schema knowledge** (table descriptions, FK relationships, column types, choice fields, metrics) → `~/.agami/<profile>/<schema>.yaml` (OSI semantic model)
- **Domain vocabulary specific to one database** ("MRR means…", "gold tier means…") → `~/.agami/<profile>/ORGANIZATION.md`
- **Specific question→SQL examples** → `~/.agami/<profile>/examples.yaml` (few-shot library)
- **Telemetry consent / install ID / connection-method choice** → `~/.agami/.config`
- **Email opt-in state** → `~/.agami/.optins`

`agami-save-correction/SKILL.md` classifies each correction and routes the knowledge to the right file. A `user_preference` correction lands here. An `org_context` correction lands in ORGANIZATION.md. Other kinds land in the per-schema yamls (per the table in save-correction).

## Default seed (written by `agami-init/SKILL.md` on first run)

```markdown
# agami user memory

Free-form preferences and policies. Every agami skill loads this file on each
invocation and applies what's here to query generation, result formatting, and
follow-up suggestions.

Edit by hand, or ask the skill to "remember" something during a conversation
(e.g., "from now on, always exclude test users where email matches @example.com").

## Default filters
<!-- Things to always include or exclude in queries.
     - Exclude rows where customers.email LIKE '%@example.com'
     - Default time window: last 30 days unless the question specifies otherwise
-->

## Naming and synonyms
<!-- Domain vocabulary that isn't already in the OSI model's ai_context.
     - "active" means is_active = true AND status = 'live'
     - "MRR" = SUM(price) WHERE plan_type = 'subscription'
-->

## Display preferences
<!-- Output formatting.
     - Currency: USD with 2 decimals
     - Dates: ISO format (2026-05-06), not relative ("today")
-->

## Avoid
<!-- Things to never do.
     - Don't query the _audit schema
     - Don't include cancelled orders unless I explicitly ask for them
-->
```

The HTML-comment hints (`<!-- -->`) document each section; the user replaces them with their actual policies, or leaves the section empty.

## How skills consume this file

On every invocation, the skill:

1. Reads `~/.agami/USER_MEMORY.md` (entire file — it's intentionally small, target ≤ 4 KB).
2. Strips HTML comments (`<!-- ... -->`) from the loaded content. Comments are scaffolding for the user, not for the LLM.
3. Includes the stripped text in the SQL-generation prompt as a labeled section: `## User memory (preferences and policies)\n<file content>`.

The LLM uses this as additional steering context: it should respect the policies when generating SQL, when picking a chart type, when formatting output.

## How preferences get added

Three paths:

1. **User edits the file directly.** They open `~/.agami/USER_MEMORY.md` in their editor, write a bullet under a section, save. The next query picks it up.

2. **The `agami-save-correction` skill classifier identifies a `user_preference`.** During Phase 3 of save-correction, if the user's feedback reads like a general policy ("from now on, always …"; "I prefer …"; "never include …"), the classifier routes it to USER_MEMORY.md instead of the semantic model or examples library. The skill picks the right section, appends a bullet, shows the diff, gets approval before writing.

3. **Explicit "remember this" in a query session.** The user says "remember that test users have @example.com emails". agami-query-database's Phase 4d follow-up logic catches this phrasing and offers to add it to USER_MEMORY.md.

## Validation

This file is intentionally **not** schema-validated. It's free-form markdown the user owns. The agami skills read it as opaque context — they don't enforce structure beyond stripping HTML comments. If the user writes something contradictory or incoherent, the skill follows it anyway.

The one rule: if the file is missing, skills behave as if it were empty. They never error out on an absent USER_MEMORY.md. The `agami-init/SKILL.md` ensures the seed is written on first run, so this case is rare.

## Size cap

Soft cap: 4 KB. Above that, prompt context starts to crowd out semantic model and examples library. Skills should warn the user if USER_MEMORY.md exceeds 8 KB and suggest pruning. No hard cap or truncation — that surprises the user.
