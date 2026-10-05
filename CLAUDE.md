# CLAUDE.md — working on Dcode

Dcode is a coding agent (terminal + per-user localhost web UI) for **local / air-gapped LLMs with small context windows (≤32K)**. Python ≥3.10, package `dcode/`. Repo/GitHub name is still `code-one-cli` (formerly "code-one").

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
.venv/bin/python -m pytest -q                 # ~154 tests, ~20 s
.venv/bin/ruff check dcode tests              # must pass
PYTHONPATH=. .venv/bin/dcode                  # run from source (see macOS .pth gotcha)
python docs/dev/terminal_e2e.py [--binary /opt/homebrew/bin/dcode] [--verbose]   # 31 real-terminal checks
python docs/dev/screen.py --type "say hello" [--html out.html]       # rendered screen (needs pyte)
make mac PYTHON=.venv/bin/python              # macOS binary → /opt/homebrew/bin/dcode (no sudo)
make deb                                      # Linux .deb (build on matching Linux host)
```

## Map
- `engine.py` — agent loop (`Agent.run_turn`), tool dispatch, approvals by mode, delegation, compaction, follow-through, end-of-task report, artifact dedupe, checkpoints. **I/O-free**: talks to a `Host`.
- `session.py` — `Session` (the Host): worker/queue, `EventBus`, approvals (first answer wins), modes, plan approval, compact, undo, titles, jobs, thread autosave.
- `cli.py` + `ui.py` — terminal: state-driven loop, event rendering, slash commands, live panel; widgets (bottom input box, selectors, approval menu).
- `server.py` — per-session API + per-user hub (UI, settings, memory, skills, artifacts, proxy/SSE, lifecycle).
- `llm.py` (server quirks, `<think>`, text tool-call recovery, name cleaning, `with_model`), `context.py` (budget/compaction), `tools.py` (sandboxed tools, file guard, outside-workspace allowance), `agents.py` (prompts, roles, modes, extra schemas, todo helpers), `memory.py`, `artifacts.py`, `skills.py` (+ `skills/*.md`, `parse_frontmatter`), `checkpoints.py`, `jobs.py`, `config.py`, `theme.py`.
- Phase 3: `checks.py` (post-edit checks), `mentions.py` (`@path`), `custom_agents.py` (`.dcode/agents`), `commands.py` (`.dcode/commands`), `rules.py` (allow/ask/deny), `hooks.py`.
- `web/` — `index.html`/`app.js`/`app.css` (main UI), `artifact.html` (`/a/<id>`), `guide.html` (bilingual user guide, `/guide`).
- Details: `docs/architecture.md`. History & every past bug: `docs/history.md`. Rationale: `docs/decisions.md`.

## Conventions
- Match surrounding style; type hints; small functions; comments explain *why*.
- New tools: schema in `tools.TOOL_SCHEMAS` (file/shell/git) or `agents.EXTRA_TOOL_SCHEMAS`, availability in `Agent.tool_names()` (respect permissions, roles, **plan mode**), dispatch in `Agent._dispatch()`, a humanized label in `web/app.js` `describe()`, a CLI rendering if noisy.
- Events are the UI contract: add new ones to `cli._render` **and** `app.js render()`; web UI must handle replay (`seq=0`).
- Tests: use `conftest.FakeLLM` + `scripted(...)`/router functions; `isolated` fixture gives a private `DCODE_HOME`. Server tests use the in-process `stack` fixture.
- User-facing text: humanized, short. Guide is bilingual — update **both** English and Persian sections.
- Security boundaries: anything read from a project folder (`.dcode/agents`, `.dcode/commands`, `.dcode/rules.json`) is untrusted — project rules may only tighten, hooks come only from `~/.dcode/config.json`, and command `` !`shell` `` goes through mode + rules.
- Keep the system prompt stable within a turn (local servers cache the prompt prefix): only refresh it at turn start and after compaction.

## Gotchas (each one bit us)
- **macOS hidden `.pth`:** repo is in iCloud-synced `~/Documents`; `.venv` editable `.pth` files get the `hidden` flag and Python 3.12.4 skips them → use `PYTHONPATH=<repo>` or the binary.
- **PyInstaller one-file:** child processes inherit `_PYI_*`/`_MEIPASS` and reuse the parent's unpack dir — the hub must be spawned with `_clean_child_env()` or its web files vanish when the starting session exits.
- **python.org macOS Python has no CA store** → outbound HTTPS via `httpx`, not `urllib`.
- **`sudo` can't prompt from Claude Code's shell** → install to `/opt/homebrew/bin`.
- **prompt_toolkit:** keys typed before raw mode turn Enter into `c-j` → both submit; `PROMPT_TOOLKIT_NO_CPR=1` in pty tests; use `interrupt_prompt()` (works for prompts and selectors) to close UI from other threads.
- **rich:** `Text("x", style=S)` sets a *base* style that leaks into appended text; partial lines (`end=""`) break under `Live`/spinners — one print per line.
- **macOS Terminal has no OSC 8 links** → `ui.link()` emits them only where supported; `/ui`, `/guide`, `/artifacts` open the browser.
- **Headless Chrome:** `--virtual-time-budget` hangs on SSE pages; late iframes may not paint; min viewport 500 px. Never let tests open real browsers (`BROWSER=true`).
- Session approvals: don't send an "attention" ping on `approval_request` (it closes the terminal's own menu); only when the web answers.

## Release checklist
`dcode/__init__.py` version → `guide.html` (4 version spots + `.deb` names) → `README.md` `.deb` name → `CHANGELOG.md` → pytest + ruff → `make mac` → `terminal_e2e.py --binary …` → commit `Release X.Y.Z` → `git tag -a vX.Y.Z` → push only if asked.

## The owner
Builds for air-gapped DevOps work; tests by real use and pastes terminal output/screenshots — treat those as bug reports. Prefers concise updates, honest assessments, ChatGPT-like web UI, Claude-Code-like CLI with Dcode's own identity, one artifact per chat. More in `docs/memory.md`.
