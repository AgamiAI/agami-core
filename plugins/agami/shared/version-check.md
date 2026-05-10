# Version-check probe

A best-effort, non-blocking check that surfaces "agami X.Y.Z is available" when a newer plugin version exists on `main`. Used by `agami-connect/SKILL.md` Phase 0 step 7 (and reusable from any skill that wants to nudge users to update).

## Why it exists

Claude Code plugins don't auto-update — users have to run `/plugin marketplace update litebi && /reload-plugins`. They mostly forget. The probe surfaces the nudge once per `agami-connect` invocation so users don't sit on stale code without knowing.

## Behavior contract

- **Best-effort.** Network failure, missing curl, missing local file, parse error → silent skip. Never blocks the rest of the skill.
- **Non-spammy.** Only surface when remote version > local version. If equal, say nothing.
- **Single line.** No multi-line announcement. The user is here to query their data, not read changelogs.

## How to run it

From any skill, after `$AGAMI_PLUGIN_ROOT` is resolved:

```bash
# Read the local plugin version from the marketplace.json that ships with the
# installed plugin. The path is relative to the plugin root.
local_v=$(
  grep -m1 '"version"' "$AGAMI_PLUGIN_ROOT/../../.claude-plugin/marketplace.json" 2>/dev/null \
    | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
)

# Fetch the remote version from main on GitHub (3-second timeout).
remote_v=$(
  curl -fsS --max-time 3 \
    https://raw.githubusercontent.com/AgamiAI/LiteBi/main/.claude-plugin/marketplace.json \
    2>/dev/null \
    | grep -m1 '"version"' \
    | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
)

# Surface only when both parsed AND remote is strictly newer.
if [ -n "$local_v" ] && [ -n "$remote_v" ] && [ "$local_v" != "$remote_v" ]; then
  # Naive lexicographic check is wrong for 1.10 vs 1.9. Use sort -V (version sort).
  newer=$(printf '%s\n%s\n' "$local_v" "$remote_v" | sort -V | tail -n1)
  if [ "$newer" = "$remote_v" ]; then
    echo "agami $remote_v is available (you have $local_v)."
    echo "Update: /plugin marketplace update litebi && /reload-plugins"
  fi
fi
```

## When to skip

- The user pinned a branch via `#trust-layer` etc. — they explicitly opted into not-main, don't nag them about main being newer. Detect by checking whether `$AGAMI_PLUGIN_ROOT` is under a marketplace dir that includes a branch suffix (rare; safe to skip the optimization for v1).
- `AGAMI_NO_UPDATE_CHECK=1` is set in the environment — power-user opt-out.
- The local marketplace.json file isn't found (e.g., when running from a `--plugin-dir` local-development install) — there's no canonical version to compare against.

## Failure modes

| Symptom | Why | Behavior |
|---|---|---|
| `curl` not installed | Stripped-down container | Silent skip |
| Network blocked / no DNS | Air-gapped env | Silent skip (3s timeout) |
| GitHub returns 404 | Repo renamed / branch deleted | Silent skip |
| JSON parse fails | marketplace.json shape changed | Silent skip |
| Versions equal | User on latest | Silent skip |

The probe is informational, not load-bearing. If it fires, great; if it doesn't, the skill works the same.
