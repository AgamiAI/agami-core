# migrations/core (skeleton)

Empty migration home for the agami-core Postgres schema. **ACE-002** fills it with the
`org → datasource → subject-area` model (subject-areas primary; tables/columns/metrics/
entities/relationships; prompt examples; entity `value_pattern`s; org/user memory;
`model_version`/snapshots) plus runtime `query_executions` + feedback.

Intentionally empty per **OCR-028** — do **not** design tables here. The data-agent
`migrations/` + `scripts/ops/migrations/run_migrations.py` runner is the reuse *template*
(ACE-002), not the schema.
