"""``dcode acp``: Dcode as an agent inside an editor, over the Agent Client Protocol (v1).

The editor (Zed, or any ACP client) starts this process and speaks JSON-RPC on its standard
input and output, one message per line. Each ACP session is an ordinary Dcode
:class:`~dcode.session.Session`; this module only translates:

* the session's events become ``session/update`` notifications (messages, reasoning, tool
  calls with diffs and file locations, the checklist as a plan, the mode);
* an approval or a question becomes a ``session/request_permission`` request to the editor;
* ``session/prompt``, ``session/cancel`` and ``session/set_mode`` drive the session.

Standard output carries protocol messages only; anything else Dcode prints goes to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .agents import MODE_LABELS, MODES

PROTOCOL_VERSION = 1
OUTPUT_CHARS = 6000     # of a tool result shown in the editor
MODE_NOTES = {
    "manual": "Ask before every edit and command",
    "accept_edits": "Edit files freely, ask before commands",
    "auto": "Never ask",
    "plan": "Read-only: propose a plan first",
}
# ACP tool kinds: read, edit, delete, move, search, execute, think, fetch, switch_mode, other
TOOL_KINDS = {
    "read_file": "read", "list_files": "read", "git_status": "read", "git_diff": "read", "git_log": "read",
    "artifact_get": "read", "db_schema": "read", "job_status": "read", "mcp_tools": "read", "skill_use": "read",
    "edit_file": "edit", "write_file": "edit", "create_directory": "edit", "artifact_save": "edit", "memory_save": "edit",
    "skill_save": "edit", "delete_file": "delete", "search_in_files": "search", "memory_search": "search",
    "run_command": "execute", "git_commit": "execute", "job_stop": "execute", "db_query": "execute", "mcp_call": "execute",
    "delegate": "think", "todo_write": "think",
}
APPROVAL_TITLES = {
    "run_command": "Run a command", "delete_file": "Delete a file", "git_commit": "Make a git commit",
    "edit_file": "Change a file", "rule": "A rule says to confirm this", "outside_workspace": "Use a file outside the project",
    "db_write": "Change a database", "mcp_tool": "Use an external tool",
}
BUILTIN_COMMANDS = [
    {"name": "compact", "description": "Summarize the chat to free context", "input": {"hint": "what to keep (optional)"}},
    {"name": "init", "description": "Write or refresh AGENTS.md for this project"},
    {"name": "review", "description": "Review the uncommitted git changes"},
]
FAILED_PREFIXES = ("Tool error", "Blocked", "MCP error", "Database error", "Invalid JSON")


def _args_title(name: str, args: Any) -> str:
    """A short human title for a tool call: the tool and the one argument that matters."""
    if isinstance(args, dict):
        for key in ("command", "path", "sql", "task", "title", "tool", "name", "query", "pattern", "question"):
            if args.get(key):
                return f"{name} {' '.join(str(args[key]).split())[:120]}"
    return name


def prompt_text(blocks: Any, cwd: str) -> str:
    """The editor's content blocks as the one text message Dcode takes."""
    parts: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource_link":
            parts.append(_mention(str(block.get("uri") or ""), cwd))
        elif kind == "resource":
            resource = block.get("resource") if isinstance(block.get("resource"), dict) else {}
            text = resource.get("text")
            label = _mention(str(resource.get("uri") or ""), cwd).lstrip("@")
            parts.append(f"\n[{label}]\n```\n{text}\n```\n" if isinstance(text, str) else f"[{label}: not text, left out]")
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} left out: Dcode takes text]")
    return " ".join(p for p in parts if p).strip()


def _mention(uri: str, cwd: str) -> str:
    """A file link as Dcode's @path when it is inside the project; the link itself otherwise."""
    if uri.startswith("file://"):
        path = Path(uri[len("file://"):])
        try:
            return "@" + str(path.resolve().relative_to(Path(cwd).resolve()))
        except ValueError:
            return str(path)
    return uri


