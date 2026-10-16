# Changelog

## Unreleased — every model family, its own way

GLM, DeepSeek, Claude, GPT, Gemini, Qwen and the rest each reason and call tools differently. Dcode now detects the family and follows its conventions. Tested with scripted servers, then on OpenRouter with DeepSeek v4.1 flash and Gemma 4 31B (bash, edit, git, a background server, the real terminal); not yet with the local vLLM models.

### Model families
- **Detected from the model name** (and the server URL when the name says nothing): GPT, GPT reasoning (o-series, GPT-5), gpt-oss, Claude, Gemini, Gemini Flash, Gemma, Qwen3, Qwen3-Coder, QwQ, DeepSeek, DeepSeek reasoner (R1), GLM, Kimi, MiniMax, Mistral, Magistral, Llama, Phi, Granite, Command. `/models` shows it; `/models family <name|auto>` or Settings → Model → Model family overrides it (for a fine-tune whose name says nothing).
- **Reasoning effort, the family's way:** `reasoning_effort` for GPT, gpt-oss and Gemini; the chat-template switch (`enable_thinking`, `thinking`) for Qwen3, GLM and DeepSeek on vLLM, SGLang and llama.cpp; `think` on Ollama; a thinking budget for Claude on Anthropic's API; `reasoning` on OpenRouter. Effort `none` now really switches thinking off where the model allows it. Titles, commit messages, summaries and the router ask for no thinking.
- **Learned from the server's errors** instead of failing: a field it refuses, `max_completion_tokens` instead of `max_tokens`, reasoning it wants back during a tool loop (DeepSeek, Kimi), Gemini's thought signatures, Mistral's 9-character tool call ids, a template without a system role, strict user/assistant turns, and a server that takes no tools at all (vLLM without `--enable-auto-tool-choice`, Ollama models without tool support). Then the tools are described in the prompt and the calls are read back from text. A context overflow teaches nothing.
- **Tool calls written as text** are read in every family's format: Hermes/Qwen, Qwen3-Coder XML, GLM `arg_key`, DeepSeek tool tokens and DSML, MiniMax `<invoke>`, Kimi `functions.x:0`, Mistral `[TOOL_CALLS]`, Llama `<function=…>` and `<|python_tag|>`, Gemma `tool_code`, gpt-oss channels, fenced or bare JSON. Other agents' tool names (`bash`, `str_replace`, `functions.read_file:0`) and argument names (`file_path`, `old_string`, `offset`/`limit`, `cmd`) are mapped, and arguments get their schema types (`"false"` is no longer true).

### Too much reasoning
- Reasoning that runs past most of the reply budget, or in circles, is cut off and the request is sent again with less thinking. A reply that was all thinking gets "stop thinking, act now", not "continue".
- `<think>` text never shows in the live answer, and reasoning before a lone `</think>` (Qwen3, R1 and QwQ templates open the block in the prompt) or in gpt-oss's analysis channel stays out of the answer.

### Shell commands
- One shape for every model: `["bash", "-lc", "…"]`, lists, fenced blocks, `$ ` prompts, `workdir`, `timeout_ms` and a trailing `&` (started as a background job).
- Commands run in bash with no input, no pager, no colours and no editor prompts; a timeout stops the whole process group.
- Commands that never end or wait for keys without a terminal (`tail -f`, `logs -f`, `kubectl -w`, `watch`, `ping` without `-c`, editors, a bare `python3` or `psql`) are refused with what to run instead.
- The same command failing the same way three times in a row is stopped, like other repeated calls.

