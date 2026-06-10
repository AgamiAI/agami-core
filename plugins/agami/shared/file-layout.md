# File layout — what lives where

agami's state splits across two directories. The split exists so users can check the **shareable artifacts** (semantic model, examples, ORGANIZATION.md, cross-database preferences) into a git repo for their team without ever risking a credential leak.

## `~/.agami/` — secrets + per-user ephemeral state — **NEVER committed**

Everything in here is either a secret, an auth file derived from a secret, or per-user data that wouldn't make sense to share.

| Path | What it is |
|---|---|
| `credentials` | INI file, chmod 600 — the only place credentials live |
| `.pgpass`, `.mysql.cnf`, `.snowsql.cnf` | Provider-native auth files materialized from credentials |
| `.config` | JSON — `active_profile`, **`artifacts_dir`** (see below), `tool_paths`, `reviewer_email`, `reviewer_role` |
| `.optins` | JSON — GitHub-star ask state |
| `query_log.jsonl` | Personal record of every query you ran |
| `charts/<profile>/<ts>.html` | Per-query HTML reports |
| `exports/<profile>/<ts>.csv` | Per-query CSV exports |
| `{review,model,examples-validation}/<profile>/<ts>.html` | Per-profile dashboards |
| `.duckdb_init_*.sql` | Ephemeral, chmod-600 — federation init files, deleted after the query |

**Convention:** the entire `~/.agami/` directory should be in your global `.gitignore`. There is no scenario where committing it is correct.

## `<artifacts_dir>/` — sharable, can be committed

Everything in here is non-secret and team-useful. The default is `~/agami-artifacts/`, but the user picks the location during `agami-connect (Phase 0a)` (see "Configuring the artifacts dir" below).

| Path | What it is |
|---|---|
| `USER_MEMORY.md` | Top-level — cross-database preferences (default filters, currency, display rules). Power users committing this share their tuning with the team. |
| `<profile>/index.yaml` | Per-profile TOC of schemas + cross-schema relationships + introspect metadata |
| `<profile>/<schema>/_schema.yaml` | Per-schema slim TOC: list of tables (name + 1-line description) + within-schema relationships + multi-table metrics. NOT OSI — agami-bespoke format. |
| `<profile>/<schema>/<table>.yaml` | Per-table OSI semantic model (one dataset per file). Field definitions, indexes, choice fields, performance hints. |
| `<profile>/examples.yaml` | Per-profile NL→SQL few-shot library |
| `<profile>/ORGANIZATION.md` | Per-profile domain context |

**Why per-table files instead of one yaml per schema:** the two-pass retrieval in `agami-query` reads only `_schema.yaml` files for relevance picking (Pass 1), then lazy-loads only the picked tables' yamls (Pass 2). For a 1000-table schema, the slim `_schema.yaml` is ~100KB instead of the ~5MB the full per-schema yaml would be. Cleaner git diffs too — touching one table's metadata changes one small file, not a giant per-schema yaml.

**Convention:** to share with a team, point `artifacts_dir` at a subdirectory of a git-tracked repo. Example: `~/code/myteam/data-stack/agami/` — checked in alongside dbt models, etc.

## Configuring `artifacts_dir`

The location is set once during `agami-connect (Phase 0a)` via an `AskUserQuestion`. The choice persists in `~/.agami/.config.artifacts_dir`.

### Resolution order (every skill follows the same chain)

1. **`AGAMI_ARTIFACTS_DIR` env var** — highest priority, for "this session only" overrides (e.g. testing a different team's model).
2. **`~/.agami/.config.artifacts_dir`** — set by `agami-connect (Phase 0a)`, persists across sessions.
3. **Default**: `$HOME/agami-artifacts`.

### Resolving in bash

```bash
artifacts_dir="${AGAMI_ARTIFACTS_DIR:-}"
if [ -z "$artifacts_dir" ] && [ -f "$HOME/.agami/.config" ]; then
  artifacts_dir=$(python3 -c '
import json, os, pathlib
try:
    cfg = json.loads(pathlib.Path("~/.agami/.config").expanduser().read_text())
    print(cfg.get("artifacts_dir") or "")
except Exception:
    print("")
')
fi
artifacts_dir="${artifacts_dir:-$HOME/agami-artifacts}"
```

(The `python3` block is only needed if your `.config` exists; for fresh installs the default kicks in.)

### Resolving in Python helpers

`scripts/execute_sql.py`, `scripts/setup_pgauth.py`, `scripts/render_chart.py`, and `scripts/build_duckdb_attach.py` accept paths directly via flags (`--directory`, `--out`, etc.). They don't need to resolve the artifacts_dir themselves — the calling SKILL passes the resolved path. The semantic-model tools (`python3 -m semantic_model.cli …`) take the profile root (`<artifacts_dir>/<profile>/`) as a positional argument.

## Permissions

- `~/.agami/` is `chmod 700`. Files inside are `chmod 600`.
- `<artifacts_dir>/` is `chmod 755` by default (sharable). Files inside are `chmod 644`. **Don't `chmod 600` the artifacts dir** — that prevents teammates from reading a checked-in copy on a shared machine, which is the whole point of the split.

## Migration

Three older layouts exist; `agami-connect` auto-detects and migrates each on first run after upgrade. See agami-connect's Phase 1.1 for the detection/migration code.

| From | To | Behavior |
|---|---|---|
| v1.0 single-file `~/.agami/<profile>.yaml` | v1.3 per-table | Backup the legacy file, re-introspect from DB into the new directory tree |
| v1.1 `~/.agami/<profile>/<schema>.yaml` (under secrets dir) | v1.3 per-table | Move dir to `<artifacts_dir>`, then split each schema yaml into per-table files |
| v1.2 `<artifacts_dir>/<profile>/<schema>.yaml` (single file per schema, no `_schema.yaml`) | v1.3 per-table | Split each `<schema>.yaml` into a `<schema>/` subdirectory with `_schema.yaml` + per-table files. Pure file-rewrite, no DB queries. |

USER_MEMORY.md is migrated alongside on the v1.0/v1.1 paths.

The migration is one-shot per profile — once a `<schema>/_schema.yaml` exists, no further migration runs for that schema. Mixed v1.2/v1.3 profiles are supported by the validator (some schemas migrated, some not yet) so a partial-migration crash doesn't leave the user stranded.

## Why this split exists

Three concrete wins:

1. **Zero credential-leak risk on commit.** Before, accidentally `git add ~/.agami/` would commit the password. Now `~/.agami/` is gitignored by default and `<artifacts_dir>/` is the only place anything goes when teams share.
2. **Team workflows just work.** `cd ~/code/myteam/data && git add agami/` commits everyone's tuned semantic model, examples, ORGANIZATION.md, and USER_MEMORY.md preferences. One command.
3. **Power users can override per-environment.** Set `AGAMI_ARTIFACTS_DIR=/path/to/staging-models` for an experimental session without touching the global config.
