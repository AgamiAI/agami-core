# `ORGANIZATION.md` — per-database human narrative

Free-form Markdown describing **what this database is about** in the human's own words: the company / product, what the data represents, who the users are. One file per profile at `<artifacts_dir>/<profile>/ORGANIZATION.md` — context is database-specific.

**It holds the human narrative ONLY.** The factual summary — subject areas, conventions, and the decoded domain **glossary** — is NOT stored here. That's derived from the structured model at read time and combined with the narrative when a reader needs the full context. The two homes stay separate so:

- a human editing their prose can never accidentally overwrite or delete the auto facts, and
- the glossary always reaches the LLM, even if the narrative file is empty or hand-mangled.

Local-only. Never sent in telemetry.

## The two parts of "domain context"

| Part | Where it lives | Who owns it |
|---|---|---|
| **Narrative** — what the company/product is, who the users are | `ORGANIZATION.md` (this file) | the human (free prose) |
| **Glossary** — term → meaning (e.g. `MRR` → "monthly recurring revenue") | `key_terminology` field on `org.yaml` (structured) | written by enrichment / `set-terminology`; surfaced read-only |
| **Summary** — shape, subject areas, conventions (units/currency) | derived from the model each time | computed, never stored |

`cli org-context "$ROOT"` assembles all three for a reader: the narrative (HTML comments stripped) + a `## Model summary (auto-generated from your schema)` block with the subject areas, conventions, and glossary. The model explorer shows the narrative as an **editable** field and the derived summary as a **read-only** field beneath it.

## Why it's separate from `USER_MEMORY.md`

| File | Scope | Examples |
|---|---|---|
| `<artifacts_dir>/USER_MEMORY.md` | **User preferences**, across every database | "default window: last 30 days", "show currency as EUR" |
| `<artifacts_dir>/<profile>/ORGANIZATION.md` | **Domain narrative** for this database | "We swap EV batteries at stations; a 'member' has an active plan." |

`ORGANIZATION.md` answers *what does this data mean*; `USER_MEMORY.md` answers *how should I display / filter results*.

## Format

Just Markdown. No required schema — free prose under `# About this database`. On the skip path the skill writes a tiny starter (a prompt comment, nothing else):

```markdown
# About this database

<!-- Describe what only you know: what the company/product is, who the users are,
     and what your key terms mean. agami already knows your schema — this is for the
     human context it can't infer. Leaving this as-is is fine; agami still works. -->
```

The user replaces the comment with real prose, or leaves it — agami works either way, because the summary + glossary come from the model regardless.

## Where each kind of fact goes

- **Narrative / "what we are"** → `ORGANIZATION.md` (this file).
- **A term's meaning** ("gold tier = lifetime spend > $10k", an acronym → its expansion) → the structured `key_terminology` field via `cli set-terminology`. It then renders into the derived summary automatically — no file edit needed.
- **A display/formatting convention** tied to a column (currency/unit) → the column's `caveat`/`value_transform` in the model. A genuinely cross-cutting presentation rule → `USER_MEMORY.md`.

## When the skill loads it

- **`query-database`** — Phase 1d.2 runs `cli org-context`, injecting the combined narrative + derived summary under `## Organization context`, ahead of `## User memory`.
- **`connect`** — enrichment uses the narrative (if any) as a domain prior for descriptions; writes the glossary to `key_terminology`; writes the starter on the skip path.

If there's no narrative and no model, the context is empty — never an error.

## Privacy

Local-only: never sent anywhere, read only by the agami skills running locally, `chmod 600` on creation. If the user shares `<artifacts_dir>/<profile>/` with a teammate, the file goes with the model — a deliberate user action.
