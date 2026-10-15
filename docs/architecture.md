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
| `__main__.py` | the real entry point: `hub`, `run` and `acp` skip the terminal app's imports, everything else goes to `main.cli` |
| `main.py` | click entry point, setup wizard, signal handling, starting/joining the hub, `--continue` / `--resume`, and the subcommands `ui`, `attach`, `stats`, `export`, `import`, hidden `hub` |
| `cli.py` | `TerminalApp`: a state-driven loop (approval → busy → plan approval → input), event rendering, slash commands, live activity panel, `/jobs`, `/artifacts`, `/commit` |
| `ui.py` | rich and prompt_toolkit widgets: header, bottom input box (labeled top rule, mode badge, slash suggestions, Shift+Tab, Ctrl+O, Ctrl+C×2), selectors, approval menu, diffs, links |
| `theme.py` | **the single source of colors**: web `LIGHT`/`DARK` token dicts (served as `/theme.css`), terminal colors, logo |
| `session.py` | `Session`: worker thread and queue, `EventBus` with replay, approvals (first answer wins; once or for the session), modes, plan approval, `/compact`, undo, auto titles, thread autosave, jobs |
| `engine.py` | `Agent`: the loop, tool dispatch, approvals by mode, delegation, context compaction, follow-through checks, end-of-task report, activity tracking, artifact dedupe, checkpoints |
| `agents.py` | prompts, roles, role tool sets, modes, extra tool schemas (delegate, memory, artifacts, skills, jobs) |
| `llm.py` | `LLMClient`: streaming and non-streaming, request shaping per family, what the server taught it (`_learned`), runaway-reasoning cut-off, `<think>` stripping, tool-name and argument cleaning, `finish_reason` |
| `families.py` | model families: `detect()` from the name and server, `request_extras()` turns `reasoning_effort` into the family's own fields |
| `toolcalls.py` | tool calls written as text, in every family's format (`parse`) |
| `shell.py` | `run_command` the same for every model: `clean_command`, `refusal` (no terminal), `run` (bash, no stdin, process-group timeout) |
| `models.py` | more than one model: named profiles (own server, window, key), `ModelPool` (profile → config + client, clients reused), the task router (`decide`, `heuristic_route`) |
| `context.py` | `TokenCounter` (calibrated heuristic), `ContextBudget`, `compact()` (elide → summarize → drop) |
| `tools.py` | sandboxed file/shell/git tools, terse schemas, output caps scaled by `configure_limits()` |
| `memory.py` | Markdown memories (project/global) with BM25 search, `recall`, `index_text`, session summaries |
| `artifacts.py` | artifact store: versions, append, rename, duplicate, restore |
| `skills.py` | built-in skill seeding, user and project skills, `save_skill` |
| `checkpoints.py` | per-task file snapshots for `/undo`, and what each undo replaced for `/redo` |
| `transcripts.py` | saved chats → UI events; export to Markdown / one HTML page / JSON; `import_chat` (untrusted input); `HelperLog` (each helper's own steps, saved with the chat) |
| `stats.py` | one line per finished task in `stats.jsonl` (counts only) and the summary for `dcode stats` |
| `mcp.py` | MCP stdio client: `Server` (one process, JSON-RPC, timeouts), `Manager` (per session), the names-only prompt index, `describe` for `mcp_tools` |
| `attach.py` | `dcode attach`: `RemoteSession` answers what `TerminalApp` asks of a session over the hub's HTTP API; `AttachApp` is the terminal |
| `acp.py` | `dcode acp`: Agent Client Protocol v1 over stdio; `Bridge` turns a session's events into `session/update` and approvals into `session/request_permission` |
| `openapi.py` | the hub's API as an OpenAPI 3.1 document, from one `ROUTES` table |
| `checks.py` | post-edit check (in-process Python/JSON/TOML; ruff, bash, node, gofmt, ruby, php when installed) and optional formatter; errors are appended to the edit result |
| `mentions.py` | `@path` parsing (attached as synthetic `read_file` results) and the fuzzy file index for completion |
| `custom_agents.py` | `AgentSpec`: built-in helpers plus Markdown helpers from `~/.dcode/agents` and `.dcode/agents` (tools, model, steps, prompt) |
| `commands.py` | custom slash commands from `~/.dcode/commands` and `.dcode/commands` (`$ARGUMENTS`, `$1`–`$9`, `` !`shell` ``) |
| `rules.py` | `RuleSet`: allow/ask/deny by pattern for commands and files, outside-workspace policy, project rules that can only tighten |
| `hooks.py` | user hooks: `before_tool` (can block), `after_tool`, `task_done`, `approval_needed` |
| `jobs.py` | background jobs (process group, log file, exit callback) |
| `db.py` | saved database connections; `Link` (one open connection, the only engine-specific code); schema, summary, guarded `run_query`, streaming exports; text for the model |
| `schedules.py` | scheduled jobs: cron parser, jobs and runs on disk, `run_job` (log, time limit), `Scheduler` (a thread in the hub) |
| `headless.py` | `dcode run`: a `Session` with no terminal; approvals are denied, events are printed as plain text |
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
   - Call `llm.chat(...)`, streaming deltas as events. (Before the loop, `@mentions` in the user's message are attached as synthetic `read_file`/`list_files` calls and results.)
   - **No tool calls:** nudge once if the reply is empty. Otherwise, if `_follow_up()` finds a reason (cut off, requested artifact missing, or a promised action), remind the model, at most twice. Otherwise the reply is final.
   - **Tool calls:** run them, with consecutive read-only `delegate` calls in parallel. Each result is recorded, tracked for the report, and large artifact/skill payloads are stubbed out of history.
3. If the model never wrote an answer, run `_final_report()`: ask the model, or fall back to the activity log.
4. Emit `assistant_message`, then `usage` (elapsed time, context, files changed, commands, helpers).

## More than one model

- **Profiles** live in `config.models.profiles`: a name → model, and optionally its own server URL, API key, context window, reply length and reasoning effort. A role names a profile, or a bare model name on the main server; empty means the main model.
- **Roles:** `models.reasoning` (hard tasks), `models.utility` (titles, commit messages, the router's check), and for helpers `agents.helper_model` or a helper file's `model:`.
- **Routing** happens in `Session._run_task`, once per task: `models.decide()` asks the utility model for one word (HARD or EASY) when one is set, else uses keywords, error traces and length. Short follow-ups keep the previous task's model. Plan mode and the "plan approved" task are never routed.
- **The swap:** `Agent.use(config, llm)` switches the client, the context budget and the tool output caps for that task; the history stays. Afterwards the session switches back to its *current* main config. A `route` event tells both UIs.
- **Helpers** resolve their model through `host.models` (the pool), so a helper gets the right window as well as the right server.

## Model families

Each family reasons and calls tools its own way. `families.detect(model, base_url, override)` returns a `Family`; `LLMClient` reads it on every request (so `/model` and `/models family` take effect at once).

- **Request:** `_shape()` adds the family's reasoning fields (`request_extras`), picks `max_tokens` or `max_completion_tokens`, and `_prepare()` rewrites a *copy* of the history when the API needs it: reasoning back on the steps of the current task (DeepSeek reasoner, Kimi thinking), 9-character call ids (Mistral), no system role, strict turn order, Gemini signatures, or tools as text.
- **Learning:** `_learn()` reads a rejected request's error and changes one thing (drop a field, rename one, change the history's shape, switch to text tools), then the request goes again, at most six times. What a server refuses carries over to other models on it; what a model needs resets when the model changes. Context overflows and missing models teach nothing.
- **Reasoning:** while streaming, reasoning that passes 75% of the reply cap or repeats itself raises `_Overthinking`; `chat()` asks again one effort lower (straight to none for a loop), at most twice, only for families where effort changes anything. The `notes` on the turn become info notices.
- **Text tool calls** are parsed for every family (`toolcalls.parse`), whatever was detected: a wrong guess must never lose a call. The engine then maps argument names (`tools.normalize_call`, before approval) and `llm.coerce_arguments` gives each its schema type.

## Approvals and modes

| Mode | File edits | Commands / deletes / commits |
|---|---|---|
| `accept_edits` (default) | free | ask |
| `auto` | free | free (commits still need the user to ask) |
| `plan` | blocked | blocked; read-only tools and explore/review helpers only, then the plan approval menu |
| `manual` | ask, with a diff preview | ask |

**Rules** (`rules.py`) sit on top: a matching `allow` skips the prompt, `ask` always asks (kind `rule`, even in auto mode), `deny` refuses. Chained commands are judged part by part. Denied files are blocked inside `tools._safe_path` (so every tool and `@mentions` respect them); paths outside the workspace ask (kind `outside_workspace`) and are then allowed for that one call via a thread-local. **Hooks** run around every tool call (`before_tool` can block) and on `task_done` / `approval_needed` (in background threads).

`Session.request_approval` emits `approval_request` and waits. Questions from `ask_user` use the same channel with kind `question`, `options`, and an `answer` on resolve. Either the terminal (menu) or the web UI (card) resolves it; the first answer wins. "Allow for this session" adds the kind to `Session.allowed_kinds`.

## Databases

- **Connections** are saved in `~/.dcode/db/connections.json` (0600). The password is never returned by any API; a blank or masked password on save keeps the stored one.
- **`Link`** opens one connection per operation. Reads set the session read-only (`default_transaction_read_only`, `SET SESSION TRANSACTION READ ONLY`, `PRAGMA query_only`) and a statement timeout, then stream rows (a server-side cursor on PostgreSQL, an unbuffered cursor on MySQL). `classify()` only decides *which path* a statement takes; the read-only session is what actually stops a write.
- **Changes**: `run_query` raises `NeedsConfirmation` until called with `confirmed=True`. The engine turns that into a `db_write` approval that is asked in every mode and is excluded from "allow for this session" (`session.ALWAYS_ASK_KINDS`). The web UI and `/db` show the statement and ask.
- **The model's tools** (`db_schema`, `db_query`) and their prompt line exist only when a connection is saved; results are capped at half the tool-output budget.
- **Exports** go to `~/.dcode/db/exports` (or a workspace path with `save_as`), stop at the row and size limits, and refuse tables whose catalog estimate is above them unless forced.

## Scheduled jobs

- Jobs: `~/.dcode/cron/jobs.json`; runs and logs: `~/.dcode/cron/logs/<job>/` (`runs.json` + one `.log` per run, 50 kept).
- The hub owns a `Scheduler`: every 5 s it starts jobs whose expression matches the current minute (only in the first 20 s of it), and jobs flagged `run_requested` (how `/cron run` asks without talking HTTP).
- Each run is a child in its own process group with a clean environment; output is copied to the log (cut at 5 MB), and a timer kills the group at the time limit.
- A prompt job runs `dcode run --mode <mode> <prompt>` in the job's folder.
- The hub's idle watcher keeps it alive while any job is enabled or running.

## External tools, attach and editors

- **MCP:** `Session` starts a `mcp.Manager` when servers are configured; each `Server` is a child process spoken to over stdin/stdout (newline-delimited JSON-RPC). The engine sees it as `host.mcp` and offers two fixed tools, `mcp_tools` and `mcp_call`. A call asks (`mcp_tool` approval) unless the mode is auto or the server is marked `allow`; plan mode needs the tool's `readOnlyHint`. A failed or hung server sets a state, never raises into the loop.
- **Attach:** `dcode attach` has no session of its own. `RemoteSession` keeps `busy`, approvals, mode and the chat name current from the SSE stream (plus a 2-second `/state` poll), and posts actions with `source: "attach"`. `AttachApp` subclasses `TerminalApp`, so rendering is the same code.
- **ACP:** `dcode acp` owns real `Session`s (one per `session/new`; the process changes into that folder). Requests are handled on threads because `session/prompt` blocks until the turn ends while `session/cancel` and permission answers must still get through. Stdout carries protocol messages only.

## Events (`EventBus`)

All UI comes from events, which the web UI can replay from `seq=0`:

| Group | Events |
|---|---|
| Conversation | `user_message` · `assistant_delta` · `assistant_note` · `assistant_message` |
| Steps | `tool_call` · `tool_result` (both carry `by: "user"` for a `!command`) · `thinking` (the model's reasoning; display only) |
| Helpers | `agent_start` (with `model`) · `agent_end` |
| Phase 3 | `todos` · `mentions` · `check` |
| Approvals | `approval_request` · `approval_resolved` · `approval_auto` |
| Status | `status` · `usage` · `compaction` · `summary` · `notice` · `error` · `config` · `route` (a task went to the reasoning model: `model`, `reason`) |
| Chat | `thread` (new, resume, rename, title; a resume carries `items` and `helpers`) · `mode` · `plan_ready` · `plan_resolved` · `undo` · `redo` |
| Stored items | `memory_saved` · `skill_saved` · `artifact_saved` (with `update`) |
| Jobs | `job_started` · `job_exited` |

## Hub and web UI

- **Registry:** every `dcode` runs a `SessionServer` on an ephemeral port and registers it in `~/.dcode/run/sessions/<id>.json`.
- **Starting the hub:** the first session spawns `dcode hub`, detached and with a clean environment. Later sessions reuse it if `hub_usable()` (same version, assets present); otherwise they replace it. The hub exits about 6 seconds after the last session ends.
- **Security:** binds 127.0.0.1 only. A per-user token is stored in `~/.dcode/run/token` (mode 0600). The browser gets an HttpOnly, SameSite=Strict cookie lasting 30 days. Mutations require an `X-Dcode: 1` header, and a Host-header check blocks DNS rebinding. Artifacts render in `sandbox="allow-scripts"` iframes with a CSP of `default-src 'none'`. Downloads are served as `application/octet-stream`.
- **API:**
  - Hub, for the browser: `/api/{hub,sessions,threads,config,models,stats,memory,skills,agents,artifacts,db,cron}`; chats also have `/export`, `/helpers/<id>` and `POST /api/threads/import`
  - Proxied to a session: `/api/s/<id>/{state,events,files,helpers,mcp,message,approve,interrupt,new,rename,resume,model,reload,mode,plan,compact,undo,redo,init,command,mcp/restart}`
  - Public (no token): `/api/health`, `/api/openapi.json`, `/guide`, `/static/logo.svg`
  - The whole list, with parameters, is `openapi.py`; `test_every_documented_route_exists` keeps it honest.
- **Who did it:** a POST to a session may carry `"source": "attach"`. Events then say `by` / `source` = `cli`, `web`, `attach`, `acp` or `run`, and each terminal names the others ("from another terminal").

## Data on disk (`~/.dcode`, or `DCODE_HOME`)

```
config.json           0600 settings (env vars override); MCP servers are under "mcp"
threads/*.json        saved chats (messages + cwd/model/summary/todos/helpers)
stats.jsonl           0600 one line per finished task: counts only (capped at 4 MB)
memory/global|projects/<name>-<hash>/*.md
skills/*.md           (+ .builtin-seeded marker)
agents/*.md           your helpers        (projects: .dcode/agents/*.md)
commands/*.md         your slash commands (projects: .dcode/commands/*.md; .dcode/rules.json = stricter rules)
artifacts/<id>.json   content + history (≤10 versions)
db/connections.json   0600 saved database connections · db/exports/ exported files
cron/jobs.json        scheduled jobs · cron/logs/<job>/ runs.json + one log per run
run/                  hub.json, token, sessions/, hub.log, hub.lock, jobs/*.log
```

## Small-context strategy (the core idea)

Every token of a 32K window counts. The strategy:
- Terse tool schemas.
- Read files by line range; edit instead of rewriting.
- Output caps of about 12% of the window per tool result.
- Helpers do the exploration in their own context.
- Memory and skills are indexed, not inlined; full text is loaded on demand.
- MCP tools are indexed by name only; `mcp_tools` loads one tool's schema when it is needed.
- Artifact and skill payloads are stubbed out of history after saving.
- Compaction starts at 75% of the input budget.
- The reply budget is at most a quarter of the window.
