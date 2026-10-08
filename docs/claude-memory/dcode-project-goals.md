---
name: dcode-project-goals
description: "Dcode (ex code-one) goals — air-gapped coding agent for ≤32K open models, CLI + localhost web UI, phased roadmap"
metadata:
  node_type: memory
  type: project
  originSessionId: 8ea51d4c-aca8-475a-be52-47f40c3dac22
  modified: 2026-10-05T12:22:36.382Z
---

Dcode (renamed from code-one on 2026-10-03, Phase 1) is a coding agent mostly used in air-gapped environments against open-source models with ≤32K context.

Status 2026-10-05: v0.3.1 released on dev (Phase 2 = artifacts done); full docs in repo `docs/` + `CLAUDE.md`; user wanted v1.0.0, Claude recommended 1.0.0-rc.1 + Linux/local-model validation first (user: "don't build it yet").

2026-10-07: user asked to push everything to continue on another device — main fast-forwarded to dev and pushed with dev, tags v0.3.0/v0.3.1, a macOS arm64 binary in `builds/`, and these memory files in `docs/claude-memory/` (restore steps in its README).

Phase 3 (2026-10-05, on main and dev, still unreleased as a version — binary reports 0.3.1): user picked Tier 1+2 of an opencode comparison — post-edit checks, @file mentions, todo checklist, /init, helper model, custom helpers (.dcode/agents) + @name, custom commands (.dcode/commands), allow/ask/deny rules, hooks, ask_user. Tier 3–4 ideas are in docs/roadmap.md backlog.

Phase 4 (2026-10-08, on dev, unreleased, version still 0.4.0): the owner said "going for phase 3" after 0.4.0 shipped; asked what was meant, they chose Tier 3 and Tier 4 of the opencode comparison. Built: `!command`, `/editor`, `/thinking`, `/details`, `/notify`, `/redo`, `/sessions` + `--continue`/`--resume`, export/import, `dcode stats`, helper sessions in the web UI, MCP stdio servers with a names-only tool index, `dcode attach`, `/api/openapi.json`, `dcode acp` (Zed), a VS Code extension, and `.rpm`/tarball/Docker packages from a glibc-2.28 binary (`make packages`). Not yet run in a real Zed or VS Code, nor with a real local model. My interpretation: when the owner names a phase that is already done, ask which work they mean rather than guess.

Phase 1 asks (delivered 2026-10-03): rename to Dcode, blue theme, small-context performance, sub-agents/multi-agent, memory architecture, localhost web UI per user that controls live sessions (URL printed in CLI, hub stops when last session exits, history persists), CLI and UI share one design system.

**Why:** targets have no internet — no CDNs, no new pip deps, stdlib HTTP only.
**How to apply:** keep web assets self-contained, prefer stdlib, keep prompt/tool-schema tokens lean, and expect later phases ("PHASE 2", …) to build on engine/session/server split. See [[macos-editable-install-hidden-pth]].
