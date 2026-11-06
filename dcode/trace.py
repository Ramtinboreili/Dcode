"""A research trace: one JSON object per line, append-only, off unless a path is given.

``DCODE_TRACE=<path>`` or config ``trace.path`` turns it on; ``trace.snapshots`` also stores each exact request
body under ``<trace dir>/blobs/<sha256>.json``. The first line of a file is a ``run_header`` that describes the
run. Records hold counts, sizes and hashes, not prompts or file contents (snapshots do hold them), and never an
API key. docs/research/tracing.md has every field.

Writing a trace must never break a task: everything public here swallows its own errors. Standard library only.

Which task and agent a call belongs to travels in a ContextVar, so nothing has to pass it through the client's
signature. A new thread starts with an empty context: helpers and the title job run in a copy of their parent's.
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, experiment
from .context import TokenCounter

HEADER = "X-Dcode-Task"
HASH_CHARS = 16        # of each message's SHA-256: 64 bits is plenty to tell messages apart, and a long task has hundreds
ERROR_CHARS = 300
# These calls extend one conversation, so each compares with the one before it. A side job (title, summary) has its
# own prompt: comparing across would report a broken prefix between two main steps that share everything.
CHAIN_PURPOSES = {"main_step", "helper_step", "final_report"}
PURPOSES = {"main_step", "helper_step", "title", "commit_msg", "router", "compaction_summary", "final_report", "other"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._:-]")


class Ledger:
    """Tokens spent outside an agent's own loop (side jobs, failed attempts), until a task's total takes them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cached": None}

    def add(self, usage: dict[str, Any]) -> None:
        with self._lock:
            add_usage(self._totals, usage)

    def drain(self) -> dict[str, Any]:
        with self._lock:
            taken, self._totals = self._totals, self._empty()
        return taken


def add_usage(totals: dict[str, Any], usage: dict[str, Any]) -> None:
    """Add one call's numbers. ``cached`` stays None until some server reports it: unknown isn't zero."""
    for key in ("prompt", "completion", "total", "calls"):
        totals[key] = totals.get(key, 0) + int(usage.get(key) or 0)
    if usage.get("cached") is not None:
        totals["cached"] = (totals.get("cached") or 0) + int(usage["cached"])


@dataclass(frozen=True)
class Scope:
    task_id: str
    agent_id: str = "main"
    purpose: str = "other"
    step: int | None = None
    retry_reason: str = "none"   # why the next request is a repeat (the engine's overflow recovery sets it)
    helper: bool = False         # inside a delegated helper: its task_end belongs to the parent
    started: float = field(default_factory=time.perf_counter)
    ledger: Ledger = field(default_factory=Ledger)


_scope: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("dcode_trace_scope", default=None)
_process_task_id = ""
_id_lock = threading.Lock()


def clean_task_id(value: str) -> str:
    """Goes into an HTTP header and a database column: letters, digits and . _ : - only, 64 at most."""
    return _SAFE_ID.sub("_", value.strip())[:64]


def new_task_id() -> str:
    """``DCODE_TASK_ID`` when a harness set one, else a fresh id."""
    return clean_task_id(os.environ.get("DCODE_TASK_ID", "")) or f"t-{uuid.uuid4().hex[:12]}"


def current() -> Scope | None:
    return _scope.get()


def current_task_id() -> str:
    """Every request carries an id, even one sent outside any task (a /commit message)."""
    scope = _scope.get()
    if scope is not None:
        return scope.task_id
    global _process_task_id
    with _id_lock:
        _process_task_id = _process_task_id or new_task_id()
    return _process_task_id


@contextmanager
def use(**changes: Any) -> Iterator[Scope]:
    """Run a block with these scope values on top of the current ones (a new task when there is none)."""
    base = _scope.get()
    if base is None:
        changes.setdefault("task_id", new_task_id())
        scope = Scope(**changes)
    else:
        scope = dataclasses.replace(base, **changes)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def enter_task(ledger: Ledger | None = None) -> contextvars.Token:
    """Start a new task on this thread; pass the token to leave_task when it is over."""
    return _scope.set(Scope(task_id=new_task_id(), ledger=ledger or Ledger()))


def leave_task(token: contextvars.Token) -> None:
    _scope.reset(token)


def charge(usage: dict[str, Any]) -> None:
    """Count tokens that no agent's reply carries back."""
    scope = _scope.get()
    if scope is not None:
        scope.ledger.add(usage)


# --- hashing --------------------------------------------------------------------------------------------------
def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def message_hash(message: dict[str, Any]) -> str:
    """Of what shapes the prompt: role, content and tool calls."""
    shape = {"role": message.get("role"), "content": message.get("content"), "tool_calls": message.get("tool_calls")}
    return hashlib.sha256(_canonical(shape)).hexdigest()[:HASH_CHARS]


