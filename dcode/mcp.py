"""External tools from local MCP servers (Model Context Protocol, stdio transport).

A server is a program on this machine that Dcode starts and talks to over its standard input
and output, one JSON-RPC message per line. Nothing here needs a network.

Small-context rule: a server can offer dozens of tools with long schemas, and every schema in
the tool list is re-sent on each model turn. So the model gets only an *index* (server and tool
names) in its prompt, plus two fixed tools: ``mcp_tools`` reads one tool's description and
arguments when it is needed, ``mcp_call`` runs it.

Servers come only from ``~/.dcode/config.json`` ("mcp" → "servers"): starting one runs a
command, so a project folder must never be able to add one. Standard library only.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from typing import Any

from . import __version__

PROTOCOL_VERSION = "2025-06-18"
START_TIMEOUT = 30.0     # seconds for a server to start and list its tools
CALL_TIMEOUT = 60.0      # default seconds for one tool call
MAX_TOOLS = 200          # per server; more than this is cut (and said so)
APPROVE_CHOICES = ("ask", "allow")


class MCPError(Exception):
    """An MCP server failed or refused; the message is for the model and the user."""


# --- config ---------------------------------------------------------------------------------

def clean_name(value: Any) -> str:
    name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value or "").strip().lower())
    return "-".join(part for part in name.split("-") if part)[:40]


def clean_servers(value: Any) -> dict[str, dict[str, Any]]:
    """Keep well-formed server entries: ``{name: {command: [...], env: {}, enabled, approve, timeout}}``."""
    servers: dict[str, dict[str, Any]] = {}
    if not isinstance(value, dict):
        return servers
    for raw_name, spec in value.items():
        name = clean_name(raw_name)
        if not name or not isinstance(spec, dict):
            continue
        command = spec.get("command")
        if isinstance(command, str):
            try:
                command = shlex.split(command)
            except ValueError:
                continue
        if isinstance(command, list) and isinstance(spec.get("args"), list):
            command = [*command, *spec["args"]]  # the {"command": "x", "args": [...]} shape other tools use
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            continue
        env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        try:
            timeout = float(spec.get("timeout") or CALL_TIMEOUT)
        except (TypeError, ValueError):
            timeout = CALL_TIMEOUT
        servers[name] = {
            "command": command,
            "env": {str(k): str(v) for k, v in env.items()},
            "enabled": bool(spec.get("enabled", True)),
            "approve": spec.get("approve") if spec.get("approve") in APPROVE_CHOICES else "ask",
            "timeout": max(1.0, min(timeout, 3600.0)),
        }
    return servers


# --- one server -----------------------------------------------------------------------------

class Server:
    def __init__(self, name: str, spec: dict[str, Any], cwd: str, env: dict[str, str] | None = None) -> None:
        self.name = name
        self.spec = spec
        self.cwd = cwd
        self.env = env
        self.state = "starting"   # starting | ready | failed | stopped
        self.error = ""
        self.info: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.cut = 0              # tools left out because the server offers too many
        self.process: subprocess.Popen | None = None
        self._next_id = 0
        self._waiting: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._stderr: list[str] = []

    # --- lifecycle
    def start(self) -> None:
        """Start the program, shake hands and read its tool list. Sets ``state`` instead of raising."""
        try:
            self.process = subprocess.Popen(
                self.spec["command"], cwd=self.cwd, env={**(self.env or os.environ), **self.spec.get("env", {})},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True)
        except OSError as exc:
            return self._fail(f"couldn't start {self.spec['command'][0]}: {exc.strerror or exc}")
        threading.Thread(target=self._read, name=f"dcode-mcp-{self.name}", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name=f"dcode-mcp-{self.name}-err", daemon=True).start()
        try:
            result = self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": {"name": "dcode", "version": __version__},
            }, timeout=START_TIMEOUT)
            self.info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
            self.notify("notifications/initialized")
            self.refresh_tools()
            self.state = "ready"
        except MCPError as exc:
            self._fail(str(exc))

    def _fail(self, reason: str) -> None:
        self.error = reason
        self.state = "failed"
        self.stop(state="failed")

    def stop(self, state: str = "stopped") -> None:
        process, self.process = self.process, None
        self.state = state
        for waiter in list(self._waiting.values()):
            waiter.put({"gone": f"the MCP server '{self.name}' was stopped"})
        if process is None:
            return
        try:
            process.stdin.close()  # the polite way: a stdio server exits when its input ends
        except OSError:
            pass
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    # --- wire
    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            raise MCPError(self._gone())
        try:
            with self._lock:
                process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(self._gone()) from exc

    def _gone(self) -> str:
        tail = " — ".join(self._stderr[-3:])
        return f"the MCP server '{self.name}' is not running" + (f" ({tail[:300]})" if tail else "")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._waiting[request_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            try:
                reply = waiter.get(timeout=timeout)
            except queue.Empty:
                try:
                    self.notify("notifications/cancelled", {"requestId": request_id, "reason": "timed out"})
                except MCPError:
                    pass
                raise MCPError(f"'{self.name}' did not answer {method} within {timeout:.0f}s") from None
        finally:
            self._waiting.pop(request_id, None)
        if "gone" in reply:
            raise MCPError(reply["gone"])
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {"message": str(reply["error"])}
            raise MCPError(f"'{self.name}' refused {method}: {str(error.get('message') or error)[:500]}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def _read(self) -> None:
        process = self.process
        if process is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue  # a server that prints logs to stdout: skip what isn't a message
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                self._answer(message)
            elif "id" in message:
                waiter = self._waiting.get(message["id"])
                if waiter is not None:
                    waiter.put(message)
            elif message.get("method") == "notifications/tools/list_changed":
                threading.Thread(target=self._quiet_refresh, daemon=True).start()
        if self.state in ("starting", "ready"):  # the program ended on its own
            self.error = self._gone()
            self.state = "failed"
        for waiter in list(self._waiting.values()):
            waiter.put({"gone": self._gone()})

    def _answer(self, message: dict[str, Any]) -> None:
        """The few things a server may ask of us."""
        method = message.get("method")
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "ping":
            reply["result"] = {}
        elif method == "roots/list":
            reply["result"] = {"roots": [{"uri": "file://" + self.cwd, "name": os.path.basename(self.cwd) or self.cwd}]}
        else:
            reply["error"] = {"code": -32601, "message": f"Dcode does not support {method}"}
        try:
            self._send(reply)
        except MCPError:
            pass

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None:
            return
        for line in process.stderr:
            line = line.strip()
            if line:
                self._stderr = (self._stderr + [line[:300]])[-20:]

    # --- tools
    def refresh_tools(self) -> None:
        tools: list[dict[str, Any]] = []
        cursor, total = None, 0
        for _ in range(50):  # pages
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=START_TIMEOUT)
            for tool in result.get("tools") or []:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"]:
                    total += 1
                    if len(tools) < MAX_TOOLS:
                        tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools, self.cut = tools, total - len(tools)

    def _quiet_refresh(self) -> None:
        try:
            self.refresh_tools()
        except MCPError:
            pass

    def tool(self, name: str) -> dict[str, Any] | None:
        return next((t for t in self.tools if t["name"] == name), None)

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (text for the model, whether the tool reported an error)."""
        result = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout=float(self.spec.get("timeout") or CALL_TIMEOUT))
        return result_text(result), bool(result.get("isError"))


