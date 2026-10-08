# Dcode for VS Code

Dcode is a coding agent for local and air-gapped models. This extension puts it one command
away inside VS Code. It runs nothing by itself: it starts the `dcode` program you already
installed and talks to Dcode's own localhost service. No network, no telemetry, no dependencies.

## Commands

| Command | What it does |
|---|---|
| **Dcode: Start in Terminal** | Runs `dcode` in the integrated terminal, in the workspace folder |
| **Dcode: Continue the Last Chat of This Folder** | Runs `dcode --continue` |
| **Dcode: Attach a Terminal to a Running Session** | Runs `dcode attach` |
| **Dcode: Open the Web UI** | Opens the running session's web UI in a VS Code tab |
| **Dcode: Send Selection** (`Ctrl+Alt+D` / `Cmd+Alt+D`) | Asks what to do, then sends the selected lines to the session working in this folder |
| **Dcode: Ask About This File** | The same for the whole file (sent as `@path`, so Dcode reads it itself) |

The last two are also in the editor's right-click menu.

## Settings

- `dcode.path`: the `dcode` program, if it is not on your `PATH`.
- `dcode.openUiInside`: open the web UI in a VS Code tab (default) or in your browser.

## Install

The extension is not on the Marketplace (Dcode targets machines without internet). Build the
package and install it from the file:

```bash
python3 editors/vscode/build_vsix.py          # writes editors/vscode/dcode-<version>.vsix
code --install-extension editors/vscode/dcode-*.vsix
```

Over Remote-SSH, install it on the remote side: it must run where `dcode` runs.

## Other editors

Zed and other editors that speak the Agent Client Protocol don't need an extension: point them
at `dcode acp`. See the Dcode guide, "Editors".