def tools_hash(tools: list[dict[str, Any]] | None) -> str | None:
    return hashlib.sha256(_canonical(tools)).hexdigest()[:HASH_CHARS] if tools else None


def common_prefix(previous: list[str], hashes: list[str]) -> int:
    """How many leading messages are the same."""
    length = 0
    for old, new in zip(previous, hashes, strict=False):
        if old != new:
            break
        length += 1
    return length


def prefix_fields(previous: dict[str, Any] | None, messages: list[dict[str, Any]],
                  tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """How much of this request repeats the previous one. Needs nothing from the server, so it can check the
    server's cached_tokens. Token figures are chars/3.5, with the same counter for every run."""
    counter = TokenCounter()
    hashes = [message_hash(m) for m in messages]
    sizes = [counter._raw_message(m) for m in messages]
    tools_id = tools_hash(tools)
    fields: dict[str, Any] = {
        "message_hashes": hashes, "tools_hash": tools_id, "prev_messages": None, "lcp_messages": 0, "lcp_tokens": 0,
        "first_changed_index": None, "first_changed_role": None, "prefix_intact": None, "tools_changed": None,
    }
    if previous is not None:
        lcp = common_prefix(previous["hashes"], hashes)
        intact = lcp == len(previous["hashes"])
        unchanged_tools = previous["tools"] == tools_id
        # Servers render the tools before the messages: an unchanged list is part of the shared prefix.
        tool_tokens = counter.raw(json.dumps(tools, separators=(",", ":"))) if tools and unchanged_tools else 0
        fields.update(
            prev_messages=len(previous["hashes"]), lcp_messages=lcp,
            lcp_tokens=(sum(sizes[:lcp]) + tool_tokens) if unchanged_tools else 0, prefix_intact=intact,
            tools_changed=not unchanged_tools,
            first_changed_index=None if intact else lcp,
            first_changed_role=None if intact or lcp >= len(messages) else messages[lcp].get("role"))
    fields["_state"] = {"hashes": hashes, "tools": tools_id}
    return fields


# --- describing the run ---------------------------------------------------------------------------------------
def _host_only(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _dcode_git() -> dict[str, Any]:
    root = Path(__file__).resolve().parent.parent
    info: dict[str, Any] = {"commit": None, "dirty": None}
    if not (root / ".git").exists():  # an installed package or a binary
        return info
    try:
        run = lambda *args: subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10)  # noqa: E731
        head = run("rev-parse", "HEAD")
        if head.returncode == 0:
            info["commit"] = head.stdout.strip()
            info["dirty"] = bool(run("status", "--porcelain", "--untracked-files=no").stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


def _profiles(config: Any) -> list[dict[str, Any]]:
    """Names, hosts and sizes. Never a key, a path or credentials from a URL."""
    rows = [{"name": "main", "model": config.model, "base_url_host": _host_only(config.base_url),
             "context_window": config.context_window, "max_output_tokens": config.max_output_tokens,
             "family": config.family, "reasoning_effort": config.reasoning_effort}]
    profiles = (config.models or {}).get("profiles") or {}
    for name, spec in profiles.items():
        rows.append({"name": name, "model": spec.get("model"),
                     "base_url_host": _host_only(spec.get("base_url") or config.base_url),
                     "context_window": spec.get("context_window") or config.context_window,
                     "max_output_tokens": spec.get("max_output_tokens") or config.max_output_tokens,
                     "family": spec.get("family") or "auto",
                     "reasoning_effort": spec.get("reasoning_effort") or config.reasoning_effort})
    return rows


class Tracer:
    def __init__(self, path: str, config: Any, snapshots: bool = False) -> None:
        self.path = Path(path).expanduser()
        self.config = config
        self.snapshots = snapshots
        self._lock = threading.Lock()
        self._fd: int | None = None
        self._header_written = False
        self._chains: dict[tuple[str, str], dict[str, Any]] = {}
        self._totals: dict[str, dict[str, Any]] = {}

    # --- writing ----------------------------------------------------------------------------------
    def _open(self) -> int:
        if self._fd is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.chmod(self.path, 0o600)
        return self._fd

    def _header(self, task_id: str) -> dict[str, Any]:
        config = self.config
        section = config.experiment
        return {
            "ts": time.time(), "task_id": task_id, "agent_id": None, "type": "run_header",
            "dcode_version": __version__, "dcode_git": _dcode_git(), "python": sys.version.split()[0],
            "platform": platform.platform(), "experiment": section, "experiment_hash": experiment.config_hash(section),
            "experiment_overrides": experiment.overrides(section), "profiles": _profiles(config),
            "stream": config.stream, "router": (config.models or {}).get("router"),
            "workspace": os.environ.get("DCODE_WORKSPACE") or os.getcwd(), "snapshots": self.snapshots,
        }

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        os.write(self._open(), line.encode("utf-8", "replace"))

    def emit(self, kind: str, *, task_id: str | None = None, agent_id: str | None = None, **fields: Any) -> None:
        """Append one record. Any failure is dropped: a full disk must not end a task."""
        try:
            scope = _scope.get()
            task = task_id or (scope.task_id if scope else current_task_id())
            agent = agent_id or (scope.agent_id if scope else "main")
            with self._lock:
                self._tally(kind, task, fields)
                if not self._header_written:
                    self._write(self._header(task))
                    self._header_written = True
                if scope is not None and scope.step is not None and kind == "tool_call":
                    fields = {"step": scope.step, **fields}
                self._write({"ts": round(time.time(), 3), "task_id": task, "agent_id": agent, "type": kind, **fields})
        except Exception:
            pass

    # --- what task_end sums ---------------------------------------------------------------------
    def _bucket(self, task: str) -> dict[str, Any]:
        return self._totals.setdefault(task, {"prompt": 0, "completion": 0, "cached": None, "calls": 0, "estimated_calls": 0,
                                              "model_ms": 0.0, "tool_ms": 0.0, "approval_ms": 0.0, "tool_calls": 0})

    def _tally(self, kind: str, task: str, fields: dict[str, Any]) -> None:
        if kind == "llm_call":
            bucket = self._bucket(task)
            bucket["calls"] += 1
            bucket["prompt"] += int(fields.get("counted_prompt_tokens") or 0)
            bucket["completion"] += int(fields.get("counted_completion_tokens") or 0)
            bucket["estimated_calls"] += 1 if fields.get("usage_estimated") else 0
            bucket["model_ms"] += float(fields.get("total_ms") or 0.0)
            if fields.get("cached_tokens") is not None:
                bucket["cached"] = (bucket["cached"] or 0) + int(fields["cached_tokens"])
        elif kind == "tool_call":
            bucket = self._bucket(task)
            bucket["tool_calls"] += 1
            bucket["approval_ms"] += float(fields.get("approval_wait_ms") or 0.0)
            if fields.get("name") != "delegate":  # a helper's time is already its own calls and tools
                bucket["tool_ms"] += float(fields.get("duration_ms") or 0.0)

    def task_end(self, reason: str, steps: int, started: float, task_id: str | None = None) -> None:
        try:
            scope = _scope.get()
            task = task_id or (scope.task_id if scope else current_task_id())
            with self._lock:
                bucket = self._totals.pop(task, None) or self._bucket(task)
                self._totals.pop(task, None)
            self.emit("task_end", task_id=task, reason=reason, steps=steps, calls=bucket["calls"],
                      estimated_calls=bucket["estimated_calls"], tool_calls=bucket["tool_calls"],
                      model_ms=round(bucket["model_ms"], 1), tool_ms=round(bucket["tool_ms"], 1),
                      approval_wait_ms=round(bucket["approval_ms"], 1),
                      wall_ms=round((time.perf_counter() - started) * 1000, 1), prompt_tokens=bucket["prompt"],
                      completion_tokens=bucket["completion"], cached_tokens=bucket["cached"])
        except Exception:
            pass

    # --- one request ----------------------------------------------------------------------------
    def prefix(self, agent: str, purpose: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Computed when the request is sent: the history is changed in place later."""
        try:
            key = (agent, "chain" if purpose in CHAIN_PURPOSES else purpose)
            with self._lock:
                previous = self._chains.get(key)
            fields = prefix_fields(previous, messages, tools)
            state = fields.pop("_state")
            with self._lock:
                self._chains[key] = state
            return fields
        except Exception:
            return {}

    def snapshot(self, body: dict[str, Any]) -> str | None:
        """Store the exact request, named by its hash, before anything can change it."""
        if not self.snapshots:
            return None
        try:
            raw = json.dumps(body, ensure_ascii=False, default=str).encode()
            digest = hashlib.sha256(raw).hexdigest()
            blobs = self.path.parent / "blobs"
            blobs.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = blobs / f"{digest}.json"
            if not target.exists():
                temp = blobs / f".{digest}.{uuid.uuid4().hex[:8]}"
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                os.replace(temp, target)
            return digest
        except Exception:
            return None


_tracers: dict[str, Tracer] = {}
_tracers_lock = threading.Lock()


def for_config(config: Any) -> Tracer | None:
    """The tracer this config asks for, or None (the normal case, and it costs one lookup)."""
    settings = getattr(config, "trace", None) or {}
    path = settings.get("path")
    if not path:
        return None
    try:
        key = str(Path(path).expanduser().resolve())
        with _tracers_lock:
            tracer = _tracers.get(key)
            if tracer is None:
                tracer = _tracers[key] = Tracer(key, config, bool(settings.get("snapshots")))
        return tracer
    except Exception:
        return None


def error_fields(exc: BaseException | None) -> dict[str, Any]:
    if exc is None:
        return {"error_class": None, "error_message": None}
    return {"error_class": type(exc).__name__, "error_message": " ".join(str(exc).split())[:ERROR_CHARS]}
