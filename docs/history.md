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

## 12. Phase 4 — the rest of the opencode comparison (unreleased, October 8, 2026)

After 0.4.0 the owner said "going for phase 3". Phase 3 was already released, so Claude asked what was meant; the owner chose **Tier 3 and Tier 4** of the opencode comparison, everything that had been left in the backlog. One commit per feature on `dev`:

| Feature | Notes |
|---|---|
| `!command`, `/editor`, `/thinking`, `/details`, `/notify` | reasoning is a display-only `thinking` event; a `!command` is recorded as a tool call so compaction can shrink it |
| `/redo` | undo remembers what it replaced; a new file change clears it |
| `/sessions`, `--continue`, `--resume` | this folder's chats first; a chat open in another terminal is not loaded twice |
| Export and import | Markdown, one script-free HTML page, JSON; an imported file is untrusted |
| `dcode stats` | counts only, in `stats.jsonl` |
| Helper sessions | `HelperLog` keeps each helper's steps and saves them with the chat |
| MCP | stdio client; tool names in the prompt, details on demand |
| OpenAPI + `dcode attach` | attach reuses `TerminalApp` through a `RemoteSession` |
| `dcode acp` + VS Code extension | ACP v1; the extension has no build step |
| Packaging | portable binary from an AlmaLinux 8 container; `.rpm`, tarball, Docker image |

**Found along the way:**
- `/help` had always dropped text in square brackets (`/compact [focus]`): rich read it as a style tag.
- The published 0.4.0 `.deb` is zstd-compressed and Debian 11 can't open it; and its binary needs glibc 2.35, so the "RHEL 9.2+" claim in the 0.4.0 notes was wrong (RHEL 9 has 2.34). Both are fixed for the next build, and the notes are corrected.
- An `update(kind, **fields)` helper collided with a `kind` field and failed silently inside an event listener.
- The MCP Python SDK had moved to 2.x (`FastMCP` → `MCPServer`) since Claude's training data; the client was checked against 2.3.0.

**Checked with outside references:** the MCP client against the official SDK; every `dcode acp` message against the official ACP v1 JSON schema; packages in AlmaLinux 8/9, Debian 11/12 and Ubuntu 20.04 containers. **Not checked:** a real Zed, a real VS Code, a real local model.

## 13. More than one model — 0.6.0 (October 9–13, 2026)

Work moved to the bastion host of the owner's GPU cluster (Ubuntu 24.04), where vLLM serves the models; the saved Claude memory was restored there. Before testing against the real model, the owner asked for "multi models for tasks, agents, reasoning and planning".

- **What existed:** one helper model, on the same server only. No model for reasoning, and each vLLM deployment has its own URL.
- **Asked what "reasoning" should mean,** the owner chose a router: Dcode decides per task and uses the better model when the task needs reasoning. Planning stays with the main model.
- **Built:** `models.py` (profiles, `ModelPool`, the router), `Agent.use`, routing in `Session._run_task`, a utility model for titles and commit messages, `/models`, the More models section in Settings, a `route` event.
- **Found along the way:** the first follow-up rule ("any short message continues the last task") kept a new short task such as "list the files in src" on the reasoning model; it now needs a reply word or a pronoun ("yes", "fix it"). `/models` lost `[server-url] [context]` to rich's bracket parsing, caught on the rendered screen.
- 268 unit tests, 64 terminal checks (a second mock server proves a hard task reaches another server).

**First run with real local models** (the same day): `gemma4:31b` as the main model and `diffusiongemma-26B-A4B-it` as the reasoning model, both on vLLM with a 16K window. A project with two real bugs (a lock taken twice, a `KeyError`) was fixed in 12–15 s; an easy task stayed on the main model, a hard one was routed, a six-turn chat moved between the two models with the history intact, plan mode stayed on the main model, and helpers ran on the reasoning model.
- **Found:** the reasoning model's first `edit_file` calls failed ("old_text not found") in every run: it copied the space after `N|` in `read_file` output as indentation. `edit_file` now accepts text whose every line has one space too many. Regression test: `test_edit_file_tolerates_the_space_after_the_line_number`.
- **Measured:** on that cluster gemma4 wrote about 7 tokens a second and diffusiongemma about 240, so turns on the main model took 20–160 s. That is the servers, not Dcode.
- **Reported by the owner three days later** (gemma4 editing a Kubernetes manifest on another machine, still on 0.5.0): two "old_text not found", then the same `edit_file` call repeated under the "identical arguments 3 times" warning until Ctrl+C. Reproduced with the real model on 0.5.0: 13 spaces where the YAML has 12, the same separator-space bug. Second bug: the repeat warning never ended the loop. Now the second ignored warning removes the tools and asks for a report, and a failed edit names the closest line. Tests: `test_a_model_stuck_on_one_failing_edit_is_stopped`, `test_edit_file_yaml_block_copied_one_space_too_deep`, `test_a_failed_edit_says_which_line_it_probably_meant`.
- **Still open:** a long session that compacts, 32K, Ollama and llama.cpp, a small utility model for the router.

