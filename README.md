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
  __init__.py     # package version (single source of truth)
  main.py         # CLI entry point + setup wizard
  agent.py        # agent loop, tool-call handling, slash commands
  tools.py        # sandboxed filesystem / shell / git tools
  config.py       # config, threads, skills, env overrides
  ui.py           # rich/prompt_toolkit terminal UI
packaging/
  build-binary.sh   # PyInstaller one-file build
  build-deb.sh      # wrap the binary into a .deb
  install-airgap.sh # offline installer (.deb or raw binary)
tests/            # pytest suite
pyproject.toml
```

## Air-gapped install + local LLM

`code-one` talks to **any OpenAI-compatible endpoint**, so it runs fully offline
against a local model server. Python and all dependencies are bundled inside the
binary — no internet, no `pip`, no system Python needed on the target box.

### 1. Build on a connected machine (matching OS/arch as the server)

```bash
make deb        # produces dist/code-one_<version>_<arch>.deb  (+ dist/code-one)
```

`make deb` runs PyInstaller then `dpkg-deb`. PyInstaller does **not** cross-compile,
so build on the same OS/CPU as the air-gapped target (e.g. Ubuntu x86_64 → Ubuntu x86_64).

### 2. Move the artifact + installer to the air-gapped server

Copy `dist/code-one_*.deb` (or the raw `dist/code-one`) and
`packaging/install-airgap.sh` onto the server (USB, internal mirror, etc.).

```bash
sudo ./install-airgap.sh code-one_0.1.0_amd64.deb   # Debian/Ubuntu
# or, on any Linux/macOS with no dpkg:
sudo ./install-airgap.sh ./code-one                 # copies to /usr/local/bin
```

### 3. Point it at your local LLM

Any local server that speaks the OpenAI API works — Ollama, vLLM, llama.cpp
(`llama-server`), LM Studio, Text-Generation-Inference. Configure via environment
variables (they override the saved config and need no interactive setup):

```bash
export CODE_ONE_BASE_URL=http://127.0.0.1:11434/v1   # Ollama default
export CODE_ONE_API_KEY=local                        # any non-empty string
export CODE_ONE_MODEL=qwen2.5-coder:7b
code-one
```

| Env var | Purpose |
| --- | --- |
| `CODE_ONE_BASE_URL` | OpenAI-compatible endpoint of your local LLM |
| `CODE_ONE_API_KEY` | Key (use any placeholder for keyless local servers) |
| `CODE_ONE_MODEL` | Model name to request |
| `CODE_ONE_REASONING_EFFORT` | `none` / `low` / `medium` / `high` |
| `CODE_ONE_WORKSPACE` | Root that file tools are confined to (default: cwd) |
| `CODE_ONE_ALLOW_OUTSIDE_WORKSPACE` | Set `1` to disable the path sandbox |

> `http://` endpoints are allowed because a local/air-gapped network is trusted.
> Nothing ever leaves that network.

## Security model

`code-one` runs untrusted model output, so it is defensive by default:

- **Path sandbox** — `read_file` / `write_file` / `delete_file` / `list_files` /
  `search_in_files` are confined to the workspace root (the launch directory, or
  `CODE_ONE_WORKSPACE`). Absolute paths and `../` escapes are blocked unless you set
  `CODE_ONE_ALLOW_OUTSIDE_WORKSPACE=1`.
- **Command confirmation** — every `run_command`, `delete_file`, and `git_commit`
  prompts for `y/N` before executing. Enable the `auto_approve_commands`
  experimental flag only in fully trusted setups.
- **Secret hygiene** — `~/.code-one/config.json` is written `0600` and the config
  directory `0700`. Prefer `CODE_ONE_API_KEY` over storing keys on disk.
- **Output caps** — file reads (512 KB), command/search/listing output are bounded so
  a single tool call cannot exhaust memory or the context window; noisy directories
  (`.git`, `node_modules`, `.venv`, …) are skipped.

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
- `auto_approve_commands` - run shell commands without the per-command `y/N` prompt (trusted environments only)

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
