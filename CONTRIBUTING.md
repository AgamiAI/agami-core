# Contributing to LiteBi

Issues and PRs welcome at [github.com/AgamiAI/LiteBi](https://github.com/AgamiAI/LiteBi).

## Running tests

Privacy-invariant + unit tests (no DB required):

```bash
python3 -m pytest tests/ -q
```

The privacy test (`tests/test_privacy_no_network.py`) is a contract: no shipped script may make a network call — adding a network-egress primitive fails the build.

End-to-end integration tests (Postgres + MySQL fixtures):

```bash
cd tests/integration
docker compose up -d
./test_postgres_e2e_cli.sh        # native CLI (psql)
./test_mysql_e2e_cli.sh
./test_postgres_e2e_duckdb.sh     # DuckDB (skipped if duckdb not on PATH)
docker compose down -v
```

## Version-bump discipline (read this before any release-shaped commit)

Claude Code's plugin marketplace caches each plugin **by version number**. The cache key for any user who installed `agami@1.1.0` is pinned to that version — Claude Code does not re-fetch source files until the version changes, even if the upstream `main` branch has moved on.

This has a real consequence: if we rename a skill, change file layouts, or remove a Bash invocation pattern from `.claude/settings.json` and **don't bump the version**, every user who installed an earlier version stays on the old code forever. They'll see stale slash commands, hit broken file paths, and have no obvious way to invalidate their cache short of deleting `~/.claude/plugins/cache/litebi/<old-version>/` by hand.

So: **bump the version on any commit that changes user-visible behavior.**

### When to bump what

The version lives in three files. They should always match:

- `.claude-plugin/marketplace.json` — `metadata.version` and `plugins[0].version`
- `plugins/agami/.claude-plugin/plugin.json` — `version`

Use semver:

| Change | Bump |
|---|---|
| Bug fix, doc-only change, internal refactor with no user-visible behavior shift | **patch** (1.1.0 → 1.1.1) |
| New feature, new skill, new database type, new optional flag — anything additive | **minor** (1.1.0 → 1.2.0) |
| Skill rename, file-layout change, removed flag, default behavior change, anything that breaks an existing install | **minor pre-launch, MAJOR after** (1.1.0 → 1.2.0 pre-launch; 1.1.0 → 2.0.0 post-launch) |

Pre-launch (before public availability), even breaking changes are minor bumps — the implicit promise is that you've told all your alpha users they need to reinstall. Post-launch, semver-strict: a major bump is the contract that says "this will break your config".

**The most common mistake** is renaming a skill (e.g., `init` → `agami-init`) or changing a file path (e.g., `~/.agami/<profile>/index.yaml` → `<artifacts_dir>/<profile>/index.yaml`) without bumping. Users installed at the old version see neither rename. Always bump on those changes.

### What "user-visible behavior" means

- Slash command name changed → bump
- File the skill writes moved → bump
- Allowlisted Bash command removed from `.claude/settings.json` → bump
- New required flag → bump
- New SKILL.md `when_to_use` trigger phrase added → patch (additive)
- New optional flag → patch (additive)
- Typo fix in a SKILL.md description → patch
- Test-only or scripts/README-only change → no bump needed

When in doubt, bump. Patch bumps are cheap; stale caches in the wild are not.

### Migrations are NOT a substitute for version bumps

We do auto-migrate on layout changes (e.g., v1.0 single-file → v1.1 directory → v1.2 split). That doesn't replace the version bump — it complements it. Without the bump, the new code that *does* the migration never reaches the user's machine. Bump first; migration code runs on next invocation.

## Files to touch on a release

When opening a PR that warrants a version bump, the checklist is:

1. `.claude-plugin/marketplace.json` — bump both `metadata.version` and `plugins[0].version` to the same string.
2. `plugins/agami/.claude-plugin/plugin.json` — bump `version`.
3. Add a note to the PR description summarizing the user-visible change and which version it lands in.

We don't keep a `CHANGELOG.md` yet. The git log + version bumps are the changelog.

## A community Discord will land soon

Once it's live, the link will appear here and in [`agami-connect/SKILL.md`](plugins/agami/skills/agami-connect/SKILL.md).
