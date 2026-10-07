---
name: git-and-release-workflow
description: "User's git/release agreements for Dcode — commit in parts on dev, push only when asked, user picks versions"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 8ea51d4c-aca8-475a-be52-47f40c3dac22
  modified: 2026-10-04T21:57:10.024Z
---

Commit work in logical parts on the `dev` branch; never push (branch or tags) unless the user explicitly asks; the user decides version numbers (asked for 0.2.1, 0.3.0, 0.3.1; for 1.0.0 Claude recommended an RC first — ask before tagging 1.0).

**Why:** user repeatedly asked "commit part by part" and issued separate "push it" requests; they also asked for an honest readiness assessment before 1.0.
**How to apply:** follow the release checklist in the repo's CLAUDE.md; see [[dcode-project-goals]] and [[dcode-ui-preferences]].
