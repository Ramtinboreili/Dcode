# Changelog

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
