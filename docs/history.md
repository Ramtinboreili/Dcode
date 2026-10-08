# Project history

The story of how **code-one** became **Dcode**, built in one long pairing session with Claude Code (Opus 5.5), October 3–5, 2026. Each section lists what was asked, what was built, and what broke along the way. For the reasons behind each choice see [decisions.md](decisions.md); for the current code layout see [architecture.md](architecture.md).

---

## 0. Starting point: code-one 0.1.0

- `code_one/` was a single-agent terminal assistant (`agent.py`, `tools.py`, `ui.py` in orange). It worked with any OpenAI-compatible endpoint, had a workspace sandbox and y/N confirmations, and could be packaged with PyInstaller into a `.deb` plus an air-gapped installer.
- The test baseline was 14 passing and 1 failing. `_truncate(limit=MAX_OUTPUT_CHARS)` bound its default at definition time, so the output cap couldn't be changed at runtime.

## 1. Phase 1 — Dcode (0.2.0)

**Asked:** install pytest and run the tests. Then make the agent work well with open-source models with ≤32K context, using sub-agents and a better memory architecture. Rename it to Dcode, recolor it blue, and add multi-agent support. Add a localhost web UI, one per OS user, that shows all of that user's sessions, can control them, appears when `dcode` starts, stops when the last session ends but keeps its memory, is lightweight, and exposes every setting. Make the CLI and UI share one design.

**Built:**
- Renamed the package to `dcode`, with `~/.dcode` (copied from `~/.code-one` on first run) and `DCODE_*` env vars (`CODE_ONE_*` still accepted).
- An I/O-free `Engine` that reports through a `Host` (events and approvals). A `Session` owns the task queue, event bus, approvals and threads, so the terminal and the web UI are both clients of it.
- Small-context design:
  - token budget calibrated against the server's reported usage
  - two-stage compaction: elide old tool output, then summarize
  - line-ranged numbered `read_file` and exact-match `edit_file`
  - output caps scaled to the context window
  - terse tool schemas
- Model quirk handling in `llm.py`:
  - learns which optional parameters a server rejects
  - strips `<think>`
  - recovers tool calls written as text (`<tool_call>`, fenced JSON)
  - breaks repeated-call loops and nudges empty replies
- The `delegate` tool, with explore/code/review/test helpers (fresh context, role-scoped tools, a capped report back). Read-only helpers run in parallel.
- Memory: working memory, episodic session summaries, and semantic project/global facts stored as Markdown and searched with BM25. Relevant memories are recalled automatically.
- The web UI hub: stdlib HTTP, one per OS user, a token cookie, a Host-header check, and a CSRF header. It proxies to per-session APIs, including SSE, and exits after the last session ends.
- One theme file (`theme.py`) drives both the CLI and the web UI.

**Found and fixed along the way:**
- **Env-only configs couldn't be saved from the UI.** `config.json` had no key when the endpoint came only from env vars, and env values silently overrode UI edits. Fixed by validating against the effective config and showing env-locked fields as read-only.
- **The macOS `.venv` editable install broke.** The `.pth` files get the `hidden` flag (the repo lives under iCloud-synced `~/Documents`), and Python 3.12.4 skips hidden `.pth` files. Worked around with `PYTHONPATH`.
- **Typed-ahead Enter became a newline**, through the terminal's cooked-mode translation of `\r` to `\n`. Fixed later by also binding `c-j` to submit.

## 2. Web UI redesign — ChatGPT style

**Asked:** more white and blue, more humanized and friendly, dark and light mode, a ChatGPT-like layout, and a logo built from `╱────╲ │ D > │ ╲────╱`, possibly dynamic.

**Built:**
- Light and dark palettes, plus system mode.
- The **Dcode mark**: a chamfered octagon with a D, a `>` prompt and a blinking cursor. A cyan light sweeps the frame while Dcode works.
- Sidebar with live sessions and dated history. Welcome screen with suggestion cards.
- Humanized tool steps ("Reading app.py…" → "Read app.py") grouped into "Worked through N steps". Helper cards. Approval cards.
- Settings and Memory dialogs. The terminal also switched to the terminal's own foreground color, so it stays readable on light terminals.

## 3. macOS build, plus round 1 of real-use fixes

**Asked:** build the CLI for the user's MacBook (Apple Silicon).

**Built:**
- A PyInstaller one-file binary (23 MB, arm64). `sudo` can't prompt from inside Claude Code, so it's installed to `/opt/homebrew/bin` (user-writable and on PATH). Later this became `make mac`.

