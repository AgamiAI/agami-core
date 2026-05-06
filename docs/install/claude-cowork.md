# Install agami in Claude Cowork

Claude Cowork is a browser-based collaborative environment. The agami plugin installs through Cowork's plugin manager.

## 1. Open Claude Cowork

Browse to your Cowork workspace. Sign in if you're not already.

## 2. Add the agami marketplace

1. Open **Settings** (gear icon, top right).
2. Click **Plugins**.
3. Click **Add marketplace**.
4. Paste: `AgamiAI/LiteBi`
5. Click **Submit**.

The marketplace appears in your list with one available plugin: `agami`.

## 3. Install the plugin

In the marketplace view, click **Install** next to `agami`.

When prompted to grant permissions, approve:
- **Filesystem access** — to read/write `~/.agami/`
- **Bash access** — to run `psql` / `mysql` / `duckdb` and edit credentials

## 4. Verify

In any Cowork chat, type `@agami` — autocomplete should suggest the four agami skills (`init`, `connect`, `query-database`, `save-correction`).

Try:

```
@agami init
```

## 5. Set up credentials

Cowork runs on your machine (it's a local-first product despite the browser UI), so credentials live in the same `~/.agami/credentials` file as the CLI / VS Code / Cursor flows. See the [main README's "Setup credentials" section](../../README.md#setup-credentials).

If your Cowork environment runs in a sandbox or remote VM, `~/.agami/` lives in **that** environment's home directory — verify with:

```
@agami init verify
```

It prints `~/.agami/` resolution and confirms credentials exist.

## Cowork-specific notes

- **Per-workspace plugins**: agami is installed at the workspace level. Other Cowork members in the same workspace see and use the plugin too — but each member's `~/.agami/credentials` is in their own home directory, so they each need to set up their own connection.
- **Shared `~/.agami/`?** No — each user has their own. Never commit `~/.agami/credentials` to a shared repo.
- **Sandboxed runtimes**: if your Cowork uses a sandboxed Bash environment, native CLIs (`psql`, `mysql`) may not be installed. Use `brew install duckdb` (or the Linux equivalent) inside the sandbox — DuckDB is one binary and works as the universal client.

## Updating

In Cowork settings → Plugins → click **Update** next to `agami`.

## Uninstalling

Cowork settings → Plugins → click **Uninstall** next to `agami`. Optionally also remove the marketplace.

Your `~/.agami/` directory is not touched.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Marketplace not found | Double-check the spelling: `AgamiAI/LiteBi` (case-sensitive on GitHub) |
| Permissions prompt didn't appear | Settings → Plugins → click the gear next to `agami` → Permissions → grant explicitly |
| `psql: command not found` in the Cowork sandbox | The sandbox doesn't have a Postgres CLI; install DuckDB inside the sandbox or have your admin add `postgresql-client` |
| `~/.agami/credentials` not found, but I just made it | The Cowork sandbox's `~` may differ from your laptop's. Run `@agami init verify` to see where the skill is looking |