## 14. Every model family, its own way — 0.7.0 (October 15, 2026)

The owner plans to connect very different models (GLM, DeepSeek, Claude, GPT, Gemini, Qwen…) and asked for a detector that follows each one's best practice for reasoning and tool calls; for the differences in how models run shell commands; and for runaway reasoning. Also: test every slash command, commit per change, and no tests with local models until the owner confirms.

- **Built:** `families.py` (detection, reasoning fields per family and server), `toolcalls.py` (every family's text tool-call format), a learning `LLMClient` (refused fields, `max_completion_tokens`, reasoning back, call ids, system role, turn order, text tools), a reasoning cut-off, `shell.py` (one command shape, no-terminal refusals, bash, no stdin, process-group timeouts), argument-name mapping before approval, `/models family`, a Model family setting.
- **Found while testing:** `run_command` inherited the terminal's stdin (a command reading input could take the user's keystrokes), ran in `/bin/sh` (dash on Ubuntu), and a backgrounded child kept the pipe open past the timeout; a model re-running one failing command was never stopped (the repeat guard skipped `run_command`); a tool argument `"false"` counted as true; Qwen3/R1-style replies that only close `</think>` kept their reasoning in the answer; the web UI streamed `<think>` text live.
- **Slash commands:** `tests/test_slash_commands.py` runs all 41 through the terminal's dispatcher; the pty check (98 checks, up from 64) now opens and closes each menu in a real terminal. It found one bug: Esc did nothing in `/setup`, so there was no way out and the next message typed became the server URL. Esc now cancels every one-line prompt (`test_esc_leaves_a_one_line_prompt`).
- Tested with scripted servers first. With the owner's go-ahead, then on OpenRouter (DeepSeek v4.1 flash, Gemma 4 31B, Qwen3.8 27B, Qwen3-Coder-Next, GLM 5.3 and 5.3 flash: shell, edit, git, a background server, the real terminal), which found seven more bugs: OpenRouter's `exclude` only hid reasoning (now `enabled: false`); a 429 failed at once with a JSON dump (now waits 3/8/15 s); Gemma rewrote files it never read (`write_file` now refuses that once); a bare "web" or `index.html` counted as asking for an artifact; Qwen3-Coder's `background: true` plus a trailing `&` detached the server so `job_stop` couldn't reach it; GLM 5.3 refuses turning reasoning off, and Dcode dropped the whole field for every model on the server (now a per-model "must think"); GLM's 60-token titles came back empty (side jobs are asked again with room to think).

## 15. Dcode Manager — 0.7.0 (October 16, 2026)

The owner asked for a manager for all users: set the manager's URL in Dcode's setup, sign in, get an API key and the models; admins manage models (context length, on/off), users (which models, token limits per model and user, usage resets), guards per model, discovery of the models on the cluster and a models path, a gateway for all models, and monitoring of usage and model performance; users see their usage, limits and reset times. Test with OpenRouter.

- **Built:** `dcode/manager/` (`dcode manager serve`): SQLite store, auth (scrypt, hashed sessions and keys, device sign-in), limits (per model and total, day/week/month/none, time zone, resets that move the start of counting), guards (block, redact secrets, mask output across streamed chunks, requests per minute, input size, policy prompt, guard model), the `/v1` gateway (streaming, usage and latency per request, vLLM-worded context refusals, reasoning translated per model with `families.py`, learning what an upstream refuses), discovery (Kubernetes API, weights folder, scale and `--max-model-len`), monitoring, a web UI (`app.js` + `admin.js`), `deploy/manager` (kustomize). Dcode: setup's first question, `dcode login`, `/usage`, the family and context from the manager, a spent limit reported at once.
- **Tested:** 38 tests with a scripted model server (`tests/test_manager.py`); live through OpenRouter (DeepSeek, GLM, Qwen; Dcode tasks, guard refusal, secret masking, a day limit hit); discovery read from the real `vllm` namespace through `kubectl proxy`; the UI in Chromium (light, dark, phone); `dcode login`, `/usage` and a task in a real terminal.
- **Found while testing:** usage rows were written after the answer, so a quick next request could slip past a limit; discovery assumed one server per deployment (`jina-stack` runs two behind two services) and would have listed any web server; the monitoring page waited for every server's health check; kustomize's `namespace:` would have moved the Role meant for `vllm` into `dcode`; a token limit read as "the model server is rate-limiting"; the new setup menu broke Esc-to-leave `/setup` (caught by the pty checks); the portable binary (built on AlmaLinux 8) trusted no CA on Ubuntu, so every HTTPS model server failed with CERTIFICATE_VERIFY_FAILED (`manager/tls.py` loads the host's bundle, also where a company CA lives).
- Not yet deployed on the cluster (needs an image in the registry and the owner's go-ahead).

