# Roadmap and plans

## Current status (October 5, 2026)

- **Released:** v0.3.1, tagged locally and pushed to `origin/dev`. The tags `v0.3.0` and `v0.3.1` are **not pushed** yet.
- **After 0.3.1** (committed, unreleased): the fix for the web UI breaking after the starting session exits (clean hub environment, version and asset health check), and these docs.
- **Quality bar:** 110 unit tests. `docs/dev/terminal_e2e.py` runs 22 real-terminal checks, all passing on macOS arm64.
- **Pending decision:** the user wanted **v1.0.0**; Claude recommended **1.0.0-rc.1** (see below). The user said "don't build it yet".

## Is it production-ready? Claude's honest assessment

**Not yet.** It's a solid, well-tested beta. What blocks a real 1.0:

1. **The real target is unverified.** Dcode is meant for **air-gapped Linux with local ~32K models**. A Linux binary or `.deb` has never been built or installed, and Dcode has never run against Ollama, vLLM or llama.cpp with a real 7B–32B model. All testing so far: macOS, a mock model, and one hosted endpoint.
2. **Real use keeps finding bugs:** the artifact never built, then the re-save loop, then duplicates, then the hub breaking after a session exit. Expect more.
3. **Untuned heuristics:** compaction thresholds, follow-through regexes, recall budgets, tool-call recovery.
4. **No CI**, and only macOS tested. Terminals checked: macOS Terminal and VS Code.
5. **Days old, with one user.** The commands and config format aren't stable yet.

## Path to 1.0

### 1.0.0-rc.1 — feature freeze
- [ ] Tag rc.1 from `dev`, and push the tags (`v0.3.0`, `v0.3.1`, `v1.0.0-rc.1`).
- [ ] Open a PR from `dev` to `main`.

### Validation round (what makes it 1.0)
- [ ] **Linux build:** `make deb` on Ubuntu x86_64 (and arm64 if relevant); install with `packaging/install-airgap.sh` on a machine with no network.
- [ ] **Local models:** run real tasks with Ollama, llama.cpp (`llama-server --parallel N`) and vLLM, using at least Qwen2.5-Coder 7B and 32B and a DeepSeek-style model, at 16K and 32K context. Record the failures.
- [ ] Tune from that data: compaction thresholds, `MAX_FOLLOW_UPS`, the `PROMISE` / `ARTIFACT_REQUEST` patterns, output caps, helper step limits.
- [ ] Test terminals: iTerm2, GNOME Terminal, tmux and screen, an SSH session, a narrow 80-column terminal, and a light background.
- [ ] Test multiple users on one Linux server: per-user hubs, ports and token isolation.
- [ ] Test long sessions: hours, compaction many times, resume.
- [ ] **CI:** GitHub Actions running `pytest` and `ruff` on Linux and macOS, plus `docs/dev/terminal_e2e.py` against a built binary.
- [ ] Freeze the config format and slash commands; document any migration.

### 1.0.0
- [ ] Fix everything from the validation round, then cut 1.0.0 with a changelog entry and push the tag.

## Backlog and ideas (not committed to)

- **Web UI:** background jobs panel (start/stop/view logs); diff viewer for file edits; plan-mode editing before approval; true phone-width testing (headless Chrome won't render below 500 px).
- **Artifacts:** export bundle (zip); share an artifact between users on a shared server (opt-in); Mermaid diagrams, which would need a bundled renderer because there are no CDNs.
- **Agent:** per-project `DCODE.md` editor in the UI; an MCP-like plugin tools interface; better recovery when the server drops connections mid-stream.
- **Memory:** a forget/expire policy; UI to review what was recalled for a task.
- **Packaging:** a Homebrew formula or tap for macOS; `--onedir` builds for faster startup; signed and notarized macOS binary.
- **Security:** an optional allowlist or denylist for shell commands in auto mode; an audit log of approved commands.

## Open housekeeping

- `Screenshot 1405-07-12 at 16.21.19.png` sits in the repo root, untracked. It's the user's screenshot of the duplicate-artifact bug. Delete it or move it out.
- The GitHub repo is still named `code-one-cli`; rename it in GitHub settings if wanted.
