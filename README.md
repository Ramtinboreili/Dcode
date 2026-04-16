# code-one

Terminal-based, agentic coding assistant CLI with OpenAI-compatible API support and built-in file / shell tools.

## Features

- Interactive commands:
  - `/exit` - quit the app
  - `/reset` - clear the current conversation and start fresh
  - `/model` - show current model, pick a new model, and set reasoning effort
  - `/setup` - re-run the provider setup wizard
  - `/permissions` - toggle agent tool permissions for the session and config
  - `/experimental` - toggle experimental features
  - `/skills` - enable or disable markdown skills from `~/.code-one/skills/`
  - `/review` - review current git changes with the model
  - `/rename` - rename the current chat thread
  - `/new` - save the current thread and start a new one
  - `/resume` - load and continue a saved thread
  - `/help` - show command help
- Tool-calling support:
  - Filesystem tools: `read_file`, `write_file`, `list_files`, `run_command`, `create_directory`, `delete_file`, `search_in_files`
  - Git tools: `git_status`, `git_diff`, `git_log`, `git_commit`
- Config file at `~/.code-one/config.json`
- Saved threads at `~/.code-one/threads/`
- Skills directory at `~/.code-one/skills/`
- Per-task summary with wall-clock time and token usage

## Project layout

```
code_one/
  __init__.py
  init.py
  main.py
  agent.py
  tools.py
  config.py
  ui.py
pyproject.toml
```

## Install (single command)

The distribution is a standalone binary (`code-one`) built with PyInstaller `--onefile`.

```bash
curl -L https://my-storage.com/install.sh | bash
```

The installer:

- Detects Linux/macOS and architecture (`x86_64` / `arm64`)
- Downloads the correct binary from object storage
- Installs it to `/usr/local/bin/code-one`
- Prints: `✓ Done! Run: code-one`

You can point the installer to a different base URL with:

```bash
export CODE_ONE_STORAGE_URL=https://your-storage.example.com/
curl -L https://my-storage.com/install.sh | bash
```

## Build release binary

Use the `dist` target in `Makefile`:

```bash
make dist
```

This runs:

- `python3 -m pip install -i https://mirror-pypi.runflare.com/ .`
- `python3 -m pip install -i https://mirror-pypi.runflare.com/ pyinstaller`
- PyInstaller one-file build from `code_one/main.py`
- Output to `dist/code-one`

Upload `dist/code-one` to your storage paths:

- `linux-x86_64/code-one`
- `linux-arm64/code-one`
- `macos-x86_64/code-one`
- `macos-arm64/code-one`

## Setup flow

After running/installing:

```bash
code-one --setup
```

This asks for:

- Base URL
- API Key
- Model

If the provider supports model listing, `code-one` fetches the list automatically. If not, you can enter the model manually.

Values are saved to `~/.code-one/config.json`.

## CLI usage

```bash
code-one                    # start interactive session
code-one --setup            # rerun setup
code-one --base-url X --api-key Y --model Z   # override config
```

## Slash commands

All slash commands are available from the interactive prompt.

### `/exit`

Quit the program immediately.

### `/reset`

Clear the current conversation history and start a fresh in-memory session.

### `/model`

Show the current model and reasoning effort, then let you:

- Pick a new model from the provider model list when available
- Fall back to manual model entry if model listing is unavailable
- Set reasoning effort to `none`, `low`, `medium`, or `high`

The selected model and reasoning effort are saved to `~/.code-one/config.json`.

### `/setup`

Re-run the setup wizard for:

- Base URL
- API key
- Model

This is the fastest way to switch providers or rotate credentials.

### `/permissions`

Open an interactive checklist and toggle what the agent is allowed to do:

- `read files`
- `write files`
- `run shell commands`
- `delete files`
- `create directories`
- `search files`

Disabled tools are removed from the agent tool list. The selection is saved to `~/.code-one/config.json`.

### `/experimental`

Toggle experimental features:

- `multi_file_edit` - allow the model to attempt multi-file edits in one response
- `auto_commit` - offer git commit after file writes
- `streaming_tools` - show tool output in the terminal as tools run

These flags are saved to `~/.code-one/config.json`.

### `/skills`

Load extra markdown instructions from `~/.code-one/skills/`.

- Each `.md` file in that directory is shown in an interactive checklist
- Enabled skills are appended to the system prompt for the current session
- `code-one` auto-creates a default `code_review.md` skill file on first run

This is useful for reusable local workflows, team rules, or review instructions.

### `/review`

Run a built-in review on the current git changes.

`code-one` does this by:

- running `git diff HEAD`
- sending that diff to the model
- asking: `Review these changes. Find bugs, issues, and improvements.`

The review response is streamed back into the terminal.

### `/rename`

Rename the current chat thread. The thread name is used when saving to `~/.code-one/threads/`.

### `/new`

Save the current thread to `~/.code-one/threads/` and start a fresh conversation. If the thread was not renamed, `code-one` uses a timestamp-based name.

### `/resume`

Show saved threads from `~/.code-one/threads/`, sorted by date, then resume the selected conversation.

### `/help`

Show the in-app help panel with the available slash commands.

## Threads

Threads are stored as JSON files in `~/.code-one/threads/`.

Each thread file contains:

- `name`
- `created_at`
- `messages`

This makes it easy to pause a conversation and continue it later.

## Skills

Skills are markdown files stored in `~/.code-one/skills/`.

When enabled with `/skills`, the contents of those files are appended to the agent system prompt as extra instructions. This is a simple local extension mechanism for coding standards, custom review rules, or project-specific workflows.

## Git integration

When `code-one` starts inside a git repository, it adds git context to the welcome screen and system prompt:

- current branch
- last commit hash and message
- number of modified files

The built-in git tools are:

- `git_status` - returns `git status`
- `git_diff` - returns `git diff HEAD`
- `git_log` - returns `git log --oneline -10`
- `git_commit` - stages all changes and commits with a provided message

`git_commit` always requires confirmation before the commit is created.

## Dependencies

Declared in `pyproject.toml`:

- `openai>=1.0.0`
- `rich>=13.0.0`
- `click>=8.0.0`
- `prompt_toolkit>=3.0.0`
- `httpx>=0.24.0`
