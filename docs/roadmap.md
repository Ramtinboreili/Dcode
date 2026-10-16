# Roadmap and plans

## Current status (October 16, 2026)

- **Released:** v0.7.0 on 2026-10-16 (the Dcode Manager, model families), with portable Linux packages in `builds/linux-amd64/`. Before it: v0.6.0 on 2026-10-13 (more than one model), v0.5.0 on 2026-10-08 (Phase 4), v0.4.0 the same day (Phase 3, the lean hub, databases, scheduled jobs), and v0.3.1. On 2026-10-07 everything was pushed: `main` and `dev` (same commit), the tags `v0.3.0` and `v0.3.1`, the macOS binary in `builds/`, and Claude's memory files in `docs/claude-memory/`.
- **In 0.4.0, after 0.3.1:** the hub fix (clean environment, version and asset health check), these docs, and **Phase 3** (Tier 1 + 2 from the opencode comparison: post-edit checks, `@file` mentions, checklist, `/init`, helper model, custom helpers and commands, permission rules, hooks, questions). See CHANGELOG "Unreleased".
- **Also in 0.4.0** (2026-10-08): a lean hub process, **databases** (connections, schema, guarded queries, summary, export; web UI, `/db`, model tools) and **scheduled jobs** (`/cron`, the Scheduled screen, `dcode run`).
- **In 0.5.0: Phase 4**, the rest of the opencode comparison (Tier 3 and Tier 4): `!command`, `/editor`, `/thinking`, `/details`, `/notify`, `/redo`, `/sessions` with `--continue` / `--resume`, export and import, `dcode stats`, helper sessions in the web UI, MCP servers with a lazy tool index, `dcode attach`, the OpenAPI document, `dcode acp` for Zed, a VS Code extension, and `.rpm` / tarball / Docker packages from one portable binary.
- **In 0.6.0 (2026-10-09 to 13):** more than one model. Named model profiles (each may be on its own server), a reasoning model that hard tasks are routed to, a model for small jobs, `/models`.
- **In 0.7.0 (October 15–16):** model families (detection, reasoning and tool calls per family, learned server quirks, a reasoning cut-off, one shell for every model), tested live on OpenRouter; and the **Dcode Manager** (gateway, users, limits, guards, discovery, monitoring, web UI, `dcode login`, `/usage`), tested against OpenRouter and deployed on the cluster (`dcode` namespace, NodePort 31010).
- **Quality bar:** 473 unit tests. `docs/dev/terminal_e2e.py` runs 99 real-terminal checks (every slash command). All pass from source on Linux x86_64 (Ubuntu 24.04, Python 3.12). The 0.6.0 binary (AlmaLinux 8 container) passed the 64 checks of that time; the earlier 31 also passed on macOS arm64.
- **Pending decision:** the user wanted **v1.0.0**; Claude recommended **1.0.0-rc.1** (see below). The user said "don't build it yet".

## Is it production-ready? Claude's honest assessment

**Not yet.** It's a solid, well-tested beta. What blocks a real 1.0:

1. **The real target is only half verified.** Dcode is meant for **air-gapped Linux with local ~32K models**. Linux packages now exist and install on RHEL 8/9-family, Debian 11/12 and Ubuntu 20.04 (in containers), but Dcode has still never run against Ollama, vLLM or llama.cpp with a real 7B–32B model. All testing so far: a mock model and one hosted endpoint.
2. **Real use keeps finding bugs:** the artifact never built, then the re-save loop, then duplicates, then the hub breaking after a session exit. Expect more.
3. **Untuned heuristics:** compaction thresholds, follow-through regexes, recall budgets, tool-call recovery.
4. **No CI**, and only macOS tested. Terminals checked: macOS Terminal and VS Code.
5. **Days old, with one user.** The commands and config format aren't stable yet.

## Path to 1.0

