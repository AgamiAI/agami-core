# Contributing to agami-core

Issues and PRs welcome at [github.com/AgamiAI/agami-core](https://github.com/AgamiAI/agami-core).

## Contributor License Agreement (CLA)

This project is **fair-code** (source-available) and is offered under a dual-license model: the [Agami Functional Use License](LICENSE) for the community, and a separate commercial license. For that model to hold, every external contribution has to come in with the rights that let us ship it under **both** licenses.

So before your first contribution can be merged, you sign a short **Contributor License Agreement** ([CLA.md](CLA.md)). In one sentence: you give Agami AI permission to license your contribution **on any terms — including the fair-code FUL and a commercial license** — and your contribution comes as is, without warranty or liability on your part. The full text is ~75 words; please read it.

**How to sign — no separate account, no form.** The first time you open a PR, our CLA bot comments on it with a link to the agreement and asks you to sign. You sign by replying with a single comment:

```
I have read the CLA Document and I hereby sign the CLA
```

The bot records your signature (stored in this repo) and flips the **CLA** status check to green; it stays green for all your future PRs. Until it's signed, the check blocks merge — that's the only thing it gates.

Maintainers and first-party (Agami AI) authors are allowlisted, so internal commits aren't gated; the CLA is for contributions from outside the organization.

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

Claude Code's plugin marketplace caches each plugin **by version number**. The cache key for any user who installed `agami-core@1.1.0` is pinned to that version — Claude Code does not re-fetch source files until the version changes, even if the upstream `main` branch has moved on.

This has a real consequence: if we rename a skill, change file layouts, or remove a Bash invocation pattern from `.claude/settings.json` and **don't bump the version**, every user who installed an earlier version stays on the old code forever. They'll see stale slash commands, hit broken file paths, and have no obvious way to invalidate their cache short of deleting `~/.claude/plugins/cache/agami/agami-core/<old-version>/` by hand.

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

**The most common mistake** is renaming a skill (e.g., `init` → `agami-init`) or changing a file path (e.g., `<artifacts_dir>/local/<profile>/` → `<artifacts_dir>/<profile>/`) without bumping. Users installed at the old version see neither rename. Always bump on those changes.

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
