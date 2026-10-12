# Design decisions

Why Dcode is built the way it is. Each decision gives the choice and the reason; most reasons come from the air-gapped, small-model target or from a bug found in real use.

## Platform and dependencies
- **Standard-library HTTP, no web framework, no CDN, no web fonts.** The target machines have no internet. The web UI is plain HTML, CSS and JavaScript, and the guide uses system fonts (with Vazirmatn when installed for Persian).
- **No embedding model for memory.** BM25 over Markdown works offline, costs nothing, and is good enough for a few hundred notes.
- **PyInstaller one-file binary.** Python and all dependencies are bundled, so nothing needs to be installed on the target. The cost: each run unpacks itself (about 0.8 s), and any child process must be started with a **clean environment**. The hub once reused its parent's unpack folder, and the web UI broke when that session exited (see history §8).
- **certifi's CA bundle for outbound HTTPS.** python.org's macOS Python has no CA store. Sessions use `httpx` (which uses certifi); the hub uses `urllib` with certifi's bundle, so it doesn't load a client library for one request.
- **A lean hub process.** The hub lives as long as any session, so `dcode hub` skips the terminal app's imports. That cut it from about 40 MB to about 22 MB. A session is about 60–75 MB, of which the `openai` SDK is about 23 MB and prompt_toolkit about 13 MB.

## Databases and scheduled jobs
- **Two bundled drivers (pg8000, PyMySQL) despite the no-new-dependencies rule.** The owner asked to work with external databases as an admin would. Servers in air-gapped sites often have no `psql`/`mysql` client, and parsing a CLI's output can't enforce row limits or read-only sessions. Both drivers are pure Python, small, and imported only when a connection of that kind is used.
- **The database's own permissions are the access model.** Dcode connects as the user it is given and adds no role system of its own; the catalog queries only list what that user can read.
- **The read-only session is the guard, not the SQL classifier.** Classifying SQL by its first word is easy to fool (`WITH … DELETE`, `SELECT … INTO`), so it only picks the path; the server refuses writes on the read path.
- **Database changes always ask, and never "for the session".** The owner's rule: every change is manual. That holds in auto mode too.
- **Size estimates come from the catalog.** Counting rows to decide whether a table is too heavy would itself be the heavy query.
- **Dcode's own scheduler instead of the system crontab.** Chosen by the owner. It needs no cron daemon, logs are in one place, and prompt jobs can reuse the session machinery. The cost: nothing runs after a reboot until `dcode` is started once, and missed runs are not caught up.
- **Unattended runs deny approvals instead of waiting.** Nobody is there; a job that hangs on a question forever is worse than one that fails loudly. The job's mode says what it may do.
- **The model has no tool to create scheduled jobs**, and nothing in a project folder can add one: a schedule is standing permission to run things later.

## External tools, editors and packaging (Phase 4)
- **MCP tool schemas never enter the tool list.** A typical server's schemas would cost thousands of tokens on every turn of a 32K model. The prompt carries names only; two fixed tools fetch details and run calls. The cost is one extra model step the first time a tool is used.
- **MCP servers only from the user's own config.** Adding a server means running a command, so it follows the hooks rule: never from a project folder. Editors may pass servers over ACP (that is the user's editor configuration).
- **A server's "read-only" note is not trusted to skip approval.** It is the server's own claim. It is enough to let a tool run in plan mode, where the user still approves it.
- **stdio transport only.** Air-gapped sites run local programs; HTTP and OAuth transports need a network and were left out with the other cloud-only features.
- **`dcode attach` goes through the hub's HTTP API, not a new protocol.** The web UI already proved that API can drive a session; a terminal client is one more consumer, and an SSH tunnel makes it remote without opening a port.
- **ACP sends whole messages, not streamed deltas.** Deltas still contain `<think>` text and text-form tool calls that are cleaned only at the end of a model call; sending them would show the editor text that is then taken back.
- **Reasoning is display-only.** It is emitted as its own event and never stored in the message history, so it costs no context.
- **`!command` output is recorded as a tool call**, not as user text, so compaction can shrink it like any other tool result.
- **Usage statistics hold counts only**, in a plain local file. Prompts and file contents never go in.
- **Linux binaries are built on an old glibc in a container** (AlmaLinux 8, glibc 2.28). A PyInstaller binary needs at least the build machine's glibc; the first Linux build (Ubuntu 22.04) could not run on RHEL 9.
- **The VS Code extension is plain JavaScript with no build step and no dependencies**, packed by a Python script: the project has no node toolchain and targets machines without a registry.

