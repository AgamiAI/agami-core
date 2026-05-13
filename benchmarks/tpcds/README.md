# Tier 3b — TPC-DS workload benchmark

End-to-end benchmark of the agami-connect → agami-query-database chain
against a realistic decision-support workload. Answers the question:
**does the trust layer (OSI model + few-shot examples) measurably improve
SQL correctness over schema-only prompting?**

Opt-in, not in CI. Slow (minutes). Costs API tokens.

## When to run

- Before a release that touches semantic-model generation, validator,
  example-generation, or SQL-generation prompts in any SKILL.md
- When tuning prompt or model selection in `tools/run_skill_evals.py`
- To produce the with-vs-without-semantic-model delta number for a
  release note or design doc

This benchmark is **not** a leaderboard. TPC-DS NL paraphrasing dominates
absolute scores; we treat the *delta* between with-model and without-model
runs as the meaningful signal.

## Setup

1. Generate a TPC-DS dump at small scale (`SF=0.01` ≈ 10 MB) using
   [`gregrahn/tpcds-kit`](https://github.com/gregrahn/tpcds-kit) and
   load it into a local Postgres / DuckDB.
2. Configure an agami profile pointing at the loaded DB (see
   `plugins/agami/skills/agami-connect/SKILL.md`).
3. Run `agami-connect` once to produce the OSI model.

The existing `tests/integration/fixtures/sample_osi_tpcds.yaml` is a
hand-written reference OSI model for the TPC-DS schema — useful as a
golden source if you want to skip the agami-connect step while iterating
on the benchmark itself.

## Run

```bash
pip install psycopg2-binary anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export AGAMI_PROFILE=tpcds
export AGAMI_DB_URL=postgresql://localhost/tpcds

LITEBI_BENCHMARK=tpcds python3 benchmarks/tpcds/run.py
LITEBI_BENCHMARK=tpcds python3 benchmarks/tpcds/run.py --baseline   # with-vs-without
```

## What's asserted

Shape-level only (per-question):
- Result row count within ±10% of expected
- Top-N ordering matches when the question asks for "top N by X"
- Aggregates within a tolerance (currency rounded to cents, ratios within 1%)

**Not asserted** (intentionally):
- Byte-exact SQL match — Claude rewrites SQL across runs
- Byte-exact row match — TPC-DS tie-breaking and float aggregation flap
- Absolute pass rate — paraphrasing dominates that number

## Files

- `questions.json` — NL questions paraphrased from TPC-DS reporting-tier
  queries, with expected shape per question (this directory is a stub —
  contributors extend the question set when adding skill changes that
  affect SQL generation on realistic schemas)
- `run.py` — harness (mirrors `tools/run_skill_evals.py` but adds
  shape-level assertions and the with-vs-without comparison)
