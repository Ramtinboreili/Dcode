"""The hub's HTTP API as an OpenAPI 3.1 document, served at ``/api/openapi.json``.

It is what the web UI and ``dcode attach`` use, written down so scripts and other tools can
use it too. One compact table (``ROUTES``) is the source; ``spec()`` expands it. A test checks
every route here against the server, so the document can't drift from the code.
"""

from __future__ import annotations

from typing import Any

from . import __version__

# (method, path, summary, tag, request body properties or None, query parameters)
# In a body or query spec: name -> type, a trailing "*" on the name marks it required.
Route = tuple[str, str, str, str, dict[str, str] | None, dict[str, str]]

S = "/api/s/{session}"
ROUTES: list[Route] = [
    # --- hub
    ("get", "/api/health", "Is the hub up, which version, are its web files present (no login needed)", "hub", None, {}),
    ("get", "/api/openapi.json", "This document (no login needed)", "hub", None, {}),
    ("get", "/api/hub", "Version, user and home folder of this hub", "hub", None, {}),
    ("get", "/api/sessions", "Running sessions with their state", "sessions", None, {}),
    ("get", "/api/config", "Settings (the API key and MCP env values are masked)", "settings", None, {}),
    ("put", "/api/config", "Change settings; running sessions reload them", "settings",
     {"base_url": "string", "api_key": "string", "model": "string", "reasoning_effort": "string",
      "context_window": "integer", "max_output_tokens": "integer", "stream": "boolean", "permissions": "object",
      "experimental": "object", "agents": "object", "memory": "object", "ui": "object", "artifacts": "object",
      "checks": "object", "db": "object", "rules": "object", "hooks": "array", "mcp": "object", "enabled_skills": "array"}, {}),
    ("get", "/api/models", "Models the configured server offers", "settings", None, {}),
    ("get", "/api/stats", "Usage numbers: tasks, tokens, time, tools", "settings", None, {"days": "integer"}),
    # --- saved chats
    ("get", "/api/threads", "Saved chats, newest first", "chats", None, {}),
    ("post", "/api/threads/import", "Import a chat exported as JSON", "chats", {"messages*": "array", "name": "string"}, {}),
    ("get", "/api/threads/{thread}", "One saved chat as UI events, with its helper sessions listed", "chats", None, {}),
    ("delete", "/api/threads/{thread}", "Delete a saved chat", "chats", None, {}),
    ("get", "/api/threads/{thread}/export", "Download a chat: md, html or json", "chats", None, {"format": "string"}),
    ("get", "/api/threads/{thread}/helpers/{helper}", "A helper's own steps in a saved chat", "chats", None, {}),
    # --- memory, skills, helpers
    ("get", "/api/memory", "Saved memories of every scope", "memory", None, {}),
    ("post", "/api/memory", "Save a memory", "memory",
     {"name*": "string", "content": "string", "description": "string", "tags": "string", "scope_key": "string"}, {}),
    ("delete", "/api/memory/{scope}/{name}", "Delete a memory (scope: global or projects/<folder>)", "memory", None, {}),
    ("get", "/api/skills", "Skills, with which are pinned", "skills", None, {}),
    ("post", "/api/skills", "Save a skill", "skills", {"name*": "string", "description": "string", "content": "string"}, {}),
    ("delete", "/api/skills/{name}", "Delete one of your skills", "skills", None, {}),
    ("get", "/api/agents", "Helpers (built in, yours, and a live project's)", "helpers", None, {"cwd": "string"}),
    ("post", "/api/agents", "Save a helper", "helpers",
     {"name*": "string", "description*": "string", "prompt": "string", "tools": "string", "model": "string"}, {}),
    ("delete", "/api/agents/{name}", "Delete one of your helpers", "helpers", None, {}),
    # --- artifacts
    ("get", "/api/artifacts", "Artifacts, newest first", "artifacts", None, {}),
    ("post", "/api/artifacts", "Create an artifact", "artifacts",
     {"title": "string", "content*": "string", "kind": "string", "language": "string"}, {}),
    ("get", "/api/artifacts/{artifact}", "One artifact with its content and versions", "artifacts", None, {"version": "integer"}),
    ("patch", "/api/artifacts/{artifact}", "Rename an artifact", "artifacts", {"title*": "string"}, {}),
    ("delete", "/api/artifacts/{artifact}", "Delete an artifact", "artifacts", None, {}),
    ("get", "/api/artifacts/{artifact}/raw", "Download an artifact as a file", "artifacts", None, {}),
    ("put", "/api/artifacts/{artifact}/content", "Save new content as the next version", "artifacts", {"content*": "string"}, {}),
    ("post", "/api/artifacts/{artifact}/restore", "Make an older version the current one", "artifacts", {"version*": "integer"}, {}),
    ("post", "/api/artifacts/{artifact}/duplicate", "Copy an artifact", "artifacts", {}, {}),
    # --- databases
    ("get", "/api/db", "Saved connections (never the passwords), drivers and limits", "databases", None, {}),
    ("post", "/api/db", "Save a connection", "databases",
     {"name*": "string", "engine*": "string", "host": "string", "port": "integer", "database": "string", "user": "string",
      "password": "string", "read_only": "boolean", "tls": "string"}, {}),
    ("post", "/api/db/test", "Try a connection before saving it", "databases", {"engine*": "string"}, {}),
    ("delete", "/api/db/{connection}", "Remove a connection", "databases", None, {}),
    ("get", "/api/db/{connection}/schema", "Tables, columns, keys and sizes", "databases", None, {"refresh": "string"}),
    ("get", "/api/db/{connection}/summary", "Size, counts and the largest tables", "databases", None, {"refresh": "string"}),
    ("post", "/api/db/{connection}/query",
     "Run one statement. A change comes back as needs_confirmation until sent with confirmed=true", "databases",
     {"sql*": "string", "limit": "integer", "confirmed": "boolean"}, {}),
    ("post", "/api/db/{connection}/export", "Export a table, a query, the structure or everything", "databases",
     {"what": "string", "table": "string", "sql": "string", "format": "string", "force": "boolean"}, {}),
    ("get", "/api/db/exports", "Export files made so far", "databases", None, {}),
    ("get", "/api/db/exports/{file}", "Download an export file", "databases", None, {}),
    ("delete", "/api/db/exports/{file}", "Delete an export file", "databases", None, {}),
    # --- scheduled jobs
    ("get", "/api/cron", "Scheduled jobs with their last and next run", "scheduled jobs", None, {}),
    ("post", "/api/cron", "Create or change a job", "scheduled jobs",
     {"schedule*": "string", "command*": "string", "kind": "string", "name": "string", "cwd": "string", "mode": "string",
      "timeout": "integer", "enabled": "boolean", "id": "string"}, {}),
    ("get", "/api/cron/preview", "Explain a schedule and list its next runs", "scheduled jobs", None, {"schedule*": "string"}),
    ("delete", "/api/cron/{job}", "Delete a job and its logs", "scheduled jobs", None, {}),
    ("get", "/api/cron/{job}/runs", "A job's runs, newest first", "scheduled jobs", None, {}),
    ("get", "/api/cron/{job}/log", "The output of one run", "scheduled jobs", None, {"run*": "string"}),
    ("post", "/api/cron/{job}/toggle", "Turn a job on or off", "scheduled jobs", {"enabled*": "boolean"}, {}),
    ("post", "/api/cron/{job}/run", "Run a job now", "scheduled jobs", {}, {}),
    ("post", "/api/cron/{job}/stop", "Stop a running job", "scheduled jobs", {}, {}),
    # --- one running session (the hub forwards these to it)
    ("get", f"{S}/state", "Model, mode, busy, approvals waiting, context used, jobs, checklist", "session", None, {}),
    ("get", f"{S}/events", "Server-sent events from sequence number `since` on (replays, then follows)", "session", None,
     {"since": "integer"}),
    ("get", f"{S}/files", "Project files and helpers matching what is typed after @", "session", None, {"q": "string"}),
    ("get", f"{S}/helpers", "Helper sessions of the current chat", "session", None, {}),
    ("get", f"{S}/helpers/{{helper}}", "A helper's own steps", "session", None, {}),
    ("get", f"{S}/mcp", "MCP servers: state and tools", "session", None, {}),
    ("post", f"{S}/message", "Send a message (a leading ! runs your own shell command)", "session",
     {"text*": "string", "source": "string"}, {}),
    ("post", f"{S}/approve", "Answer an approval or a question (the first answer wins)", "session",
     {"id*": "string", "approved*": "boolean", "scope": "string", "answer": "string", "source": "string"}, {}),
    ("post", f"{S}/interrupt", "Stop the running task", "session", {}, {}),
    ("post", f"{S}/mode", "Switch mode: manual, accept_edits, auto, plan", "session", {"mode*": "string"}, {}),
    ("post", f"{S}/plan", "After plan mode: accept_edits, manual or auto runs the plan; keep stays", "session",
     {"decision*": "string"}, {}),
    ("post", f"{S}/model", "Switch model or reasoning effort", "session", {"model": "string", "reasoning_effort": "string"}, {}),
    ("post", f"{S}/new", "Save the chat and start a new one", "session", {}, {}),
    ("post", f"{S}/rename", "Rename the chat", "session", {"name*": "string"}, {}),
    ("post", f"{S}/resume", "Continue a saved chat in this session", "session", {"thread_id*": "string"}, {}),
    ("post", f"{S}/command", "Run one of your /commands", "session", {"name*": "string", "args": "string"}, {}),
    ("post", f"{S}/init", "Write or refresh AGENTS.md", "session", {}, {}),
    ("post", f"{S}/compact", "Summarize the chat to free context", "session", {"focus": "string"}, {}),
    ("post", f"{S}/undo", "Undo the file changes of the last task", "session", {}, {}),
    ("post", f"{S}/redo", "Bring back what the last undo removed", "session", {}, {}),
    ("post", f"{S}/reload", "Reload settings from disk", "session", {}, {}),
    ("post", f"{S}/mcp/restart", "Restart one MCP server, or all", "session", {"name": "string"}, {}),
]

