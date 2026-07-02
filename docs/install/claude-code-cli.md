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
/plugin marketplace add AgamiAI/agami-core
```

Expected output:

```
Added marketplace: agami (AgamiAI)
1 plugin available: agami-core
```

## 4. Install the agami plugin

```
/plugin install agami-core@agami
```

Expected output:

```
Installed agami-core v0.3.3 (from agami)
5 skills available: agami-connect, agami-query, agami-model, agami-save-correction, agami-reconcile
```

## 5. Verify

```
/plugin list
```

You should see `agami-core v0.3.3` in the active list.

Try invoking it:

```
/agami-connect
```

On first run, the skill detects there are no credentials and walks you through the DB-type picker, then writes a `<artifacts_dir>/local/credentials.example` template you fill in. (No separate `/agami-init` — the setup flow lives in `/agami-connect` Phase 0a.)

## 6. Set up credentials

`agami-connect` Phase 0a writes a template at `<artifacts_dir>/local/credentials.example`. Edit it with your DB connection, save as `<artifacts_dir>/local/credentials`, run `chmod 600 <artifacts_dir>/local/credentials`. See the [main README's "Setup credentials" section](../../README.md#setup-credentials) for format.

## Updating

```
/plugin update agami-core@agami
```

## Uninstalling

```
/plugin uninstall agami-core
/plugin marketplace remove agami
```

Your `<artifacts_dir>/local/` directory and its contents are not touched by uninstall — delete it manually if you want to clean up.

## Troubleshooting


| Symptom                                            | Fix                                                 |
| -------------------------------------------------- | --------------------------------------------------- |
| `/plugin: command not found`                       | Update Claude Code: `claude update`                 |
| `Failed to fetch marketplace`                      | Check your network; the marketplace lives on GitHub |
| `Plugin not authorized`                            | Run `claude login` again                            |
| Skill doesn't appear in autocomplete after install | Restart Claude Code (`/exit` then `claude` again)   |


