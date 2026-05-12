# `ORGANIZATION.md` — per-database domain context

Free-form Markdown describing **what this database is about**: the company / product, what the data represents, who the users are, what the domain vocabulary means. The skill loads it into every SQL-generation prompt as steering context, ahead of `USER_MEMORY.md`.

Lives at `<artifacts_dir>/<profile>/ORGANIZATION.md`. One file per profile — context is database-specific (a fintech database has different vocabulary than an ITSM database).

Local-only. Never sent in telemetry. The skill reads it on each query, but neither the file nor any extracted snippet leaves the machine.

## Why it's separate from `USER_MEMORY.md`

| File | Lives where | Scope | Examples |
|---|---|---|---|
| `<artifacts_dir>/USER_MEMORY.md` | Profile-agnostic (one global file) | **User preferences** that apply across every database | "default time window: last 30 days", "exclude test users with email matching @example.com", "show currency as EUR" |
| `<artifacts_dir>/<profile>/ORGANIZATION.md` | Per-profile (one per database) | **Domain knowledge** about this specific database | "MRR = monthly recurring revenue", "active user = signed in within 30 days", "fiscal year starts October" |

The skill loads both on every query. `ORGANIZATION.md` answers *what does this data mean*, `USER_MEMORY.md` answers *how should I display / filter results*. They don't overlap.

## Format

It's just Markdown. No required schema. Free prose, optional headings.

### Default template (written by `init` at first connect)

```markdown
# About this database

(Write one paragraph describing what this company / product / system is about,
what the data represents, who the users are. The skill loads this on every
query as domain context.)

## Key terminology

(Domain vocabulary the skill should know. Examples below — replace with yours.)

- "MRR" = monthly recurring revenue, computed as SUM(price) WHERE plan='subscription'
- "active user" = signed in within the last 30 days
- "fiscal year" starts in October

## Who's in this data

(Customers / users / operators / suppliers — whatever roles matter.)

## What we DON'T track here

(Helps the skill avoid wild guesses about tables that don't exist.)
```

The user is expected to replace the parenthetical guidance with real prose. The template is intentionally minimal; the skill works fine with just a paragraph under `# About this database`.

## When the skill loads it

- **`query-database`** — Phase 1d.2 reads `<artifacts_dir>/<profile>/ORGANIZATION.md`, strips HTML comments, injects under `## Organization context` in the SQL-generation prompt (ahead of `## User memory (preferences and policies)` from `USER_MEMORY.md`).
- **`connect`** — Phase C (description generation) loads ORGANIZATION.md as a domain prior so the auto-generated table / column descriptions reflect the user's terminology.
- **`save-correction`** — `org_context` correction kind appends to ORGANIZATION.md.

If the file is missing or empty, the skill treats it as no-context and proceeds. Never errors.

## How `save-correction` extends it

When the user says something like "gold tier means lifetime spend > $10k" or "we use 'churn' to mean a customer who hasn't placed an order in 90 days", the agami-save-correction skill classifies that as `org_context` and appends a one-line entry under the `## Key terminology` heading (creating the heading if missing). The next query picks it up.

The `user_preference` correction kind still routes to `USER_MEMORY.md` — those go where they always went.

## Privacy

`ORGANIZATION.md` is local-only:

- **Never sent in telemetry.** The 11-field allowlist in [`telemetry-payload.md`](telemetry-payload.md) doesn't include any free-text field, period.
- **Never read by anything other than the agami skills running locally.**
- The agami-connect Phase 0a enforces `chmod 600` on creation.

If the user shares `<artifacts_dir>/<profile>/` with a teammate (e.g. via dotfiles), the file goes with the model — that's a deliberate user action.
