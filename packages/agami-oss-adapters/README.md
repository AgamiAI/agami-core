# agami-oss-adapters (placeholder)

Skeleton home for the **OSS default adapters** behind agami-core's ports — the in-place
file/jsonl `ActivitySink`, the single-tenant `OrgResolver`, the warn-only `GovernancePolicy`,
the token-presence `AuthProvider`.

The port **Protocols** + the default adapters land in **OCR-029** (the seams); the Postgres
`ActivitySink` adapter lands in **ACE-002**. This directory is an intentionally empty skeleton
per **OCR-028** — no implementations here yet.