**Fixed from user testing:**
- The CLI approval prompt was `y/N` text, so typing "allow" meant *deny*. Replaced with an arrow-key menu: Allow / Allow for this session / Deny.
- Long model lists didn't scroll. Pickers now scroll and filter as you type.
- Spacing improved, and the usage line now shows only the elapsed time.
- **The web model list was empty:** python.org's macOS Python has no CA store, so `urllib` failed with CERTIFICATE_VERIFY_FAILED. Switched the hub to `httpx` (bundles certifi), and added model search plus custom names.
- Added the **artifact store** (`artifact_save` / `artifact_get`) with a side-panel viewer and library.
- Added **9 built-in skills** (devops, developer, ui-designer, code-reviewer, artifact-creator, tester, security-auditor, docs-writer, debugger). Skills are loaded on demand with `skill_use`, and users can create them via `skill_save`, the UI, or `.md` files.

## 4. Bilingual guide

**Asked:** replace `code-one_Guide.docx` with an HTML guide for everyone, in English and Persian.

**Built:**
- `dcode/web/guide.html`: self-contained and offline, with full RTL for Persian (code stays LTR) and light/dark modes.
- Served at `/guide` without the token (public docs), linked from the sidebar and from `/guide` in the terminal.

## 5. Real-use round 2 — 0.2.1

**Asked, from an Ansible session:**
- a report at the end of every task
- a full-screen CLI like Claude Code with a dynamic input border
- Ctrl+C to exit
- automatic chat names
- clickable links
- `/version`
- git-like features
- expandable output
- modes (auto / accept edits / manual / plan)
- `/compact`

**Built:**
- An end-of-task report. Dcode asks the model when it stops silently, with a factual fallback built from tracked activity.
- Modes, with Shift+Tab to cycle. Plan mode is read-only and ends with an approval menu.
- Checkpoints and `/undo`, plus `/diff`, `/commit` (generated message), `/compact` and `/clear`.
- A Claude-Code-like layout: compact header, input pinned to the bottom between rules, slash suggestions below the input, Ctrl+O to expand output, Ctrl+C twice to exit.
- Chat titles: instantly from the first message, then refined by the model.

**Fixed:**
- **Data loss:** the model called `write_file` with empty content three times and blanked `inventory.ini`. Empty writes to non-empty files are now refused.
- DeepSeek-style models leaked `｜DSML｜` markup into tool names (`git_diff</｜DSML｜ parameter>`). Names are cleaned and arguments salvaged.
- The approval menu stayed in the scrollback. It now erases itself, leaving one decision line.
- macOS Terminal doesn't support OSC 8 links. Links are emitted only in terminals that support them, and `/ui` and `/guide` open the browser directly.
- The `/` suggestion menu floated in the wrong place. Suggestions moved below the input.
- A VS Code renderer was stuck at 97% CPU for 20 hours. That's VS Code, not Dcode; the user asked, and it was killed.

## 6. Phase 2 — artifacts — 0.3.0

**Asked:** commit everything, then Phase 2: artifact management in the web UI, building artifacts and linking them, and a CLI that is less of a Claude Code copy.

**Built:**
- **Library:** live sandboxed thumbnails, search, filter, sort, and tile menus.
- **Viewer:** Preview/Code/Edit tabs, version history with restore, rename, a link back to the source chat, and "Ask Dcode to change this".
- **New artifact:** ask Dcode, write or paste, or upload a file.
- Standalone pages at `/a/<id>`. Login links accept `next=` (relative paths only). `/artifacts` in the CLI.
- CLI identity: a labeled `── ❯ dcode ─── ◆ mode ──` rule, `◇`/`╰─` glyphs, a blue answer gutter, a chevron spinner and a context meter.

**Fixed from user testing:**
- **The artifact was never built.** The model said "Building the artifact now." and ended its turn without a tool call. Added a follow-through check (announced action or requested artifact without a tool call means remind it, at most twice), continuation for cut-off replies, and `append=true` so long artifacts can be built in parts.
- Added visibility: a live panel showing the current step, each running helper and background jobs, with timers. Background jobs use `run_command background=true`, `job_status` / `job_stop` and `/jobs`.

## 7. 0.3.1 — artifact noise and colors

**Fixed from user testing:**
- **Re-save loop.** The history stub `[N chars — saved, see tool result]` made the model believe the save had failed, so it re-saved placeholders (v2…v9) and then created `Untitled` copies. Now the stub says the content was delivered and stored, the save result confirms the size and a preview, saves are capped at 3 per artifact per task, and saves without a title or with the same title update the current artifact.
- **One artifact per chat.** Duplicates also came from separate tasks in the same chat. A save without an id now matches the chat's artifact by normalized title, or takes the latest one if untitled. The user's 7 leftover `Untitled` artifacts were deleted at their request.
- **Too much blue in the CLI.** Text is now white, and blue appears only on structure (prompt, answer gutter, bullets, links, logo, mode badge). Rich `Text(style=…)` base styles had been leaking blue into answers.
- Artifact logging shrank to one line plus `↻ updated · vN`. Short `/a/<id>` links work because the login cookie lasts 30 days.

