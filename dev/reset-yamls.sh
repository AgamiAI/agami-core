#!/usr/bin/env bash
# Nuke the generated YAML artifacts for a profile so you can re-run
# /agami-connect end-to-end without losing credentials, reviewer config,
# hand-written docs, or the git audit trail.
#
# What gets nuked (default):
#   <artifacts_dir>/<profile>/index.yaml
#   <artifacts_dir>/<profile>/examples.yaml
#   <artifacts_dir>/<profile>/agami.config.yaml          (if present)
#   <artifacts_dir>/<profile>/<schema>/                  (every per-schema dir)
#   <artifacts_dir>/<profile>/.snapshots/                (immutable past versions)
#
# What gets preserved (default):
#   ~/.agami/credentials                      DB connection details
#   ~/.agami/.config                          reviewer email, threshold, etc.
#   <artifacts_dir>/USER_MEMORY.md            cross-DB preferences
#   <artifacts_dir>/<profile>/ORGANIZATION.md domain context the user wrote
#   <artifacts_dir>/<profile>/.git/           audit trail of past curator edits
#   <artifacts_dir>/<profile>/curation_log.jsonl
#   <artifacts_dir>/<profile>/corrections.jsonl
#   ~/.agami/review/                          rendered review dashboards
#   ~/.agami/examples-validation/             rendered validation dashboards
#   ~/.agami/model/                           rendered model-explorer dashboards
#   ~/.agami/charts/                          rendered query charts
#
# Usage:
#   dev/reset-yamls.sh                        # default profile, soft reset
#   dev/reset-yamls.sh finbud                 # specific profile, soft reset
#   dev/reset-yamls.sh finbud --hard          # also drop ORGANIZATION.md + .git/ + logs
#   dev/reset-yamls.sh finbud --dry-run       # show what would be deleted, do nothing
#
# Env:
#   AGAMI_ARTIFACTS_DIR (default: $HOME/agami-artifacts)

set -euo pipefail

PROFILE=""
HARD=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hard)    HARD=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
    *)
      if [[ -z "$PROFILE" ]]; then
        PROFILE="$1"
      else
        echo "unexpected positional arg: $1" >&2
        exit 2
      fi
      shift
      ;;
  esac
done

PROFILE="${PROFILE:-default}"
ARTIFACTS_DIR="${AGAMI_ARTIFACTS_DIR:-$HOME/agami-artifacts}"
PROFILE_DIR="$ARTIFACTS_DIR/$PROFILE"

if [[ ! -d "$PROFILE_DIR" ]]; then
  echo "no profile dir at $PROFILE_DIR — nothing to nuke"
  exit 0
fi

action() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] would: $*"
  else
    eval "$@"
  fi
}

echo "Resetting YAMLs in $PROFILE_DIR (profile: $PROFILE)"
echo "  mode: $([[ $HARD -eq 1 ]] && echo 'hard (drops ORGANIZATION.md, .git/, logs)' || echo 'soft (keeps ORGANIZATION.md, .git/, logs)')"
[[ $DRY_RUN -eq 1 ]] && echo "  dry-run: nothing will actually be deleted"
echo ""

# Snapshots are intentionally chmod 444 (immutable). Make writable first so
# rm can delete them — same as agami-connect's snapshot-cleanup contract.
if [[ -d "$PROFILE_DIR/.snapshots" ]]; then
  action "chmod -R u+w '$PROFILE_DIR/.snapshots'"
fi

# 1. Top-level YAML files (index.yaml, examples.yaml, agami.config.yaml, etc.)
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  action "rm -f '$f'"
done < <(find "$PROFILE_DIR" -maxdepth 1 -name "*.yaml" -o -name "*.yml" 2>/dev/null)

# 2. Per-schema directories (anything under the profile dir that isn't
#    .git / .snapshots / .rejected — those are preserved or handled separately).
for sub in "$PROFILE_DIR"/*/; do
  [[ -d "$sub" ]] || continue
  bn="$(basename "$sub")"
  case "$bn" in
    .git|.snapshots|.rejected) continue ;;
  esac
  action "rm -rf '$sub'"
done

# 3. Snapshots directory (immutable copies of past introspect runs).
if [[ -d "$PROFILE_DIR/.snapshots" ]]; then
  action "rm -rf '$PROFILE_DIR/.snapshots'"
fi

# 4. Hard reset extras: ORGANIZATION.md, the audit trail (.git/), and logs.
if [[ $HARD -eq 1 ]]; then
  action "rm -f '$PROFILE_DIR/ORGANIZATION.md'"
  action "rm -rf '$PROFILE_DIR/.git'"
  action "rm -f '$PROFILE_DIR/curation_log.jsonl'"
  action "rm -f '$PROFILE_DIR/corrections.jsonl'"
fi

echo ""
echo "✓ Done."
echo ""
echo "Always preserved (this script never touches these regardless of flags):"
echo "  ~/.agami/credentials              DB connection"
echo "  ~/.agami/.config                  reviewer email, threshold"
echo "  ~/agami-artifacts/USER_MEMORY.md  cross-DB preferences"
echo "  ~/.agami/review/, charts/, ...    rendered dashboards / charts"
if [[ $HARD -eq 0 ]]; then
  echo ""
  echo "Preserved in soft mode (would be dropped on --hard):"
  [[ -f "$PROFILE_DIR/ORGANIZATION.md" ]] && echo "  $PROFILE_DIR/ORGANIZATION.md"
  [[ -d "$PROFILE_DIR/.git" ]] && echo "  $PROFILE_DIR/.git                       (audit trail)"
  [[ -f "$PROFILE_DIR/curation_log.jsonl" ]] && echo "  $PROFILE_DIR/curation_log.jsonl"
  [[ -f "$PROFILE_DIR/corrections.jsonl" ]] && echo "  $PROFILE_DIR/corrections.jsonl"
fi
echo ""
echo "Now run /agami-connect in Claude Code to regenerate the YAMLs."
[[ -d "$PROFILE_DIR/.git" ]] && \
  echo "(Or 'git -C $PROFILE_DIR reset --hard HEAD' to undo this reset.)"
