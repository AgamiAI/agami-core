# Design: AI descriptions that earn trust through use

## Problem

agami-connect generates **table and column descriptions** with an LLM (for a wide coded
schema that meant ~350 column descriptions in one run). These are AI output — some are
mechanical (expanded from a decoded naming legend), some are genuine guesses
(`EL_AOV_90D = "average order value for the Electronics category, last 90 days"`). They are
**not** facts the database declared, yet today they ride along on the column with no review.

The naive fix — a "review all the descriptions" queue — fails at scale:

- Hundreds (or thousands) of descriptions is too much cognitive load. People **skip** it
  (it did nothing) or **rubber-stamp** it (worse — the model is now marked "human-approved"
  and carries *false* confidence into production).
- A wrong description is a **soft** failure: it doesn't produce a silently-wrong *number*
  the way a wrong metric or join does — it nudges the SQL generator with a misleading hint.
  But it can still steer an answer wrong, and the catch happens in production when someone
  reads the answer and notices.

**Asking a human to verify what they can't judge in the abstract manufactures false trust.**

## Principle

> Only ask a human to confirm what they can judge **in the moment, with the evidence in
> front of them.**

A column description is unjudgeable in the abstract ("is this right?" about a column you've
never queried) but trivially judgeable next to a real answer ("this number used `DT_VALUE`
meaning X — yep / nope").

So descriptions get **no upfront approval gate.** They earn trust through *use*, and wrong
ones are caught at *answer time*, where the human is already looking and motivated.

## Design

### 1. Provenance flag on the description

`Column` and `Table` get an optional `description_source`:

| value | meaning |
|---|---|
| `null` (default) | unknown / legacy — treated as trusted, never surfaced |
| `human` | written or edited by a person — trusted |
| `ai_unvalidated` | AI-generated guess, never confirmed in an accepted answer |
| `ai_validated` | AI-generated, confirmed by a human (via a receipt or explicitly) |
| `ai_unknown` | the AI looked at an **opaque** column (`xyz`, `v_1`) and **couldn't** determine its meaning; description stays empty, flagged for a human to fill in |

Backward-compatible (optional, defaults `null`). The validator adds no new rule.

### `ai_unknown` vs `ai_unvalidated` — opposite handling

A **guess** (`ai_unvalidated`) and a **blank** (`ai_unknown`) are surfaced oppositely, because
the rule is: *the review queue only contains things a human can act on right now, in the
abstract.*

| | `ai_unvalidated` (a guess) | `ai_unknown` (a blank) |
|---|---|---|
| Volume | potentially hundreds | rare (only genuinely opaque columns) |
| Judgeable upfront? | **no** — "is this description right?" is meaningless about a column you've never queried | **yes** — "what is `xyz`?" is answerable by anyone who knows the schema |
| Surface | **through use only** (answer receipt) — never a queue | a small **upfront group** in the Review tab ("Columns agami couldn't read") with an inline "describe it" box, **plus** the through-use backstop |
| Action | confirm (→ `ai_validated`) or correct (→ `human`) | describe it (→ `human`); you can't "confirm" a blank |

Filling in an `ai_unknown` column's description (in the Review-tab group or at query time) writes
a real `description` → the curate edit flips `description_source` to `human`, and it leaves the
group. An `ai_unknown` column that's *never* described still gets surfaced the first time a query
uses it ("I used `xyz` but don't know what it is — is this the right column?"), so the
opaque-but-queried case is never silent.

### 2. Tag at generation; flip on human touch

- **agami-connect** generation passes `source: "ai"` on each description `edit` op → the
  curate engine stores `description_source: "ai_unvalidated"`.
- **Any human edit** of a description (via the model dashboard) → curate stores
  `description_source: "human"` automatically. Human-authored = trusted, full stop.
- **Confirm** (see below) → an `edit` op sets `description_source: "ai_validated"`.

The curate `edit` handler owns this so the rule is enforced in one place.

### 3. Surface the assumptions **used by this answer** (not a dashboard)

`agami-query`'s trust receipt gains an `assumptions` section. For each column the SQL
**actually used in a load-bearing way** (SELECT / WHERE / GROUP BY / ORDER BY — *not* pure
join plumbing) whose `description_source == "ai_unvalidated"` and whose description is
non-empty, add:

```json
"assumptions": [
  {"column": "ANALYTICS.ORDERS.EL_AOV_90D",
   "meaning": "Average order value for the Electronics category over the last 90 days.",
   "source": "ai_unvalidated"}
]
```

Rules that keep it from becoming noise:

- **Cap + rank.** At most ~3 per answer, prioritising load-bearing columns. A query touching
  10 columns surfaces 1–3 assumptions, not 10.
- **Passive by default.** They live in the receipt's provenance panel (always traceable). A
  short one-line nudge appears in the chat answer *only* when a load-bearing unvalidated
  description fed the answer: *"ℹ This answer read `DT_VALUE` as '…'. If that's off, tell me;
  otherwise say 'confirmed'."*
- **Self-extinguishing.** Frequently-used columns get confirmed once and never resurface;
  obscure columns nobody queries never nag.

This is exactly where the hawk-eyed catch happens today — except now it's loud, in context,
and one click, instead of relying on someone noticing a number looks off.

### 4. Confirm / correct loop closes it

- **"confirmed" / "looks right"** → emit `edit` ops setting `description_source: "ai_validated"`
  for the surfaced columns. They stop surfacing.
- **A correction** ("`DT_VALUE` actually means …") → routes to `agami-save-correction`, which
  updates the `description` **and** sets `description_source: "human"` (a corrected
  description is human-authored). Also saved as a NL→SQL example as usual.
- **Silence** → stays `ai_unvalidated`. Not auto-validated (that's the rubber-stamp we're
  avoiding) — it just remains visible in the receipt and may resurface if the column is used
  in a load-bearing way again later.

### 5. Explorer shows provenance, but never bulk-approves

The model dashboard tags descriptions: a muted **`ai`** chip on `ai_unvalidated`, a subtle
**`✓ ai`** on `ai_validated`, nothing on `human`. This makes "what did the AI write vs what's
been validated" visible while browsing. **There is deliberately no bulk "approve all
descriptions" action** — that's the trap. Editing a description (which a human would do to
fix one) flips it to `human` automatically.

## What this is NOT

- Not a sign-off gate. A query is never *refused* because a description is unvalidated (unlike
  an unsigned metric). Descriptions are advisory context; the trust spine still gates the
  things that change the *number* (metrics = Rule 1, entities/joins = Rule 2).
- Not a queue. There is no list of 345 descriptions to walk.

## Touch points

| Layer | Change |
|---|---|
| `semantic_model/models.py` | `description_source` on `Column` + `Table` |
| `semantic_model/curate.py` | edit handler sets `description_source` (ai vs human); confirm via direct edit |
| `skills/agami-connect/SKILL.md` | Phase 2a passes `source: "ai"` on description edits |
| `scripts/render_model_explorer.py` | expose `description_source` in the manifest |
| `shared/model-explorer-template.html` | `ai` / `✓ ai` chip; no bulk approve |
| `skills/agami-query/SKILL.md` | `assumptions` in the receipt; nudge; confirm/correct routing |
| `shared/chart-template.html` | render the Assumptions section in the receipt panel |
| `skills/agami-save-correction/SKILL.md` | description fixes set `description_source: "human"` |