## More than one model
- **A profile carries its own server URL.** One vLLM deployment serves one model, so "another model" usually means another URL; switching only the model name (the old helper model) could not reach it.
- **Routing is per task, not per step.** Changing models mid-task would throw away the server's prompt cache at every step and mix two models' habits in one tool loop. The router runs once, before the task starts.
- **The router is cheap by default.** Without a utility model it is keywords, error traces and length: no extra request, works offline, easy to predict. With a utility model it is one short request, and the keywords are the fallback when that fails.
- **Plans stay on the main model** (the owner's choice), and so does carrying out an approved plan: the model that wrote the plan has it in context.
- **The context budget follows the model that runs the task.** A 16K reasoning model given a 32K history must compact first, not fail at the server.
- **No new tool and no prompt text.** The model never sees the routing, so it costs no context.

## Architecture
- **The engine has no I/O.** It reports through a `Host` (events and approvals). That's what lets the terminal and the web UI drive the *same* session, and what makes the engine testable with a fake LLM.
- **Approvals: the first answer wins.** Either surface can answer. The session raises an "attention" ping to close the terminal prompt only when the *web* answers. Pinging on the request itself closed the menu the terminal was about to open.
- **One hub per OS user, token-protected.** Shared servers are common in air-gapped environments, and other users must not see or drive your sessions.
- **The hub dies with the last session, but data stays on disk.** That's how the user asked the UI to behave.

## Small-model behavior
- **Helpers start with an empty context.** Exploration doesn't fill the main window; only a capped report returns. Only read-only roles run in parallel, so helpers never write the same file at once.
- **Index, don't inline.** Skills appear as `name: description` (full text via `skill_use`), and memories as an index plus recall of the most relevant ones.
- **Tolerate model mistakes rather than fail:**
  - recover tool calls written as text
  - strip `<think>`
  - clean leaked markup from tool names
  - make reused tool-call ids unique
  - break identical-call loops: warn at the third identical call; if it is ignored, take the tools away and ask for a report (a warning alone did not stop a real model)
  - accept an edit whose every line has one space too many (models copy the space after `N|`), and name the closest line when an edit fails
  - nudge empty replies
- **Follow-through check.** Models announce "Building the artifact now." and stop. Dcode reminds them, at most twice so it can't loop. It also continues replies that hit the output limit.
- **History stubs must be unmistakable.** A vague `[N chars — saved]` made the model think its save failed and loop. Stubs now say the content was delivered and stored, and tool results confirm the size and a preview.
- **One artifact per chat.** Saves without an id match the chat's artifact by normalized title (or take the latest when untitled), and saves are capped at 3 per artifact per task.
- **Refuse empty writes to non-empty files.** A model blanked `inventory.ini` three times.

## Terminal UX
- **The input sits at the bottom between rules, like Claude Code**, because the user asked. **Plus Dcode's own touches**, because the user asked for "something different": a labeled `── ❯ dcode ─── ◆ mode ──` rule, `◇`/`╰─` glyphs, a blue answer gutter, a chevron spinner and a context meter.
- **White text; blue only for structure** (the user's request). Watch out for rich's `Text("…", style=X)`: it sets a *base style* that leaks into appended text. Append styled spans to an unstyled `Text()` instead.
- **Approval is a menu** (Allow / Allow for session / Deny) that erases itself afterwards. As free text, typing "allow" meant *deny*.
- **Enter and Ctrl+J both submit.** Keys typed while Dcode is busy arrive before raw mode, with Enter translated to a line feed. A new line is Alt+Enter or `\` then Enter.
- **Ctrl+C twice to exit** at an empty prompt; once to stop a task or clear the text.
- **OSC 8 links only where supported.** macOS Terminal doesn't support them, so `/ui`, `/guide` and `/artifacts` open the browser instead.
- **One print per line under live displays.** rich `Live` and spinners swallow partial lines (`end=""`).

## Web UX
- **ChatGPT-like layout**, light and dark, humanized step labels, helper cards and approval cards (the user's request).
- **Artifacts:** sandboxed iframes without `allow-same-origin`, plus a CSP `<meta>` with `default-src 'none'`. They render offline and can't reach the hub API.
- **Settings pinned by env vars are read-only in the UI.** Otherwise UI edits would silently do nothing.

## Process
- **Commit in logical parts; push only when asked** (the user's working agreement).
- **Version numbers are the user's call.** Claude's honest assessment (October 5, 2026): not production-ready until validated on air-gapped Linux with local models. See [roadmap.md](roadmap.md).
