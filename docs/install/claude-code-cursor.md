# Install agami in Claude Code for Cursor

Cursor is a VS Code-derived editor; the Claude Code extension installs and behaves the same way.

## 1. Install the Claude Code extension

In Cursor:
1. Open the Extensions sidebar (Cmd+Shift+X / Ctrl+Shift+X).
2. Search for **Claude Code** (publisher: Anthropic).
3. Click **Install**.
4. Reload the window when prompted.

Or from a terminal:

```bash
cursor --install-extension anthropic.claude-code
```

## 2. Sign in

Open the Claude pane (Cmd+Shift+P → "Claude Code: Open"). Sign in via the browser flow when prompted.

## 3. Add the agami marketplace and install

In the Claude pane's chat input:

```
/plugin marketplace add AgamiAI/LiteBi
/plugin install agami@litebi
```

## 4. Verify

```
/plugin list
/agami-init
```

## 5. Set up credentials

Same as the CLI / VS Code: edit `~/.agami/credentials.example`, save as `~/.agami/credentials`, `chmod 600`. See the [main README's "Setup credentials" section](../../README.md#setup-credentials).

## Cursor-specific notes

- **Cursor's own AI features** (Composer, Tab autocomplete) are independent of agami. agami runs only inside the Claude Code pane.
- **Working directory**: Cursor uses the open workspace root, same as VS Code.
- **Workspace settings**: if you've configured Cursor to disable certain extensions per-workspace, make sure Claude Code is enabled.

## Updating

```
/plugin update agami@litebi
```

## Uninstalling

```
/plugin uninstall agami
/plugin marketplace remove litebi
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Claude Code extension missing from Cursor marketplace | Try `cursor --install-extension anthropic.claude-code` directly, or download the `.vsix` from the Anthropic releases page |
| Cursor's privacy mode blocks the marketplace | Cursor settings → Privacy → ensure "Allow extensions to fetch from external sources" is on |
| Skill autocomplete missing after install | Reload window (Cmd+Shift+P → "Developer: Reload Window") |