### Found by the OpenRouter runs
- **No reasoning on OpenRouter** is now `reasoning: {enabled: false}`; `exclude` only hid it (DeepSeek still spent 136 reasoning tokens on a one-word answer).
- **A busy server** (429, 503: OpenRouter's free Gemma pool) is waited for (3, 8, 15 s, or its Retry-After) before the task fails, and the error is one line instead of a JSON dump.
- **`write_file` won't replace a file the agent never read**, the first time: Gemma 4 rewrote both files it was only asked to commit. Asked again on the same path, it goes through.
- **No unasked-for artifacts:** "start a web server" (any "web", or a file name like `index.html`) counted as asking for an artifact, so both models saved an HTML report nobody wanted.

### Tests
- Every slash command is run by `tests/test_slash_commands.py` (all 41 are handled; menus answered from a script) and in a real terminal by `docs/dev/terminal_e2e.py` (98 checks, up from 64). That found one bug: Esc did nothing in `/setup`, so the next message became the server URL.

## 0.6.0 — more than one model, and the first fixes from real local models

Tested with real local models for the first time (vLLM: `gemma4:31b` and `diffusiongemma-26B-A4B-it`, 16K context). Linux packages are in `builds/linux-amd64/`.

### More than one model
- **Model profiles:** add other models by name in Settings → Model → More models, or with `/models add <name> <model> [server-url] [context]`. Each can be on its own server (one vLLM deployment serves one model) with its own context window, API key and reasoning effort.
- **A reasoning model and a router:** pick one profile as the reasoning model. Before each task Dcode decides whether it needs careful thought (an error to debug, design, a root cause, a long detailed request, or "think hard") and runs that task there, with that model's context budget. One line says so: "Reasoning model: …". A short follow-up ("yes, go on") stays on the model that was working. Plans, and carrying out an approved plan, stay on the main model. `/models route auto|off|always` changes it.
- **A model for small jobs:** chat titles, commit messages and, when set, the check that sorts tasks (it overrules the keyword rules).
- **Helpers** (`agents.helper_model` and a helper file's `model:`) can name a profile, so helpers can run on another server too.
- `/models` shows which model does what. `dcode run` prints which model ran the task.

### Fixed
- `edit_file` failed with "old_text not found" when a model copied the space after the line number (`9| text`) as indentation, so every line had one space too many. Seen on the first run with a real local model; the edit now goes through with the file's own indentation.
- A model stuck on one failing call no longer loops until you press Ctrl+C. After the "identical arguments" warning is ignored once more, Dcode takes the tools away and asks the model to say what it tried and what it needs ("Stopped: the model kept repeating the same step").
- A failed `edit_file` now names the line it probably meant ("Line 33 has those words with other spacing: …"), so the model can correct itself instead of guessing.

## 0.5.0 — Phase 4 (the rest of the opencode comparison: Tier 3 and Tier 4)

Linux packages are built from one portable binary (glibc 2.28): `.deb`, `.rpm` and `.tar.gz` in `builds/linux-amd64/`.

### In the terminal
- **`!command`** runs a shell command yourself: no approval, no model call, and Dcode sees the command and its output. Works in the web UI too. Ctrl+C stops it.
- **`/editor`** writes a long message in `$EDITOR`.
- **`/thinking`** shows the model's reasoning (from `<think>` blocks or the server's `reasoning_content`), dimmed. It never goes back into the context. The web UI shows it as a closed "Thought it through" step.
- **`/details`** hides tool calls and results; Ctrl+O still expands the last task.
- **`/notify off|bell|desktop`**: a bell or a desktop notification when a task of 10 seconds or more ends, or when Dcode needs an answer.
- **`/redo`** brings back what the last `/undo` removed, until Dcode changes a file again.
- **`/sessions [words]`** switches to a saved chat (this folder's first) and shows where it left off. **`dcode --continue`** continues this folder's last chat; **`--resume NAME`** one by name.
- **`/export [md|html|json]`**, `dcode export` and an export menu in the web UI save a chat as Markdown, as one self-contained HTML page (no scripts, nothing loaded) or as JSON. **`dcode import`** and an import button bring a JSON export back; the file is treated as untrusted.
- **`/stats`**, `dcode stats` and Settings → Usage: tasks, tokens, working time and tools, by model and project. Counted in `~/.dcode/stats.jsonl`; numbers only, nothing leaves the machine.

### Helper sessions
- Each helper's own steps are now kept and saved with the chat. The web UI opens a helper's session from its card: task, steps with results, time, tokens, report. Saved chats show their helper cards again (they were missing).

### External tools (MCP)
- **Local MCP servers** (stdio) give Dcode extra tools: `/mcp add <name> <command…>`, or Settings → External tools.
- **Lazy tool index:** only tool names go into the prompt. The model reads one tool's description and arguments with `mcp_tools` and runs it with `mcp_call`, so a server with 46 tools costs under 400 characters of prompt instead of every schema on every turn.
- Every call asks first, unless the mode is auto or the server is marked `allow`. Plan mode runs only tools the server marks read-only. Servers come from `~/.dcode/config.json` only.

### Other terminals, editors, scripts
- **`dcode attach`** joins a running session from another terminal (also over an SSH tunnel to a server): follow the task, send messages, answer approvals, switch mode, detach without stopping anything.
- **`/api/openapi.json`** describes the hub's HTTP API (79 operations, OpenAPI 3.1). A test calls every documented route.
- **`dcode acp`** speaks the Agent Client Protocol (v1) for Zed and other ACP editors: messages, reasoning, tool calls with diffs, the checklist as a plan, approvals, modes, cancel.
- **VS Code extension** in `editors/vscode` (start, continue, attach, open the web UI, send the selection or file). `build_vsix.py` packs it without node.

### Packaging
- **One portable Linux binary:** `make linux` builds on AlmaLinux 8 (glibc 2.28) in a container, so the binary runs on RHEL 8+, Debian 10+ and Ubuntu 20.04+. Before, it needed the build machine's glibc (2.35 on Ubuntu 22.04).
- **`.rpm`, `.tar.gz` (with an installer that needs no root) and a Docker image** saved as a file; `make packages` builds them all. `install-airgap.sh` installs `.rpm` and `.tar.gz` too.

### Fixed
- `/help` dropped text in square brackets, such as `[focus]` in `/compact [focus]`.
- The `.deb` is now xz-compressed. Built on Ubuntu 22.04 it used zstd, which Debian 11's `dpkg` can't open (this affects the published `dcode_0.4.0_amd64.deb`).
- Correction to the 0.4.0 notes: that binary needs glibc 2.35, so it does **not** run on RHEL 9 (glibc 2.34).

### Numbers
- 256 unit tests. 59 real-terminal checks, passing from source and against the binary built in the container. Packages installed and run in AlmaLinux 8 and 9, Debian 11 and 12, and Ubuntu 20.04 containers. The MCP client was checked against a server built with the official Python SDK; every message `dcode acp` sends was validated against the official ACP v1 schema.
- **Not checked:** `dcode acp` inside a real Zed, and the extension inside a real VS Code.

## 0.4.0 — Phase 3 (ideas from opencode), databases and scheduled jobs

The first version built and checked on Linux (x86_64).

### Databases
- **Connect to your databases** (PostgreSQL, MySQL / MariaDB, SQLite) from the web UI, the terminal or by asking Dcode. Connections are saved once; the password stays in a file only you can read and is never shown. Dcode has exactly the rights of the database user you connect with.
- **Databases screen in the web UI**: table list with sizes, data preview, structure (columns, keys, indexes), an SQL editor with a result grid, a summary of the whole database, and export.
- **`/db` in the terminal**: `/db add`, `/db <name>` (tables), `/db <name> <table>`, `/db <name> SELECT …`, `summary`, `export`.
- **Dcode can query them** with two new tools (`db_schema`, `db_query`), which exist only while a connection is saved (about 270 tokens). It can save a full result to a CSV or JSON-lines file.
- **Guards**: reads run in a read-only session, one statement at a time, limited to 200 rows and 30 seconds. Anything that changes data or structure is shown to you and runs only after you confirm it, in every mode (auto included), and can't be allowed for the session. A connection can be marked read-only in Dcode. Exports refuse tables estimated above 1,000,000 rows or 200 MB (or cut there if you insist) and watch the free disk space. The limits are in Settings → Permissions → Databases.
- The drivers (pg8000, PyMySQL) are pure Python, bundled in the binary and loaded only when used.

### Scheduled jobs
- **Run a shell command or a Dcode prompt on a schedule** (five-field cron expressions, or `@hourly`, `@daily`, …) and read each run's log afterwards: the Scheduled screen in the web UI, or `/cron` in the terminal.
- Jobs run in the background even with no terminal open: the background service now stays up while a job is switched on. Run now, stop, switch off, time limit per run, overlapping runs skipped, last 50 runs kept.
- **`dcode run "<prompt>"`** does one task unattended and prints plain text (`--mode plan|accept_edits|auto`). Nobody can approve anything there, so whatever would ask is refused and the run ends with a non-zero exit code.

### Memory use
- The background web UI process (the hub) uses about 22 MB instead of about 40 MB: it no longer loads the terminal libraries, and it fetches the model list without an HTTP client library.

### Small-model quality
- **Checks after edits.** Right after a write or edit, Dcode checks the file and appends the errors to the tool result, so the model fixes them in its next step. Python, JSON and TOML are checked in-process; ruff (syntax and undefined names only), bash, node, gofmt, ruby and php run when installed. Formatting is optional and off by default. Custom per-extension checks and formatters go in the new `checks` config section.
- **`@file` mentions** with fuzzy completion in the terminal and the web composer. Up to 3 files are attached as synthetic `read_file` results (folders as `list_files`), so they cost no tool call and compaction can shrink them.
- **Checklist tool** (`todo_write`). It is drawn in the terminal and as a live card on the web, saved with the chat, and put back into the system prompt after compaction. The prompt is only rewritten at turn start and after compaction, so local servers keep their prompt cache.
- **`/init`** writes `AGENTS.md` (or updates `DCODE.md`): build, test and lint commands, layout, conventions and gotchas.
- **Helper model.** Helpers can run on another model of the same server (`agents.helper_model`, or per helper).

### Control
- **Custom helpers** from Markdown files in `~/.dcode/agents/` or `.dcode/agents/`, with description, tools, model, steps and instructions; a file named like a built-in changes it. A message starting with `@name` runs that helper directly. They are listed in `/agents` and in web Settings → Helpers, where new ones can be added.
- **Custom commands** from `~/.dcode/commands/*.md` or `.dcode/commands/*.md`, with `$ARGUMENTS`, `$1`–`$9`, `@file` and `` !`shell` `` (which follows the same mode and rules as `run_command`). `agent:` runs the command as a helper.
- **Permission rules**: `allow`, `ask` or `deny` commands and files by pattern, on top of the modes. Chained commands are judged part by part, and `ask` asks even in auto mode. `.env` files are denied by default for every tool and for `@mentions`. Files outside the project ask instead of failing. Projects can only tighten the rules, via `.dcode/rules.json`. Rules are edited with `/rules` or in web Settings → Permissions.
- **Hooks**: shell commands on `before_tool` (a non-zero exit blocks the tool), `after_tool`, `task_done` and `approval_needed`. They get `DCODE_*` environment variables and JSON on stdin, and are set in your own config only. `/hooks` lists them, and web Settings → Permissions edits them.
- **Questions** (`ask_user`): the model can ask a multiple-choice question, answered in the terminal or the web UI. If you skip it, the model decides and says what it assumed.

### Testing
- 199 unit tests, up from 110. `docs/dev/terminal_e2e.py` has 42 real-terminal checks, up from 22, and `--verbose` prints the screen when a check fails.
- The new tools add about 245 tokens per request (system prompt plus schemas, about 1.9K in total).

## 0.3.1

### Artifacts
- **One artifact per chat.** A save without an id updates the artifact this chat already made: same title, ignoring case and punctuation, or no title. This works across tasks and after resuming, so there are no more `Untitled` copies.
- Fixed a re-save loop. Saved content is now clearly confirmed to the model (size and a preview), and an artifact is saved at most 3 times per task.
- The terminal prints one `◆ Artifact` line with a short `/a/<id>` link, then `↻ updated` lines. The web UI refreshes the existing card instead of adding a new one. The login cookie lasts 30 days, so short links work.

### Terminal
- Calmer colors: text is white, and blue is used only for structure (prompt, answer gutter, bullets, links, logo, mode badge). The input rules are gray.

## 0.3.0 — Phase 2: artifacts, follow-through, background jobs

### Artifacts
- **Library in the web UI**: live sandboxed previews, search, filter by kind and sort. Each tile has a menu with open, open as a page, copy link, rename, duplicate, download and delete.
- **Viewer**: Preview, Code and Edit tabs (saving creates a new version). View and restore any version, rename, jump back to the chat that made it, and an "Ask Dcode to change this" box.
- **New artifact**: ask Dcode to build it, write or paste it, or upload a file.
- **Links**: every artifact has its own page at `/a/<id>`. The terminal prints the link when an artifact is saved, and `/artifacts` lists them and opens one.
- `artifact_save` with `append=true` builds long artifacts across several replies.

### Agent
- **Follow-through**: when the model announces an action, or skips an artifact you asked for, without calling a tool, Dcode reminds it to act (at most twice per task).
- Replies cut off by the output limit are continued.
- **Background jobs**: `run_command` with `background: true`, the `job_status` / `job_stop` tools, per-job logs and exit events. Jobs stop when the session ends.

### Terminal
- Live panel while working: the current step, each running helper and background jobs, all with timers.
- `/jobs`, `/artifacts` and `/clear`. Helper durations and a running-job count in the status line.
- A look of its own: a labeled `── ❯ dcode ─── ◆ mode ──` input rule, `◇` / `╰─` step glyphs, a blue gutter on answers, a chevron spinner and a context meter.

## 0.2.1

- Modes: accept edits, auto, plan (read-only, then approve the plan) and manual. Shift+Tab cycles them.
- `/undo` checkpoints, `/diff`, `/commit` (generated message), `/compact`, `/version` and `/guide`.
- An end-of-task report after every task, and chats named automatically from the first message.
- Claude-Code-style terminal layout: the input pinned to the bottom, slash suggestions below it, Ctrl+O to expand output and Ctrl+C twice to exit.
- Safety: refuses empty writes that would erase a file, and strips markup that models leak into tool names.
- Bilingual (English / فارسی) HTML user guide, served at `/guide`.

## 0.2.0

- Renamed from code-one to **Dcode**.
- Built for small-context (32K or less) models: budgeting, compaction, ranged reads and `edit_file`.
- Sub-agents (explore, code, review, test), long-term memory, skills and artifacts.
- Localhost web UI per user, fully integrated with terminal sessions.
