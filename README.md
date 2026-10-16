# Dcode

A coding agent for **local and air-gapped LLMs**, with a terminal UI and a matching localhost web UI that drive the same session.

Dcode works with any OpenAI-compatible server (Ollama, vLLM, llama.cpp, LM Studio, TGI). It is built for open-source models with **small context windows (32K or less)**. It delegates work to sub-agents, keeps its own context compact, and remembers what it learns across sessions.

> Formerly `code-one`. Your `~/.code-one` config, threads and skills are copied to `~/.dcode` on first run, and the old `CODE_ONE_*` env vars still work.

## Highlights

- **Built for small contexts**
  - Token budgeting, calibrated against the usage your server reports.
  - Automatic compaction: old tool output is trimmed first, then older turns are summarized.
  - Line-ranged `read_file` and exact-match `edit_file`, so the model never re-sends whole files.
  - Tool output caps that scale with the context window.
- **Sub-agents and multi-agent work.** The main agent calls `delegate` to hand self-contained tasks to `explore`, `code`, `review` or `test` workers. Each worker starts with a fresh context, and only a short report comes back. Read-only workers run in parallel.
- **Memory, in layers.**
  - *Working:* the live conversation, compacted as needed.
  - *Episodic:* summaries of past sessions.
  - *Semantic:* facts saved per project or globally.
  - Retrieval is BM25 over plain Markdown files: no embedding model, fully offline.
- **Tolerant of open-model quirks.**
  - Recovers tool calls written as text (`<tool_call>…`, fenced JSON).
  - Strips `<think>` blocks.
  - Learns which optional parameters the server rejects.
  - Makes reused tool-call ids unique.
  - Breaks repeated-call loops.
