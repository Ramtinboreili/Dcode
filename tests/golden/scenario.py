#!/usr/bin/env python3
"""A scripted session whose exact HTTP request bodies are recorded, to prove "instrumentation changes nothing".

``requests.json.gz`` was written by running this file on the code as it was before any research
instrumentation (commit 98c130b, Release 0.7.1). ``tests/test_golden_requests.py`` replays it on today's code
and compares body by body. It deliberately uses only what that commit already had, so it can still be run there:

    git worktree add /tmp/base 98c130b && PYTHONPATH=/tmp/base python tests/golden/scenario.py --write

After a *deliberate* change to prompts or schemas, regenerate with the same command on the current tree
(no PYTHONPATH) and say so in the commit: the file then records the new behaviour.
"""

from __future__ import annotations

import gzip
import itertools
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).with_name("requests.json.gz")
WORKSPACE_CHARS = 150  # the path is in the prompt: a fixed length keeps token estimates, and so compaction, the same


def _normalize(text: str, workspace: str) -> str:
    text = text.replace(workspace, "<ws>")
    text = re.sub(r"call_[0-9a-f]{12}", "call_<id>", text)
    return re.sub(r"shell_[0-9a-f]{8}", "shell_<id>", text)


def _tool(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": (tool, args)}


def _last_user(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


class Script:
    """The model: decides from the conversation, so a change in what Dcode sends can't reorder the replies."""

    def __init__(self) -> None:
        self.refused: set[str] = set()

    def reply(self, body: dict[str, Any]) -> dict[str, Any]:
        messages = body["messages"]
        system = str(messages[0].get("content") or "")
        last = messages[-1]
        task = _last_user(messages)
        tools_used = [m.get("name") for m in messages[messages.index(self._turn_start(messages)):]
                      if m.get("role") == "tool"]
        if "title (2-6 words)" in system:
            return {"content": "Golden session"}
        if "Summarize" in system:
            return {"content": "- summary of earlier work"}
        if "HARD or EASY" in system or "EASY" in system[:400]:
            return {"content": "HARD" if "architecture" in task else "EASY"}
        if "exploration sub-agent" in system:
            return _tool("list_files", path=".") if not tools_used else {"content": "REPORT: found the files"}
        if "write and run" in task:
            steps = [_tool("write_file", path="app.py", content="print('hi')\n"), _tool("read_file", path="app.py"),
                     _tool("run_command", command="echo hello"),
                     _tool("run_command", command="python3 -c \"print('x' * 3000)\"")]
            return steps[len(tools_used)] if len(tools_used) < len(steps) else {"content": "All written and run."}
        if "remember the port" in task:
            return _tool("memory_save", name="service-port", content="The service port is 8123.", scope="project") \
                if not tools_used else {"content": "Saved it."}
        if "ask explore" in task:
            return _tool("delegate", role="explore", task="list the project files") \
                if not tools_used else {"content": "The helper found the files."}
        if "promise something" in task:
            return {"content": "All done now."} if "didn't call any tool" in task else {"content": "I'll create it now."}
        if "text tool call" in task:
            return {"content": '<tool_call>{"name": "list_files", "arguments": {"path": "."}}</tool_call>'} \
                if not tools_used else {"content": "Listed."}
        if "think too long" in task:
            if (body.get("chat_template_kwargs") or {}).get("enable_thinking"):
                return {"reasoning": "thinking about it, " * 900, "finish": "length"}
            return {"content": "Answered with less thinking."}
        if "refuse a field" in task and "field" not in self.refused:
            self.refused.add("field")
            return {"status": 400, "error": "Unrecognized request argument supplied: reasoning_effort"}
        if "rate limit me" in task and "busy" not in self.refused:
            self.refused.add("busy")
            return {"status": 429, "error": "busy", "retry_after": "0"}
        if "long writes" in task:
            done = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
            return _tool("write_file", path=f"part{done}.py", content=f"# part {done}\n" + "x = 1\n" * 500) \
                if done < 12 else {"content": "All parts written."}
        if "fill the context" in task:
            count = sum(1 for name in tools_used if name == "run_command")
            return _tool("run_command", command=f"python3 -c \"print('{count}' * 1400)\"") \
                if count < 4 else {"content": "Filled."}
        if "silent stop" in task:
            return _tool("list_files", path=".") if not tools_used else {"content": "" if "Continue." not in str(last.get("content")) else "Finished."}
        if "make a checklist" in task:
            return _tool("todo_write", items=[{"text": "One", "status": "done"}]) if not tools_used else {"content": "Done."}
        return {"content": f"<think>hidden</think>Echo: {task[:40]}"}

    @staticmethod
    def _turn_start(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for message in reversed(messages):
            if message.get("role") == "user":
                return message
        return messages[0]


class Recorder:
    """An OpenAI-compatible server that keeps the raw bytes of every request."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.script = Script()
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.lock = threading.Lock()
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, payload: dict[str, Any], status: int = 200, extra: dict[str, str] | None = None) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._json({"data": []})

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                text = _normalize(raw.decode(), recorder.workspace)
                with recorder.lock:
                    recorder.requests.append({"path": self.path, "body": text})
                    recorder.headers.append({k.lower(): v for k, v in self.headers.items()})
                body = json.loads(raw)
                reply = recorder.script.reply(body)
                # Tied to the normalized request, so calibration (and so compaction) repeats exactly.
                usage = {"prompt_tokens": int(len(text) / 3.1), "completion_tokens": 7,
                         "total_tokens": int(len(text) / 3.1) + 7}
                if reply.get("status"):
                    extra = {"Retry-After": reply["retry_after"]} if "retry_after" in reply else None
                    return self._json({"error": {"message": reply["error"], "code": None}}, reply["status"], extra)
                message: dict[str, Any] = {"role": "assistant", "content": reply.get("content") or ""}
                calls = None
                if "tool" in reply:
                    name, args = reply["tool"]
                    calls = [{"index": 0, "id": "c1", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}]
                    message["tool_calls"] = calls
                if reply.get("reasoning"):
                    message["reasoning_content"] = reply["reasoning"]
                finish = reply.get("finish") or ("tool_calls" if calls else "stop")
                if not body.get("stream"):
                    return self._json({"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                                       "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                       "usage": usage})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                def send(delta: dict[str, Any] | None, end: str | None = None, used: dict[str, int] | None = None) -> None:
                    data: dict[str, Any] = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                                            "choices": [] if delta is None else
                                            [{"index": 0, "delta": delta, "finish_reason": end}]}
                    if used:
                        data["usage"] = used
                    self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()

                try:
                    if reply.get("reasoning"):
                        for start in range(0, len(reply["reasoning"]), 500):
                            send({"reasoning_content": reply["reasoning"][start:start + 500]})
                    if message["content"]:
                        send({"content": message["content"]})
                    if calls:
                        send({"tool_calls": calls})
                    send({}, finish)
                    send(None, used=usage)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Dcode cut this reply off on purpose

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


SESSION_TASKS = [
    "write and run a small program",   # four tools; one output over the earlier-turn stub size
    "remember the port of the service",
    "what is the service port",        # recall puts the memory in the prompt
    "ask explore what is in here",     # a helper, with its own requests
    "promise something",               # follow-through reminder
    "text tool call please",           # recovered from the model's text
    "think too long about this",       # reasoning cut off, asked again with less effort
    "refuse a field now",              # a field the server refuses, learned
    "rate limit me once",              # busy, waited for
    "silent stop after a tool",        # the nudge
    "make a checklist",                # checklist in the prompt
    "!echo from-the-user",             # the user's own short command
    "look at @app.py please",          # an attached file
]
COMPACTION_TASKS = [f"fill the context, round {n}" for n in range(1, 7)] + ["say hello at the end"]
ROUTED_TASKS = ["rename a variable in the module", "redesign the architecture of the module"]


def _wait(session: Any) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        if session.idle:
            time.sleep(0.02)
            if session.idle:
                return
        time.sleep(0.01)
    raise TimeoutError("the session did not finish")


def _scenario(name: str, server: Recorder, tasks: list[str], *, experiment: dict[str, Any] | None = None,
              compact_at_end: bool = False, **settings: Any) -> None:
    from dcode.config import AppConfig
    from dcode.session import Session

    fields = {"base_url": server.url, "api_key": "golden", "model": "Qwen/Qwen3-32B", "context_window": 16384,
              "max_output_tokens": 2048, "reasoning_effort": "medium", "stream": True}
    fields.update(settings)
    config = AppConfig(**fields)
    if experiment:
        config.experiment.update(experiment)
    session = Session(config)
    session.set_mode("auto", source="run")
    try:
        for task in tasks:
            if task.startswith("!"):
                session.run_shell(task[1:], source="run")
            else:
                session.submit(task, source="run")
            _wait(session)
        if compact_at_end:
            session.compact(source="run")
            _wait(session)
        for thread in threading.enumerate():
            if thread.name == "dcode-title":
                thread.join(timeout=10)
    finally:
        session.close()


SCENARIOS: dict[str, dict[str, Any]] = {
    "session-stream": {"tasks": SESSION_TASKS},
    "session-plain": {"tasks": SESSION_TASKS[:5], "stream": False},
    "compaction": {"tasks": COMPACTION_TASKS, "context_window": 6000, "max_output_tokens": 512, "compact_at_end": True},
    # One long task: only compares with the two new compaction behaviours switched off (see the test).
    "long-writes": {"tasks": ["do the long writes"], "context_window": 6000, "max_output_tokens": 512},
    "routed": {"tasks": ROUTED_TASKS, "models": {
        "profiles": {"big": {"model": "big-32b", "context_window": 8192}, "small": {"model": "small-7b"}},
        "reasoning": "big", "utility": "small", "router": "auto"}},
}


def record(name: str, **overrides: Any) -> list[dict[str, Any]]:
    """Run one scenario in a throwaway home and workspace; returns its requests, side jobs last (they race)."""
    from dcode import engine

    base = tempfile.mkdtemp(prefix="dcode-golden-")
    workspace = os.path.join(base, "w" * (WORKSPACE_CHARS - len(base) - 1))
    os.mkdir(workspace)
    saved = {key: os.environ.get(key) for key in ("DCODE_HOME", "DCODE_WORKSPACE")}
    cwd = os.getcwd()
    os.environ["DCODE_HOME"] = os.path.join(base, "home")
    os.environ.pop("DCODE_WORKSPACE", None)
    os.chdir(workspace)
    engine._agent_counter = itertools.count(1)
    server = Recorder(os.path.realpath(workspace))
    try:
        spec = {**SCENARIOS[name], **overrides}
        _scenario(name, server, spec.pop("tasks"), **spec)
    finally:
        server.stop()
        os.chdir(cwd)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(base, ignore_errors=True)
    titles = [r for r in server.requests if "title (2-6 words)" in r["body"]]
    return [r for r in server.requests if r not in titles] + titles


def load_golden() -> dict[str, list[dict[str, Any]]]:
    return json.loads(gzip.decompress(GOLDEN.read_bytes()))


def main() -> None:
    if "--write" not in sys.argv:
        sys.exit(__doc__)
    import dcode

    out = {name: record(name) for name in SCENARIOS}
    again = {name: record(name) for name in SCENARIOS}
    if out != again:  # never store something that doesn't repeat
        sys.exit("not deterministic: " + ", ".join(n for n in out if out[n] != again[n]))
    GOLDEN.write_bytes(gzip.compress(json.dumps(out, ensure_ascii=False, sort_keys=True).encode(), mtime=0))
    print("from", dcode.__file__, {name: len(rows) for name, rows in out.items()}, GOLDEN.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