class Connection:
    """JSON-RPC over two text streams: requests in, replies and our own requests out."""

    def __init__(self, reader: TextIO, writer: TextIO) -> None:
        self.reader = reader
        self.writer = writer
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[Any, tuple[threading.Event, dict[str, Any]]] = {}

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False, separators=(",", ":"), default=str)
        try:
            with self._lock:
                self.writer.write(line + "\n")
                self.writer.flush()
        except (OSError, ValueError):
            pass  # the editor went away

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any] | None:
        """Ask the editor something and wait for its answer. None if it failed or went away."""
        with self._lock:
            self._next_id += 1
            request_id = f"dcode-{self._next_id}"
        done, box = threading.Event(), {}
        self._waiting[request_id] = (done, box)
        self.send({"id": request_id, "method": method, "params": params})
        done.wait(timeout)
        self._waiting.pop(request_id, None)
        return box.get("result")

    def resolve(self, message: dict[str, Any]) -> None:
        waiter = self._waiting.get(message.get("id"))
        if waiter is not None:
            if isinstance(message.get("result"), dict):
                waiter[1]["result"] = message["result"]
            waiter[0].set()

    def abandon(self) -> None:
        for done, _ in list(self._waiting.values()):
            done.set()


class Bridge:
    """One ACP session: a Dcode session and the translation of its events."""

    def __init__(self, conn: Connection, session: Any) -> None:
        self.conn = conn
        self.session = session
        self.id = session.id
        self.cancelled = False
        self._turn_done = threading.Event()
        self._last_call: dict[str, str] = {}   # agent -> the tool call an approval refers to
        self._calls: dict[str, tuple[str, Any]] = {}
        session.bus.subscribe(self._on_event)

    def update(self, update_name: str, /, **fields: Any) -> None:
        self.conn.notify("session/update", {"sessionId": self.id, "update": {"sessionUpdate": update_name, **fields}})

    def say(self, text: str, thought: bool = False) -> None:
        if text:
            self.update("agent_thought_chunk" if thought else "agent_message_chunk", content={"type": "text", "text": text})

    # --- events -> updates
    def _on_event(self, event: dict[str, Any]) -> None:
        kind, agent = event.get("type"), str(event.get("agent") or "main")
        if kind == "status" and not event.get("busy"):
            self._turn_done.set()
        elif kind in ("assistant_note", "assistant_message") and agent == "main":
            self.say(str(event.get("text") or "") + ("\n\n" if kind == "assistant_note" else ""))
        elif kind == "thinking" and agent == "main":
            self.say(str(event.get("text") or ""), thought=True)
        elif kind == "tool_call":
            self._tool_call(event, agent)
        elif kind == "tool_result":
            self._tool_result(event, agent)
        elif kind == "agent_start":
            self.update("tool_call", toolCallId=f"helper/{agent}", kind="think", status="in_progress",
                        title=f"Helper {event.get('role') or agent}: {' '.join(str(event.get('task') or '').split())[:120]}")
        elif kind == "agent_end":
            self.update("tool_call_update", toolCallId=f"helper/{agent}", status="completed" if event.get("ok") else "failed",
                        content=[{"type": "content", "content": {"type": "text", "text": str(event.get("report") or "")[:OUTPUT_CHARS]}}])
        elif kind == "todos" and agent == "main":
            status = {"done": "completed", "in_progress": "in_progress"}
            self.update("plan", entries=[{"content": str(i.get("text") or ""), "priority": "medium",
                                          "status": status.get(i.get("status"), "pending")} for i in event.get("items") or []])
        elif kind == "mode":
            self.update("current_mode_update", currentModeId=str(event.get("mode")))
        elif kind == "usage" and agent == "main" and event.get("context_window"):
            self.update("usage_update", used=int(event.get("context_used") or 0), size=int(event["context_window"]))
        elif kind == "approval_request":
            threading.Thread(target=self._ask, args=(event,), name="dcode-acp-permission", daemon=True).start()
        elif kind == "error" or (kind == "notice" and event.get("level") in ("warning", "error")):
            text = str(event.get("text") or "")
            if not text.startswith("Interrupt requested"):
                self.say(f"\n> {text}\n\n")
        elif kind == "check":
            self.say(f"\n> A check found a problem in {event.get('path')}; fixing it.\n\n")

    def _tool_call(self, event: dict[str, Any], agent: str) -> None:
        name, args = str(event.get("name") or ""), event.get("args")
        call_id = f"{agent}/{event.get('id') or uuid.uuid4().hex[:8]}"
        self._last_call[agent] = call_id
        self._calls[call_id] = (name, args)
        if name == "todo_write":
            return  # shown as the plan
        fields: dict[str, Any] = {"toolCallId": call_id, "title": _args_title(name, args), "kind": TOOL_KINDS.get(name, "other"),
                                  "status": "in_progress", "rawInput": args if isinstance(args, dict) else {}}
        if isinstance(args, dict) and args.get("path") and name not in ("search_in_files", "list_files"):
            location: dict[str, Any] = {"path": str((Path(self.session.cwd) / str(args["path"])).resolve())}
            if args.get("start_line"):
                location["line"] = int(args["start_line"])
            fields["locations"] = [location]
        self.update("tool_call", **fields)

    def _tool_result(self, event: dict[str, Any], agent: str) -> None:
        call_id = f"{agent}/{event.get('id')}"
        name, args = self._calls.pop(call_id, (str(event.get("name") or ""), None))
        if name == "todo_write":
            return
        output = str(event.get("output") or "")
        failed = output.startswith(FAILED_PREFIXES) or "cancelled by" in output[:120]
        content: list[dict[str, Any]] = []
        if not failed and isinstance(args, dict) and name in ("edit_file", "write_file") and args.get("path"):
            path = str((Path(self.session.cwd) / str(args["path"])).resolve())
            old, new = (args.get("old_text"), args.get("new_text")) if name == "edit_file" else (None, args.get("content"))
            if isinstance(new, str):
                content.append({"type": "diff", "path": path, "oldText": old if isinstance(old, str) else None, "newText": new})
        content.append({"type": "content", "content": {"type": "text", "text": output[:OUTPUT_CHARS]}})
        self.update("tool_call_update", toolCallId=call_id, status="failed" if failed else "completed", content=content)

    # --- approvals and questions -> the editor's permission prompt
    def _ask(self, event: dict[str, Any]) -> None:
        agent, kind, detail = str(event.get("agent") or "main"), str(event.get("kind")), str(event.get("detail") or "")
        if kind == "question":
            choices = [str(o) for o in event.get("options") or []]
            options = [{"optionId": f"answer:{i}", "name": text[:80], "kind": "allow_once"} for i, text in enumerate(choices)]
            options.append({"optionId": "skip", "name": "Skip (Dcode decides)", "kind": "reject_once"})
            call = {"toolCallId": self._last_call.get(agent, f"question/{event['id']}"), "title": detail[:300], "kind": "other",
                    "status": "pending"}
        else:
            options = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]
            if kind != "db_write":  # a database change is confirmed one by one, always
                options.append({"optionId": "session", "name": "Allow for this session", "kind": "allow_always"})
            options.append({"optionId": "deny", "name": "Deny", "kind": "reject_once"})
            call = {"toolCallId": self._last_call.get(agent, f"approval/{event['id']}"), "status": "pending",
                    "title": f"{APPROVAL_TITLES.get(kind, kind)}: {' '.join(detail.split())[:200]}",
                    "content": [{"type": "content", "content": {"type": "text", "text": detail[:OUTPUT_CHARS]}}]}
        result = self.conn.request("session/request_permission", {"sessionId": self.id, "toolCall": call, "options": options})
        outcome = (result or {}).get("outcome") if isinstance((result or {}).get("outcome"), dict) else {}
        picked = str(outcome.get("optionId") or "") if outcome.get("outcome") == "selected" else ""
        if kind == "question":
            index = int(picked[7:]) if picked.startswith("answer:") and picked[7:].isdigit() else -1
            answer = choices[index] if 0 <= index < len(choices) else ""
            self.session.resolve_approval(event["id"], bool(answer), by="acp", answer=answer)
        else:
            self.session.resolve_approval(event["id"], picked in ("allow", "session"), by="acp",
                                          scope="session" if picked == "session" else "once")

    # --- one prompt turn
    def prompt(self, text: str) -> str:
        """Run one message to the end. Returns the ACP stop reason."""
        session = self.session
        self.cancelled = False
        while True:
            self._turn_done.clear()
            command, _, arg = text.partition(" ")
            if command == "/compact":
                session.compact(arg.strip(), source="acp")
            elif command == "/init":
                session.init_project(source="acp")
            elif command == "/review":
                session.review(source="acp")
            elif command.startswith("/") and command[1:].lower() in session.custom_commands():
                session.run_custom_command(command[1:], arg.strip(), source="acp")
            else:
                session.submit(text, source="acp")
            self._turn_done.wait()
            while not session.idle:  # a queued follow-up (plan approval) keeps the turn open
                self._turn_done.clear()
                self._turn_done.wait(0.5)
            if self.cancelled:
                return "cancelled"
            if not session.plan_pending:
                return "end_turn"
            decision = self._plan_decision()
            if decision == "keep":
                session.resolve_plan("keep", source="acp")
                return "end_turn"
            session.plan_pending = False  # implement in the same turn, in the mode that was picked
            session.set_mode(decision, source="acp")
            text = "The plan is approved. Implement it now, step by step."

    def _plan_decision(self) -> str:
        result = self.conn.request("session/request_permission", {
            "sessionId": self.id,
            "toolCall": {"toolCallId": f"plan/{uuid.uuid4().hex[:8]}", "title": "Ready to implement this plan?",
                         "kind": "switch_mode", "status": "pending"},
            "options": [{"optionId": "accept_edits", "name": "Yes, and auto-accept edits", "kind": "allow_always"},
                        {"optionId": "manual", "name": "Yes, and ask before each edit", "kind": "allow_once"},
                        {"optionId": "keep", "name": "No, keep planning", "kind": "reject_once"}],
        })
        outcome = (result or {}).get("outcome") if isinstance((result or {}).get("outcome"), dict) else {}
        picked = outcome.get("optionId") if outcome.get("outcome") == "selected" else "keep"
        return picked if picked in ("accept_edits", "manual") else "keep"

    def cancel(self) -> None:
        self.cancelled = True
        self.session.interrupt(source="acp")

    def commands(self) -> list[dict[str, Any]]:
        custom = [{"name": c.name, "description": c.description or "Custom command", "input": {"hint": "arguments"}}
                  for c in self.session.custom_commands().values()]
        return BUILTIN_COMMANDS + custom


