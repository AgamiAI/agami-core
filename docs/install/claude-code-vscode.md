# Install agami in Claude Code for VS Code

The agami plugin works the same in VS Code as in the CLI. The only difference is how you open the Claude pane.

## 1. Install the Claude Code extension

In VS Code:
1. Open the Extensions sidebar (Cmd+Shift+X / Ctrl+Shift+X).
2. Search for **Claude Code** (publisher: Anthropic).
3. Click **Install**.
4. Reload the window when prompted.

Or from a terminal:

```bash
code --install-extension anthropic.claude-code
```

## 2. Sign in

The first time you open the Claude pane (Cmd+Shift+P → "Claude Code: Open"), it prompts for sign-in. Follow the browser flow.

## 3. Add the agami marketplace and install

In the Claude pane's chat input, type `/plugin`. This surfaces a **Manage Plugins** option — click it.

The plugins dialog opens. From here:

1. **Add the marketplace.** In the marketplace input, paste `AgamiAI/agami-core` and click **Add**.
2. **Install the plugin.** Switch to the **Plugins** tab in the dialog. Find `agami-core` in the list and click **Install**.

The dialog confirms the install; agami's skills become available immediately.

## 4. Verify

Try a skill in the Claude pane chat input:

```
/agami-connect
```

If you haven't set up credentials yet, the skill walks you through the DB-type picker and writes `<artifacts_dir>/local/credentials.example` for you to fill in. (No separate `/agami-init` — its setup flow was folded into `/agami-connect` Phase 0a.)

## 5. Set up credentials

Same as the CLI: edit `<artifacts_dir>/local/credentials.example`, save as `<artifacts_dir>/local/credentials`, `chmod 600`. See the [main README's "Setup credentials" section](../../README.md#setup-credentials).

The terminal-side `chmod` works identically — the skill's `init` flow walks you through it via Bash inside the Claude pane.

## VS Code-specific notes

- **Working directory**: Claude Code uses the open VS Code workspace root as the working directory. `<artifacts_dir>/local/` is in your home, not the workspace, so it's safe across projects.
- **Terminal access**: the Claude pane has its own Bash session; running `@agami` doesn't open a separate terminal.
- **Inline chart artifacts**: when you ask for a chart, the rendered HTML path appears in the response. Open it via VS Code's "Open File…" or click the path if your terminal supports clickable file paths.

## Updating

```
/plugin update agami-core@agami
```

## Uninstalling

```
/plugin uninstall agami-core
/plugin marketplace remove agami
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Claude Code" extension not in marketplace | Install from the official VS Code marketplace; the publisher must be Anthropic |
| `/plugin` not recognized in the chat input | The extension is on an old version; update via the Extensions sidebar |
| Sign-in loop | Sign out from VS Code's account menu and try again |
| Skill autocomplete missing after install | Reload the VS Code window (Cmd+Shift+P → "Developer: Reload Window") |
