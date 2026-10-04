# Architecture

Dcode is a coding agent for local and air-gapped LLMs. One Python package (`dcode/`, about 10k lines including the web assets) provides a terminal app, a per-user localhost web UI, and an agent engine that both of them drive.

```
dcode (terminal) ──┐                         ┌── browser (per OS user)
                   ▼                         ▼
            Session ◄── session API ◄── Hub  127.0.0.1:<7300+uid%500>
   event bus · task queue · approvals     serves UI, settings, memory, artifacts,
   modes · checkpoints · jobs · threads   history; proxies /api/s/<id>/… (incl. SSE)
                   │
             Agent (main) ── delegate ──► helpers (explore · code · review · test)
                   │                       fresh context, role tools, capped report
            LLMClient ──► any OpenAI-compatible server (Ollama, vLLM, llama.cpp, LM Studio, TGI)
```

## Modules

| Module | Responsibility |
|---|---|
| `main.py` | click entry point, setup wizard, signal handling, starting/joining the hub, `dcode ui`, hidden `dcode hub` |
| `cli.py` | `TerminalApp`: a state-driven loop (approval → busy → plan approval → input), event rendering, slash commands, live activity panel, `/jobs`, `/artifacts`, `/commit` |
| `ui.py` | rich and prompt_toolkit widgets: header, bottom input box (labeled top rule, mode badge, slash suggestions, Shift+Tab, Ctrl+O, Ctrl+C×2), selectors, approval menu, diffs, links |
| `theme.py` | **the single source of colors**: web `LIGHT`/`DARK` token dicts (served as `/theme.css`), terminal colors, logo |
| `session.py` | `Session`: worker thread and queue, `EventBus` with replay, approvals (first answer wins; once or for the session), modes, plan approval, `/compact`, undo, auto titles, thread autosave, jobs |
| `engine.py` | `Agent`: the loop, tool dispatch, approvals by mode, delegation, context compaction, follow-through checks, end-of-task report, activity tracking, artifact dedupe, checkpoints |
| `agents.py` | prompts, roles, role tool sets, modes, extra tool schemas (delegate, memory, artifacts, skills, jobs) |
| `llm.py` | `LLMClient`: streaming and non-streaming, learned server quirks, `<think>` stripping, text tool-call recovery, tool-name cleaning, `finish_reason` |
| `context.py` | `TokenCounter` (calibrated heuristic), `ContextBudget`, `compact()` (elide → summarize → drop) |
| `tools.py` | sandboxed file/shell/git tools, terse schemas, output caps scaled by `configure_limits()` |
| `memory.py` | Markdown memories (project/global) with BM25 search, `recall`, `index_text`, session summaries |
| `artifacts.py` | artifact store: versions, append, rename, duplicate, restore |
| `skills.py` | built-in skill seeding, user and project skills, `save_skill` |
| `checkpoints.py` | per-task file snapshots for `/undo` |
| `jobs.py` | background jobs (process group, log file, exit callback) |
| `server.py` | `SessionServer` (per session), `Hub` (per user), registry, token, hub lifecycle (`ensure_hub`, `hub_usable`, `_retire_hub`, clean child env) |
| `config.py` | `AppConfig`, paths (`DCODE_HOME`), env overrides, legacy migration, threads |
| `web/` | `index.html`, `app.js`, `app.css` (main UI), `artifact.html` (`/a/<id>`), `guide.html` (`/guide`), `logo.svg` |
| `skills/*.md` | built-in skills (bundled, seeded once into `~/.dcode/skills`) |

## The agent loop (`Agent.run_turn`)

1. Recall memories into the system prompt, start a checkpoint, and reset per-task state (`_activity`, `_artifact_saves`).
2. Loop until done:
   - Check for interrupt.
   - Pick tools by mode and role; when the step limit is reached, offer no tools and ask for a wrap-up.
   - Compact the history if it's over 75% of the input budget.
   - Call `llm.chat(...)`, streaming deltas as events.
   - **No tool calls:** nudge once if the reply is empty. Otherwise, if `_follow_up()` finds a reason (cut off, requested artifact missing, or a promised action), remind the model, at most twice. Otherwise the reply is final.
   - **Tool calls:** run them, with consecutive read-only `delegate` calls in parallel. Each result is recorded, tracked for the report, and large artifact/skill payloads are stubbed out of history.