### 1.0.0-rc.1 — feature freeze
- [ ] Tag rc.1 from `dev`, and push the tags (`v0.3.0`, `v0.3.1`, `v1.0.0-rc.1`).
- [ ] Open a PR from `dev` to `main`.

### Validation round (what makes it 1.0)
- [x] **Linux build (x86_64):** `make packages` builds a portable binary and every package; installed in distribution containers. Still open: arm64, and an install on a real machine with no network.
- [ ] **Editors:** run `dcode acp` inside a real Zed and the extension inside a real VS Code (so far: the official ACP schema and a node stand-in).
- [ ] **MCP with real servers** the owner uses, and with a small model: does a 7B model reliably call `mcp_tools` before `mcp_call`?
- [x] **First real local models (2026-10-09):** vLLM with `gemma4:31b` (main) and `diffusiongemma-26B-A4B-it` (reasoning) at 16K: tool calls, edits, routing, plan mode and helpers work; one bug found and fixed (`edit_file` and the space after the line number).
- [ ] **Local models:** run real tasks with Ollama, llama.cpp (`llama-server --parallel N`) and vLLM, using at least Qwen2.5-Coder 7B and 32B and a DeepSeek-style model, at 16K and 32K context. Record the failures.
- [ ] **The router with real models:** does it send the right tasks to the reasoning model? Tune the keyword list and the utility prompt from real sessions; check a 16K reasoning model taking over a long 32K chat.
- [ ] Tune from that data: compaction thresholds, `MAX_FOLLOW_UPS`, the `PROMISE` / `ARTIFACT_REQUEST` patterns, output caps, helper step limits.
- [ ] Test terminals: iTerm2, GNOME Terminal, tmux and screen, an SSH session, a narrow 80-column terminal, and a light background.
- [ ] Test multiple users on one Linux server: per-user hubs, ports and token isolation.
- [ ] Test long sessions: hours, compaction many times, resume.
- [ ] **CI:** GitHub Actions running `pytest` and `ruff` on Linux and macOS, plus `docs/dev/terminal_e2e.py` against a built binary.
- [ ] Freeze the config format and slash commands; document any migration.

### 1.0.0
- [ ] Fix everything from the validation round, then cut 1.0.0 with a changelog entry and push the tag.

## Backlog and ideas (not committed to)

- **Databases:** cancel a running query from the UI; more engines (SQL Server, Oracle, ClickHouse) if needed; SSH tunnels; a real `pg_dump`-style export; per-connection row limits; pagination in the data grid; saved queries.
- **Scheduled jobs:** start the background service at boot (a systemd user unit) so jobs survive reboots; catch up one missed run; notify on failure; let a job's output become an artifact.

- **Web UI:** background jobs panel (start/stop/view logs); diff viewer for file edits; plan-mode editing before approval; true phone-width testing (headless Chrome won't render below 500 px).
- **Artifacts:** export bundle (zip); share an artifact between users on a shared server (opt-in); Mermaid diagrams, which would need a bundled renderer because there are no CDNs.
- **Agent:** per-project `DCODE.md` editor in the UI; better recovery when the server drops connections mid-stream.
- **From the opencode comparison:** Tier 3 and Tier 4 are built (Phase 4). Follow-ups: stream answers to ACP editors; `session/load` so an editor can reopen a saved chat; MCP resources and prompts (only tools are used); MCP over HTTP if a site needs it; `dcode attach` to switch sessions without leaving; an arm64 build; publishing the extension to Open VSX.
- **Memory:** a forget/expire policy; UI to review what was recalled for a task.
- **Packaging:** a Homebrew formula or tap for macOS; `--onedir` builds for faster startup; signed and notarized macOS binary.
- **Security:** permission rules and hooks now cover allow/deny lists and audit logs; still open: a built-in audit log view in the web UI.

## Open housekeeping

- `Screenshot 1405-07-12 at 16.21.19.png` sits in the repo root, untracked. It's the user's screenshot of the duplicate-artifact bug. Delete it or move it out.
