#!/usr/bin/env python3
"""Scripted OpenAI-compatible model server for end-to-end testing of Dcode.

It speaks just enough of the OpenAI chat-completions API (streaming and not,
text and tool calls, ``/v1/models``) to drive Dcode through real scenarios
without a GPU. Replies are chosen from the *last message* by trigger words,
so each scenario is reproducible.

    python docs/dev/mock_llm.py 18080
    DCODE_BASE_URL=http://127.0.0.1:18080/v1 DCODE_API_KEY=x DCODE_MODEL=mock dcode

Trigger words (in the user's message unless noted):

    say hello / anything else → plain text reply (with a <think> block Dcode must strip)
    list                      → list_files tool call
    run                       → run_command "echo hi-from-tool" (exercises approvals)
    explore                   → delegate to an explore helper (helper replies after 1.5 s)
    dashboard                 → artifact_save of an HTML dashboard
    about this project        → "Building the artifact now." with NO tool call
                                (the follow-through bug; Dcode must remind the model)
    loop                      → re-saves the same artifact up to 5 times, then untitled
                                (the re-save bug; Dcode must cap and merge them)
    start the server          → run_command with background=true (background jobs)
    make a checklist          → todo_write with two items
    ask me                    → ask_user "Which database?" (Postgres / SQLite)
    break the syntax          → edit_file that leaves app.py with a syntax error (post-edit check)
    count the parts           → db_query SELECT on the connection "stock" (database tools)
    delete the old parts      → db_query DELETE on "stock" (a change: must ask, in every mode)
    use the notebook          → mcp_call add(20, 22) on the MCP server "notes" (external tools)

After any tool result the mock answers "Done. Tool `x` returned N chars." Tool
arguments stream in 400-character chunks (8-character chunks made long
artifacts take ~7 s per save and looked like a hang).
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DASHBOARD = """<!doctype html><html><head><style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:28px;background:#f7f9fc;color:#0f172a}
h1{font-size:22px;margin:0 0 4px}.sub{color:#64748b;margin:0 0 22px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.card{background:#fff;border:1px solid #e4e9f0;border-radius:14px;padding:16px}
.k{color:#64748b;font-size:13px}.v{font-size:28px;font-weight:700;margin-top:6px}.ok{color:#16a34a}.warn{color:#d97706}
.bar{height:8px;border-radius:6px;background:#e6ebf3;margin-top:12px;overflow:hidden}.bar i{display:block;height:100%;background:#2563eb}
</style></head><body><h1>Cluster health</h1><p class="sub">prod-eu-1 · updated just now</p>
<div class="grid"><div class="card"><div class="k">Nodes ready</div><div class="v ok">12 / 12</div><div class="bar"><i style="width:100%"></i></div></div>
<div class="card"><div class="k">CPU usage</div><div class="v">63%</div><div class="bar"><i style="width:63%"></i></div></div>
<div class="card"><div class="k">Pods pending</div><div class="v warn">3</div><div class="bar"><i style="width:12%;background:#d97706"></i></div></div></div>
</body></html>"""
REPORT = "<!DOCTYPE html><html><body><h1>Repo report</h1>" + "<p>details</p>" * 200 + "</body></html>"


def choose_reply(messages: list[dict]) -> dict:
    """{"content": text} or {"tool": (name, args)} for the conversation so far."""
    last = messages[-1]
    system = str(messages[0].get("content", ""))
    text = str(last.get("content", ""))
    saves = sum(1 for m in messages if m.get("role") == "tool" and m.get("name") == "artifact_save")

    if "title (2-6 words)" in system:
        return {"content": "Cluster dashboard demo"}
    if "Summarize" in system:
        return {"content": "- summary"}
    if last["role"] == "tool" and last.get("name") == "artifact_save" and "loop" in str(messages[1:]) and saves < 5:
        args = {"title": "Repo report", "kind": "html", "content": REPORT} if saves < 2 else {"content": REPORT}
        return {"tool": ("artifact_save", args)}
    if last["role"] == "tool":
        return {"content": f"Done. Tool `{last.get('name')}` returned {len(text)} chars."}
    if "exploration sub-agent" in system:
        time.sleep(1.5)  # slow enough to see the live helper panel
        return {"tool": ("list_files", {"path": "."})} if len(messages) == 2 else {"content": "REPORT: found files"}
    if "loop" in text:
        return {"tool": ("artifact_save", {"title": "Repo report", "kind": "html", "content": "<h1>Repo report</h1>"})}
    if "artifact_save NOW" in text:  # Dcode's follow-through reminder
        return {"tool": ("artifact_save", {"title": "Project overview", "kind": "html",
                                           "content": "<h1>Project</h1><p>Overview.</p>"})}
    if "about this project" in text:
        return {"content": "I have the full picture. Building the artifact now."}
    if "count the parts" in text:
        return {"tool": ("db_query", {"connection": "stock", "sql": "SELECT count(*) AS parts FROM parts"})}
    if "delete the old parts" in text:
        return {"tool": ("db_query", {"connection": "stock", "sql": "DELETE FROM parts WHERE id = 1"})}
    if "use the notebook" in text:
        return {"tool": ("mcp_call", {"server": "notes", "tool": "add", "arguments": {"a": 20, "b": 22}})}
    if "make a checklist" in text:
        return {"tool": ("todo_write", {"items": [{"text": "Read the code", "status": "done"},
                                                  {"text": "Write the fix", "status": "in_progress"}]})}
    if "ask me" in text:
        return {"tool": ("ask_user", {"question": "Which database?", "options": ["Postgres", "SQLite"]})}
    if "break the syntax" in text:
        return {"tool": ("edit_file", {"path": "app.py", "old_text": "print('hi')", "new_text": "print('hi'"})}
    if "start the server" in text:
        return {"tool": ("run_command", {"command": "python3 -m http.server 18099", "background": True})}
    if "dashboard" in text:
        return {"tool": ("artifact_save", {"title": "Cluster health dashboard", "kind": "html", "content": DASHBOARD})}
    if "explore" in text:
        return {"tool": ("delegate", {"role": "explore", "task": "list the project files"})}
    if "run" in text:
        return {"tool": ("run_command", {"command": "echo hi-from-tool"})}
    if "list" in text:
        return {"tool": ("list_files", {"path": "."})}
    return {"content": "<think>hidden reasoning</think>Echo: " + text[:60]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # /v1/models
        self._send({"data": [{"id": "mock-coder-7b"}, {"id": "mock-coder-32b"}]})

    def do_POST(self) -> None:  # /v1/chat/completions
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = choose_reply(body["messages"])
        usage = {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520}
        if not body.get("stream"):
            message = {"role": "assistant", "content": reply.get("content")}
            if "tool" in reply:
                name, args = reply["tool"]
                message["tool_calls"] = [{"id": "c1", "type": "function",
                                          "function": {"name": name, "arguments": json.dumps(args)}}]
            return self._send({"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(delta, usage_info=None) -> None:
            data = {"choices": [{"index": 0, "delta": delta}] if delta is not None else []}
            if usage_info:
                data["usage"] = usage_info
            self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.02)

        try:
            if "tool" in reply:
                name, args = reply["tool"]
                encoded = json.dumps(args)
                chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                       "function": {"name": name, "arguments": ""}}]})
                for i in range(0, len(encoded), 400):
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": encoded[i:i + 400]}}]})
            else:
                for word in reply["content"].split(" "):
                    chunk({"content": word + " "})
            chunk(None, usage)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass  # the client stopped reading (interrupt)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    print(f"mock model on http://127.0.0.1:{port}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