def result_text(result: dict[str, Any]) -> str:
    """A tool result as text: the text parts, and a note for anything that isn't text."""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource":
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            parts.append(str(resource.get("text") or f"[resource {resource.get('uri', '')}: not text, left out]"))
        elif kind == "resource_link":
            parts.append(f"[link: {item.get('uri', '')} {item.get('name', '')}]".replace(" ]", "]"))
        else:
            parts.append(f"[{kind or 'content'} left out: Dcode passes text only]")
    text = "\n".join(p for p in parts if p).strip()
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], ensure_ascii=False, indent=1)
    return text or "(no output)"


def read_only(tool: dict[str, Any]) -> bool:
    """A tool that says it changes nothing (allowed in plan mode). The server's word, nothing more."""
    notes = tool.get("annotations")
    return isinstance(notes, dict) and notes.get("readOnlyHint") is True


def describe_tool(server: str, tool: dict[str, Any]) -> str:
    """Everything the model needs to call one tool."""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {"type": "object"}
    lines = [f"{server} · {tool['name']}" + (" (read-only)" if read_only(tool) else "")]
    if tool.get("description"):
        lines.append(" ".join(str(tool["description"]).split()))
    lines.append("Arguments (JSON Schema): " + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    lines.append(f'Call it with mcp_call {{"server": "{server}", "tool": "{tool["name"]}", "arguments": {{…}}}}')
    return "\n".join(lines)


# --- all servers of a session ---------------------------------------------------------------

class Manager:
    """The MCP servers of one session: started in the background, stopped with it."""

    def __init__(self, servers: dict[str, dict[str, Any]], cwd: str, env: dict[str, str] | None = None) -> None:
        self.cwd = cwd
        self.config = json.loads(json.dumps(servers))  # our own copy: the caller's dict is edited by /mcp
        self.servers = {name: Server(name, spec, cwd, env) for name, spec in servers.items() if spec.get("enabled", True)}
        self._started = False

    def __bool__(self) -> bool:
        return bool(self.servers)

    def start(self, wait: float = 0.0) -> Manager:
        """Start every server on its own thread; ``wait`` blocks up to that long for them to settle."""
        if not self._started:
            self._started = True
            for server in self.servers.values():
                threading.Thread(target=server.start, name=f"dcode-mcp-start-{server.name}", daemon=True).start()
        deadline = time.time() + wait
        while time.time() < deadline and any(s.state == "starting" for s in self.servers.values()):
            time.sleep(0.05)
        return self

    def close(self) -> None:
        for server in self.servers.values():
            server.stop()

    def restart(self, name: str = "") -> None:
        for server_name in [name] if name else list(self.servers):
            old = self.servers.get(server_name)
            if old is None:
                continue
            old.stop()
            self.servers[server_name] = fresh = Server(server_name, old.spec, self.cwd, old.env)
            threading.Thread(target=fresh.start, name=f"dcode-mcp-start-{server_name}", daemon=True).start()

    def status(self) -> list[dict[str, Any]]:
        rows = []
        for name, spec in self.config.items():
            server = self.servers.get(name)
            rows.append({
                "name": name, "command": " ".join(shlex.quote(p) for p in spec["command"]), "approve": spec["approve"],
                "enabled": bool(spec.get("enabled", True)), "state": server.state if server else "off",
                "error": server.error if server else "", "title": str((server.info if server else {}).get("name") or ""),
                "tools": [{"name": t["name"], "description": " ".join(str(t.get("description") or "").split())[:160],
                           "read_only": read_only(t)} for t in (server.tools if server else [])],
            })
        return rows

    def index_text(self, max_chars: int = 1500) -> str:
        """Server and tool names for the prompt. Descriptions and arguments come from mcp_tools."""
        lines = []
        for name, server in self.servers.items():
            if server.state == "ready" and server.tools:
                names = [t["name"] for t in server.tools]
                shown = ", ".join(names[:25]) + (f" (+{len(names) - 25 + server.cut} more)" if len(names) > 25 or server.cut else "")
                lines.append(f"- {name}: {shown}")
            elif server.state == "starting":
                lines.append(f"- {name}: still starting (call mcp_tools to see its tools)")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0] + "\n- … (call mcp_tools for the rest)"

    def _server(self, name: str) -> Server:
        server = self.servers.get(clean_name(name))
        if server is None:
            if not name and len(self.servers) == 1:
                return next(iter(self.servers.values()))
            raise MCPError(f"No MCP server named '{name}'. Servers: {', '.join(self.servers) or '(none)'}.")
        deadline = time.time() + START_TIMEOUT
        while server.state == "starting" and time.time() < deadline:
            time.sleep(0.05)
        if server.state != "ready":
            raise MCPError(f"The MCP server '{server.name}' is not available: {server.error or server.state}.")
        return server

    def find(self, server: str, tool: str) -> tuple[Server, dict[str, Any]]:
        found = self._server(server)
        spec = found.tool(tool)
        if spec is None:
            close = [t["name"] for t in found.tools if tool.lower() in t["name"].lower()][:8]
            hint = f" Did you mean: {', '.join(close)}?" if close and tool else f" Its tools: {', '.join(t['name'] for t in found.tools[:40])}."
            raise MCPError(f"'{found.name}' has no tool '{tool}'.{hint}")
        return found, spec

    def describe(self, server: str = "", tool: str = "", max_chars: int = 6000) -> str:
        """mcp_tools: one tool in full, or a server's tools with a line each."""
        if not server and not tool and len(self.servers) != 1:
            return "\n".join(f"- {s.name}: {len(s.tools)} tools ({s.state})" for s in self.servers.values()) or "(no MCP servers)"
        if tool:
            found, spec = self.find(server, tool)
            return describe_tool(found.name, spec)[:max_chars]
        found = self._server(server)
        lines = [f"{found.name}: {len(found.tools)} tools. Call mcp_tools with a tool name for its arguments."]
        room = max(40, (max_chars - 200) // max(1, len(found.tools)) - 30)
        for spec in found.tools:
            about = " ".join(str(spec.get("description") or "").split())
            lines.append(f"- {spec['name']}" + (f": {about[:room]}" if about else ""))
        if found.cut:
            lines.append(f"({found.cut} more tools were left out: the server offers too many)")
        return "\n".join(lines)[:max_chars]