class Agent:
    """The ACP agent: answers the editor's requests, owns the sessions."""

    def __init__(self, reader: TextIO, writer: TextIO, session_factory: Any = None) -> None:
        self.conn = Connection(reader, writer)
        self.bridges: dict[str, Bridge] = {}
        self._session_factory = session_factory or self._new_session

    @staticmethod
    def _new_session(cwd: str, servers: dict[str, dict[str, Any]]) -> Any:
        from .config import load_config
        from .session import Session

        config = load_config()
        if config is None:
            raise RuntimeError("Dcode is not set up yet: run `dcode` once in a terminal, or set DCODE_BASE_URL, "
                               "DCODE_API_KEY and DCODE_MODEL for the editor.")
        os.chdir(cwd)  # Dcode's project folder is the process's folder: one project per editor connection
        if servers:
            from .mcp import clean_servers

            config.mcp = {"servers": {**clean_servers(servers), **(config.mcp or {}).get("servers", {})}}
        return Session(config)

    def serve(self) -> None:
        for line in self.reader_lines():
            try:
                message = json.loads(line)
            except ValueError:
                self.conn.send({"id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(message, dict):
                continue
            if "method" not in message:
                self.conn.resolve(message)  # the editor's answer to one of our requests
            elif "id" in message:
                threading.Thread(target=self._handle, args=(message,), name="dcode-acp-request", daemon=True).start()
            else:
                self._notified(message)
        self.close()

    def reader_lines(self):
        try:
            yield from self.conn.reader
        except (OSError, ValueError):
            return

    def close(self) -> None:
        self.conn.abandon()
        for bridge in list(self.bridges.values()):
            bridge.session.close()
        self.bridges.clear()

    def _notified(self, message: dict[str, Any]) -> None:
        if message.get("method") == "session/cancel":
            bridge = self.bridges.get(str((message.get("params") or {}).get("sessionId")))
            if bridge is not None:
                bridge.cancel()

    def _handle(self, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            result = self._dispatch(method, params)
        except LookupError as exc:
            return self.conn.send({"id": message["id"], "error": {"code": -32601 if method == str(exc) else -32602, "message": f"Unknown: {exc}"}})
        except Exception as exc:  # report it to the editor instead of dying
            return self.conn.send({"id": message["id"], "error": {"code": -32603, "message": str(exc)}})
        self.conn.send({"id": message["id"], "result": result})
        if method == "session/new":  # after the reply (the editor now knows the session): its /commands
            bridge = self.bridges[result["sessionId"]]
            bridge.update("available_commands_update", availableCommands=bridge.commands())

    def _bridge(self, params: dict[str, Any]) -> Bridge:
        bridge = self.bridges.get(str(params.get("sessionId")))
        if bridge is None:
            raise LookupError(f"session {params.get('sessionId')}")
        return bridge

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": False,
                                      "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True}},
                "agentInfo": {"name": "dcode", "title": "Dcode", "version": __version__},
                "authMethods": [],
            }
        if method == "authenticate":
            return {}
        if method == "session/new":
            cwd = str(params.get("cwd") or os.getcwd())
            if not os.path.isdir(cwd):
                raise ValueError(f"The folder {cwd} does not exist.")
            session = self._session_factory(cwd, _editor_servers(params.get("mcpServers")))
            bridge = self.bridges[session.id] = Bridge(self.conn, session)
            return {"sessionId": session.id, "modes": {
                "currentModeId": session.mode,
                "availableModes": [{"id": m, "name": MODE_LABELS.get(m, m), "description": MODE_NOTES.get(m, "")} for m in MODES]}}
        if method == "session/prompt":
            bridge = self._bridge(params)
            text = prompt_text(params.get("prompt"), bridge.session.cwd)
            if not text:
                return {"stopReason": "end_turn"}
            return {"stopReason": bridge.prompt(text)}
        if method == "session/set_mode":
            bridge = self._bridge(params)
            bridge.session.set_mode(str(params.get("modeId") or ""), source="acp")
            return {}
        raise LookupError(method)


def _editor_servers(value: Any) -> dict[str, dict[str, Any]]:
    """MCP servers the editor passes along (stdio ones): ACP's list shape as Dcode's config shape."""
    servers: dict[str, dict[str, Any]] = {}
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and item.get("name") and isinstance(item.get("command"), str):
            env = {str(e.get("name")): str(e.get("value") or "") for e in item.get("env") or [] if isinstance(e, dict) and e.get("name")}
            servers[str(item["name"])] = {"command": [item["command"], *[str(a) for a in item.get("args") or []]], "env": env}
    return servers


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print("usage: dcode acp   (started by an editor: speaks the Agent Client Protocol on stdin/stdout)")
        return 0
    protocol_out = sys.stdout
    sys.stdout = sys.stderr  # nothing but protocol messages may reach the editor's pipe
    try:
        Agent(sys.stdin, protocol_out).serve()
    except KeyboardInterrupt:
        pass
    return 0
