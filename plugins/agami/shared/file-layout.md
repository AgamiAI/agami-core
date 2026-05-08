# File layout — what lives where

agami's state splits across two directories. The split exists so users can check the **shareable artifacts** (semantic model, examples, ORGANIZATION.md, cross-database preferences) into a git repo for their team without ever risking a credential leak.

## `~/.agami/` — secrets + per-user ephemeral state — **NEVER committed**

Everything in here is either a secret, an auth file derived from a secret, or per-user data that wouldn't make sense to share.

| Path | What it is |
|---|---|
| `credentials` | INI file, chmod 600 — the only place credentials live |
| `.pgpass`, `.mysql.cnf`, `.snowsql.cnf` | Provider-native auth files materialized from credentials |
| `.config` | JSON — telemetry consent, install_id, active_profile, **artifacts_dir** (see below), tool_paths |
| `.optins` | JSON — GitHub-star ask state |
| `query_log.jsonl` | Personal record of every query you ran |
| `charts/<ts>.html` | Per-query HTML reports |
| `exports/<ts>.csv` | Per-query CSV exports |
| `.telemetry-queue.jsonl` | Pending telemetry events (only flushed if opted in) |
| `.duckdb_init_*.sql` | Ephemeral, chmod-600 — federation init files, deleted after the query |

**Convention:** the entire `~/.agami/` directory should be in your global `.gitignore`. There is no scenario where committing it is correct.

## `<artifacts_dir>/` — sharable, can be committed

Everything in here is non-secret and team-useful. The default is `~/agami-artifacts/`, but the user picks the location during `agami-init` (see "Configuring the artifacts dir" below).

| Path | What it is |
|---|---|
| `USER_MEMORY.md` | Top-level — cross-database preferences (default filters, currency, display rules). Power users committing this share their tuning with the team. |
| `<profile>/index.yaml` | Per-profile TOC + cross-schema relationships |
| `<profile>/<schema>.yaml` | Per-schema OSI semantic model |
| `<profile>/examples.yaml` | Per-profile NL→SQL few-shot library |
| `<profile>/ORGANIZATION.md` | Per-profile domain context |

**Convention:** to share with a team, point `artifacts_dir` at a subdirectory of a git-tracked repo. Example: `~/code/myteam/data-stack/agami/` — checked in alongside dbt models, etc.

## Configuring `artifacts_dir`

The location is set once during `agami-init` via an `AskUserQuestion`. The choice persists in `~/.agami/.config.artifacts_dir`.

### Resolution order (every skill follows the same chain)

1. **`AGAMI_ARTIFACTS_DIR` env var** — highest priority, for "this session only" overrides (e.g. testing a different team's model).
2. **`~/.agami/.config.artifacts_dir`** — set by `agami-init`, persists across sessions.
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

`scripts/execute_sql.py`, `scripts/setup_pgauth.py`, `scripts/render_chart.py`, `scripts/build_duckdb_attach.py`, and `scripts/validate_semantic_model.py` all accept paths directly via flags (`--directory`, `--out`, etc.). They don't need to resolve the artifacts_dir themselves — the calling SKILL passes the resolved path.

## Permissions

- `~/.agami/` is `chmod 700`. Files inside are `chmod 600`.
- `<artifacts_dir>/` is `chmod 755` by default (sharable). Files inside are `chmod 644`. **Don't `chmod 600` the artifacts dir** — that prevents teammates from reading a checked-in copy on a shared machine, which is the whole point of the split.

## Migration

If the user's install predates this layout (v1.0 single-file `~/.agami/<profile>.yaml`, or v1.1 directory `~/.agami/<profile>/`), the `agami-connect` skill auto-migrates on first run after upgrade:

1. Read or default `artifacts_dir`.
2. `mkdir -p "$artifacts_dir/<profile>" && chmod 755 "$artifacts_dir"`.
3. Move `~/.agami/<profile>/` → `$artifacts_dir/<profile>/` (or, for v1.0, re-introspect into the new layout per the existing `_legacy.yaml.bak` flow).
4. Move `~/.agami/USER_MEMORY.md` → `$artifacts_dir/USER_MEMORY.md` if found.
5. Surface a one-liner: "Migrated semantic model and USER_MEMORY.md to `<artifacts_dir>/`. You can `git init` there and commit if you want to share with your team."

The migration is one-shot per profile — once `<artifacts_dir>/<profile>/index.yaml` exists, no further migration runs for that profile.

## Why this split exists

Three concrete wins:

1. **Zero credential-leak risk on commit.** Before, accidentally `git add ~/.agami/` would commit the password. Now `~/.agami/` is gitignored by default and `<artifacts_dir>/` is the only place anything goes when teams share.
2. **Team workflows just work.** `cd ~/code/myteam/data && git add agami/` commits everyone's tuned semantic model, examples, ORGANIZATION.md, and USER_MEMORY.md preferences. One command.
3. **Power users can override per-environment.** Set `AGAMI_ARTIFACTS_DIR=/path/to/staging-models` for an experimental session without touching the global config.
