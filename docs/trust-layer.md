# The trust layer (in depth)

Most AI data agents quietly pick a join, quietly pick a definition of "revenue",
and quietly return a number. `agami` makes every one of those decisions
auditable — with one knob per workspace and one queue per curator.

This page is the deep dive. For the summary, see the [README](../README.md#the-trust-layer).

## Every entry carries a confidence + a review state

`agami-connect` writes each join, metric, and entity with a flat **trust block** —
a confidence label, a review state, and (once approved) a sign-off identity. No
vendor blobs, no numeric scores to tune:

```yaml
# a relationship in subject_areas/<area>/relationships.yaml
- from_table: orders
  to_table: customers
  from_column: customer_id
  to_column: id
  relationship: many_to_one
  confidence: confirmed        # confirmed | inferred | proposed
  review_state: approved       # unreviewed | approved | rejected | stale | not_applicable
  signed_off_by: null          # set when a human approves
  signed_off_at: null
  signed_off_role: null        # cfo | cto | data_lead | engineer | analyst | other
```

A **metric** carries the same block plus its definition — prose `calculation` +
per-dialect `bindings` — so an answer can show exactly what "revenue" means and
who vouched for it.

Auto-approve collapses the queue to what actually needs human eyes:
- A **DB-declared foreign key** → relationship `confidence: confirmed`,
  `review_state: approved` (the database already vouches for it).
- A **probe-inferred** join (name + value overlap, no declared FK) →
  `confidence: proposed` / `inferred`, `review_state: unreviewed`.
- A column with a **self-evident structural name** (`id`, `*_id`, `created_at`,
  `email`, `status`, `is_*`/`has_*` flags…) needs no description and is never
  queued.

Everything inferred stays `unreviewed` and surfaces in the Review tab.

## Rule 1 vs Rule 2 — and the hybrid review order

- **Rule 1 — metrics** (always queue): a metric must be signed off — a
  `signed_off_by` email AND a `signed_off_role` AND a non-empty `calculation` —
  before the runtime treats it as truth. Highest blast radius: one bad metric
  skews every report that uses it. The validator enforces all three before a
  metric can be `approved`.
- **Rule 2 — joins & entities** (lazy): usable while `unreviewed`; they
  self-approve as you query and surface as receipt warnings until confirmed. No
  threshold to tune — it's review *state*, not a number.

At runtime, `agami-query` still **answers** questions that use `unreviewed`
metrics, joins, or entities — but every unreviewed entry it relied on surfaces as
a **warning** in the receipt (e.g. *"Used metric `revenue` which has not been
signed off"*), with a one-click link to the Review tab. Nothing is silently
trusted; nothing is hard-blocked. Only `rejected` (excluded) entries are dropped
entirely — those never appear in an answer.

**Hybrid review order in `/agami-connect`**: Phase 4 surfaces a Rule 1 sign-off
gate *before* seed examples are generated (Phase 5). Reason: seed SQL exercises
metric definitions; signing them off first means the seeds inherit approved truth
instead of LLM guesses. Rule 2 polish (low-confidence joins / field descriptions)
stays in Phase 7's optional collapsed panel — it self-approves as the user queries
and never blocks the path to first answer.

## The review queue (a tab of the model dashboard)

`/agami-model review` (or "open the review dashboard") opens the model dashboard
on its **Review** tab — the trust-layer sign-off queue. The queue splits into
**Needs your eyes** (Rule 1 metrics, low-confidence or drifted entries) and
**Looks right (confident)** (FK-derived joins, clearly-defined entries) with a
one-click "Approve all". Each card shows:

- The inferred SQL fragment / definition / mapping
- Its confidence + review state
- An inline editable textarea for the description / `calculation`
- Per-card Approve / Reject / Edit buttons + group-level "Approve all"

Approving stamps the curator's email + role (resolved once and saved). Click
through the queue, hit "Generate feedback for Claude" at the bottom, paste back
into chat. agami applies each edit, runs the validator, commits the result to
`<artifacts_dir>/<profile>/.git/`, and re-renders.

## Every answer ships a receipt

Every `agami-query` answer includes a "Provenance for this answer" panel:

- The literal SQL that ran (no paraphrase)
- Tables touched + row count per table
- Relationships used, each with its confidence + review state
- Metric definitions invoked, with author + sign-off date
- Named-filter predicates used (named, not anonymous)
- Source-data freshness per table (when the DB exposes it)
- Model snapshot hash (so the answer is reproducible from
  `<artifacts_dir>/<profile>/.snapshots/<hash>/`)
- A warning banner if any unreviewed entry was used

## Examples validation

Phase 5 of `agami-connect` generates 10–12 NL→SQL seed examples that each satisfy
one of five **analytical shapes**: aggregation with a measure, segmentation, time
comparison, filtered top-N with context, or cohort / retention. Plain row-listing
is disqualified. Each seed is EXPLAIN-validated against the live DB, then surfaced
in an examples-validation dashboard
(`<artifacts_dir>/local/examples-validation/<ts>.html`) — same per-card pattern as
the review dashboard, with Validate / Reject / Edit / Add note buttons + an inline
"Add example" affordance.
