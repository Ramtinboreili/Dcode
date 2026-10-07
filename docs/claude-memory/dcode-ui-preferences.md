---
name: dcode-ui-preferences
description: "User's taste for Dcode's CLI and web UI — CC-like layout with own identity, white text/blue structure, one artifact per chat, quiet output"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 8ea51d4c-aca8-475a-be52-47f40c3dac22
  modified: 2026-10-04T21:57:10.038Z
---

CLI: Claude Code-like layout (input pinned at bottom between rules, status line) but with Dcode's own identity; white text, blue only on structure; quiet output (one line per artifact save, no raw JSON); show helpers/background jobs live; end every task with a report. Web: ChatGPT-like, white+blue, light/dark, humanized. Artifacts: one per chat, updated in place.

**Why:** user explicitly asked for each (said "too like claude code", "blue is too much", "i just want one artifact").
**How to apply:** verify visuals on rendered screens (docs/dev/screen.py --html, headless Chrome) before claiming done. Related: [[git-and-release-workflow]].
