# Changelog

## Unreleased — Phase 3 (ideas from opencode), databases and scheduled jobs

The version number is the owner's decision.

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
