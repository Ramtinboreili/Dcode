"""A live Dcode session, shared by the terminal and the web UI.

The session owns the agent, a task queue processed by one worker thread, an
event log every surface can subscribe to, and approvals that whichever
surface answers first resolves.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import tools
from .agents import DEFAULT_MODE, INIT_PROMPT, MODES
from .checkpoints import CheckpointStore
from .commands import expand, load_commands
from .config import AppConfig, load_config, load_thread, run_dir, save_thread, thread_path, threads_dir
from .engine import Agent, git_summary
from .jobs import JobManager
from .llm import Interrupted, LLMClient
from .memory import MemoryStore
from .models import ROUTES, ModelPool, decide
from .tools import _truncate, workspace_root
from .transcripts import SHELL_NOTE, HelperLog, transcript_from_messages

EVENT_BUFFER = 4000
# Approvals that can't be granted "for this session": each one must be seen (database changes).
ALWAYS_ASK_KINDS = {"db_write"}
PLAN_GO = "The plan is approved. Implement it now, step by step."  # stays on the model that planned
SHELL_PREVIEW_CHARS = 8000  # of that command's output sent to the terminal and the web UI
TITLE_PROMPT = (
    "Write a short title (2-6 words) for a coding chat that starts with the user's message below. "
    "Reply with the title only: no quotes, no punctuation at the end. Use the same language as the message."
)
_FILLER = re.compile(r"^(please|pls|can you|could you|would you|hey|hi|hello|dcode)[\s,:]+", re.IGNORECASE)


def heuristic_title(text: str, max_words: int = 6, max_chars: int = 48) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "New chat")
    line = re.sub(r"https?://\S+|`[^`]*`", "", line)
    previous = None
    while previous != line:
        previous, line = line, _FILLER.sub("", line).strip()
    words = line.split()[:max_words]
    title = " ".join(words).strip(" .,:;!?-") or "New chat"
    if len(title) > max_chars:
        title = title[:max_chars].rsplit(" ", 1)[0]
    return title[:1].upper() + title[1:]


def clean_title(text: str) -> str:
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    line = re.sub(r"^(title\s*:\s*)", "", line, flags=re.IGNORECASE).strip().lstrip("-*•").strip(" .\"'`*#“”«»")
    if not line or len(line) > 60 or len(line.split()) > 8:
        return ""
    return line
Listener = Callable[[dict[str, Any]], None]


class EventBus:
    """Append-only event log with replay (for late web clients) and live listeners."""

    def __init__(self, maxlen: int = EVENT_BUFFER) -> None:
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._seq = 0
        self._cond = threading.Condition()
        self._listeners: list[Listener] = []

    @property
    def seq(self) -> int:
        return self._seq

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def emit(self, type: str, **data: Any) -> dict[str, Any]:
        with self._cond:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "type": type, **data}
            self._events.append(event)
            self._cond.notify_all()
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass
        return event

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._cond:
            return [e for e in self._events if e["seq"] > seq]

    def wait_since(self, seq: int, timeout: float) -> list[dict[str, Any]]:
        with self._cond:
            if self._seq <= seq:
                self._cond.wait(timeout)
            return [e for e in self._events if e["seq"] > seq]


class _Approval:
    def __init__(self, approval_id: str, kind: str, detail: str, agent: str) -> None:
        self.id = approval_id
        self.kind = kind
        self.detail = detail
        self.agent = agent
        self.done = threading.Event()
        self.result = False
        self.options: list[str] = []  # kind "question": choices offered to the user
        self.answer = ""

    def as_dict(self) -> dict[str, Any]:
        data = {"id": self.id, "kind": self.kind, "detail": self.detail, "agent": self.agent}
        if self.kind == "question":
            data["options"] = self.options
        return data


class Session:
    def __init__(self, config: AppConfig, llm: LLMClient | None = None) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.pid = os.getpid()
        self.cwd = str(workspace_root())
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.config = config
        self.llm = llm or LLMClient(config)
        self.memory = MemoryStore(workspace_root())
        self.bus = EventBus()
        self.git = git_summary()
        self.mode = "auto" if config.experimental.get("auto_approve_commands") else DEFAULT_MODE
        self.checkpoints = CheckpointStore()
        self.jobs = JobManager(run_dir() / "jobs", on_exit=self._job_exited)
        self.plan_pending = False
        self.mcp = self._start_mcp(config)  # external tool servers; None when there are none
        self.models = ModelPool(config, self.llm)  # profiles: reasoning, utility and helper models
        self.route = config.models.get("router", "auto")  # auto | off | always (this session; /models route)
        self._reasoned = False  # the last task ran on the reasoning model (short follow-ups stay there)
        self.agent = Agent(config, self.llm, self, self.memory, git=self.git)
        self.thread_name = self._timestamp()
        self.thread_created_at = datetime.now(timezone.utc).isoformat()
        self.last_summary = ""
        self.port: int | None = None
        self.ui_url: str | None = None

        self._queue: queue.Queue[tuple | None] = queue.Queue()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._closed = threading.Event()
        self._approvals: dict[str, _Approval] = {}
        self.allowed_kinds: set[str] = set()  # "allow for this session" choices
        self._approvals_lock = threading.Lock()
        self._attention: list[Callable[[], None]] = []
        self._task_tools: collections.Counter[str] = collections.Counter()  # for the usage log (stats.py)
        self._helper_tokens = {"prompt": 0, "completion": 0}
        self.helpers = HelperLog()  # each helper's own steps, so its session can be opened later
        self.bus.subscribe(self._on_event)
        self._worker = threading.Thread(target=self._work, name="dcode-worker", daemon=True)
        self._worker.start()

    # --- Host protocol (called by the engine) ----------------------------------------
    def emit(self, type: str, **data: Any) -> None:
        self.bus.emit(type, **data)

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def request_approval(self, kind: str, detail: str, agent: str) -> bool:
        if kind in self.allowed_kinds:
            self.emit("approval_auto", kind=kind, detail=detail, agent=agent)
            return True
        approval = _Approval(uuid.uuid4().hex[:8], kind, detail, agent)
        with self._approvals_lock:
            self._approvals[approval.id] = approval
        self._fire_hooks("approval_needed", tool=kind, detail=detail)
        # No attention ping here: the terminal polls for approvals while a task runs,
        # and a ping would close the approval menu it is about to open.
        self.emit("approval_request", **approval.as_dict())
        while not approval.done.wait(0.2):
            if self._stop.is_set() or self._closed.is_set():
                self.resolve_approval(approval.id, False, by="interrupt")
                break
        return approval.result

    def ask_user(self, question: str, options: list[str], agent: str) -> str:
        """The model asks the user something (ask_user tool). Returns the answer, "" if skipped."""
        approval = _Approval(uuid.uuid4().hex[:8], "question", question, agent)
        approval.options = list(options)
        with self._approvals_lock:
            self._approvals[approval.id] = approval
        self._fire_hooks("approval_needed", tool="question", detail=question)
        self.emit("approval_request", **approval.as_dict())
        while not approval.done.wait(0.2):
            if self._stop.is_set() or self._closed.is_set():
                self.resolve_approval(approval.id, False, by="interrupt")
                break
        return approval.answer if approval.result else ""

    # --- commands (from CLI or web) ----------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    @property
    def idle(self) -> bool:
        """No task running and none queued."""
        return not self._busy.is_set() and self._queue.empty()

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._approvals_lock:
            return [a.as_dict() for a in self._approvals.values()]

    def on_attention(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when the session needs the terminal's attention."""
        self._attention.append(callback)

    def submit(self, text: str, source: str = "cli") -> None:
        text = text.strip()
        if text:
            self._queue.put(("task", text, source))
            self._notify_attention()

    def _start_mcp(self, config: AppConfig) -> Any:
        servers = (config.mcp or {}).get("servers") or {}
        if not any(spec.get("enabled", True) for spec in servers.values()):
            return None
        from .mcp import Manager

        return Manager(servers, self.cwd).start()

    def run_shell(self, command: str, source: str = "cli") -> None:
        """"!command": the user runs a shell command themselves; the model sees it and its output."""
        command = command.strip()
        if command:
            self._queue.put(("shell", command, source))
            self._notify_attention()

    def _run_shell_item(self, command: str, source: str) -> None:
        self.emit("user_message", text=f"!{command}", source=source)
        call_id = f"shell_{uuid.uuid4().hex[:8]}"
        self.emit("tool_call", agent="main", id=call_id, name="run_command", args={"command": command}, by="user")
        output = self._shell(command)
        # Recorded as a tool call, so compaction can shrink the output like any other.
        self.agent.messages += [
            {"role": "user", "content": f"{SHELL_NOTE}{command}]"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]},
            {"role": "tool", "tool_call_id": call_id, "name": "run_command", "content": output},
            {"role": "assistant", "content": "Noted."},
        ]
        self.emit("tool_result", agent="main", id=call_id, name="run_command", output=output[:SHELL_PREVIEW_CHARS],
                  by="user")

    def _shell(self, command: str) -> str:
        """Run the user's own command: no approval, no time limit, but Ctrl+C / Stop ends it."""
        try:
            process = subprocess.Popen(
                command, shell=True, cwd=str(workspace_root()), text=True, errors="replace",
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as exc:
            return f"Could not start: {exc}"
        chunks: list[str] = []
        reader = threading.Thread(target=lambda: chunks.extend(iter(process.stdout.readline, "")), daemon=True)
        reader.start()
        stopped = False
        while process.poll() is None:
            if self._stop.is_set() or self._closed.is_set():
                stopped = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            time.sleep(0.05)
        reader.join(timeout=2)
        output = "".join(chunks).strip()
        if stopped:
            output += "\n(stopped by the user)"
        elif process.returncode:
            output += f"\n(return code: {process.returncode})"
        output = output.strip()
        if len(output) > tools.MAX_OUTPUT_CHARS:
            half = tools.MAX_OUTPUT_CHARS // 2
            output = output[:half] + f"\n... (truncated {len(output) - 2 * half} chars) ...\n" + output[-half:]
        return output or "(no output)"

    # --- modes, plans, compaction, undo, git ----------------------------------------
    def set_mode(self, mode: str, source: str = "cli") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
        if mode == self.mode:
            return
        self.mode = mode
        if mode != "plan":
            self.plan_pending = False
        self.agent.refresh_system_message()
        self.emit("mode", mode=mode, by=source)

    def cycle_mode(self, source: str = "cli") -> str:
        self.set_mode(MODES[(MODES.index(self.mode) + 1) % len(MODES)], source)
        return self.mode

    def resolve_plan(self, decision: str, source: str = "cli") -> None:
        """After a plan-mode answer: 'accept_edits' / 'manual' / 'auto' runs it, 'keep' stays in plan mode."""
        if not self.plan_pending:
            return
        self.plan_pending = False
        self.emit("plan_resolved", decision=decision, by=source)
        if decision in ("accept_edits", "manual", "auto"):
            self.set_mode(decision, source)
            self.submit(PLAN_GO, source=source)

    def init_project(self, source: str = "cli") -> str:
        """/init: have the agent write (or refresh) AGENTS.md. Returns the file it will write."""
        if self.mode == "plan":
            raise RuntimeError("Plan mode is read-only. Switch mode (Shift+Tab or /mode) to run /init.")
        root = workspace_root()
        name = "DCODE.md" if (root / "DCODE.md").is_file() else "AGENTS.md"
        exists = (root / name).is_file()
        extra = " CLAUDE.md exists: use it as a source." if name == "AGENTS.md" and (root / "CLAUDE.md").is_file() else ""
        prompt = INIT_PROMPT.format(action="Update" if exists else "Create", file=name, extra=extra,
                                    keep=" (keep what is still correct, fix and add the rest)" if exists else "")
        self._queue.put(("init", prompt, source))
        self._notify_attention()
        return name

    def custom_commands(self) -> dict:
        try:
            return load_commands(workspace_root())
        except OSError:
            return {}

    def run_custom_command(self, name: str, arguments: str = "", source: str = "cli") -> None:
        """/name from .dcode/commands or ~/.dcode/commands: expanded and run on the worker."""
        name = name.lstrip("/").lower()
        if name not in self.custom_commands():
            raise RuntimeError(f"Unknown command /{name}")
        self._queue.put(("command", (name, arguments), source))
        self._notify_attention()

    def _may_run_shell(self, command: str, origin: str) -> str | None:
        """Gate a command template's !`shell`: same rules and mode as the agent's run_command."""
        decision, pattern = self.agent.rules.command(command)
        if decision == "deny":
            return f"(not run: the rule '{pattern}' denies it)"
        if decision == "allow" or (self.mode == "auto" and decision != "ask"):
            return None
        kind = "rule" if decision == "ask" else "run_command"
        approved = self.request_approval(kind, f"{command}   (from {origin})", "main")
        return None if approved else "(not run: the user said no)"

    def _run_command_item(self, name: str, arguments: str, source: str) -> None:
        shown = f"/{name}" + (f" {arguments}" if arguments else "")
        self.emit("user_message", text=shown, source=source)
        command = self.custom_commands().get(name)
        if command is None:
            self.emit("error", text=f"The command /{name} no longer exists.")
            return
        if self._default_name():
            self._set_title(command.description[:48] or f"/{name}")
        prompt = expand(command, arguments, workspace_root(), lambda cmd: self._may_run_shell(cmd, shown))
        if command.agent and command.agent in self.agent.specs and self.config.agents.get("enabled", True):
            self._run_helper(command.agent, prompt)
        else:
            result = self._run_task(prompt)
            self._fire_hooks("task_done", text=result.text[:4000], task=shown)
        if self.mode == "plan":
            self.plan_pending = True
            self.emit("plan_ready")

    def compact(self, focus: str = "", source: str = "cli") -> None:
        self._queue.put(("compact", focus, source))
        self._notify_attention()

    def undo(self) -> tuple[str, list[str]] | None:
        if self.busy:
            raise RuntimeError("Dcode is busy. Stop the current task first.")
        result = self.checkpoints.undo()
        if result is None:
            return None
        label, paths = result
        shown = [self._rel(p) for p in paths]
        # Tell the model, so it doesn't assume its edits are still there.
        self.agent.messages.append({"role": "user", "content": (
            f"[The user undid your changes from the task \"{label}\". These files are back to their "
            f"previous state: {', '.join(shown)}]")})
        self.agent.messages.append({"role": "assistant", "content": "Noted — those changes were reverted."})
        self.emit("undo", label=label, files=shown)
        self.save()
        return label, shown

    def redo(self) -> tuple[str, list[str]] | None:
        """Put back what the last /undo removed (until Dcode changes a file again)."""
        if self.busy:
            raise RuntimeError("Dcode is busy. Stop the current task first.")
        result = self.checkpoints.redo()
        if result is None:
            return None
        label, paths = result
        shown = [self._rel(p) for p in paths]
        self.agent.messages.append({"role": "user", "content": (
            f"[The user brought back your changes from the task \"{label}\" (redo). These files have "
            f"your changes again: {', '.join(shown)}]")})
        self.agent.messages.append({"role": "assistant", "content": "Noted — those changes are back."})
        self.emit("redo", label=label, files=shown)
        self.save()
        return label, shown

    def suggest_commit_message(self) -> str:
        from .tools import execute_tool

        diff = execute_tool("git_diff", {})
        status = execute_tool("git_status", {})
        if diff in ("", "(no output)") and "??" not in status:
            return ""
        prompt = ("Write a git commit message for these changes: a concise imperative subject line "
                  "(max 70 chars), optionally a blank line and 1-4 bullet points. Reply with the message only.")
        try:
            text = self.models.utility_llm().complete_text(
                [{"role": "system", "content": prompt},
                 {"role": "user", "content": _truncate(f"{status}\n\n{diff}", int(self.config.context_window * 3.5 * 0.5))}],
                max_tokens=200,
            ).strip().strip("`")
        except Exception:
            text = ""
        return text or "Update files"

    @staticmethod
    def _rel(path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(workspace_root()))
        except ValueError:
            return path

    def review(self, source: str = "cli") -> None:
        self._queue.put(("review", "", source))
        self._notify_attention()

    def resolve_approval(self, approval_id: str, approved: bool, by: str = "cli", scope: str = "once",
                         answer: str = "") -> bool:
        with self._approvals_lock:
            approval = self._approvals.pop(approval_id, None)
        if approval is None or approval.done.is_set():
            return False
        if approval.kind == "question":
            approval.answer = " ".join(str(answer or "").split())[:2000]
            approved = bool(approved and approval.answer)
        elif approved and scope == "session" and approval.kind not in ALWAYS_ASK_KINDS:
            self.allowed_kinds.add(approval.kind)
        approval.result = bool(approved)
        approval.done.set()
        extra = {"answer": approval.answer} if approval.kind == "question" else {}
        self.emit("approval_resolved", id=approval_id, approved=bool(approved), by=by, **extra)
        self._notify_attention()
        return True

    def interrupt(self, source: str = "cli") -> None:
        if self.busy:
            self._stop.set()
            self.emit("notice", level="warning", text=f"Interrupt requested ({source})")

    def new_thread(self) -> None:
        if self.busy:
            raise RuntimeError("Session is busy")
        self.save()
        self.agent.todos = []
        self.helpers.clear()
        self.agent.messages = [{"role": "system", "content": self.agent.system_prompt()}]
        self.thread_name = self._timestamp()
        self.thread_created_at = datetime.now(timezone.utc).isoformat()
        self.last_summary = ""
        self.emit("thread", name=self.thread_name, action="new")

    def rename(self, name: str) -> None:
        name = name.strip()
        if not name or name == self.thread_name:
            return
        old = thread_path(self.thread_name)
        self.thread_name = name
        if old.exists() and old != thread_path(name):
            old.unlink()
        self.save()
        self.emit("thread", name=self.thread_name, action="rename")

    def resume(self, thread_id: str, source: str = "cli") -> bool:
        if self.busy:
            raise RuntimeError("Session is busy")
        path = threads_dir() / f"{thread_id}.json"
        if not path.exists():
            return False
        payload = load_thread(path)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return False
        self.save()
        self.agent.messages = messages
        self.agent.todos = [t for t in payload.get("todos") or [] if isinstance(t, dict)]
        self.helpers.load(payload.get("helpers"))
        self.agent.refresh_system_message()
        self.thread_name = str(payload.get("name") or thread_id)
        self.thread_created_at = str(payload.get("created_at") or datetime.now(timezone.utc).isoformat())
        self.emit("thread", name=self.thread_name, action="resume", by=source,
                  items=transcript_from_messages(messages), helpers=self.helpers.summaries())
        return True

    def set_model(self, model: str | None = None, reasoning_effort: str | None = None,
                  route: str | None = None) -> None:
        if model:
            self.config.model = model
        if reasoning_effort:
            self.config.reasoning_effort = reasoning_effort
        if route in ROUTES:
            self.route = route
        self.agent.refresh_system_message()
        self.emit("config", model=self.config.model, reasoning_effort=self.config.reasoning_effort, route=self.route)

    def apply_config(self, config: AppConfig) -> None:
        """Swap in a new config (e.g. edited from the web UI) without losing history."""
        endpoint_changed = (config.base_url, config.api_key) != (self.config.base_url, self.config.api_key)
        if (config.mcp or {}).get("servers") != (self.mcp.config if self.mcp else {}):
            if self.mcp:
                self.mcp.close()
            self.mcp = self._start_mcp(config)
        if config.models.get("router") != self.config.models.get("router"):
            self.route = config.models.get("router", "auto")
        self.config = config
        self.llm = LLMClient(config) if endpoint_changed else self.llm
        self.llm.config = config
        self.models = ModelPool(config, self.llm, self.models.factory)
        self.agent.use(config, self.llm)
        self.agent.refresh_system_message()
        self.emit("config", model=config.model, reasoning_effort=config.reasoning_effort,
                  context_window=config.context_window, route=self.route)

    def reload_config(self) -> bool:
        config = load_config()
        if config is None:
            return False
        self.apply_config(config)
        return True

    def state(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pid": self.pid,
            "cwd": self.cwd,
            "model": self.config.model,
            "reasoning_effort": self.config.reasoning_effort,
            "route": self.route,
            "reasoning_model": self._reasoning_model(),
            "context_window": self.config.context_window,
            "context_used": self.agent.budget.usage(self.agent.messages, self.agent.tools()),
            "thread_name": self.thread_name,
            "started_at": self.started_at,
            "busy": self.busy,
            "approvals": self.pending_approvals(),
            "seq": self.bus.seq,
            "git": self.git,
            "agents_enabled": bool(self.config.agents.get("enabled", True)),
            "mode": self.mode,
            "jobs": [job.as_dict() for job in self.jobs.list()],
            "plan_pending": self.plan_pending,
            "can_undo": self.checkpoints.latest() is not None,
            "can_redo": self.checkpoints.can_redo(),
            "todos": self.agent.todos,
            "commands": [c.as_dict() for c in self.custom_commands().values()],
            "mcp": [{"name": s["name"], "state": s["state"], "tools": len(s["tools"])} for s in self.mcp.status()] if self.mcp else [],
        }

    def save(self) -> None:
        messages = self.agent.messages
        if len(messages) <= 1:
            return
        save_thread(
            self.thread_name,
            messages,
            created_at=self.thread_created_at,
            extra={"cwd": self.cwd, "model": self.config.model, "summary": self.last_summary,
                   "todos": self.agent.todos, "helpers": self.helpers.dump()},
        )

    def _fire_hooks(self, event: str, tool: str = "", **payload: Any) -> None:
        """Notification-style hooks run in the background: they must never slow Dcode down."""
        if not self.config.hooks:
            return
        from . import hooks

        def fire() -> None:
            try:
                hooks.run(self.config.hooks, event, workspace_root(), tool, session=self.id, **payload)
            except Exception:
                pass

        threading.Thread(target=fire, name=f"dcode-hook-{event}", daemon=True).start()

    def _job_exited(self, job) -> None:
        self.emit("job_exited", id=job.id, command=job.command, state=job.state(), exit_code=job.exit_code)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self.jobs.stop_all()
        if self.mcp:
            self.mcp.close()
        self._stop.set()
        self._closed.set()
        self._queue.put(None)
        self._worker.join(timeout=3)
        self.save()
        if self.last_summary:
            try:
                self.memory.save_session_summary(self.thread_name, self.last_summary)
            except OSError:
                pass

    # --- internals -------------------------------------------------------------------
    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    def _notify_attention(self) -> None:
        for callback in list(self._attention):
            try:
                callback()
            except Exception:
                pass

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        self.helpers.feed(event)
        if kind == "tool_call" and event.get("by") != "user":
            self._task_tools[str(event.get("name") or "")] += 1
        elif kind == "usage" and event.get("agent", "main") != "main":
            for key in self._helper_tokens:  # a helper's tokens count toward the task that asked for it
                self._helper_tokens[key] += int(event.get(key) or 0)
        elif kind == "usage":
            from . import stats

            stats.record({
                "session": self.id, "chat": self.thread_name, "cwd": self.cwd, "model": str(event.get("model") or ""),
                "prompt": int(event.get("prompt") or 0) + self._helper_tokens["prompt"],
                "completion": int(event.get("completion") or 0) + self._helper_tokens["completion"],
                "elapsed": float(event.get("elapsed") or 0.0), "files": len(event.get("files_changed") or []),
                "commands": int(event.get("commands") or 0), "helpers": int(event.get("helpers") or 0),
                "tools": dict(self._task_tools),
            })
            self._task_tools.clear()
            self._helper_tokens = {"prompt": 0, "completion": 0}
        if event["type"] == "summary":
            self.last_summary = str(event.get("text") or "")
            try:
                self.memory.save_session_summary(self.thread_name, self.last_summary)
            except OSError:
                pass

    def _work(self) -> None:
        while not self._closed.is_set():
            item = self._queue.get()
            if item is None:
                break
            kind, payload, source = item
            self._stop.clear()
            self._busy.set()
            self.emit("status", busy=True)
            try:
                if kind == "task":
                    first = not any(m.get("role") == "user" for m in self.agent.messages)
                    self.emit("user_message", text=payload, source=source)
                    if first and self._default_name():
                        self._auto_title(payload)
                    self.plan_pending = False
                    helper = self._helper_request(payload)
                    if helper:
                        self._run_helper(*helper)
                    else:
                        result = self._run_task(payload, routed=payload != PLAN_GO)
                        self._fire_hooks("task_done", text=result.text[:4000], task=payload[:2000])
                    if self.mode == "plan":
                        self.plan_pending = True
                        self.emit("plan_ready")
                elif kind == "shell":
                    self._run_shell_item(payload, source)
                elif kind == "command":
                    self.plan_pending = False
                    self._run_command_item(payload[0], payload[1], source)
                elif kind == "init":
                    self.emit("user_message", text="/init", source=source)
                    if self._default_name():
                        self._set_title("Project instructions (/init)")
                    self.agent.run_turn(payload)
                elif kind == "review":
                    self._run_review(source)
                elif kind == "compact":
                    self.emit("user_message", text="/compact" + (f" {payload}" if payload else ""), source=source)
                    report = self.agent.force_compact(payload)
                    if report is None:
                        self.emit("notice", level="info", text="Nothing to compact yet")
                    else:
                        self.emit("notice", level="success", text=(
                            f"Compacted the conversation: ~{report['before']:,} → ~{report['after']:,} tokens. "
                            "The summary was saved to memory."))
            except Interrupted:
                self._repair_history()
                self.emit("notice", level="warning", text="Task interrupted")
            except Exception as exc:
                self._repair_history()
                self.emit("error", text=f"Task failed: {exc}")
            finally:
                with self._approvals_lock:
                    leftovers = list(self._approvals)
                for approval_id in leftovers:
                    self.resolve_approval(approval_id, False, by="interrupt")
                self._busy.clear()
                self._stop.clear()
                try:
                    self.save()
                except OSError:
                    pass
                self.emit("status", busy=False)
                self._notify_attention()

    def _default_name(self) -> bool:
        return bool(re.fullmatch(r"\d{8}-\d{6}", self.thread_name))

    def _set_title(self, title: str) -> None:
        title = title.strip()
        if not title or title == self.thread_name:
            return
        old = thread_path(self.thread_name)
        self.thread_name = title
        if old.exists() and old != thread_path(title):
            old.unlink()
        self.emit("thread", name=title, action="title")

    def _auto_title(self, first_message: str) -> None:
        """Name the chat after its first message: instant heuristic, then a model-written title."""
        self._set_title(heuristic_title(first_message))
        expected = self.thread_name

        def refine() -> None:
            try:
                text = self.models.utility_llm().complete_text(
                    [{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": first_message[:1500]}],
                    max_tokens=60,
                )
            except Exception:
                return
            title = clean_title(text)
            if title and self.thread_name == expected and not self._closed.is_set():
                self._set_title(title)

        threading.Thread(target=refine, name="dcode-title", daemon=True).start()

    def _reasoning_model(self) -> str:
        target = self.models.role("reasoning")
        return target[0].model if target else ""

    def _run_task(self, text: str, routed: bool = True) -> Any:
        """Run one task, on the reasoning model when the router says it needs one. Plans stay on the main model."""
        target = None
        if routed and self.mode != "plan":
            hard, why = decide(self.models, text, self.route, self._reasoned)
            target = self.models.role("reasoning") if hard else None
            if target is not None:
                self.emit("route", model=target[0].model, role="reasoning", reason=why)
        self._reasoned = target is not None
        if target is None:
            return self.agent.run_turn(text)
        self.agent.use(*target)
        try:
            return self.agent.run_turn(text)
        finally:
            self.agent.use(self.config, self.llm)  # read now: settings may have changed during the task

    def _repair_history(self) -> None:
        """Answer any tool calls left dangling by an interrupt so history stays valid."""
        messages = self.agent.messages
        answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                for call in message["tool_calls"]:
                    if call.get("id") not in answered:
                        messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                         "name": (call.get("function") or {}).get("name", ""),
                                         "content": "Interrupted by user."})
                break
            if message.get("role") == "user":
                break

    def _helper_request(self, text: str) -> tuple[str, str] | None:
        """'@name task' runs helper `name` directly — unless a file or folder called `name` exists."""
        match = re.match(r"@([\w-]+)\s+(.+)", text.strip(), re.DOTALL)
        if not match or not self.config.agents.get("enabled", True):
            return None
        name, task = match.group(1).lower(), match.group(2).strip()
        if name not in self.agent.specs or (workspace_root() / match.group(1)).exists():
            return None
        return name, task

    def _run_helper(self, name: str, task: str) -> None:
        started = time.perf_counter()
        self.agent.messages.append({"role": "user", "content": f"@{name} {task}"})
        report = self.agent._delegate(name, task)
        text = report.split("\n", 1)[1] if report.startswith("[") and "\n" in report else report
        self.agent.messages.append({"role": "assistant", "content": text})
        self.emit("assistant_message", agent="main", text=text)
        self.emit("usage", agent="main", model=self.config.model, elapsed=round(time.perf_counter() - started, 2),
                  context_used=self.agent.budget.usage(self.agent.messages, self.agent.tools()),
                  context_window=self.config.context_window, files_changed=[], commands=0, commands_failed=0,
                  helpers=1, prompt=0, completion=0, total=0)

    def _run_review(self, source: str) -> None:
        from .tools import execute_tool

        self.emit("user_message", text="/review", source=source)
        if not self.git.get("is_repo"):
            self.emit("notice", level="warning", text="Current directory is not a git repository")
            return
        diff = execute_tool("git_diff", {})
        if not diff.strip() or diff == "(no output)":
            self.emit("notice", level="info", text="No git changes to review")
            return
        # Review in a fresh sub-agent context so a big diff never floods the main thread.
        diff = _truncate(diff, int(self.config.context_window * 3.5 * 0.45))
        reviewer = Agent(self.config, self.llm, self, self.memory, role="review", agent_id="review", git=self.git)
        self.emit("agent_start", agent="review", parent="main", role="review", task="Review the current git diff")
        result = reviewer.run_turn(
            "Review these changes. Find bugs, issues, and improvements. Read surrounding code if needed.\n\n" + diff
        )
        self.emit("agent_end", agent="review", role="review", ok=True, report=result.text)
        self.agent.messages.append({"role": "user", "content": "[built-in /review of the current git diff]"})
        self.agent.messages.append({"role": "assistant", "content": result.text})
