# Install agami in Claude Code CLI

This is the simplest path. Most agami users start here.

## 1. Install Claude Code

If you don't have Claude Code yet:

```bash
# macOS / Linux
curl -fsSL https://claude.com/install.sh | sh

# Windows (PowerShell, in an elevated session)
irm https://claude.com/install.ps1 | iex
```

Then sign in:

```bash
claude login
```

## 2. Open Claude Code

In any terminal:

```bash
claude
```

You should see the Claude Code prompt.

## 3. Add the agami marketplace

```
/plugin marketplace add AgamiAI/LiteBi
```

Expected output:

```
Added marketplace: litebi (AgamiAI)
1 plugin available: agami
```

## 4. Install the agami plugin

```
/plugin install agami@litebi
```

Expected output:

```
Installed agami v0.1.0 (from litebi)
5 skills available: agami-connect, agami-query, agami-model, agami-save-correction, agami-reconcile
```

## 5. Verify

```
/plugin list
```

You should see `agami v0.1.0` in the active list.

Try invoking it:

```
/agami-connect
```

On first run, the skill detects there are no credentials and walks you through the DB-type picker, then writes a `~/.agami/credentials.example` template you fill in. (No separate `/agami-init` — the setup flow lives in `/agami-connect` Phase 0a.)

## 6. Set up credentials

`agami-connect` Phase 0a writes a template at `~/.agami/credentials.example`. Edit it with your DB connection, save as `~/.agami/credentials`, run `chmod 600 ~/.agami/credentials`. See the [main README's "Setup credentials" section](../../README.md#setup-credentials) for format.

## Updating

```
/plugin update agami@litebi
```

## Uninstalling

```
/plugin uninstall agami
/plugin marketplace remove litebi
```

Your `~/.agami/` directory and its contents are not touched by uninstall — delete it manually if you want to clean up.

## Troubleshooting


| Symptom                                            | Fix                                                 |
| -------------------------------------------------- | --------------------------------------------------- |
| `/plugin: command not found`                       | Update Claude Code: `claude update`                 |
| `Failed to fetch marketplace`                      | Check your network; the marketplace lives on GitHub |
| `Plugin not authorized`                            | Run `claude login` again                            |
| Skill doesn't appear in autocomplete after install | Restart Claude Code (`/exit` then `claude` again)   |