3. If the model never wrote an answer, run `_final_report()`: ask the model, or fall back to the activity log.
4. Emit `assistant_message`, then `usage` (elapsed time, context, files changed, commands, helpers).

## Approvals and modes

| Mode | File edits | Commands / deletes / commits |
|---|---|---|
| `accept_edits` (default) | free | ask |
| `auto` | free | free (commits still need the user to ask) |
| `plan` | blocked | blocked; read-only tools and explore/review helpers only, then the plan approval menu |
| `manual` | ask, with a diff preview | ask |

`Session.request_approval` emits `approval_request` and waits. Either the terminal (menu) or the web UI (card) resolves it; the first answer wins. "Allow for this session" adds the kind to `Session.allowed_kinds`.

## Events (`EventBus`)

All UI comes from events, which the web UI can replay from `seq=0`:

| Group | Events |
|---|---|
| Conversation | `user_message` · `assistant_delta` · `assistant_note` · `assistant_message` |
| Steps | `tool_call` · `tool_result` |
| Helpers | `agent_start` · `agent_end` |
| Approvals | `approval_request` · `approval_resolved` · `approval_auto` |
| Status | `status` · `usage` · `compaction` · `summary` · `notice` · `error` · `config` |
| Chat | `thread` (new, resume, rename, title) · `mode` · `plan_ready` · `plan_resolved` · `undo` |
| Stored items | `memory_saved` · `skill_saved` · `artifact_saved` (with `update`) |
| Jobs | `job_started` · `job_exited` |

## Hub and web UI

- **Registry:** every `dcode` runs a `SessionServer` on an ephemeral port and registers it in `~/.dcode/run/sessions/<id>.json`.
- **Starting the hub:** the first session spawns `dcode hub`, detached and with a clean environment. Later sessions reuse it if `hub_usable()` (same version, assets present); otherwise they replace it. The hub exits about 6 seconds after the last session ends.
- **Security:** binds 127.0.0.1 only. A per-user token is stored in `~/.dcode/run/token` (mode 0600). The browser gets an HttpOnly, SameSite=Strict cookie lasting 30 days. Mutations require an `X-Dcode: 1` header, and a Host-header check blocks DNS rebinding. Artifacts render in `sandbox="allow-scripts"` iframes with a CSP of `default-src 'none'`. Downloads are served as `application/octet-stream`.
- **API:**
  - Hub, for the browser: `/api/{hub,sessions,threads,config,models,memory,skills,artifacts}`
  - Proxied to a session: `/api/s/<id>/{state,events,message,approve,interrupt,new,rename,resume,model,reload,mode,plan,compact,undo}`
  - Public (no token): `/api/health`, `/guide`, `/static/logo.svg`

## Data on disk (`~/.dcode`, or `DCODE_HOME`)

```
config.json           0600 settings (env vars override)
threads/*.json        saved chats (messages + cwd/model/summary)
memory/global|projects/<name>-<hash>/*.md
skills/*.md           (+ .builtin-seeded marker)
artifacts/<id>.json   content + history (≤10 versions)
run/                  hub.json, token, sessions/, hub.log, hub.lock, jobs/*.log
```

## Small-context strategy (the core idea)

Every token of a 32K window counts. The strategy:
- Terse tool schemas.
- Read files by line range; edit instead of rewriting.
- Output caps of about 12% of the window per tool result.
- Helpers do the exploration in their own context.
- Memory and skills are indexed, not inlined; full text is loaded on demand.
- Artifact and skill payloads are stubbed out of history after saving.
- Compaction starts at 75% of the input budget.
- The reply budget is at most a quarter of the window.
