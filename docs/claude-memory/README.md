# Claude Code session memory

A copy of Claude Code's memory files for this project, taken on 2026-10-07, so work can continue on another machine with the same context.

| File | What it holds |
|---|---|
| `MEMORY.md` | the index Claude loads at the start of every session |
| `dcode-project-goals.md` | goals, phases and current status |
| `git-and-release-workflow.md` | commit in parts on `dev`, push only when asked, the owner picks versions |
| `dcode-ui-preferences.md` | how the terminal and web UI should look |
| `macos-editable-install-hidden-pth.md` | the macOS `.pth` quirk and its workaround |

## Restore on another machine

Claude Code keeps memory per project folder, under a name made from the folder's full path with every `/` and space turned into `-`. After cloning the repo:

```bash
cd /path/to/code-one-cli
DEST=~/.claude/projects/$(pwd | sed 's|[/ ]|-|g')/memory
mkdir -p "$DEST" && cp docs/claude-memory/*.md "$DEST"/ && rm "$DEST/README.md"
```

Then start Claude Code in the repo folder. `CLAUDE.md` in the repo root is picked up by itself.

## What else to read

- [`../history.md`](../history.md): every phase, request, feature and bug fix, in order
- [`../roadmap.md`](../roadmap.md): current status, the path to 1.0, and the backlog (including Tier 3–4 from the opencode comparison)
- [`../memory.md`](../memory.md): the owner's preferences and working agreements, in prose
- [`../../CHANGELOG.md`](../../CHANGELOG.md): the "Unreleased" section lists the Phase 3 work

The raw chat transcript is not in the repo. It is about 27 MB and holds login links and pasted terminal output. It stays on the first machine at `~/.claude/projects/<project>/*.jsonl`.