## 8. Web UI broke after a session exit (post-0.3.1, unreleased)

- **Symptom:** `{"error": "not found"}` on the web UI.
- **Cause:** the frozen binary's hub inherited `_PYI_*` / `_MEIPASS` variables and reused the *starting session's* unpack folder. When that session exited, the folder was deleted under the still-running hub.
- **Fix:**
  - spawn the hub with a clean environment (`PYINSTALLER_RESET_ENVIRONMENT=1`)
  - `/api/health` reports the version and whether assets exist; a starting session replaces an outdated or broken hub
  - the hub exits cleanly on SIGTERM

## 9. Version discussion

The user asked to release **v1.0.0** and whether Dcode is production-ready. Claude's honest answer was **not yet**: the actual target (air-gapped Linux plus local 7B–32B models) has never been built or run, real-use testing keeps finding bugs, the small-model heuristics are untuned, there's no CI, and there's one user. Claude recommended **1.0.0-rc.1**, then a Linux and local-model validation round, then 1.0.0. The user asked not to build yet and to write these docs first. See [roadmap.md](roadmap.md).

## 10. Phase 3 — ideas from opencode (unreleased)

The user asked what Dcode could learn from [opencode](https://github.com/anomalyco/opencode). Claude read its docs (agents, tools, permissions, LSP, MCP, TUI, commands, server, plugins, sharing, formatters, CLI) and proposed four tiers, filtered for air-gapped machines and ≤32K models. Cloud-only features (Zen, web search, OAuth MCP, public sharing, the GitHub app) were skipped. The user chose **Tier 1 and Tier 2**:

| # | Feature | Notes |
|---|---|---|
| 1 | Checks after edits (+ optional formatting) | opencode uses LSP servers; plain CLI checkers are lighter and need nothing offline |
| 2 | `@file` mentions with fuzzy completion | attached as synthetic `read_file` results so compaction can shrink them |
| 3 | `todo_write` checklist | in the system prompt only at turn start / after compaction (keeps the local prompt cache) |
| 4 | Helper model | `agents.helper_model` or per helper |
| 5 | `/init` → `AGENTS.md` | updates `DCODE.md` instead if it exists |
| 6 | Permission rules | stricter-only project rules; `.env` denied by default; outside-workspace asks |
| 7 | Custom helpers (`.dcode/agents/*.md`) and `@name` | built-ins can be changed by a same-named file |
| 8 | Custom commands (`.dcode/commands/*.md`) | `` !`shell` `` goes through the same mode and rules |
| 9 | Hooks | global config only, never from a project |
| 10 | `ask_user` questions | main agent only; asked even in auto mode |

One commit per feature on `dev`, plus end-to-end checks and docs. 154 unit tests, 31 terminal checks, and the web UI was checked in headless Chrome. The extra prompt cost is about 245 tokens per request.

## 11. A second machine, memory use, databases and scheduled jobs (unreleased, October 8, 2026)

Work moved to a Linux server (Ubuntu 22.04). The GitHub repo was renamed to `Ramtinboreili/Dcode` and `main` became the default branch.

- **Linux check:** 154 unit tests and the 31 terminal checks passed there from source, the first run outside macOS.
- **Memory:** the owner reported the background server using too much memory. The hub was importing the whole terminal stack; it now starts through its own entry point and fetches the model list with the standard library: about 40 MB → 22 MB. A session is still 60–75 MB (the `openai` SDK and prompt_toolkit).
- **Databases** (asked: connect from the CLI or UI, see the schema "like DBeaver", access decided by the database user, summaries and exports with guards for heavy databases, every change manual). Built `db.py`, the Databases screen, `/db`, and the model's `db_schema` / `db_query`. Checked against real PostgreSQL 16, MySQL 8.4 and MariaDB 11 in throwaway containers.
- **Scheduled jobs** (asked: set cron jobs and see their logs). The owner chose shell commands *and* Dcode prompts, run by Dcode's own background scheduler. Built `schedules.py`, `dcode run`, the Scheduled screen and `/cron`.
- **Found along the way:** PyMySQL printed "Exception ignored" noise when a half-read result was dropped; a job saved in the middle of a matching minute fired at once; the log-cut note was skipped when all output arrived in one chunk; a shell `pkill -f` matched its own command line.
