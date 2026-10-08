# Working memory: the user, preferences, environment

What Claude learned while working on Dcode. It complements the persistent Claude Code memory (`~/.claude/projects/…/memory/`) so anyone, human or agent, can pick this up.

## About the project owner
- Builds Dcode for **air-gapped DevOps environments** (Ansible, nginx, Traefik, Kubernetes-type work) using **open-source models with ≤32K context**.
- Works in phases: Phase 1 was Dcode itself, Phase 2 was artifacts. Expect "Phase 3" and so on to build on the engine/session/server split.
- Tests by actually using Dcode on real repos (for example `nginx-setup`) and pastes terminal output or screenshots of problems. Treat those pastes as bug reports and reproduce them as regression tests.
- Messages are terse, use `//` as separators, and sometimes contain typos. Persian is their second language, so the guide is bilingual.

## Preferences (confirmed in this session)
- **Git:** commit **part by part** in logical commits; **push only when asked**; work on the `dev` branch. End commit messages with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- **Versions:** the user decides. They asked for 0.2.1, 0.3.0 and 0.3.1 explicitly. On v1.0.0, Claude recommended an RC first (see roadmap). Ask before tagging 1.0.
- **Terminal look:** Claude Code's layout (input at the bottom, rules, status line) *but with Dcode's own identity*. **White text, blue only on structure.** Quiet output: one line per artifact save, no raw JSON.
- **Web look:** ChatGPT-like, white and blue, light and dark, humanized wording.
- **Artifacts:** **one artifact per chat**, updated in place, never `Untitled` duplicates.
- Wants to *see* what's happening: helpers, background commands, end-of-task reports.
- Wants honest answers (they asked "is this really production-ready?").

## Environment facts
- MacBook, Apple Silicon (arm64), macOS 26.x, zsh. Terminals: macOS **Terminal** (no OSC 8 links) and the VS Code integrated terminal. Browser: Safari.
- Python 3.12.4 from python.org (no CA store, so use `httpx`, not `urllib`, for TLS).
- The repo is at `~/Documents/devops repos/Codeone` (iCloud-synced, which causes the hidden `.pth` problem in `.venv`).
- `sudo` can't prompt from Claude Code's shell, so install to `/opt/homebrew/bin` (user-writable, on PATH).
- The user's live endpoint during testing was an OpenAI-compatible gateway (`services.irangpu.com`, 466 models), running models like `apodex-1.1-mini` and a DeepSeek variant (which leaks DSML markup into tool names).
- The GitHub remote is `git@github.com:Ramtinboreili/Dcode.git` (renamed from `code-one-cli` on 2026-10-08).

## Things that went wrong in *Claude's own* process (learn from them)
- Shipped a vague history stub that caused a model loop. Make anything the model reads back unambiguous.
- Fixed duplicates within a single task, but not across tasks. Test the *user's actual flow* (several requests in one chat), not just the single-call case.
- A packaging bug (the hub's unpack folder) only appeared in the binary after a session exit. Run the e2e against the **built binary**, with multiple sessions, before releasing.
- Rich `Text(style=…)` leaked blue into answers. Verify colors on a rendered screen (`docs/dev/screen.py --html`), not by reading code.