- **Web UI.** A hub on `127.0.0.1`, one per OS user, lists all of your live sessions and saved threads. You can watch a session stream, send tasks, approve commands, stop work, and edit every setting, memory and skill. It starts with your first `dcode` and stops after the last one exits. History stays on disk.
- **Artifacts.** Dcode saves HTML pages, dashboards, SVG diagrams, Markdown reports, CSV tables and long code with `artifact_save`. You open them from the chat or the Artifacts library in the web UI. HTML runs in a sandboxed iframe with no network access, and every update keeps version history. Saved content becomes a short stub in the conversation, so artifacts don't fill the context.
- **Skills.** Nine built-in playbooks: devops, developer, ui-designer, code-reviewer, artifact-creator, tester, security-auditor, docs-writer and debugger. Only names and descriptions go in the prompt; the agent loads a full skill with `skill_use` when one fits. Pin a skill to always include it. To add your own, drop a `.md` file in `~/.dcode/skills` (or a project's `.dcode/skills`), write it in Settings → Skills, or ask Dcode, which uses `skill_save`.
- **Small-model helpers (Phase 3).**
  - Every edited file is checked right away (syntax errors, undefined names), and the errors go back to the model.
  - `@file` mentions with fuzzy completion attach files without spending tool calls.
  - A checklist tool keeps long tasks on track across compaction.
  - `/init` writes `AGENTS.md`.
  - Helpers can run on a smaller, faster model.
- **Your own helpers, commands and rules.**
  - Markdown helpers in `.dcode/agents/`; start a message with `@name` to use one.
  - Slash commands in `.dcode/commands/`, with `$ARGUMENTS`, `@file` and `` !`shell` ``.
  - `allow` / `ask` / `deny` rules for commands and files (`.env` protected by default).
  - Hooks around tools and tasks.
  - The model can ask you multiple-choice questions.
- **Databases.** Save a connection to PostgreSQL, MySQL / MariaDB or SQLite and browse it like a small DBeaver in the web UI (tables, data, structure, SQL editor, summary, export), use `/db` in the terminal, or just ask Dcode. Dcode has the rights of the database user you give it. Reads are read-only and limited; every change needs your confirmation in every mode; heavy tables are not exported whole.
- **Scheduled jobs.** Run a shell command or a Dcode prompt on a cron schedule and read each run's log (`/cron`, or the Scheduled screen). They run in the background with no terminal open. `dcode run "<prompt>"` does one task unattended.
- **External tools (MCP).** Local MCP servers add tools (`/mcp add …`). Only the tool names go into the prompt; the model loads one tool's details when it needs them, so many tools cost little context.
- **More than one way in.** `dcode attach` joins a running session from another terminal (also through an SSH tunnel). `dcode acp` puts Dcode inside Zed and other ACP editors, and `editors/vscode` holds a small VS Code extension. The hub's HTTP API is described at `/api/openapi.json`.
- **Everyday conveniences.** `!command` to run something yourself, `/editor`, `/thinking`, `/details`, `/notify`, `/undo` and `/redo`, `/sessions` and `dcode --continue`, `/export` and `dcode import`, `/stats`.
- **One look.** The terminal and the web UI share the same blue palette, defined once in `dcode/theme.py`.
- **Dcode Manager, for teams.** `dcode manager serve` runs a model gateway with users, API keys, token limits per model and in total (per day, week or month), guards per model, discovery of the model servers on a Kubernetes cluster (start, stop, `--max-model-len`), and monitoring of usage, latency and speed, in its own web UI. People sign in with `dcode login <url>` and see their usage with `/usage`. See [`docs/manager.md`](docs/manager.md) and [`deploy/manager`](deploy/manager/README.md).

> 🛠 **Developer docs:** [`docs/`](docs/README.md) (architecture, history, decisions, roadmap, testing, operations) · agent instructions in [`CLAUDE.md`](CLAUDE.md).
>
> 📖 **User guide in English and Persian (فارسی):** [`dcode/web/guide.html`](dcode/web/guide.html). It works offline, is served at `/guide` by the web UI, and `/guide` in the terminal prints the link.

## Quick start

```bash
pip install .            # or install the air-gapped binary, see below
export DCODE_BASE_URL=http://127.0.0.1:11434/v1   # Ollama
export DCODE_API_KEY=local
export DCODE_MODEL=qwen2.5-coder:7b
export DCODE_CONTEXT_WINDOW=32768                 # match your server's setting
dcode
```

With a Dcode Manager, sign in instead of setting variables: `dcode login http://<manager>:31010`.

The welcome panel prints a link like `http://127.0.0.1:7301/?token=…#s=<session>`. Open it to control the same session from your browser. `dcode ui` reprints the link, and `dcode --no-ui` skips the web UI.

## How it works

```
dcode (terminal) ──┐                       ┌── browser
                   ▼                       ▼
            Session  ◄── session API ◄── Hub (127.0.0.1, per user)
       (event bus, task queue,          serves UI, settings, memory,
        approvals, threads)             history; proxies to sessions
                   │
             Agent (main) ── delegate ──► sub-agents (fresh context)
                   │
            LLMClient ──► your OpenAI-compatible server
```

- **`engine.py`** contains the agent loop. It has no terminal or web code. It emits events, and it asks the session for approvals.
- **`session.py`** owns the queue and event log. The terminal and the web UI are both clients of it, and an approval goes to whichever one answers first.
- **`server.py`** holds two stdlib-only HTTP pieces: a small API server for each session, and the hub. Requests need a per-user secret token, stored in a `0600` file. The hub also checks the Host header (against DNS rebinding) and requires a custom header on every write (against CSRF).

## Project layout

```
dcode/
  main.py      CLI entry point, setup wizard, UI bootstrap
  cli.py       terminal front-end (state-driven loop, event rendering, slash commands)
  ui.py        terminal widgets (rich / prompt_toolkit)
  theme.py     shared color tokens for CLI + web
  session.py   live session: event bus, worker, approvals, persistence
  engine.py    agent loop, tool dispatch, delegation
  agents.py    roles, prompts, delegate/memory tool schemas
  context.py   token budget + compaction
  memory.py    long-term memory store (Markdown + BM25)
  llm.py       OpenAI-compatible client wrapper
  tools.py     sandboxed file / shell / git tools
  db.py        database connections, schema, guarded queries, export
  schedules.py scheduled jobs (cron), runs and logs, the scheduler
  headless.py  `dcode run`: one unattended task
  attach.py    `dcode attach`: a second terminal on a running session
  acp.py       `dcode acp`: Agent Client Protocol for editors
  mcp.py       MCP stdio client: external tools, described on demand
  transcripts.py  chat export (md / html / json), import, helper sessions
  stats.py     usage numbers (~/.dcode/stats.jsonl)
  openapi.py   the hub's HTTP API as an OpenAPI document
  server.py    session API + per-user hub
  manager_client.py  sign in to a Dcode Manager, usage
  manager/     Dcode Manager: gateway, users, limits, guards, discovery, monitoring, its web UI
  web/         index.html, app.css, app.js (no external assets)
deploy/manager/  the Dcode Manager on Kubernetes (kustomize)
packaging/     PyInstaller binary, .deb, .rpm, tarball, Docker image, offline installer
editors/       VS Code extension
tests/         pytest suite (fake LLM, no network)
```

## Configuration

Settings live in `~/.dcode/config.json` (mode `0600`). You can edit them with `/model`, `/agents`, `/context` and the other slash commands, or on the web UI's Settings page. Environment variables override the file, and the web UI shows those fields as read-only:

| Env var | Purpose |
| --- | --- |
| `DCODE_BASE_URL` | OpenAI-compatible endpoint |
| `DCODE_API_KEY` | Key (any placeholder for keyless local servers) |
| `DCODE_MODEL` | Model name |
| `DCODE_CONTEXT_WINDOW` | Model context window in tokens (default 32768) |
| `DCODE_MAX_OUTPUT_TOKENS` | Reply budget (default 4096, capped at ¼ of the window) |
| `DCODE_REASONING_EFFORT` | `none` / `low` / `medium` / `high` |
| `DCODE_WORKSPACE` | Root that file tools are confined to (default: cwd) |
| `DCODE_ALLOW_OUTSIDE_WORKSPACE` | `1` disables the path sandbox |
| `DCODE_NO_UI` | `1` disables the web UI |
| `DCODE_HOME` | Alternative home directory (default `~/.dcode`) |

**Project instructions:** if the workspace root contains `DCODE.md`, `AGENTS.md` or `CLAUDE.md`, the first one found is added to the system prompt, trimmed to fit the context budget.

**Parallel sub-agents** need a server that can serve concurrent requests, such as vLLM or `llama-server --parallel N`. Otherwise set *max parallel* to 1.

## Slash commands

| Command | Does |
| --- | --- |
| `/ui` | Show the web UI link |
| `/model` | Switch model and reasoning effort |
| `/models` | More models: a reasoning model for hard tasks, a model for helpers and one for small jobs |
| `/agents` | List helpers (built-in and yours); `/agents settings` for parallelism, steps, helper model |
| `/init` | Write `AGENTS.md`: commands, layout, conventions |
| `/rules`, `/hooks` | Allow / ask / deny by pattern (`/rules deny "rm -rf *"`); list hooks |
| `/<your-command>` | Any `.dcode/commands/<name>.md` or `~/.dcode/commands/<name>.md` |
| `/context` | Show context usage, change the window size |
| `/memory`, `/remember <text>` | List memories or save one |
| `/permissions`, `/experimental`, `/skills` | Tool access, feature flags, prompt skills |
| `/review` | Review the git diff in a fresh sub-agent context |
| `/new`, `/reset`, `/rename`, `/sessions [words]` | Thread management; `/sessions` switches to a saved chat |
| `/export [md\|html\|json] [file]` | Save the chat as a file (`dcode import` brings a JSON export back) |
| `/undo`, `/redo` | Undo the last task's file changes; bring them back |
| `/editor`, `/thinking`, `/details`, `/notify` | Write in `$EDITOR`; show reasoning; hide tool details; bell or desktop notification |
| `/mcp` | External tools from MCP servers: `/mcp add <name> <command…>` |
| `/stats [days]` | Usage numbers |
| `/usage` | Your tokens and limits on the Dcode Manager, and when they reset |
| `!command` | Run a shell command yourself; Dcode sees the output |
| `/setup`, `/help`, `/exit` | Setup again, show help, quit |

Press `Ctrl-C` during a task to interrupt it. Interrupting leaves the saved history valid.

## Security model

Dcode executes model output, so its defaults are defensive:

- **Path sandbox.** File tools are confined to the workspace. Reaching outside it asks first (the `outside_workspace` rule), or is blocked if set to `deny`.
- **Rules and hooks.** `rules.commands` / `rules.files` patterns allow, ask or deny. `.env` files are denied by default. A project's `.dcode/rules.json` can only make things stricter. Hooks (`before_tool` can block) come only from your own `~/.dcode/config.json`, never from a project folder.
- **Approvals.** `run_command`, `delete_file` and `git_commit` ask first, in the terminal or the web UI. `auto_approve_commands` turns this off and is meant only for trusted sandboxes.
- **Local only.** All servers bind to `127.0.0.1`. The hub needs a per-user token, so other users on a shared server can't see or drive your sessions.
- **Secrets.** `~/.dcode` is `0700`, and the config and token files are `0600`. The web UI never shows the API key.

## Air-gapped install

PyInstaller doesn't cross-compile, so build on a machine with the same CPU as the target:

```bash
make packages   # needs docker: one binary built on glibc 2.28 (RHEL 8+, Debian 10+, Ubuntu 20.04+),
                # then dist/: .deb, .rpm, .tar.gz, a Docker image file and the VS Code .vsix
make deb        # without docker: dist/dcode + .deb, for this machine's OS version and newer
make mac PYTHON=.venv/bin/python   # local macOS build, installs to /opt/homebrew/bin (no sudo)
```

Copy the `.deb` (or the raw binary) and `packaging/install-airgap.sh` to the server:

```bash
sudo ./install-airgap.sh dcode_0.6.0_amd64.deb       # Debian/Ubuntu
sudo ./install-airgap.sh dcode-0.6.0-1.x86_64.rpm    # RHEL/Rocky/Alma/Fedora/SUSE
sudo ./install-airgap.sh ./dcode                     # any Linux/macOS
tar -xzf dcode-0.6.0-linux-x86_64.tar.gz && ./dcode-0.6.0-linux-x86_64/install.sh   # no root needed
```

For fully offline builds, prepare a wheelhouse and run `PIP_ARGS="--no-index --find-links ./wheels" make binary`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
make test      # pytest
make lint      # ruff
```
