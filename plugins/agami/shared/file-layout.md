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

The semantic model is a small tree of YAML files under `<artifacts_dir>/<profile>/` — `org.yaml` at the root plus a `subject_areas/<area>/` directory per subject area.

| Path | What it is |
|---|---|
| `USER_MEMORY.md` | Top-level — cross-database preferences (default filters, currency, display rules). Power users committing this share their tuning with the team. |
| `<profile>/org.yaml` | Org root: description, the `key_terminology` glossary, storage-connection + subject-area references, and cross-area relationships / entities / metrics |
| `<profile>/datasources/<connection>/storage.yaml` | Physical connection metadata (storage type + config; **no secrets** — references, not values) |
| `<profile>/subject_areas/<area>/subject_area.yaml` | Subject-area definition: name + the tables it exposes (TableRefs, with optional column-group scoping) |
| `<profile>/subject_areas/<area>/tables/<table>.yaml` | Canonical table (one per file): columns + types, primary-key grain, foreign keys, `column_groups`, choice fields, caveats, performance hints |
| `<profile>/subject_areas/<area>/metrics/<slug>.yaml`, `entities/<slug>.yaml` | One metric / entity per file |
| `<profile>/subject_areas/<area>/relationships.yaml` | In-area join edges (cardinality + trust block) |
| `<profile>/prompt_examples/<area>/examples.yaml` | Per-area NL→SQL few-shot library |
| `<profile>/ORGANIZATION.md` | Per-profile human narrative (the model-derived summary + glossary are assembled at read time, not stored here) |
| `<profile>/.snapshots/<hash>/` | Pinned model snapshots — an answer reproduces against the hash it ran on |

**Why a tree of small files rather than one big YAML:** the query path and the explorer lazy-load only what a question (or view) needs — one table's file, not a 1000-table monolith. Git diffs stay small (editing one table's metadata touches one small file), and the model never has to be parsed all at once. Relationships, metrics, and entities live at the **area** level, not inside a table file.

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

## Migration from a legacy (v1) profile

Older profiles used a per-schema layout (`index.yaml` + `<schema>/_schema.yaml` + per-table files at the profile root). On the first introspect after upgrade, the engine **auto-detects** any such legacy artifacts at the profile root, **moves them into `.legacy_backup/`** (so nothing is silently clobbered and the old model is recoverable), and writes the v2 tree (`org.yaml` + `subject_areas/…`) in their place. It's one-shot per profile and surfaces a one-liner when it happens. `USER_MEMORY.md` and credentials are untouched.

## Why this split exists

Three concrete wins:

1. **Zero credential-leak risk on commit.** Before, accidentally `git add ~/.agami/` would commit the password. Now `~/.agami/` is gitignored by default and `<artifacts_dir>/` is the only place anything goes when teams share.
2. **Team workflows just work.** `cd ~/code/myteam/data && git add agami/` commits everyone's tuned semantic model, examples, ORGANIZATION.md, and USER_MEMORY.md preferences. One command.
3. **Power users can override per-environment.** Set `AGAMI_ARTIFACTS_DIR=/path/to/staging-models` for an experimental session without touching the global config.
