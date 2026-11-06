"""The agent loop, free of any terminal or web concerns.

An :class:`Agent` talks to the model, executes tools and reports everything
through a :class:`Host` (implemented by :class:`dcode.session.Session`). The
CLI and the web UI are just two views of the same host, which is what lets a
session be driven from either one.
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import itertools
import json
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import agents as roles
from . import checks, hooks, mentions, tools, trace
from . import db as databases
from . import mcp as external
from . import rules as permission_rules
from .artifacts import ArtifactStore
from .config import AppConfig
from .context import (
    SUMMARY_INSTRUCTIONS,
    SUMMARY_PREFIX,
    ContextBudget,
    compact,
    elide_earlier_turns,
    render_for_summary,
)
from .custom_agents import AgentSpec, load_specs
from .llm import ContextOverflow, Interrupted, LLMClient, is_context_overflow
from .memory import MemoryStore
from .skills import get_skill, list_skills, normalize_name, save_skill
from .tools import (
    TOOL_SCHEMAS,
    _safe_path,
    _truncate,
    allowed_tool_names,
    configure_limits,
    execute_tool,
    normalize_call,
    parse_arguments,
    workspace_root,
)

PROJECT_INSTRUCTION_FILES = ("DCODE.md", "AGENTS.md", "CLAUDE.md")
MAIN_MAX_STEPS = 60
REPEAT_LIMIT = 3
STUCK_LIMIT = 2  # the repeat warning was ignored this many times: take the tools away and ask for a report
PREVIEW_CHARS = 4000
THOUGHT_ONLY = ("You used your whole reply on thinking and wrote nothing. Stop thinking: make the next tool "
                "call now, or give a short answer.")
THINKING_CHARS = 6000   # of the model's reasoning kept for display (it never enters the context)
MAX_FOLLOW_UPS = 2
MAX_ARTIFACT_SAVES = 3  # per artifact per task (appends don't count)
DB_SCHEMA_TTL = 300     # seconds a schema read is reused within a session
DB_MODEL_ROWS = 100     # rows a db_query hands back to the model (the text cap usually cuts earlier)


def _norm_title(title: str) -> str:
    """'nginx-setup — Repo Activity Report' == 'Nginx setup: repo activity report'."""
    return " ".join(re.findall(r"[a-z0-9\u0600-\u06ff]+", title.lower()))
# "make an artifact", "an html page", "a dashboard", "draw a diagram", "as an svg"… but not a file name
# ("fix index.html") or a bare "web" ("start a web server" got an unasked-for report page).
ARTIFACT_REQUEST = re.compile(
    r"\bartifacts?\b|(?<![\w./-])(html|svg)\b|\bweb\s*(page|report|dashboard|version)\b|\bdashboard\b|\bdiagram\b"
    r"|آرتیفکت|داشبورد|نمودار",
    re.IGNORECASE,
)
# A reply that promises work instead of doing it: "Building the artifact now.", "Doing it now", "I'll create…"
PROMISE = re.compile(
    r"(doing it now|building (it|the \w+)( now)?|creating (it|the \w+) now|saving (it|the \w+) now"
    r"|\bi(?:'|’)?ll (now )?(create|build|save|write|make|generate|run|add|update)\b"
    r"|\blet me (now )?(create|build|save|write|make|generate|run|add|update)\b"
    r"|\bnow (i(?:'|’)?ll|i will) |\bi will (now )?(create|build|save|write|make|generate))",
    re.IGNORECASE,
)
COMPACT_ARG_TOOLS = {"artifact_save", "skill_save"}  # their payload is stored on disk: see _stub_payload
STUB_ARG_KEYS = ("content", "instructions", "old_text", "new_text")
MCP_FUNCTION_NAME = re.compile(r"[^A-Za-z0-9_-]")
# Tools whose "path" argument the file rules (and the outside-workspace rule) apply to.
FILE_ARG_TOOLS = {"read_file", "edit_file", "write_file", "delete_file", "create_directory", "list_files",
                  "search_in_files", "git_diff"}


def _call_signature(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True)}"


def _failed(output: str) -> bool:
    """A command result that says it went wrong (a refusal, a timeout or a non-zero exit)."""
    return "(return code:" in output or output.startswith(("Not run:", "Command timed out")) or "(stopped after" in output


class Host(Protocol):
    def emit(self, type: str, **data: Any) -> None: ...

    def request_approval(self, kind: str, detail: str, agent: str) -> bool: ...

    def should_stop(self) -> bool: ...


@dataclass
class TurnResult:
    text: str
    usage: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0})
    elapsed: float = 0.0
    steps: int = 0


_agent_counter = itertools.count(1)


def git_summary(cwd: Path | None = None) -> dict[str, Any]:
    def run(command: list[str]) -> str:
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return ((result.stdout or "") + (result.stderr or "")).strip() if result.returncode == 0 else ""

    if run(["git", "rev-parse", "--is-inside-work-tree"]) != "true":
        return {"is_repo": False}
    modified = run(["git", "status", "--porcelain"])
    return {
        "is_repo": True,
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "-",
        "last_commit": run(["git", "log", "-1", "--pretty=format:%h %s"]) or "-",
        "modified_files": len([line for line in modified.splitlines() if line.strip()]),
    }


TOOL_ERROR_PREFIXES = ("Tool error:", "Missing required", "Unknown tool", "MCP error:", "Database error:", "Blocked:")


def _error_kind(output: str) -> str:
    """Which way a tool failed, without quoting its output: the trace holds no file contents."""
    if output.startswith("Command timed out") or "(stopped after" in output:
        return "timeout"
    return "return_code" if "(return code:" in output else "tool_error"


class Agent:
    def __init__(
        self,
        config: AppConfig,
        llm: LLMClient,
        host: Host,
        memory: MemoryStore | None = None,
        role: str = "main",
        agent_id: str = "main",
        git: dict[str, Any] | None = None,
        spec: AgentSpec | None = None,
    ) -> None:
        self.config = config
        self.spec = spec  # helpers only: tools, prompt, model and steps for this role
        self.llm = llm
        self.host = host
        self.memory = memory
        self.role = role
        self.agent_id = agent_id
        self.git = git if git is not None else {"is_repo": False}
        self.budget = self._new_budget(config)
        self.recalled = ""
        self.current_task = ""
        self._artifact_saves: dict[str, int] = {}
        self._repeat: list[str] = []
        self._command_outputs: list[str] = []  # the last outputs of the command being repeated
        # Files this agent has read or written: write_file on any other existing file is refused once,
        # so a model can't replace a file it never looked at (Gemma 4 rewrote both files of a task it
        # was only asked to commit).
        self._seen_files: set[str] = set()
        self.todos: list[dict[str, str]] = []
        self._tool_note = threading.local()  # why the tool call running on this thread was refused, how long it waited
        self._stubbed: dict[str, str] = {}   # calls whose output left the context, for the trace's elision_regret
        # This task's tokens: its own calls, its helpers and (when the task ends) its side jobs.
        self.spent: dict[str, Any] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cached": None}
        self._spent_lock = threading.Lock()
        self._estimated = False
        self._steps_done = 0
        self._end_reason = "final_answer"
        self._db_names: dict[str, str] = {}  # saved connections (name -> engine), refreshed at turn start
        self._db_schemas: dict[str, tuple[float, dict[str, Any]]] = {}
        self._apply_limits(config)
        self.artifacts = ArtifactStore()
        self.checkpoints = getattr(host, "checkpoints", None)
        self.specs: dict[str, AgentSpec] = self._load_specs()
        self.rules = self._load_rules()
        self._load_db_names()
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt()}]

    def use(self, config: AppConfig, llm: Any) -> None:
        """Run on another model: its client, window and reply budget (the history stays)."""
        ratio = self.budget.counter.ratio
        self.config = config
        self.llm = llm
        self.budget = self._new_budget(config)
        self.budget.counter.ratio = ratio
        self._apply_limits(config)

    @staticmethod
    def _new_budget(config: AppConfig) -> ContextBudget:
        exp = config.experiment
        return ContextBudget(config.context_window, config.max_output_tokens, exp["compact_trigger"], exp["compact_target"])

    @staticmethod
    def _apply_limits(config: AppConfig) -> None:
        exp = config.experiment
        configure_limits(config.context_window, exp["output_cap_share"], exp["output_cap_min"], exp["output_cap_max"])

    # --- prompt & tools -------------------------------------------------------------
    @property
    def is_main(self) -> bool:
        return self.role == "main"

    @property
    def mode(self) -> str:
        mode = getattr(self.host, "mode", None)
        return mode if mode in roles.MODES else roles.DEFAULT_MODE

    def tool_names(self) -> list[str]:
        allowed = allowed_tool_names(self.config.permissions)
        memory_on = self.memory is not None and self.config.memory.get("enabled", True)
        if self.is_main:
            names = list(allowed)
            if memory_on:
                names += roles.MEMORY_TOOL_NAMES
            if self._delegation_on:
                names.append("delegate")
            if self.config.artifacts.get("enabled", True):
                names += roles.ARTIFACT_TOOL_NAMES
            names += roles.SKILL_TOOL_NAMES
            names.append("todo_write")
            if self._db_names:
                names += roles.DB_TOOL_NAMES
            if getattr(self.host, "mcp", None):
                names += list(self._mcp_functions()) if self._mcp_full else roles.MCP_TOOL_NAMES
            if hasattr(self.host, "ask_user"):
                names.append("ask_user")
        else:
            wanted = self.spec.tools if self.spec else roles.ROLE_TOOLS.get(self.role, roles.ROLE_TOOLS["explore"])
            ungated = {n for n in roles.UNGATED_TOOLS if n != "memory_search" or memory_on}
            names = [n for n in wanted if n in allowed or n in ungated]
        if "run_command" in names and getattr(self.host, "jobs", None) is not None:
            names += roles.JOB_TOOL_NAMES
        if self.mode == "plan":
            names = [n for n in names if n not in roles.PLAN_BLOCKED_TOOLS]
        return names

    @property
    def _delegation_on(self) -> bool:
        return bool(self.config.agents.get("enabled", True) and self.config.experiment["delegation"])

    @property
    def _mcp_full(self) -> bool:
        return self.config.experiment["mcp_schemas"] == "full"

    def _mcp_functions(self) -> dict[str, dict[str, Any]]:
        """The "full schemas" variant: each MCP tool as a function of its own (only for comparing with names-only)."""
        functions: dict[str, dict[str, Any]] = {}
        servers = getattr(self.host, "mcp", None)
        for server in (servers.servers.values() if servers else ()):
            if server.state != "ready":
                continue
            for spec in server.tools:
                name = MCP_FUNCTION_NAME.sub("_", f"mcp__{server.name}__{spec['name']}")[:64]
                parameters = spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {"type": "object"}
                functions[name] = {"server": server.name, "tool": spec["name"], "schema": {"type": "function", "function": {
                    "name": name, "description": str(spec.get("description") or ""), "parameters": parameters}}}
        return functions

    def tools(self) -> list[dict[str, Any]]:
        schemas = []
        functions = self._mcp_functions() if self.is_main and self._mcp_full else {}
        for name in self.tool_names():
            schema = TOOL_SCHEMAS.get(name) or roles.EXTRA_TOOL_SCHEMAS.get(name)
            if not schema and name in functions:
                schema = functions[name]["schema"]
            if schema and name == "delegate" and self.specs:
                schema = json.loads(json.dumps(schema))  # the role list includes your own helpers
                schema["function"]["parameters"]["properties"]["role"]["enum"] = list(self.specs)
            if schema:
                schemas.append(schema)
        return schemas

    def _load_specs(self) -> dict[str, AgentSpec]:
        if not self.is_main:
            return {}
        try:
            return load_specs(workspace_root())
        except OSError:
            return {}

    def system_prompt(self) -> str:
        cwd = str(workspace_root())
        window = self.config.context_window
        if not self.is_main:
            return roles.role_prompt(self.role, cwd, self.spec.prompt if self.spec else "")

        parts = [roles.MAIN_PROMPT.format(cwd=cwd)]
        if self._delegation_on:
            descriptions = {name: spec.description for name, spec in self.specs.items()} or None
            parts.append(roles.delegation_block(window, descriptions))
        if self.memory is not None and self.config.memory.get("enabled", True):
            parts.append(roles.MEMORY_PROMPT)
        if self.config.experimental.get("multi_file_edit"):
            parts.append("You may edit multiple files in one response.")
        if self.git.get("is_repo"):
            parts.append(
                f"Git: branch {self.git.get('branch', '-')}, last commit {self.git.get('last_commit', '-')}, "
                f"{self.git.get('modified_files', 0)} modified files."
            )
        # Budgets scale with the window: ~4% for instructions, ~3% for memory index.
        instructions = self._project_instructions(max_chars=int(window * 3.5 * 0.04))
        if instructions:
            parts.append(f"Project instructions:\n{instructions}")
        if self.memory is not None and self.config.memory.get("enabled", True):
            index = self.memory.index_text(max_chars=int(window * 3.5 * 0.03))
            if index:
                parts.append(f"Saved memories (memory_search for details):\n{index}")
            if self.recalled and self.config.experiment["recall_placement"] == "system":
                parts.append(f"Memories relevant to the current task:\n{self.recalled}")
        if self.config.artifacts.get("enabled", True):
            parts.append(roles.ARTIFACT_PROMPT)
        if self.mode == "plan":
            parts.append(roles.PLAN_PROMPT)
        elif getattr(self.host, "jobs", None) is not None and self.config.permissions.get("run_command", True):
            parts.append(roles.JOBS_PROMPT)
        if self._db_names:
            parts.append(roles.DB_PROMPT.format(names=", ".join(f"{n} [{e}]" for n, e in self._db_names.items())))
        servers = getattr(self.host, "mcp", None)
        if servers and not self._mcp_full:
            # ~3% of the window: names only. Like the checklist, read once per turn (keeps the prompt cache).
            index = servers.index_text(max_chars=max(300, int(window * 3.5 * 0.03)))
            if index:
                parts.append(roles.MCP_PROMPT.format(index=index))
        parts.append(roles.TODO_PROMPT)
        if self.todos:
            # Refreshed only at turn start and after compaction, so local servers keep their prompt cache.
            parts.append("Your checklist so far:\n" + roles.render_todos(self.todos))
        skills = self._skills_block(max_chars=int(window * 3.5 * 0.05))
        if skills:
            parts.append(skills)
        return "\n\n".join(parts)

    def _load_rules(self) -> permission_rules.RuleSet:
        ruleset = permission_rules.load(self.config.rules, workspace_root())
        tools.set_file_guard(ruleset.denies_file)  # denied files are blocked for every tool, @mentions too
        return ruleset

    def _load_db_names(self) -> None:
        """Saved database connections. The db tools and their prompt line exist only when there are any."""
        self._db_names = {}
        if self.is_main and self.config.db.get("enabled", True):
            try:
                self._db_names = {name: conn["engine"] for name, conn in databases.load_connections().items()}
            except Exception:
                pass

    def refresh_system_message(self) -> None:
        self.specs = self._load_specs()  # pick up helpers added since the last turn
        if self.is_main:
            self.rules = self._load_rules()  # ...and rule changes (config or .dcode/rules.json)
            self._load_db_names()            # ...and database connections
        system = {"role": "system", "content": self.system_prompt()}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = system
        else:
            self.messages.insert(0, system)

    def _project_instructions(self, max_chars: int) -> str:
        root = workspace_root()
        for name in PROJECT_INSTRUCTION_FILES:
            path = root / name
            if path.is_file():
                try:
                    return _truncate(path.read_text(errors="replace").strip(), max(400, max_chars))
                except OSError:
                    return ""
        return ""

    def _skills_block(self, max_chars: int) -> str:
        """Pinned skills in full; every other skill as a one-line index entry."""
        try:
            available = list_skills(workspace_root())
        except OSError:
            return ""
        if not available:
            return ""
        if self.config.experiment["skills_mode"] == "inline":
            # The variant to compare the index with: every skill in full, so no cap (it would hide the cost).
            return "\n\n".join([roles.SKILLS_PROMPT] + [f"## Skill: {s.name}\n{s.content}" for s in available])
        pinned_names = {normalize_name(n) for n in self.config.enabled_skills}
        pinned = [s for s in available if s.name in pinned_names]
        lines = [roles.SKILLS_PROMPT]
        for skill in pinned:
            lines.append(f"## Pinned skill: {skill.name}\n{skill.content}")
        index = [f"- {s.name}: {s.description}" for s in available if s.name not in pinned_names]
        if index:
            lines.append("Available skills:\n" + "\n".join(index))
        return _truncate("\n\n".join(lines), max(600, max_chars))

    # --- main loop ----------------------------------------------------------------------
    def run_turn(self, user_text: str, max_steps: int | None = None) -> TurnResult:
        """One task. Whoever owns the task (not a helper inside it) writes its end to the trace, however it ends."""
        tracer = trace.for_config(self.config)
        self._end_reason = "final_answer"
        self._steps_done = 0
        self._estimated = False
        self.spent = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cached": None}
        with trace.use(agent_id=self.agent_id) as scope:
            try:
                return self._run_loop(user_text, max_steps)
            except Interrupted:
                self._end_reason = "interrupted"
                scope.ledger.add(self.spent)  # no usage event comes now: the next task's total takes these
                raise
            except ContextOverflow:
                self._end_reason = "context_overflow"
                scope.ledger.add(self.spent)
                raise
            except Exception:
                self._end_reason = "error"
                scope.ledger.add(self.spent)
                raise
            finally:
                if tracer is not None and not scope.helper:
                    settle = getattr(self.host, "settle_side_jobs", None)
                    if settle:
                        settle()
                    tracer.task_end(self._end_reason, self._steps_done, scope.started)

    def _run_loop(self, user_text: str, max_steps: int | None = None) -> TurnResult:
        started = time.perf_counter()
        exp = self.config.experiment
        tracer = trace.for_config(self.config)
        max_steps = max_steps or (self.config.experiment["main_max_steps"] if self.is_main else int(self.config.agents.get("max_steps", 16)))
        self.current_task = user_text
        self._repeat = []
        self._command_outputs = []
        self._stuck = 0
        self._activity = {"files": [], "commands": 0, "commands_failed": 0, "helpers": 0, "artifacts": []}
        self._artifact_saves: dict[str, int] = {}
        if self.is_main and self.checkpoints is not None:
            self.checkpoints.begin(user_text)

        if self.is_main and self.memory is not None and self.config.memory.get("auto_recall", True):
            budget_chars = int(self.config.memory.get("budget_tokens", 1200) * 3.5)
            try:
                self.recalled = self.memory.recall(user_text, max_chars=budget_chars)
            except Exception:
                self.recalled = ""
        if exp["earlier_turn_elision"]:
            before = self._tool_chars() if tracer else 0
            count = elide_earlier_turns(self.messages, exp["earlier_turn_min_chars"])
            if tracer:
                tracer.emit("elision", count=count, chars_removed=before - self._tool_chars())
                self._note_stubbed()
        self.refresh_system_message()
        sent_text = user_text
        if self.is_main and self.recalled and exp["recall_placement"] == "user":
            # The variant that keeps messages[0] the same across tasks: this stays in the history, like any message.
            sent_text = f"Memories relevant to the current task:\n{self.recalled}\n\n---\n\n{user_text}"
        last = self.messages[-1] if self.messages else {}
        if not (last.get("role") == "user" and last.get("content") == sent_text):
            self.messages.append({"role": "user", "content": sent_text})  # a retry after a failed call: once
        if self.is_main:
            self._attach_mentions(user_text)

        steps = 0
        nudged = False
        follow_ups = 0
        final = ""
        while True:
            if self.host.should_stop():
                raise Interrupted()
            tools = self.tools()
            if steps >= max_steps:
                # Out of steps: ask for a wrap-up with no tools so the loop terminates.
                self.messages.append(
                    {"role": "user", "content": "Step limit reached. Stop using tools and report progress now."}
                )
                tools = []
                self._end_reason = "step_limit"
            elif self._stuck >= exp["stuck_limit"]:
                # The model keeps making the same call although told it won't help: end the task, don't loop.
                if self._stuck == exp["stuck_limit"]:
                    self.host.emit("notice", agent=self.agent_id, level="warning",
                                   text="Stopped: the model kept repeating the same step")
                self._stuck += 1
                self.messages.append({"role": "user", "content": (
                    "You keep repeating the same call and it keeps failing. Stop using tools. Tell the user "
                    "what you tried, why it failed, and what you need from them.")})
                tools = []
                self._end_reason = "stuck_guard"
            self._maybe_compact(tools)
            with trace.use(step=steps, purpose="main_step" if self.is_main else "helper_step"):
                turn = self._ask_model(tools)
            self._tally(turn)
            actual = turn.calibration_prompt if turn.calibration_prompt is not None else turn.usage.get("prompt")
            if actual:
                self.budget.counter.calibrate(self.messages, tools, actual)
            if not tools:
                turn.tool_calls = []  # never store calls we won't answer — breaks history
            self._dedupe_call_ids(turn.tool_calls)
            for call in turn.tool_calls:
                normalize_call(call)  # other agents' argument names, a command sent as a list…
            keep = getattr(self.llm, "keep_reasoning", None)
            if keep and turn.tool_calls:
                keep(turn.tool_calls, str(getattr(turn, "reasoning", "") or ""))  # some APIs want it back
            for note in getattr(turn, "notes", None) or []:
                self.host.emit("notice", agent=self.agent_id, level="info", text=note)
            if turn.recovered_tool_calls:
                self.host.emit("notice", agent=self.agent_id, level="info", text="Recovered tool call from model text")
            thought = str(getattr(turn, "reasoning", "") or "").strip()
            if thought:
                self.host.emit("thinking", agent=self.agent_id, text=thought[:THINKING_CHARS])
            self.messages.append(turn.as_message())

            if not turn.tool_calls or not tools:
                thought_only = turn.finish_reason == "length" and not turn.content.strip() and tools
                if thought_only and follow_ups < exp["max_follow_ups"]:
                    # The whole reply went on reasoning: "continue" would only make it think more.
                    follow_ups += 1
                    self.messages.append({"role": "user", "content": THOUGHT_ONLY})
                    continue
                if not turn.content.strip() and not nudged and steps < max_steps:
                    # Small models sometimes stop silently after a tool result.
                    nudged = True
                    self.messages.append({"role": "user", "content": "Continue. If you are done, give your final answer."})
                    continue
                reminder = self._follow_up(turn, user_text) if tools and steps < max_steps else ""
                if reminder and follow_ups < exp["max_follow_ups"]:
                    # The model announced an action (or skipped a requested artifact) but called no tool.
                    follow_ups += 1
                    self.host.emit("notice", agent=self.agent_id, level="info", text="Reminding the model to follow through")
                    self.messages.append({"role": "user", "content": reminder})
                    continue
                final = turn.content.strip()
                break

            if turn.content.strip():
                self.host.emit("assistant_note", agent=self.agent_id, text=turn.content.strip())
            with trace.use(step=steps):
                self._run_tool_calls(turn.tool_calls)
            steps += 1
            self._steps_done = steps

        if self.is_main and not final and (steps or self._activity["files"] or self._activity["commands"]):
            final = self._final_report()
        elapsed = time.perf_counter() - started
        scope = trace.current()
        if tracer is not None and not scope.helper:
            settle = getattr(self.host, "settle_side_jobs", None)
            if settle:
                settle()  # its tokens belong in this task's total
        trace.add_usage(self.spent, scope.ledger.drain())
        usage = {key: self.spent[key] for key in ("prompt", "completion", "total")}
        self.host.emit("assistant_message", agent=self.agent_id, text=final)
        self.host.emit(
            "usage",
            agent=self.agent_id,
            model=self.config.model,
            elapsed=round(elapsed, 2),
            context_used=self.budget.usage(self.messages, self.tools()),
            context_window=self.config.context_window,
            files_changed=list(self._activity["files"]),
            commands=self._activity["commands"],
            commands_failed=self._activity["commands_failed"],
            helpers=self._activity["helpers"],
            steps=steps,
            calls=self.spent["calls"],
            cached=self.spent["cached"],
            usage_estimated=self._estimated,
            delegated=scope.helper,  # already inside the parent's total
            **usage,
        )
        return TurnResult(text=final, usage=usage, elapsed=elapsed, steps=steps)

    def _ask_model(self, tools: list[dict[str, Any]]) -> Any:
        """One model call. If the server says the prompt doesn't fit (our estimate was wrong, or the task outgrew
        what compaction could do by itself), compact as hard as we can and try once more."""
        def ask() -> Any:
            return self.llm.chat(
                self.messages,
                tools=tools or None,
                on_delta=lambda text: self.host.emit("assistant_delta", agent=self.agent_id, text=text),
                should_stop=self.host.should_stop,
                max_tokens=self.budget.max_output_tokens,
            )

        try:
            return ask()
        except Interrupted:
            raise
        except Exception as exc:
            if not (self.config.experiment["overflow_recovery"] and is_context_overflow(exc)):
                raise
            refusal = exc
        self.host.emit("notice", agent=self.agent_id, level="warning", text="The model's context is full: compacting and trying again")
        report = self._maybe_compact(tools, force=True)
        if report and report["after"] < report["before"]:
            try:
                with trace.use(retry_reason="context_overflow"):
                    return ask()
            except Interrupted:
                raise
            except Exception as exc:
                if not is_context_overflow(exc):
                    raise
                refusal = exc
        raise ContextOverflow(
            "The conversation no longer fits the model's context window, even after compacting. Start a new chat, "
            f"ask for less at a time, or give the model a longer context. The server said: {' '.join(str(refusal).split())[:300]}"
        ) from refusal

    def _attach_mentions(self, user_text: str) -> None:
        """Put @mentioned files into context as if the model had read them (no tool call spent)."""
        paths = mentions.find(user_text, workspace_root())
        if not paths:
            return
        attach, named = paths[:mentions.MAX_ATTACH], paths[mentions.MAX_ATTACH:]
        calls = []
        for rel in attach:
            name, args = ("list_files", {"path": rel}) if rel.endswith("/") else ("read_file", {"path": rel})
            calls.append({"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}})
        self.messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        for call in calls:
            function = call["function"]
            output = execute_tool(function["name"], parse_arguments(function["arguments"]))
            self._saw(parse_arguments(function["arguments"]).get("path"))
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": function["name"],
                                  "content": output})
        if named:
            self.messages.append({"role": "user", "content": (
                "Also mentioned (not attached, read them if needed): " + ", ".join(named))})
        self.host.emit("mentions", agent=self.agent_id, attached=attach, named=named)

    def _follow_up(self, turn: Any, user_text: str) -> str:
        """Why the model must keep going although it sent no tool call ('' = it's really done)."""
        if turn.finish_reason == "length":
            return ("Your reply was cut off by the output limit. Continue exactly where you stopped. For long "
                    "content, save it in parts (artifact_save with append=true, or write_file then edit_file).")
        if not self.is_main:
            return ""
        activity = getattr(self, "_activity", {})
        wants_artifact = (self.config.artifacts.get("enabled", True) and ARTIFACT_REQUEST.search(user_text)
                          and not activity.get("artifacts"))
        if wants_artifact:
            return ("The user asked for an artifact, but you haven't saved one. Call artifact_save NOW with the "
                    "complete content (kind html for a page). If it's long, save the first part, then call "
                    "artifact_save again with the same id and append=true. Don't describe it — save it.")
        tail = turn.content.strip()[-400:]
        if PROMISE.search(tail):
            return ("You said you would do something but didn't call any tool. Do it now with the tools — "
                    "don't just describe it. If everything is already done, give your final report.")
        return ""

    REPORT_REQUEST = (
        "The work is finished. Write a short report for the user (no tool calls): what you did, "
        "files changed, commands you ran and their outcome, the current status, and anything left to do."
    )

    def _tally(self, turn: Any) -> None:
        trace.add_usage(self.spent, {**turn.usage, "calls": turn.calls, "cached": turn.cached})
        self._estimated = self._estimated or turn.usage_estimated

    def _final_report(self) -> str:
        """The model stopped without summarizing: ask once, else build a factual report."""
        self.messages.append({"role": "user", "content": self.REPORT_REQUEST})
        try:
            with trace.use(purpose="final_report"):
                turn = self.llm.chat(self.messages, tools=None, should_stop=self.host.should_stop,
                                     max_tokens=self.budget.max_output_tokens)
            self._tally(turn)
            text = turn.content.strip()
        except Interrupted:
            raise
        except Exception:
            text = ""
        if text:
            self.messages.append({"role": "assistant", "content": text})
            return text
        self.messages.pop()  # drop the unanswered request
        activity = self._activity
        lines = ["**Report**"]
        if activity["files"]:
            lines.append("- Files changed: " + ", ".join(f"`{f}`" for f in activity["files"]))
        if activity["commands"]:
            failed = activity["commands_failed"]
            lines.append(f"- Commands run: {activity['commands']}" + (f" ({failed} failed)" if failed else ""))
        if activity["helpers"]:
            lines.append(f"- Helpers used: {activity['helpers']}")
        if activity["artifacts"]:
            lines.append("- Artifacts: " + ", ".join(activity["artifacts"]))
        lines.append("- The model did not write a summary; review the steps above for details.")
        report = "\n".join(lines)
        self.messages.append({"role": "assistant", "content": report})
        return report

    def _track(self, name: str, args: dict[str, Any], output: str) -> None:
        """Remember what a turn actually changed, for the end-of-task report."""
        activity = getattr(self, "_activity", None)
        if activity is None:
            return
        path = str(args.get("path") or "")
        ok_prefix = {"write_file": "Wrote", "edit_file": "Edited", "delete_file": "Deleted",
                     "create_directory": "Created"}.get(name)
        if ok_prefix and output.startswith(ok_prefix) and path:
            try:
                shown = str(Path(path).expanduser().resolve().relative_to(workspace_root()))
            except (ValueError, OSError):
                shown = path
            if shown not in activity["files"]:
                activity["files"].append(shown)
        elif name == "run_command" and not output.startswith("Command cancelled"):
            activity["commands"] += 1
            if "(return code:" in output or output.startswith("Command timed out"):
                activity["commands_failed"] += 1
        elif name == "delegate":
            activity["helpers"] += 1
        elif name == "artifact_save" and output.startswith("Saved artifact"):
            activity["artifacts"].append(str(args.get("title") or "artifact"))

    def _match_artifact(self, title: str) -> str | None:
        """One artifact per chat: find the one a save without an id should update.

        Same (normalized) title in this chat → that artifact. No title / "Untitled" → the
        chat's most recent artifact. Otherwise a genuinely new artifact (None).
        """
        thread = str(getattr(self.host, "thread_name", "") or "")
        mine = [a for a in self.artifacts.list() if a["id"] in self._artifact_saves or (thread and a.get("thread") == thread)]
        if not mine:
            return None
        wanted = _norm_title(title)
        if not wanted or wanted == "untitled":
            return mine[0]["id"]  # list() is newest first
        return next((a["id"] for a in mine if _norm_title(a["title"]) == wanted), None)

    def _checkpoint(self, path: str) -> None:
        if self.checkpoints is None or not path:
            return
        try:
            self.checkpoints.record(_safe_path(path))
        except Exception:
            pass  # sandbox violations are reported by the tool itself

    @staticmethod
    def _edit_detail(name: str, args: dict[str, Any]) -> str:
        """What the user sees when asked to approve a file change (manual mode)."""
        path = str(args.get("path", ""))
        if name == "edit_file":
            old = str(args.get("old_text", "")).splitlines()[:8]
            new = str(args.get("new_text", "")).splitlines()[:8]
            return "\n".join([f"edit {path}"] + [f"- {line}" for line in old] + [f"+ {line}" for line in new])
        if name == "write_file":
            content = str(args.get("content", ""))
            return f"write {path} ({len(content.splitlines())} lines)"
        return f"create folder {path}"

    def force_compact(self, focus: str = "") -> dict[str, Any] | None:
        """/compact: fold the whole conversation into one summary to free the context."""
        history = self.messages[1:]
        if len([m for m in history if m.get("role") == "user"]) < 1:
            return None
        before = self.budget.usage(self.messages, self.tools())
        instructions = SUMMARY_INSTRUCTIONS + (f" Pay special attention to: {focus}." if focus else "")
        transcript = render_for_summary(history, limit_chars=int(self.budget.input_budget * 3.5 * 0.7))
        try:
            summary = self.llm.complete_text(
                [{"role": "system", "content": instructions}, {"role": "user", "content": transcript}],
                max_tokens=min(900, self.budget.max_output_tokens),
                purpose="compaction_summary",
            ).strip()
        except Exception:
            summary = ""
        if not summary:
            summary = "\n".join(
                f"- {m['content'][:200]}" for m in history
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            )[:2500]
        tracer = trace.for_config(self.config)
        if tracer:
            self._note_stubbed(history)
        self.messages[1:] = [
            {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary}"},
            {"role": "assistant", "content": "Understood. Continuing from the summary."},
        ]
        self.refresh_system_message()
        after = self.budget.usage(self.messages, self.tools())
        if tracer:
            tracer.emit("compaction", stage=2, trigger="forced", before_tokens=before, after_tokens=after, elided=0,
                        summarized=len(history), summary_tokens=self.budget.counter.text(summary))
        self.host.emit("compaction", agent=self.agent_id, before=before, after=after, elided=0, summarized=True)
        self.host.emit("summary", agent=self.agent_id, text=summary)
        return {"before": before, "after": after, "summary": summary}

    def _dedupe_call_ids(self, calls: list[dict[str, Any]]) -> None:
        """Some local servers reuse ids like 'call_0' every turn; make them unique.

        Duplicate ids make tool results ambiguous for the model and the UI.
        """
        seen = {c.get("id") for m in self.messages for c in (m.get("tool_calls") or [])}
        for call in calls:
            if not call.get("id") or call["id"] in seen:
                call["id"] = f"call_{uuid.uuid4().hex[:12]}"
            seen.add(call["id"])

    def _maybe_compact(self, tools: list[dict[str, Any]], force: bool = False) -> dict[str, Any] | None:
        if not force and not self.budget.needs_compaction(self.messages, tools):
            return None

        def summarize(transcript: str) -> str:
            return self.llm.complete_text(
                [{"role": "system", "content": SUMMARY_INSTRUCTIONS}, {"role": "user", "content": transcript}],
                max_tokens=min(700, self.budget.max_output_tokens),
                purpose="compaction_summary",
            )

        report = compact(self.messages, self.budget, summarize, tools, self.config.experiment, force=force,
                         task=self.current_task)
        if not report:
            return None
        tracer = trace.for_config(self.config)
        if tracer:
            self._note_stubbed(report["folded"])
            tracer.emit("compaction", stage=report["stage"], trigger="forced" if force else "auto", before_tokens=report["before"],
                        after_tokens=report["after"], elided=report["elided"], summarized=len(report["folded"]),
                        summary_tokens=self.budget.counter.text(report["summary"]) if report["summary"] else 0,
                        args_shrunk=report["args_shrunk"], intra_task=report["intra_task"])
        if self.todos:
            self.refresh_system_message()  # the checklist must survive the compaction
        self.host.emit(
            "compaction", agent=self.agent_id, before=report["before"], after=report["after"],
            elided=report["elided"], summarized=bool(report["summary"]),
        )
        if report["summary"] and self.is_main:
            self.host.emit("summary", agent=self.agent_id, text=report["summary"])
        return report

    # --- tools --------------------------------------------------------------------------
    def _run_tool_calls(self, calls: list[dict[str, Any]]) -> None:
        """Execute calls in order; consecutive read-only delegations run in parallel."""
        index = 0
        while index < len(calls):
            group = [calls[index]]
            if self._parallel_delegate(calls[index]):
                while index + len(group) < len(calls) and self._parallel_delegate(calls[index + len(group)]):
                    group.append(calls[index + len(group)])
            if len(group) > 1:
                self._run_parallel_delegations(group)
            else:
                self._run_one(group[0])
            index += len(group)

    def _parallel_delegate(self, call: dict[str, Any]) -> bool:
        function = call.get("function") or {}
        if function.get("name") != "delegate":
            return False
        spec = self.specs.get(str(parse_arguments(function.get("arguments")).get("role") or ""))
        return spec.read_only if spec else False

    def _run_parallel_delegations(self, group: list[dict[str, Any]]) -> None:
        workers = max(1, int(self.config.agents.get("max_parallel", 2)))
        for call in group:
            self._announce(call)
        # Each worker runs in a copy of this context, so its helper's requests stay in this task.
        contexts = [contextvars.copy_context() for _ in group]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dcode-agent") as pool:
            outputs = list(pool.map(lambda pair: pair[0].run(self._safe_execute, pair[1]), zip(contexts, group, strict=True)))
        for call, output in zip(group, outputs, strict=True):
            self._record(call, output)

    def _run_one(self, call: dict[str, Any]) -> None:
        self._announce(call)
        self._record(call, self._safe_execute(call))

    def _announce(self, call: dict[str, Any]) -> None:
        function = call.get("function") or {}
        self.host.emit(
            "tool_call",
            agent=self.agent_id,
            id=call.get("id", ""),
            name=function.get("name", ""),
            args=parse_arguments(function.get("arguments")),
        )

    def _record(self, call: dict[str, Any], output: str) -> None:
        function = call.get("function") or {}
        name = function.get("name", "")
        self._track(name, parse_arguments(function.get("arguments")), output)
        exp = self.config.experiment
        if exp["payload_stubbing"] and name in exp["payload_stub_tools"]:
            self._stub_payload(name, function, exp["payload_stub_min_chars"])
        if self.config.hooks:
            try:
                hooks.run(self.config.hooks, "after_tool", workspace_root(), name, output=output,
                          session=self._session_id(), **hooks.tool_payload(parse_arguments(function.get("arguments"))))
            except Exception:
                pass  # a broken hook must never break the agent
        if name == "run_command":
            self._command_outputs = (self._command_outputs + [output])[-exp["repeat_limit"]:]
        self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name, "content": output})
        self.host.emit(
            "tool_result", agent=self.agent_id, id=call.get("id", ""), name=name, output=output[:PREVIEW_CHARS]
        )

    @staticmethod
    def _stub_payload(name: str, function: dict[str, Any], min_chars: int) -> None:
        """The big payload is on disk or was just applied; keep only a stub in context."""
        args = parse_arguments(function.get("arguments"))
        for key in STUB_ARG_KEYS:
            value = args.get(key)
            if not isinstance(value, str) or len(value) <= min_chars:
                continue
            if name in COMPACT_ARG_TOOLS:
                # Make it unmistakable that the payload arrived; a vague stub made models
                # believe the save failed and re-save placeholders in a loop.
                args[key] = (f"[{len(value)} characters — delivered and stored successfully; "
                             "omitted from history only to save context]")
            else:  # write_file, edit_file: these can be refused, and the tool result says which it was
                args[key] = (f"[{len(value)} characters — delivered with this call; omitted from history only "
                             "to save context. The tool result says whether it worked.]")
        function["arguments"] = json.dumps(args)

    def _tool_chars(self) -> int:
        return sum(len(m.get("content") or "") for m in self.messages
                   if m.get("role") == "tool" and isinstance(m.get("content"), str))

    def _note_stubbed(self, folded: list[dict[str, Any]] | None = None) -> None:
        """Remember which calls' outputs just left the context, so calling one again can be traced as regret."""
        def walk(messages: list[dict[str, Any]], every: bool) -> None:
            signatures = {}
            for message in messages:
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    signatures[call.get("id")] = _call_signature(function.get("name", ""), parse_arguments(function.get("arguments")))
            for message in messages:
                if message.get("role") == "tool" and (every or str(message.get("content") or "").startswith("[elided")):
                    signature = signatures.get(message.get("tool_call_id"))
                    if signature:
                        self._stubbed[signature] = "summarized" if every else "elided"

        walk(self.messages, False)
        if folded:
            walk(folded, True)

    def _refuse(self, reason: str, text: str) -> str:
        """A tool result that says no: the call did nothing, and the trace should say why."""
        self._tool_note.refused = reason
        return text

    def _safe_execute(self, call: dict[str, Any]) -> str:
        """Run one tool call, and when tracing write down what it cost."""
        tracer = trace.for_config(self.config)
        if tracer is None:
            return self._execute_call(call)
        function = call.get("function") or {}
        name = function.get("name", "")
        raw = function.get("arguments") or ""
        note = self._tool_note
        note.refused, note.waited = "", 0.0
        tools.take_raw_chars()
        started = time.perf_counter()
        output = self._execute_call(call)
        elapsed = time.perf_counter() - started - note.waited
        kept, measured = len(output), tools.take_raw_chars()
        truncated = (measured is not None and measured > kept) or "(truncated at " in output
        refused = note.refused or ""
        failed = not refused and (_failed(output) or output.startswith(TOOL_ERROR_PREFIXES))
        tracer.emit("tool_call", name=name, arg_chars=len(raw), raw_output_chars=measured if measured is not None else (None if truncated else kept),
                    kept_chars=kept, truncated=truncated, duration_ms=round(elapsed * 1000, 1),
                    approval_wait_ms=round(note.waited * 1000, 1), ok=not (refused or failed), error=_error_kind(output) if failed else None,
                    was_refused=bool(refused), refusal=refused or None)
        signature = _call_signature(name, parse_arguments(raw))
        how = self._stubbed.pop(signature, None)
        if how:
            tracer.emit("elision_regret", name=name, how=how, signature=hashlib.sha256(signature.encode()).hexdigest()[:trace.HASH_CHARS])
        return output

    def _execute_call(self, call: dict[str, Any]) -> str:
        function = call.get("function") or {}
        name = function.get("name", "")
        raw = function.get("arguments")
        args = parse_arguments(raw)
        if raw and raw.strip() not in ("", "{}") and not args:
            return self._refuse("invalid_arguments", "Invalid JSON arguments. Call the tool again with valid JSON.")
        signature = _call_signature(name, args)
        limit = self.config.experiment["repeat_limit"]
        self._repeat = (self._repeat + [signature])[-limit:]
        same = len(self._repeat) == limit and len(set(self._repeat)) == 1
        if same and name == "run_command":
            # Running a command again is normal (tests after a fix); the same failure each time is not.
            outputs = self._command_outputs[-(limit - 1):]
            if len(outputs) == limit - 1 and len(set(outputs)) == 1 and _failed(outputs[0]):
                self._stuck += 1
                return self._refuse("repeat_guard", (
                    f"You ran this command {limit - 1} times in a row and it failed the same way each "
                    "time. Running it again won't help: read the error, change something, or ask the user."))
        elif same:
            self._stuck += 1
            return self._refuse("repeat_guard", (
                f"You called {name} with identical arguments {limit} times; the result will not change. "
                "Use the information you have or try a different approach."
            ))
        if name not in self.tool_names():
            return self._refuse("unavailable", f"Tool '{name}' is not available. Available: {', '.join(self.tool_names())}")
        if self.config.hooks:
            reason = hooks.before_tool(self.config.hooks, name, args, workspace_root(), self._session_id())
            if reason:
                self.host.emit("notice", agent=self.agent_id, level="warning", text=f"A hook blocked {name}: {reason[:200]}")
                return self._refuse("hook", f"Blocked by the user's hook: {reason}")
        try:
            return self._dispatch(name, args)
        except Interrupted:
            raise
        except Exception as exc:  # a tool must never kill the loop
            return f"Tool error: {exc}"

    def _saw(self, path: Any) -> None:
        try:
            self._seen_files.add(str(_safe_path(str(path))))
        except Exception:
            pass  # the tool itself reports a bad path

    def _unread_overwrite(self, path: str) -> str:
        """Why write_file must not replace this file yet ("" when it may). Asked once per file: a second
        write_file on the same path goes through, for when replacing it is really meant."""
        try:
            target = _safe_path(path)
        except Exception:
            return ""
        key = str(target)
        if key in self._seen_files or not target.is_file() or target.stat().st_size == 0:
            return ""
        self._seen_files.add(key)
        return (f"Not written: {path} already exists and you haven't read it, so this would replace all of it. "
                "Read it first, or use edit_file to change part of it. If replacing it is really intended, "
                "call write_file again.")

    def _session_id(self) -> str:
        return str(getattr(self.host, "id", "") or "")

    def _approve(self, kind: str, detail: str) -> bool:
        started = time.perf_counter()
        approved = self.host.request_approval(kind, detail, self.agent_id)
        self._tool_note.waited = getattr(self._tool_note, "waited", 0.0) + time.perf_counter() - started
        if not approved:
            self._tool_note.refused = "approval_denied"
        return approved

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name.startswith("mcp__") and self._mcp_full:
            target = self._mcp_functions().get(name)
            if target:
                try:
                    return self._mcp_tool("mcp_call", {"server": target["server"], "tool": target["tool"], "arguments": args})
                except external.MCPError as exc:
                    return f"MCP error: {exc}"
        if name == "delegate":
            return self._delegate(str(args.get("role") or "explore"), str(args.get("task") or ""))
        if name == "artifact_save" and args.get("append") and args.get("id"):
            existing = self.artifacts.get(str(args["id"]))
            if existing is None:
                return f"No artifact with id '{args['id']}' to append to. Save the first part without append."
            meta = self.artifacts.append(str(args["id"]), str(args.get("content") or ""))
            self.host.emit("artifact_saved", agent=self.agent_id, **meta)
            return (f"Saved artifact '{meta['title']}' (id: {meta['id']}, kind: {meta['kind']}, "
                    f"version {meta['versions']}). Appended {len(str(args.get('content') or ''))} chars; "
                    f"total {meta['size']} chars.")
        if name == "artifact_save":
            saves = self._artifact_saves  # per-task: id -> number of saves
            artifact_id = str(args.get("id") or "") or None
            if artifact_id and self.artifacts.get(artifact_id) is None:
                artifact_id = None  # unknown id: treat as a new save (matched below)
            if not artifact_id:
                artifact_id = self._match_artifact(str(args.get("title") or ""))
            if artifact_id and saves.get(artifact_id, 0) >= MAX_ARTIFACT_SAVES:
                record = self.artifacts.get(artifact_id) or {}
                return (f"Not saved again: artifact '{record.get('title', artifact_id)}' (id: {artifact_id}) was "
                        f"already saved {saves[artifact_id]} times in this task and its content IS stored "
                        f"({len(record.get('content', ''))} chars). Stop re-saving; tell the user it's ready.")
            content = str(args.get("content") or "")
            meta = self.artifacts.save(
                title=str(args.get("title") or "") or (self.artifacts.get(artifact_id) or {}).get("title", "Untitled"),
                content=content,
                kind=str(args.get("kind") or ""),
                language=str(args.get("language") or ""),
                artifact_id=artifact_id,
                cwd=str(workspace_root()),
                thread=str(getattr(self.host, "thread_name", "") or ""),
            )
            saves[meta["id"]] = saves.get(meta["id"], 0) + 1
            self.host.emit("artifact_saved", agent=self.agent_id, update=saves[meta["id"]] > 1, **meta)
            preview = " ".join(content[:80].split())
            return (f"Saved artifact '{meta['title']}' (id: {meta['id']}, kind: {meta['kind']}, "
                    f"version {meta['versions']}). Received and stored {len(content)} chars, starting "
                    f"\"{preview}…\". The save worked — don't save it again unless the user asks for changes; "
                    f"to change it later, pass id \"{meta['id']}\" so it stays one artifact. "
                    "The user can open it in the web UI.")
        if name == "artifact_get":
            artifact_id = str(args.get("id") or "").strip()
            if not artifact_id:
                items = self.artifacts.list()[:20]
                return "\n".join(f"- {m['id']}: {m['title']} ({m['kind']})" for m in items) or "(no artifacts yet)"
            record = self.artifacts.get(artifact_id)
            if record is None:
                return f"No artifact with id '{artifact_id}'. Call artifact_get with no id to list them."
            return _truncate(f"[{record['title']} · {record['kind']}]\n{record['content']}")
        if name == "ask_user":
            question = " ".join(str(args.get("question") or "").split())
            if not question:
                return "ask_user needs a question."
            raw = args.get("options")
            options = [" ".join(str(o).split())[:120] for o in raw if str(o).strip()] if isinstance(raw, list) else []
            started = time.perf_counter()
            answer = self.host.ask_user(question, options[:6], self.agent_id).strip()
            self._tool_note.waited = getattr(self._tool_note, "waited", 0.0) + time.perf_counter() - started
            if not answer:
                return "The user skipped the question. Decide sensibly yourself and say what you assumed."
            return f"The user answered: {answer}"
        if name == "todo_write":
            items = roles.normalize_todos(args.get("items"))
            if not items:
                return "todo_write needs items: [{text, status}] with status pending, in_progress or done."
            self.todos = items
            self.host.emit("todos", agent=self.agent_id, items=items)
            done = sum(1 for i in items if i["status"] == "done")
            return f"Checklist saved ({done}/{len(items)} done):\n{roles.render_todos(items)}"
        if name == "skill_use":
            skill = get_skill(str(args.get("name") or ""), workspace_root())
            if skill is None:
                names = ", ".join(s.name for s in list_skills(workspace_root()))
                return f"No skill named '{args.get('name')}'. Available: {names or '(none)'}"
            self.host.emit("skill_used", agent=self.agent_id, name=skill.name)
            return _truncate(f"# Skill: {skill.name}\n{skill.content}", 8000)
        if name == "skill_save":
            skill = save_skill(
                str(args.get("name") or "skill"), str(args.get("description") or ""),
                str(args.get("instructions") or ""),
            )
            self.host.emit("skill_saved", agent=self.agent_id, name=skill.name)
            self.refresh_system_message()
            return f"Saved skill '{skill.name}' to {skill.path}. It now appears in the skills list."
        if name in ("mcp_tools", "mcp_call"):
            try:
                return self._mcp_tool(name, args)
            except external.MCPError as exc:
                return f"MCP error: {exc}"
        if name in ("db_schema", "db_query"):
            try:
                return self._db_tool(name, args)
            except databases.DBError as exc:
                return f"Database error: {exc}"
            except tools.WorkspaceError as exc:
                return f"Blocked: {exc}"
        if name == "memory_save":
            if self.memory is None:
                return "Memory is disabled."
            memory = self.memory.save(
                name=str(args.get("name") or "note"),
                body=str(args.get("content") or ""),
                scope=str(args.get("scope") or "project"),
            )
            self.host.emit("memory_saved", agent=self.agent_id, name=memory.name, scope=memory.scope)
            return f"Saved memory '{memory.name}' ({memory.scope})."
        if name == "memory_search":
            if self.memory is None:
                return "Memory is disabled."
            hits = self.memory.search(str(args.get("query") or ""), k=5)
            if not hits:
                return "(no matching memories)"
            return _truncate("\n\n".join(f"### {m.name} ({m.scope})\n{m.body}" for m in hits), 6000)

        refusal, outside_ok = self._file_rules(name, args)
        if refusal:
            return self._refuse(getattr(self._tool_note, "refused", "") or "file_rule", refusal)
        with tools.outside_workspace_allowed(outside_ok):
            return self._dispatch_builtin(name, args)

    # --- databases ------------------------------------------------------------------------
    def _db_connection(self, wanted: str) -> dict[str, Any]:
        connections = databases.load_connections()
        if not wanted and len(connections) == 1:
            return next(iter(connections.values()))
        if not wanted:
            raise databases.DBError("Say which connection: " + ", ".join(connections) + ".")
        return databases.get_connection(wanted)

    def _db_schema(self, conn: dict[str, Any]) -> dict[str, Any]:
        cached = self._db_schemas.get(conn["name"])
        if cached and time.time() - cached[0] < DB_SCHEMA_TTL:
            return cached[1]
        schema = databases.read_schema(conn)
        self._db_schemas[conn["name"]] = (time.time(), schema)
        return schema

    def _db_tool(self, name: str, args: dict[str, Any]) -> str:
        conn = self._db_connection(str(args.get("connection") or "").strip())
        budget = tools.MAX_OUTPUT_CHARS // 2  # a schema or result must leave room to think about it
        if name == "db_schema":
            return databases.schema_text(self._db_schema(conn), str(args.get("table") or "").strip(), budget)
        statement = databases.single_statement(str(args.get("sql") or ""))
        save_as = str(args.get("save_as") or "").strip()
        if databases.classify(statement) == "write":
            if self.mode == "plan":
                return "Plan mode: a statement that changes the database can't run. Put it in the plan instead."
            if conn.get("read_only"):
                return (f"Blocked: the user marked the connection '{conn['name']}' read-only in Dcode. "
                        "Don't retry; tell the user what you would have run.")
            # Always asked, in every mode, and never "for the session": each change is seen by the user.
            if not self._approve("db_write", f"{statement}\n\non {databases.describe(conn)}"):
                return "Change cancelled by the user. Nothing was run. Don't retry it; ask what they want instead."
            result = databases.run_query(conn, statement, confirmed=True)
            self._db_schemas.pop(conn["name"], None)  # the structure may have changed
            return databases.result_text(result) + f" Ran on {conn['name']} in {result['elapsed']}s."
        if save_as:
            if self.mode == "plan":
                return "Plan mode: saving a file is disabled. Run the query without save_as."
            target = _safe_path(save_as)
            if self.mode == "manual" and not self._approve("edit_file", f"write {save_as} (database export)"):
                return f"Export cancelled by user for {save_as}"
            self._checkpoint(save_as)
            fmt = "jsonl" if target.suffix.lower() in (".json", ".jsonl", ".ndjson") else "csv"
            done = databases.export_query(conn, statement, target, fmt)
            self._track("write_file", {"path": save_as}, "Wrote")
            cut = f" Stopped at {done['stopped']}: the file is incomplete." if done["truncated"] else ""
            return f"Saved {done['rows']:,} rows ({done['size']}, {done['format']}) to {save_as}.{cut}"
        result = databases.run_query(conn, statement, row_limit=min(DB_MODEL_ROWS, int(self.config.db.get("row_limit", 200))))
        return databases.result_text(result, max_chars=budget)

    # --- external tools (MCP) ---------------------------------------------------------------
    def _mcp_tool(self, name: str, args: dict[str, Any]) -> str:
        servers = getattr(self.host, "mcp", None)
        if not servers:
            return "No MCP servers are set up."
        wanted, tool = str(args.get("server") or "").strip(), str(args.get("tool") or "").strip()
        if name == "mcp_tools":
            return servers.describe(wanted, tool, max_chars=tools.MAX_OUTPUT_CHARS // 2)
        if not tool:
            return "mcp_call needs a tool name. Call mcp_tools to list them."
        server, spec = servers.find(wanted, tool)
        arguments = args.get("arguments")
        if isinstance(arguments, str):  # small models often send the object as a JSON string
            arguments = parse_arguments(arguments)
        if not isinstance(arguments, dict):
            arguments = {}
        if self.mode == "plan" and not external.read_only(spec):
            return (f"Plan mode: {server.name} · {tool} may change something, so it can't run yet. "
                    "Put it in the plan instead.")
        # A server's "read-only" note is its own claim: enough for plan mode, not enough to skip asking.
        asks = server.spec.get("approve") != "allow" and self.mode != "auto"
        detail = f"{server.name} · {tool}  {json.dumps(arguments, ensure_ascii=False)[:600]}"
        if asks and not self._approve("mcp_tool", detail):
            return f"{server.name} · {tool} was cancelled by the user. Don't retry it; ask what they want instead."
        text, failed = server.call(tool, arguments)
        return _truncate(("The tool reported an error:\n" if failed else "") + text)

    def _ruleset(self) -> permission_rules.RuleSet:
        return self.rules if self.rules is not None else permission_rules.load(self.config.rules)

    def _file_rules(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Apply file rules before a file tool runs: (refusal or "", may it go outside the workspace)."""
        if name not in FILE_ARG_TOOLS or not str(args.get("path") or "").strip():
            return "", False
        raw = str(args["path"])
        root = workspace_root()
        target = Path(raw).expanduser()
        target = (target if target.is_absolute() else root / target).resolve()
        inside = target == root or root in target.parents
        outside_ok = False
        if not inside and not tools._allow_outside_workspace():
            decision = self._ruleset().outside_workspace
            if decision == "deny":
                return "", False  # the tool reports the sandbox error itself
            if decision == "ask" and not self._approve("outside_workspace", f"{name} {target}"):
                return f"{name} cancelled: the user did not allow access to {target} (outside the project).", False
            outside_ok = True
        rel = str(target.relative_to(root)) if inside else str(target)
        decision, pattern = self._ruleset().file(rel)
        if decision == "deny":
            return f"Blocked: '{rel}' is protected by the rule '{pattern}'. Don't read or change it.", False
        if decision == "ask" and not self._approve("rule", f"{name} {rel}   (rule: {pattern})"):
            return f"{name} cancelled by user for {rel}.", False
        return "", outside_ok

    def _dispatch_builtin(self, name: str, args: dict[str, Any]) -> str:
        experimental = self.config.experimental
        mode = self.mode
        if mode == "plan" and name in roles.PLAN_BLOCKED_TOOLS:
            return self._refuse("plan_mode", f"Plan mode: {name} is disabled. Finish the plan and ask the user to approve it.")
        auto = mode == "auto"

        if name == "run_command":
            command = str(args.get("command", ""))
            background = bool(args.get("background")) and getattr(self.host, "jobs", None) is not None
            detail = f"{command}   (background)" if background else command
            decision, pattern = self._ruleset().command(command)
            if decision == "deny":
                return self._refuse("command_rule", (f"Blocked: the rule '{pattern}' denies this command. Don't retry it — "
                                                     "find another way or ask the user to run it."))
            if decision == "ask":
                if not self._approve("rule", f"{detail}   (rule: {pattern})"):
                    return "Command cancelled by user."
            elif decision != "allow" and not (auto or experimental.get("auto_approve_commands")) \
                    and not self._approve("run_command", detail):
                return "Command cancelled by user."
            if background:
                job = self.host.jobs.start(command, str(workspace_root()))
                self.host.emit("job_started", agent=self.agent_id, **job.as_dict())
                time.sleep(1.0)  # catch instant failures and first output
                state = job.state()
                return (f"Started background job #{job.id} (pid {job.process.pid}), {state}. "
                        f"Check it with job_status {job.id}; stop it with job_stop {job.id}.\n"
                        f"First output:\n{job.tail(15) or '(none yet)'}")
            return execute_tool(name, args)

        if name in ("job_status", "job_stop"):
            jobs = getattr(self.host, "jobs", None)
            if jobs is None:
                return "Background jobs are not available."
            if name == "job_stop":
                ok = jobs.stop(int(args.get("id") or 0))
                return f"Stopped job #{args.get('id')}." if ok else f"Job #{args.get('id')} is not running."
            if args.get("id") in (None, "", 0):
                listed = jobs.list()
                return "\n".join(f"#{j.id} [{j.state()}] {j.command}" for j in listed) or "(no background jobs)"
            job = jobs.get(int(args["id"]))
            if job is None:
                return f"No job #{args['id']}."
            return _truncate(f"#{job.id} [{job.state()}, {int(job.elapsed())}s] {job.command}\n{job.tail()}")

        if name == "delete_file":
            target = str(args.get("path", ""))
            if not auto and not self._approve("delete_file", target):
                return f"Deletion cancelled by user for {target}"
            self._checkpoint(target)
            return execute_tool(name, args)

        if name == "git_commit":
            explicit = "commit" in self.current_task.lower()
            if not experimental.get("auto_commit") and not explicit:
                return self._refuse("commit_gate", "git_commit blocked: auto_commit is disabled and the user did not explicitly request a commit.")
            message = str(args.get("message", ""))
            if not auto and not self._approve("git_commit", message):
                return "Git commit cancelled by user."
            output = execute_tool(name, args)
            self.git = git_summary()
            return output

        if name == "write_file":
            refused = self._unread_overwrite(str(args.get("path", "")))
            if refused:
                return self._refuse("blind_write", refused)
        if name in ("read_file", "edit_file", "write_file"):
            self._saw(args.get("path"))
        if name in ("write_file", "edit_file", "create_directory"):
            if mode == "manual" and not self._approve("edit_file", self._edit_detail(name, args)):
                return f"{name} cancelled by user for {args.get('path', '')}"
            self._checkpoint(str(args.get("path", "")))

        output = execute_tool(name, args)
        if name in ("write_file", "edit_file") and output.startswith(("Wrote", "Edited")):
            output += self._post_edit_check(str(args.get("path", "")))
        if name in ("write_file", "edit_file") and experimental.get("auto_commit") and self.is_main:
            auto_message = f"Auto commit after write: {Path(str(args.get('path', 'file'))).name}"
            if self._approve("git_commit", auto_message):
                execute_tool("git_commit", {"message": auto_message})
                self.git = git_summary()
        return output

    def _post_edit_check(self, path: str) -> str:
        """Run the file's checker right away, so the model fixes errors in its next step."""
        try:
            target = _safe_path(path)
            note, problems = checks.after_edit(target, self.config.checks, workspace_root())
        except Exception:
            return ""  # a broken checker must never break editing
        if problems:
            self.host.emit("check", agent=self.agent_id, path=path, ok=False,
                           problems=problems, count=checks.problem_count(problems))
        return f"\n{note}" if note else ""

    # --- sub-agents ---------------------------------------------------------------------
    def _model_for(self, model: str) -> tuple[AppConfig, Any]:
        """The config and client a helper runs with: a profile (maybe another server), a model name, or ours."""
        pool = getattr(self.host, "models", None)
        if pool is not None:
            return pool.resolve(model) or (self.config, self.llm)
        if not model or model == self.config.model:
            return self.config, self.llm
        config = dataclasses.replace(self.config, model=model)
        return config, (self.llm.with_model(model) if hasattr(self.llm, "with_model") else self.llm)

    def _delegate(self, role: str, task: str) -> str:
        if not self.is_main:
            return "Sub-agents cannot delegate further."
        specs = self.specs or load_specs(None)
        if role not in specs:
            role = "explore"
        spec = specs[role]
        if self.mode == "plan" and not spec.read_only:
            return "Plan mode: only read-only helpers (like explore and review) are available until the plan is approved."
        if not task.strip():
            return "delegate needs a non-empty task."
        agent_id = f"{role}-{next(_agent_counter)}"
        config, llm = self._model_for(spec.model or str(self.config.agents.get("helper_model") or ""))
        self.host.emit("agent_start", agent=agent_id, parent=self.agent_id, role=role, task=task,
                       model=config.model)
        sub = Agent(config, llm, self.host, self.memory, role=role, agent_id=agent_id, git=self.git, spec=spec)
        ok = True
        try:
            with trace.use(agent_id=agent_id, helper=True, step=None):
                result = sub.run_turn(task, max_steps=spec.steps or None)
            report = result.text or "(sub-agent returned no report)"
        except Interrupted:
            self.host.emit("agent_end", agent=agent_id, role=role, ok=False, report="interrupted")
            raise
        except Exception as exc:
            report = f"Sub-agent failed: {exc}"
            ok = False
        finally:
            self._apply_limits(self.config)  # a helper on a smaller model shrank the output caps
            with self._spent_lock:  # helpers may run in parallel; what one spent counts even if it failed
                trace.add_usage(self.spent, sub.spent)
                self._estimated = self._estimated or sub._estimated
        # Cap what flows back into the (small) main context.
        raw_report = len(report)
        report = _truncate(report, int(self.config.context_window * 3.5 * 0.08))
        tracer = trace.for_config(self.config)
        if tracer:
            tracer.emit("delegate", role=role, helper_id=agent_id, model=config.model, ok=ok, steps=sub._steps_done,
                        report_chars=raw_report, report_kept_chars=len(report), calls=sub.spent["calls"],
                        prompt_tokens=sub.spent["prompt"], completion_tokens=sub.spent["completion"],
                        cached_tokens=sub.spent["cached"])
        self.host.emit("agent_end", agent=agent_id, role=role, ok=True, report=report)
        return f"[{agent_id} report]\n{report}"


class NullHost:
    """Host that auto-denies approvals and prints nothing (scripting / tests)."""

    def __init__(self, approve: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self._approve = approve
        self._lock = threading.Lock()
        self.stop = False
        self.mode = roles.DEFAULT_MODE
        self.checkpoints = None
        self.answer = ""  # what ask_user returns ("" = skipped)

    def emit(self, type: str, **data: Any) -> None:
        with self._lock:
            self.events.append({"type": type, **data})

    def request_approval(self, kind: str, detail: str, agent: str) -> bool:
        return self._approve

    def ask_user(self, question: str, options: list[str], agent: str) -> str:
        self.events.append({"type": "question", "question": question, "options": options})
        return self.answer

    def should_stop(self) -> bool:
        return self.stop
