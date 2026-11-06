#!/usr/bin/env python3
"""Scripted OpenAI-compatible model server for end-to-end testing of Dcode.

It speaks just enough of the OpenAI chat-completions API (streaming and not,
text and tool calls, ``/v1/models``) to drive Dcode through real scenarios
without a GPU. Replies are chosen from the *last message* by trigger words,
so each scenario is reproducible.

    python docs/dev/mock_llm.py 18080
    python docs/dev/mock_llm.py 18081 thinker    # a second server: its text replies start with "[thinker] "
    python docs/dev/mock_llm.py 18080 --simulate-cache --max-context 8192 --long-chars 3000
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
    long task 40              → 40 write_file calls in a row, one per step, then a final answer (exercises
                                compaction; 40 is the default count, --long-chars sets each file's size)

Reported usage is computed from each request and reply (chars / 3.5), so token studies mean something:

    --simulate-cache   usage.prompt_tokens_details.cached_tokens = the longest common prefix with the previous
                       request, as a one-slot prefix cache would report. Without it the field is absent.
    --max-context N    refuse a prompt over N tokens with vLLM's wording, like a server with --max-model-len N.

After any tool result the mock answers "Done. Tool `x` returned N chars." Tool
arguments stream in 400-character chunks (8-character chunks made long
artifacts take ~7 s per save and looked like a hang).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
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


CHARS_PER_TOKEN = 3.5
LONG_TASK = re.compile(r"\blong task(?: (\d+))?")
LONG_DEFAULT = 40


class Options:
    """What the command line sets."""

    name = ""
    simulate_cache = False
    max_context = 0
    long_chars = 3000


_lock = threading.Lock()
_previous_prompt = ""            # what the simulated cache holds: the last request
_long_tasks: dict[str, dict[str, int]] = {}  # per task: compaction may fold the message that started it


def prompt_text(body: dict) -> str:
    """What a server would render into the prompt, in order: tools come before the messages."""
    return json.dumps(body.get("tools") or []) + json.dumps(body.get("messages") or [])


def count_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def compute_usage(body: dict, reply: dict) -> dict:
    """Usage from what was really asked and answered, not fixed numbers."""
    global _previous_prompt
    prompt = prompt_text(body)
    prompt_tokens = count_tokens(prompt)
    answer = reply.get("content") or ""
    if "tool" in reply:
        answer += reply["tool"][0] + json.dumps(reply["tool"][1])
    completion_tokens = max(1, count_tokens(answer))
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
             "total_tokens": prompt_tokens + completion_tokens}
    if Options.simulate_cache:
        with _lock:
            shared = len(os.path.commonprefix([_previous_prompt, prompt]))
            _previous_prompt = prompt
        usage["prompt_tokens_details"] = {"cached_tokens": count_tokens(prompt[:shared])}
    return usage


def _long_task_reply(messages: list[dict], task: str) -> dict | None:
    """The next write of a running long task. Keyed by the task's header: the history may have been compacted."""
    last = messages[-1]
    text = str(last.get("content") or "")
    started = LONG_TASK.search(text) if last["role"] == "user" else None
    with _lock:
        state = _long_tasks.get(task)
        if started and state is None:
            state = _long_tasks[task] = {"left": int(started.group(1) or LONG_DEFAULT), "done": 0}
        if state is None:
            return None
        if state["left"] <= 0:
            del _long_tasks[task]
            return {"content": f"Long task finished: wrote {state['done']} files."}
        state["left"] -= 1
        state["done"] += 1
        number = state["done"]
    body = f"# part {number}\n" + "value = 1\n" * (Options.long_chars // 10)
    return {"tool": ("write_file", {"path": f"part_{number}.py", "content": body})}


def choose_reply(messages: list[dict], task: str = "default") -> dict:
    """{"content": text} or {"tool": (name, args)} for the conversation so far."""
    last = messages[-1]
    system = str(messages[0].get("content", ""))
    text = str(last.get("content", ""))
    saves = sum(1 for m in messages if m.get("role") == "tool" and m.get("name") == "artifact_save")

    if "title (2-6 words)" in system:
        return {"content": "Cluster dashboard demo"}
    if "Summarize" in system:
        return {"content": "- summary"}
    long_task = _long_task_reply(messages, task)
    if long_task is not None:
        return long_task
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

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # /v1/models
        self._send({"data": [{"id": "mock-coder-7b"}, {"id": "mock-coder-32b"}]})

    def do_POST(self) -> None:  # /v1/chat/completions
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        size = count_tokens(prompt_text(body))
        if Options.max_context and size > Options.max_context:
            return self._send({"error": {"message": (
                f"This model's maximum context length is {Options.max_context} tokens. However, your request has "
                f"{size} input tokens. Please reduce the length of the input messages."),
                "type": "BadRequestError", "code": 400}}, 400)
        reply = choose_reply(body["messages"], self.headers.get("X-Dcode-Task") or "default")
        if Options.name and reply.get("content"):  # a second server in a test: show which one answered
            reply["content"] = reply["content"].replace("Echo: ", f"[{Options.name}] Echo: ")
        usage = compute_usage(body, reply)
        if not body.get("stream"):
            message = {"role": "assistant", "content": reply.get("content")}
            if "tool" in reply:
                name, args = reply["tool"]
                message["tool_calls"] = [{"id": "c1", "type": "function",
                                          "function": {"name": name, "arguments": json.dumps(args)}}]
            finish = "tool_calls" if "tool" in reply else "stop"
            return self._send({"choices": [{"message": message, "finish_reason": finish}], "usage": usage})

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
            finish = {"index": 0, "delta": {}, "finish_reason": "tool_calls" if "tool" in reply else "stop"}
            self.wfile.write(f"data: {json.dumps({'choices': [finish]})}\n\n".encode())
            chunk(None, usage)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass  # the client stopped reading (interrupt)


def serve(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str]) -> None:
    words, index = [], 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--simulate-cache":
            Options.simulate_cache = True
        elif arg in ("--max-context", "--long-chars"):
            index += 1
            setattr(Options, arg[2:].replace("-", "_"), int(argv[index]))
        else:
            words.append(arg)
        index += 1
    port = int(words[0]) if words else 18080
    Options.name = words[1] if len(words) > 1 else ""
    print(f"mock model on http://127.0.0.1:{port}/v1", flush=True)
    serve(port).serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