_PUBLIC = {"/api/health", "/api/openapi.json"}
_DOWNLOADS = {"/api/threads/{thread}/export", "/api/artifacts/{artifact}/raw", "/api/db/exports/{file}"}
_DESCRIPTION = (
    "Dcode's per-user hub. It listens on 127.0.0.1 only and every request needs your token: open "
    "`/?token=<token>` once (the link `dcode ui` prints) to get the `dcode_token` cookie, or send it yourself. "
    "Requests that change something must also carry the header `X-Dcode: 1`. Paths under "
    "`/api/s/{session}` are forwarded to that running session. To reach a hub on a server, use an SSH "
    "tunnel: `ssh -L 7300:127.0.0.1:7300 server`."
)


def _properties(fields: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    props = {name.rstrip("*"): ({"type": kind, "items": {}} if kind == "array" else {"type": kind}) for name, kind in fields.items()}
    return props, [name[:-1] for name in fields if name.endswith("*")]


def spec() -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for method, path, summary, tag, body, query in ROUTES:
        parameters = [{"name": part[1:-1], "in": "path", "required": True, "schema": {"type": "string"}}
                      for part in path.split("/") if part.startswith("{")]
        for name, kind in query.items():
            parameters.append({"name": name.rstrip("*"), "in": "query", "required": name.endswith("*"), "schema": {"type": kind}})
        if path.endswith("/events"):
            content = {"text/event-stream": {"schema": {"type": "string"}}}
        elif path in _DOWNLOADS:
            content = {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}
        else:
            content = {"application/json": {"schema": {}}}
        operation: dict[str, Any] = {
            "summary": summary, "tags": [tag],
            "operationId": method + "".join(part.strip("{}").replace(".", "_").title().replace("-", "") for part in path.split("/")[2:]),
            "responses": {"200": {"description": "OK", "content": content},
                          "400": {"description": "The request can't be done; `error` says why"},
                          "404": {"description": "No such thing"}},
        }
        if parameters:
            operation["parameters"] = parameters
        if body is not None:
            props, required = _properties(body)
            schema: dict[str, Any] = {"type": "object", "properties": props}
            if required:
                schema["required"] = required
            operation["requestBody"] = {"content": {"application/json": {"schema": schema}}}
        if path in _PUBLIC:
            operation["security"] = []
        else:
            operation["responses"]["401"] = {"description": "Missing or wrong token (or no X-Dcode header on a change)"}
        if path.startswith(S):
            operation["responses"]["409"] = {"description": "The session can't do that now (for example, it is busy)"}
        paths.setdefault(path, {})[method] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "Dcode hub API", "version": __version__, "description": _DESCRIPTION},
        "servers": [{"url": "http://127.0.0.1:7300", "description": "Your hub (the port is per user: see `dcode ui`)"}],
        "security": [{"token": []}],
        "components": {"securitySchemes": {"token": {"type": "apiKey", "in": "cookie", "name": "dcode_token"}}},
        "paths": paths,
    }
