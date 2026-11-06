# CLAUDE.md — working on Dcode

Dcode is a coding agent (terminal + per-user localhost web UI) for **local / air-gapped LLMs with small context windows (≤32K)**. Python ≥3.10, package `dcode/`. GitHub repo: `Ramtinboreili/Dcode` (formerly `code-one-cli`).

## Rules that matter most
- **Air-gapped target:** no new runtime dependencies without strong reason; stdlib HTTP only; web assets must work offline (no CDNs, no web fonts).
- **Small-context first:** every tool schema/prompt is re-sent each turn — keep them terse. Never let big payloads (file contents, artifacts) sit in history.
- **Anything the model reads back must be unambiguous** (a vague stub once caused an artifact re-save loop).
- **Colors come only from `dcode/theme.py`.** Terminal: white text, blue only on structure (prompt, gutter, bullets, links, logo, mode badge).
- **Git:** work on `dev`; commit in logical parts; **never push unless the user asks**; end commits with
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- **Versions/tags are the user's decision.** Honest status: not production-ready until validated on air-gapped Linux with local models (see `docs/roadmap.md`).
- Reproduce every user-reported bug as a regression test before/with the fix.

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q                 # ~473 tests, ~70 s
.venv/bin/ruff check dcode tests              # must pass
PYTHONPATH=. .venv/bin/dcode                  # run from source (see macOS .pth gotcha)
PYTHONPATH=. .venv/bin/python -m dcode manager serve --host 127.0.0.1 --port 8790 --data /tmp/dm   # the Dcode Manager
python docs/dev/terminal_e2e.py [--binary /opt/homebrew/bin/dcode] [--verbose]   # 99 real-terminal checks (every slash command)
python docs/dev/screen.py --type "say hello" [--html out.html]       # rendered screen (needs pyte)
make mac PYTHON=.venv/bin/python              # macOS binary → /opt/homebrew/bin/dcode (no sudo)
make deb                                      # Linux .deb (build on matching Linux host)
make packages                                 # needs docker: portable binary (glibc 2.28) + .deb .rpm .tar.gz Docker image .vsix
```

## Map
- `engine.py` — agent loop (`Agent.run_turn`), tool dispatch, approvals by mode, delegation, compaction, follow-through, end-of-task report, artifact dedupe, checkpoints. **I/O-free**: talks to a `Host`.
- `session.py` — `Session` (the Host): worker/queue, `EventBus`, approvals (first answer wins), modes, plan approval, compact, undo, titles, jobs, thread autosave.
- `cli.py` + `ui.py` — terminal: state-driven loop, event rendering, slash commands, live panel; widgets (bottom input box, selectors, approval menu).
- `server.py` — per-session API + per-user hub (UI, settings, memory, skills, artifacts, proxy/SSE, lifecycle).
- `llm.py` (server quirks, `<think>`, text tool-call recovery, name cleaning, `with_model`), `context.py` (budget/compaction), `tools.py` (sandboxed tools, file guard, outside-workspace allowance), `agents.py` (prompts, roles, modes, extra schemas, todo helpers), `memory.py`, `artifacts.py`, `skills.py` (+ `skills/*.md`, `parse_frontmatter`), `checkpoints.py`, `jobs.py`, `config.py`, `theme.py`.
- Dcode Manager: `manager/` (`store.py` SQLite, `auth.py`, `limits.py`, `guards.py`, `gateway.py` = the `/v1` gateway, `discovery.py` Kubernetes + weights, `monitoring.py`, `server.py` routes, `cli.py` = `dcode manager …`, `web/` its UI: `app.js` everyone, `admin.js` admins). Dcode's side: `manager_client.py` (`dcode login`, `/usage`, `is_manager`). User doc: `docs/manager.md`; Kubernetes: `deploy/manager`.
- Model families: `families.py` (detect the family; its reasoning fields), `toolcalls.py` (every family's text tool-call format), `shell.py` (`run_command` the same for every model). `LLMClient._learn` adapts to what a server refuses.
- More than one model: `models.py` (profiles with their own server, `ModelPool`, the per-task router). `Session._run_task` routes; `Agent.use` swaps client + budget for one task.
- Databases: `db.py` (connections, `Link` = the only engine-specific code, schema, guarded `run_query`, exports). Scheduled jobs: `schedules.py` (cron parser, runs/logs, `Scheduler` thread in the hub) + `headless.py` (`dcode run`). Entry point: `__main__.py` (`hub` and `run` skip the terminal imports).
- Phase 4: `transcripts.py` (chat → events, export md/html/json, import, `HelperLog`), `stats.py` (usage counts), `mcp.py` (stdio MCP client, names-only index), `attach.py` (`dcode attach`: `RemoteSession` + `AttachApp`), `acp.py` (`dcode acp` for editors), `openapi.py` (the hub API, one `ROUTES` table). `editors/vscode` is the extension; `packaging/` builds every package.
- Phase 3: `checks.py` (post-edit checks), `mentions.py` (`@path`), `custom_agents.py` (`.dcode/agents`), `commands.py` (`.dcode/commands`), `rules.py` (allow/ask/deny), `hooks.py`.
- Research instrument: `experiment.py` (the `experiment` config section: every switchable mechanism, validated, hashed), `trace.py` (`DCODE_TRACE`: JSONL of every call, tool and decision; task identity in a ContextVar). Docs: `docs/research/`. A deliberate prompt or schema change means re-recording `tests/golden/requests.json.gz` (see `tests/golden/scenario.py`).
- `web/` — `index.html`/`app.js`/`app.css` (main UI), `artifact.html` (`/a/<id>`), `guide.html` (bilingual user guide, `/guide`).
- Details: `docs/architecture.md`. History & every past bug: `docs/history.md`. Rationale: `docs/decisions.md`.

## Conventions
- Match surrounding style; type hints; small functions; comments explain *why*.
- New tools: schema in `tools.TOOL_SCHEMAS` (file/shell/git) or `agents.EXTRA_TOOL_SCHEMAS`, availability in `Agent.tool_names()` (respect permissions, roles, **plan mode**), dispatch in `Agent._dispatch()`, a humanized label in `web/app.js` `describe()`, a CLI rendering if noisy.
- Events are the UI contract: add new ones to `cli._render` **and** `app.js render()`; web UI must handle replay (`seq=0`).
- Tests: use `conftest.FakeLLM` + `scripted(...)`/router functions; `isolated` fixture gives a private `DCODE_HOME`. Server tests use the in-process `stack` fixture.
- User-facing text: humanized, short. Guide is bilingual — update **both** English and Persian sections.
- Security boundaries: anything read from a project folder (`.dcode/agents`, `.dcode/commands`, `.dcode/rules.json`) is untrusted — project rules may only tighten, hooks come only from `~/.dcode/config.json`, and command `` !`shell` `` goes through mode + rules.
- Databases: the read-only session is the guard (the SQL classifier only picks the path); a change needs `confirmed=True` after the user saw it, in every mode, never per session; never return a saved password; estimate sizes from the catalog, never by scanning.
- MCP: servers only from `~/.dcode/config.json`, never a project; their tool schemas never enter the tool list (names in the prompt, `mcp_tools` on demand); every call asks unless auto mode or the server is `allow`; never return a server's `env` values.
- A new hub or session route also goes into `openapi.ROUTES` (a test calls every documented route). A new way to act on a session passes its own `source` (`cli`, `web`, `attach`, `acp`, `run`).
- The model's reasoning is display-only (`thinking` event): never put it in `messages`.
- An imported chat file is untrusted: keep only the message shapes Dcode writes.
- Scheduled jobs come only from `~/.dcode/cron`; the model gets no tool to create one; unattended runs deny approvals.
- Families: detection only shapes requests; text tool calls are parsed in every format whatever the family. Reasoning goes back to an API only when it demands it, only in the request copy, never in `messages`. Anything a server refuses is learned from its error, not configured.
- Models: route once per task, never mid-task; plan mode and an approved plan stay on the main model; never return a profile's API key; the budget and output caps must follow the model that runs.
- Manager: standard library only and no client/terminal imports (a test guards it). The `usage` table is the only counter (resets move the start, never delete). Never return a password hash, key hash or a model's upstream key. A cookie change needs the `X-Dcode-Manager` header; an API key reaches only `/v1` and read-only `/api/me*`. Cluster changes (scale, `--max-model-len`) only from an admin's click with a confirmation that says the impact. New model columns go into `store.MIGRATIONS`.
- Keep the system prompt stable within a turn (local servers cache the prompt prefix): only refresh it at turn start and after compaction.

- Research: `experiment` defaults must equal today's behaviour, and a new switch goes into `experiment.DEFAULTS` + `docs/research/experiment-config.md` (a test checks both). A new trace field goes into the schema in `tests/test_trace.py` + `docs/research/tracing.md`. A trace error must never break a task; a trace never holds a key, a prompt or file contents (snapshots do hold prompts).

## Gotchas (each one bit us)
- **macOS hidden `.pth`:** repo is in iCloud-synced `~/Documents`; `.venv` editable `.pth` files get the `hidden` flag and Python 3.12.4 skips them → use `PYTHONPATH=<repo>` or the binary.
- **PyInstaller one-file:** child processes inherit `_PYI_*`/`_MEIPASS` and reuse the parent's unpack dir — the hub must be spawned with `_clean_child_env()` or its web files vanish when the starting session exits.
- **python.org macOS Python has no CA store** → outbound HTTPS needs certifi's bundle: `httpx` in the session, `urllib` + `certifi` in the hub (`server._get_json`).
- **The hub must stay light** (it outlives sessions): it starts through `__main__.main` → `server.hub_main`, never importing click/rich/prompt_toolkit/openai/httpx. A test guards this.
- **`sudo` can't prompt from Claude Code's shell** → install to `/opt/homebrew/bin`.
- **prompt_toolkit:** keys typed before raw mode turn Enter into `c-j` → both submit; `PROMPT_TOOLKIT_NO_CPR=1` in pty tests; use `interrupt_prompt()` (works for prompts and selectors) to close UI from other threads.
- **rich:** `Text("x", style=S)` sets a *base* style that leaks into appended text; partial lines (`end=""`) break under `Live`/spinners — one print per line.
- **macOS Terminal has no OSC 8 links** → `ui.link()` emits them only where supported; `/ui`, `/guide`, `/artifacts` open the browser.
- **Headless Chrome:** `--virtual-time-budget` hangs on SSE pages; late iframes may not paint; min viewport 500 px. Never let tests open real browsers (`BROWSER=true`).
- **PyMySQL unbuffered cursors** drain the rest of a result when dropped (slow, and noisy after the socket closed) → `Link.close()` clears `unbuffered_active` first.
- **pg8000 loads whole results into memory** → big reads go through `DECLARE … CURSOR` / `FETCH`.
- **A PyInstaller binary needs the build machine's glibc or newer** → build releases with `make linux` (AlmaLinux 8 container), not on the dev host. The first Linux build (Ubuntu 22.04) could not run on RHEL 9.
- **`dpkg-deb` on Ubuntu 22.04 defaults to zstd**, which Debian 11 can't open → `build-deb.sh` passes `-Zxz`.
- **rich reads `[word]` as a style tag** and drops it → `_escape()` any text that may hold square brackets (`/help` lost "[focus]").
- **A keyword named like a positional parameter** (`update(kind, **fields)` with a `kind` field) raises inside a bus listener, where exceptions are swallowed → silent missing events. Name such parameters distinctly.
- **`pkill -f pattern` in a shell command kills that shell too** (its own command line matches) → write `[p]attern`.
- Session approvals: don't send an "attention" ping on `approval_request` (it closes the terminal's own menu); only when the web answers.

## Release checklist
`dcode/__init__.py` version → `guide.html` (4 version spots + package names) → `README.md` and `docs/operations.md` package names → `editors/vscode/package.json` version → `CHANGELOG.md` → pytest + ruff → `make mac` (and `make packages` on Linux) → `terminal_e2e.py --binary …` → commit `Release X.Y.Z` → `git tag -a vX.Y.Z` → push only if asked.

## The owner
Builds for air-gapped DevOps work; tests by real use and pastes terminal output/screenshots — treat those as bug reports. Prefers concise updates, honest assessments, ChatGPT-like web UI, Claude-Code-like CLI with Dcode's own identity, one artifact per chat. More in `docs/memory.md`.
