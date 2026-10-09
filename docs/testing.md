# Testing

Three layers. Run the first always, the second for any terminal or packaging change, and the third when layout or colors matter.

## 1. Unit tests (`tests/`, about 268 tests, about 45 s)

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check dcode tests
```

- `tests/conftest.py` gives every test its own `DCODE_HOME` and workspace (`isolated` fixture), and provides **`FakeLLM`**, a scripted stand-in for `LLMClient`. Use `scripted(AssistantTurn(...), ...)` for a fixed sequence, or a `router(messages, tools)` function to answer based on the conversation (for example, by the system prompt of a helper role).
- `call(name, json_args, id)` builds a tool call.
- Server tests start a real `SessionServer` and `Hub` in-process on ephemeral ports (the `stack` fixture) and talk HTTP to them.
- **Regression tests replay real bugs.** Keep that habit:
  - `test_announced_artifact_without_tool_call_is_followed_up`
  - `test_artifact_resave_loop_is_stopped_and_untitled_saves_update`
  - `test_one_artifact_per_chat_across_tasks`
  - `test_write_file_refuses_to_erase_existing_content`
  - `test_leaked_markup_is_stripped_from_tool_calls`
  - `test_hub_child_env_does_not_reuse_parent_unpack_dir`
  - `test_edit_file_tolerates_the_space_after_the_line_number` (the first bug a real local model found)

| File | Covers |
|---|---|
| `test_config.py` | config, env overrides, legacy env, threads |
| `test_tools.py` | sandbox, ranged reads, edit_file, search, limits, empty-write guard |
| `test_context.py` | token counter, elision, compaction keeps tool pairs valid |
| `test_memory.py` | memory store and BM25 |
| `test_llm.py` | text tool-call recovery, `<think>`, markup cleaning |
| `test_engine.py` | loop, approvals, parallel helpers, repeat guard, step limit, reports, id dedupe |
| `test_session.py` | queue, approvals from either surface, interrupt repair, titles, allow-for-session |
| `test_modes.py` | modes, checkpoints/undo, force compact, plan flow |
| `test_artifacts_skills.py` | artifacts, skills, stubbing payloads out of context |
| `test_followthrough_jobs.py` | follow-through, cut-off continuation, append, jobs, save loop, one artifact per chat |
| `test_db.py` | SQL splitting and classifying, connections and passwords, schema and summary, row limits, change confirmation, timeouts, exports and the heavy-table guard, the model's `db_*` tools in every mode (on SQLite) |
| `test_schedules.py` | cron expressions, job validation, runs and logs, time limit, trimming, the scheduler (due, once a minute, overlap, run now), `dcode run`, hub keep-alive, the cron API |
| `test_terminal_extras.py` | `!command`, reasoning kept out of the context, `/editor`, the bell, `--continue` / `--resume`, `/help` brackets |
| `test_transcripts.py` | export to Markdown / HTML / JSON, import validation, `dcode export` and `dcode import` |
| `test_stats.py` | what a finished task records, summaries, the size cap, `dcode stats` |
| `test_helper_sessions.py` | helper steps kept, capped, saved with the chat and restored |
| `test_models.py` | model profiles, the router (keywords, the utility model's verdict, follow-ups, plan mode), the budget following the model, helpers on a profile, API keys never sent back, `/models` |
| `test_mcp.py` | the MCP client against `tests/fixtures/fake_mcp_server.py`: handshake, paging, timeouts, crashes; the names-only index; `mcp_tools` / `mcp_call`; approvals and plan mode |
| `test_openapi_attach.py` | every documented route exists; `dcode attach` follows and drives a session |
| `test_acp.py` | `dcode acp` with a scripted editor: updates, diffs, plan, approvals, questions, modes, cancel; the `.vsix` packs |
| `test_server.py` | auth, CSRF, Host check, proxy and SSE, config/memory/threads/artifacts/skills API, guide, hub lifecycle, health |

## 2. Real-terminal end-to-end (`docs/dev/terminal_e2e.py`)

Drives the real `dcode` in a pseudo-terminal against **`docs/dev/mock_llm.py`**, a scripted OpenAI-compatible server. Its docstring lists the trigger words for each scenario. 64 checks, covering:
- the web UI link and hub, the bilingual guide
- streaming and `<think>` stripping, tool calls, `/version`, Shift+Tab
- approvals answered from the web and in the terminal
- Ctrl+O, helpers, follow-through, the re-save cap
- `/compact`, Ctrl+C twice, hub shutdown, thread saved
- databases: `/db add` and queries on a SQLite file, the model's `db_query`, a change that asks even in auto mode and is not run when denied
- scheduled jobs: `/cron add`, a run started by the hub with its log, `/cron rm`
- Phase 4: `!command`, `/thinking`, `/details`, `/editor`, `/export`, a local MCP server (add, a call that asks, remove), `dcode attach` in a second pty (message, approval, detach), `dcode --continue` and `/sessions` in a second run
- more than one model: `/models add` with a second mock server, a hard task answered by that server, an easy one by the main model
- Phase 3: `@` completion and attach, the checklist, the question menu, the post-edit check, `/rules`, a custom `/command`, `@helper`

`--verbose` prints the last screen when a check fails.

```bash
python docs/dev/terminal_e2e.py                                  # venv build
python docs/dev/terminal_e2e.py --binary /opt/homebrew/bin/dcode # after `make mac`
```

**Run it against the built binary before every release.** Packaging bugs, like the hub's unpack folder disappearing, only show up there.

Gotchas this harness handles (keep them in mind when writing new pty tests):
- Set `PROMPT_TOOLKIT_NO_CPR=1`. Without it, prompt_toolkit waits on cursor-position replies that a pty never sends.
- Set `BROWSER=true` so `/ui`, `/guide` and `/artifacts` don't open real browser tabs.
- Raw captures strip cursor movement, so an in-place redraw looks like duplicated output. Check behavior, not counts.
- On macOS, the repo's `.venv` editable install only imports from the repo root (hidden `.pth` files). The harness sets `PYTHONPATH`.
- The binary starts slower (it unpacks itself and spawns the hub). Wait about 8–12 s.

### Real database servers

The unit tests use SQLite, which needs nothing. PostgreSQL and MySQL/MariaDB were checked by hand against throwaway Docker containers (`postgres:16-alpine`, `mysql:8.4`, `mariadb:11`) with a restricted user: schema, limits, timeouts, read-only refusal of disguised writes, permission errors, exports. Do the same after touching `db.Link`:

```bash
docker run -d --rm --name dcode-test-pg -e POSTGRES_PASSWORD=x -p 127.0.0.1:55432:5432 postgres:16-alpine
# then: /db add t postgres://postgres:x@127.0.0.1:55432/postgres
```

## 3. Rendered screen and colors (`docs/dev/screen.py`)

```bash
pip install pyte                       # dev-only
python docs/dev/screen.py --type "say hello" --type "list files"
python docs/dev/screen.py --html /tmp/s.html && open /tmp/s.html
```

Feeds the pty output into a terminal emulator (`pyte`) and prints the *final* screen. With `--html` it writes colors too. This is how the layout and the "white text, blue structure" palette were verified.

## Web UI visual checks

Headless Chrome screenshots work, with caveats:
- `--virtual-time-budget` never finishes on pages with a live SSE stream. Use `--timeout=6000` with a wall-clock kill instead.
- Late DOM changes (iframes added after fetches) may not be painted in `--timeout` screenshots. Verify sandboxed content on the standalone `/a/<id>` page, or in a static harness page, with `--virtual-time-budget`.
- The minimum viewport width is 500 px, so true phone width can't be tested this way.
- `--blink-settings=preferredColorScheme=1` forces light mode; `--force-dark-mode` forces dark.
- Deep links help: `#settings:skills`, `#memory`, `#artifacts`, `#a=<id>`, `#s=<session>`, `#t=<thread>`.

## Checks that need something from outside

These are not in the test suite (they need docker or a download), but each was run for Phase 4 and is worth repeating after related changes:

- **MCP against the official SDK:** `pip install mcp` in a scratch venv, write a two-tool `MCPServer`, and drive it with `mcp.Manager` (handshake, `tools/list`, calls, a rejected argument).
- **ACP against the official schema:** download `schema/v1/schema.json` from the `agentclientprotocol/agent-client-protocol` repository and validate every `session/update`, `session/request_permission` and response that a real `dcode acp` process sends (driven by `mock_llm.py`). `SessionNotification`, `RequestPermissionRequest`, `InitializeResponse`, `NewSessionResponse` and `PromptResponse` are the definitions to use.
- **The VS Code extension in node:** `node --check extension.js`, then load it with a stand-in `vscode` module on `NODE_PATH` and call each command against a fake `dcode ui` script and a local HTTP server. This is not a run inside VS Code.
- **Packages in containers:** install the `.rpm` in `almalinux:8` and `almalinux:9`, the `.deb` in `debian:11`, `debian:12` and `ubuntu:20.04`, the tarball as a non-root user, and run the Docker image; then run `terminal_e2e.py --binary dist/dcode`.
