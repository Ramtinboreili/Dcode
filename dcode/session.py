"""A live Dcode session, shared by the terminal and the web UI.

The session owns the agent, a task queue processed by one worker thread, an
event log every surface can subscribe to, and approvals that whichever
surface answers first resolves.
"""

from __future__ import annotations

import collections
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import DEFAULT_MODE, MODES
from .checkpoints import CheckpointStore
from .config import AppConfig, load_config, load_thread, run_dir, save_thread, thread_path, threads_dir
from .context import ContextBudget
from .engine import Agent, git_summary
from .jobs import JobManager
from .llm import Interrupted, LLMClient
from .memory import MemoryStore
from .tools import _truncate, configure_limits, workspace_root

EVENT_BUFFER = 4000
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

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "detail": self.detail, "agent": self.agent}


def transcript_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten stored chat messages into UI events (for resumed / closed threads)."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        if role == "user":
            items.append({"type": "user_message", "text": content, "source": "history"})
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append({"type": "tool_call", "agent": "main", "name": function.get("name", ""),
                              "args": function.get("arguments", ""), "id": call.get("id", "")})
            if content:
                items.append({"type": "assistant_message", "agent": "main", "text": content})
        elif role == "tool":
            items.append({"type": "tool_result", "agent": "main", "name": message.get("name", ""),
                          "output": content[:1500], "id": message.get("tool_call_id", "")})
    return items


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
        # No attention ping here: the terminal polls for approvals while a task runs,
        # and a ping would close the approval menu it is about to open.
        self.emit("approval_request", **approval.as_dict())
        while not approval.done.wait(0.2):
            if self._stop.is_set() or self._closed.is_set():
                self.resolve_approval(approval.id, False, by="interrupt")
                break
        return approval.result

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
            self.submit("The plan is approved. Implement it now, step by step.", source=source)

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

    def suggest_commit_message(self) -> str:
        from .tools import execute_tool

        diff = execute_tool("git_diff", {})
        status = execute_tool("git_status", {})
        if diff in ("", "(no output)") and "??" not in status:
            return ""
        prompt = ("Write a git commit message for these changes: a concise imperative subject line "
                  "(max 70 chars), optionally a blank line and 1-4 bullet points. Reply with the message only.")
        try:
            text = self.llm.complete_text(
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

    def resolve_approval(self, approval_id: str, approved: bool, by: str = "cli", scope: str = "once") -> bool:
        with self._approvals_lock:
            approval = self._approvals.pop(approval_id, None)
        if approval is None or approval.done.is_set():
            return False
        if approved and scope == "session":
            self.allowed_kinds.add(approval.kind)
        approval.result = bool(approved)
        approval.done.set()
        self.emit("approval_resolved", id=approval_id, approved=bool(approved), by=by)
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

    def resume(self, thread_id: str) -> bool:
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
        self.agent.refresh_system_message()
        self.thread_name = str(payload.get("name") or thread_id)
        self.thread_created_at = str(payload.get("created_at") or datetime.now(timezone.utc).isoformat())
        self.emit("thread", name=self.thread_name, action="resume", items=transcript_from_messages(messages))
        return True

    def set_model(self, model: str | None = None, reasoning_effort: str | None = None) -> None:
        if model:
            self.config.model = model
        if reasoning_effort:
            self.config.reasoning_effort = reasoning_effort
        self.agent.refresh_system_message()
        self.emit("config", model=self.config.model, reasoning_effort=self.config.reasoning_effort)

    def apply_config(self, config: AppConfig) -> None:
        """Swap in a new config (e.g. edited from the web UI) without losing history."""
        endpoint_changed = (config.base_url, config.api_key) != (self.config.base_url, self.config.api_key)
        ratio = self.agent.budget.counter.ratio
        self.config = config
        self.llm = LLMClient(config) if endpoint_changed else self.llm
        self.llm.config = config
        self.agent.config = config
        self.agent.llm = self.llm
        self.agent.budget = ContextBudget(config.context_window, config.max_output_tokens)
        self.agent.budget.counter.ratio = ratio
        configure_limits(config.context_window)
        self.agent.refresh_system_message()
        self.emit("config", model=config.model, reasoning_effort=config.reasoning_effort,
                  context_window=config.context_window)

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
            "todos": self.agent.todos,
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
                   "todos": self.agent.todos},
        )

    def _job_exited(self, job) -> None:
        self.emit("job_exited", id=job.id, command=job.command, state=job.state(), exit_code=job.exit_code)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self.jobs.stop_all()
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
                    self.agent.run_turn(payload)
                    if self.mode == "plan":
                        self.plan_pending = True
                        self.emit("plan_ready")
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
                text = self.llm.complete_text(
                    [{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": first_message[:1500]}],
                    max_tokens=60,
                )
            except Exception:
                return
            title = clean_title(text)
            if title and self.thread_name == expected and not self._closed.is_set():
                self._set_title(title)

        threading.Thread(target=refine, name="dcode-title", daemon=True).start()

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
