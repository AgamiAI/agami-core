# Trust-layer smoke test (Semantic-Model launch — May 18)

A 10-minute manual walkthrough that exercises every May 18 deliverable end-to-end.
Run this on a clean machine after installing the `trust-layer` branch in Claude
Code (`/plugin marketplace add AgamiAI/LiteBi#trust-layer && /plugin install agami@litebi`).

The fixture is SQLite. The trust layer's worst-case substrate (no column
comments, sparse FK metadata) — if it works here, it works everywhere.

---

## 0. Build the fixture

```bash
cd <repo>/tests/integration/fixtures
sqlite3 shop.db < sqlite-shop-init.sql
sqlite3 shop.db "SELECT name FROM sqlite_master WHERE type='table';"
# customers / products / orders / order_items
```

Then point agami at it (in Claude Code or by hand):

```ini
# <artifacts_dir>/local/credentials
[default]
db_type = sqlite
path    = <absolute path to shop.db>
```

Set `chmod 600 <artifacts_dir>/local/credentials`.

---

## 1. `/agami-connect` Phase 0a — credential preflight

The credential-setup path that used to live in a separate `/agami-init` skill is now Phase 0a of `/agami-connect`. The first time you invoke `/agami-connect` after a fresh install (no `<artifacts_dir>/local/credentials` present), it should drop into Phase 0a: DB-type picker → write `<artifacts_dir>/local/credentials.example` → exit cleanly.

**Pass criteria:** the credentials check passes on the second invocation, tool detection succeeds (`sqlite3` on `PATH`).

---

## 2. `/agami-connect` — the trust spine kicks in

```text
/agami-connect
```

Watch for the new behavior:

- **Phase 2c trust block on every entry.** Open `~/agami-artifacts/default/subject_areas/<area>/tables/orders.yaml` (or wherever the fixture lands). Every table / column / relationship / metric carries the flat trust block: `confidence` (confirmed | inferred | proposed), `review_state` (unreviewed | approved | rejected | stale | not_applicable), and `signed_off_by` / `signed_off_at` / `signed_off_role` (set once approved).
  - **FK relationships** (`orders → customers`, `order_items → orders`, `order_items → products`) must show `review_state: approved`, `origin: fk`, `signed_off_by: agami_introspect_v1`, `signed_off_role: system`.
  - **Heuristic relationships** (none expected in this fixture since all FKs are declared) — would show `review_state: unreviewed`, `origin: introspect_heuristic`.
  - **Field descriptions without DBA comments** (every field in this fixture, since SQLite has no column comments) — show `review_state: unreviewed` (confidence `inferred`/`proposed`).

- **Phase 3d snapshot.** Run `ls ~/agami-artifacts/default/.snapshots/`. There should be one immutable directory named with a 12-char hash. Run `chmod` on a file inside — should refuse (write-protected).

- **Phase 3e git init.** Run `cd ~/agami-artifacts/default && git log --oneline`. There should be one commit: `introspect: default @ <hash>`. Run `cat .gitignore` — should contain `.snapshots/`.

- **Phase 5.5 summary box.** After the demo query, a summary like:
  ```
  agami-connect just ran. Here's what we found:

    ✓  4 datasets, 24 fields                                (auto-approved)
    ✓  3 FK relationships                                    (auto-approved)
    ⚠  18 field descriptions unreviewed (review)

    18 items need your attention.
  ```
  Counts will vary slightly with how the skill seeds fields. The shape and the dashboard prompt must appear.

**Pass criteria:** all of the above visible, `git status` clean (no uncommitted changes), validator exit 0 on directory mode.

---

## 3. `/agami-model review` — the sign-off queue

```text
/agami-model review
```

(The former `/agami-review` is now the **Review** tab of the model dashboard.) Watch for:

- **HTML dashboard rendered** at `<artifacts_dir>/local/model/<profile>/<ts>.html`, opened on the **Review** tab. Open it.
- The Review tab splits into **Needs your eyes** (Rule 1 metrics, low-confidence, stale) and **Looks right (confident)**, each with an "Approve all N" button.
- Each card has: title, confidence + review-state badges, the source signal (metric `calculation` / join cardinality / entity mapping), and Approve / Reject / Edit buttons.
- **Approve via the dashboard → "Generate feedback for Claude":** click Approve on a metric, generate the feedback block, paste it back. It contains `curate-ops:` with `{"op":"approve","kind":"metric",...,"at":"<UTC ISO>"}`.
  Skill resolves the curator email + role (Phase 0), applies via `sm curate --signer --role`, responds `✓ Applied: approved 1 …`, and re-renders.
  Verify the entry's YAML now shows `review_state: approved` with `signed_off_by` populated.
- **Try a reject** on an entity → its YAML shows `review_state: rejected`; it drops out of the runtime model.
- **Try done:**
  ```
  done
  ```
  Skill closes cleanly.

**Pass criteria:** dashboard renders, approve/reject/threshold/done all work, every YAML edit is followed by a passing validator run, every approval appears in `~/agami-artifacts/default/curation_log.jsonl`.

---

## 4. `/agami-query` — the receipt panel

```text
top 5 customers by spend
```

Watch the HTML report for:

- **Trust receipt collapsible** at the bottom — collapsed by default. Open it.
- Inside: tables touched (with row counts + freshness), relationships used (with confidence + review-state badge), metric definitions (if any), named filters (if any), model version pin.
- **If any unreviewed entry was used** (likely, since SQLite has no column comments and field descriptions are unreviewed), a **yellow warning banner** appears at the top of the report: *"Trust note — Used N unreviewed entr… Review now?"*
- **Model version** at the bottom of the receipt matches the model snapshot hash (the `.snapshots/<hash>/` the answer pinned).

**Pass criteria:** receipt panel renders, contents match what the SQL actually used, warning banner triggers correctly.

---

## 5. Drift smoke (optional but valuable)

```bash
sqlite3 ~/path/to/shop.db "ALTER TABLE orders RENAME COLUMN status TO order_status;"
```

Then `/agami-connect reintrospect`. Watch for:
- The Phase 5.5 summary calling out stale entries.
- The `status` field's old YAML entry flipping to `review_state: stale` (preserving the previous sign-off info).

(Drift detection is the Quality-Loop launch — June 15. If your branch is just the Semantic-Model launch, this step is informational; the stale flip may not be implemented yet.)

---

## 6. Acceptance gate (per plan §12)

All of the following must be green before merging `trust-layer` → `main`:

- `pytest tests/ --ignore=tests/integration` → all green (165+ tests).
- Steps 1–4 above pass on the SQLite fixture.
- Re-run steps 2–4 on Postgres (Pagila or the existing `tests/integration/fixtures/postgres-init.sql` shop schema with `COMMENT ON COLUMN` added) to exercise the DBA-comment auto-approve path.

The Postgres validation is what proves the column-comment signal works — SQLite cannot.
