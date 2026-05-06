# Vendoring provenance

The reference docs in this directory were copied from the private `AgamiAI/agami-skills` repo and adapted for single-user OSS use. This file tracks the parent commit so future re-syncs can diff cleanly.

## Source

- Repository: `AgamiAI/agami-skills` (private)
- Parent commit: `03ac4dd8ab7d18bf2cc1879d3c250c619c4bbb9d` (HEAD as of vendoring)
- Vendored on: 2026-05-06

## Files

| Vendored file | Source path in agami-skills | Adaptation notes |
|---|---|---|
| `connection-reference.md` | `plugins/agami-data-admin/shared/connection-reference.md` | Tier 0 (hosted) dropped; org/storage_config refs replaced with `~/.agami/credentials` + `AGAMI_DATABASE_URL` |
| `dialect-rules.md` | `plugins/agami-data-admin/shared/dialect-rules.md` | Verbatim |
| `sql-generation-rules.md` | `plugins/agami-data-admin/shared/sql-generation-rules.md` | Verbatim |
| `db_error_classifier.md` | `plugins/agami-data/shared/db_error_classifier.md` | Verbatim |
| `fk-validation.md` | `plugins/agami-data-admin/shared/fk-validation.md` | Verbatim |
| `schema-reference.md` | `plugins/agami-data-admin/shared/schema-reference.md` | Collapsed to single-file model — drop org/datasource hierarchy, drop `config.yaml` |

## How to re-sync

When the upstream files change in agami-skills, diff against this commit (`03ac4dd`) and re-apply the adaptation notes above to bring the local copies forward.
