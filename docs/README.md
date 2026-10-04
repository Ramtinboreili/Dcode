# Dcode documentation

Developer and project documentation. For **end users**, the bilingual user guide (English / فارسی) is [`dcode/web/guide.html`](../dcode/web/guide.html), which the web UI also serves at `/guide`.

| Document | What's in it |
|---|---|
| [history.md](history.md) | How code-one became Dcode: every phase, request, feature and bug fix, in order |
| [architecture.md](architecture.md) | Modules, the agent loop, modes and approvals, events, hub and web API, data on disk |
| [decisions.md](decisions.md) | Why things are built the way they are (air-gapped, small models, UX, process) |
| [roadmap.md](roadmap.md) | Current status, honest production-readiness assessment, path to 1.0, backlog |
| [testing.md](testing.md) | Unit tests, real-terminal e2e, rendered-screen checks, web screenshot tips |
| [operations.md](operations.md) | Dev setup, builds, release checklist, air-gapped install, runtime, troubleshooting |
| [memory.md](memory.md) | What we know about the owner's preferences, environment and working agreements |
| [dev/](dev/) | Test tools: `mock_llm.py` (scripted model), `terminal_e2e.py` (22 pty checks), `screen.py` (pyte renderer) |

Also see [`../CLAUDE.md`](../CLAUDE.md) (instructions for AI agents working on this repo), [`../CHANGELOG.md`](../CHANGELOG.md) and [`../README.md`](../README.md).
