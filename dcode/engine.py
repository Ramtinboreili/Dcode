"""The agent loop, free of any terminal or web concerns.

An :class:`Agent` talks to the model, executes tools and reports everything
through a :class:`Host` (implemented by :class:`dcode.session.Session`). The
CLI and the web UI are just two views of the same host, which is what lets a
session be driven from either one.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import agents as roles
from .artifacts import ArtifactStore
from .config import AppConfig
from .context import SUMMARY_INSTRUCTIONS, SUMMARY_PREFIX, ContextBudget, compact, render_for_summary
from .llm import Interrupted, LLMClient
from .memory import MemoryStore
from .skills import get_skill, list_skills, normalize_name, save_skill
from .tools import (
    TOOL_SCHEMAS,
    _safe_path,
    _truncate,
    allowed_tool_names,
    configure_limits,
    execute_tool,
    parse_arguments,
    workspace_root,
)

PROJECT_INSTRUCTION_FILES = ("DCODE.md", "AGENTS.md", "CLAUDE.md")
MAIN_MAX_STEPS = 60
REPEAT_LIMIT = 3
PREVIEW_CHARS = 4000
COMPACT_ARG_TOOLS = {"artifact_save", "skill_save"}


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
    ) -> None:
        self.config = config
        self.llm = llm
        self.host = host
        self.memory = memory
        self.role = role
        self.agent_id = agent_id
        self.git = git if git is not None else {"is_repo": False}
        self.budget = ContextBudget(config.context_window, config.max_output_tokens)
        self.recalled = ""
        self.current_task = ""
        self._repeat: list[str] = []
        configure_limits(config.context_window)
        self.artifacts = ArtifactStore()
        self.checkpoints = getattr(host, "checkpoints", None)
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt()}]

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
            if self.config.agents.get("enabled", True):
                names.append("delegate")
            if self.config.artifacts.get("enabled", True):
                names += roles.ARTIFACT_TOOL_NAMES
            names += roles.SKILL_TOOL_NAMES
        else:
            wanted = roles.ROLE_TOOLS.get(self.role, roles.ROLE_TOOLS["explore"])
            ungated = {n for n in roles.UNGATED_TOOLS if n != "memory_search" or memory_on}
            names = [n for n in wanted if n in allowed or n in ungated]
        if self.mode == "plan":
            names = [n for n in names if n not in roles.PLAN_BLOCKED_TOOLS]
        return names

    def tools(self) -> list[dict[str, Any]]:
        schemas = []
        for name in self.tool_names():
            schema = TOOL_SCHEMAS.get(name) or roles.EXTRA_TOOL_SCHEMAS.get(name)
            if schema:
                schemas.append(schema)
        return schemas

    def system_prompt(self) -> str:
        cwd = str(workspace_root())
        window = self.config.context_window
        if not self.is_main:
            return roles.role_prompt(self.role, cwd)

        parts = [roles.MAIN_PROMPT.format(cwd=cwd)]
        if self.config.agents.get("enabled", True):
            parts.append(roles.delegation_block(window))
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
            if self.recalled:
                parts.append(f"Memories relevant to the current task:\n{self.recalled}")
        if self.config.artifacts.get("enabled", True):
            parts.append(roles.ARTIFACT_PROMPT)
        if self.mode == "plan":
            parts.append(roles.PLAN_PROMPT)
        skills = self._skills_block(max_chars=int(window * 3.5 * 0.05))
        if skills:
            parts.append(skills)
        return "\n\n".join(parts)

    def refresh_system_message(self) -> None:
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
        started = time.perf_counter()
        usage = {"prompt": 0, "completion": 0, "total": 0}
        max_steps = max_steps or (MAIN_MAX_STEPS if self.is_main else int(self.config.agents.get("max_steps", 16)))
        self.current_task = user_text
        self._repeat = []
        self._activity = {"files": [], "commands": 0, "commands_failed": 0, "helpers": 0, "artifacts": []}
        if self.is_main and self.checkpoints is not None:
            self.checkpoints.begin(user_text)

        if self.is_main and self.memory is not None and self.config.memory.get("auto_recall", True):
            budget_chars = int(self.config.memory.get("budget_tokens", 1200) * 3.5)
            try:
                self.recalled = self.memory.recall(user_text, max_chars=budget_chars)
            except Exception:
                self.recalled = ""
        self.refresh_system_message()
        self.messages.append({"role": "user", "content": user_text})

        steps = 0
        nudged = False
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
            self._maybe_compact(tools)
            turn = self.llm.chat(
                self.messages,
                tools=tools or None,
                on_delta=lambda text: self.host.emit("assistant_delta", agent=self.agent_id, text=text),
                should_stop=self.host.should_stop,
                max_tokens=self.budget.max_output_tokens,
            )
            for key in usage:
                usage[key] += turn.usage.get(key, 0)
            if turn.usage.get("prompt"):
                self.budget.counter.calibrate(self.messages, tools, turn.usage["prompt"])
            if not tools:
                turn.tool_calls = []  # never store calls we won't answer — breaks history
            self._dedupe_call_ids(turn.tool_calls)
            if turn.recovered_tool_calls:
                self.host.emit("notice", agent=self.agent_id, level="info", text="Recovered tool call from model text")
            self.messages.append(turn.as_message())

            if not turn.tool_calls or not tools:
                if not turn.content.strip() and not nudged and steps < max_steps:
                    # Small models sometimes stop silently after a tool result.
                    nudged = True
                    self.messages.append({"role": "user", "content": "Continue. If you are done, give your final answer."})
                    continue
                final = turn.content.strip()
                break

            if turn.content.strip():
                self.host.emit("assistant_note", agent=self.agent_id, text=turn.content.strip())
            self._run_tool_calls(turn.tool_calls)
            steps += 1

        if self.is_main and not final and (steps or self._activity["files"] or self._activity["commands"]):
            final = self._final_report(usage)
        elapsed = time.perf_counter() - started
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
            **usage,
        )
        return TurnResult(text=final, usage=usage, elapsed=elapsed, steps=steps)

    REPORT_REQUEST = (
        "The work is finished. Write a short report for the user (no tool calls): what you did, "
        "files changed, commands you ran and their outcome, the current status, and anything left to do."
    )

    def _final_report(self, usage: dict[str, int]) -> str:
        """The model stopped without summarizing: ask once, else build a factual report."""
        self.messages.append({"role": "user", "content": self.REPORT_REQUEST})
        try:
            turn = self.llm.chat(self.messages, tools=None, should_stop=self.host.should_stop,
                                 max_tokens=self.budget.max_output_tokens)
            for key in usage:
                usage[key] += turn.usage.get(key, 0)
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
            ).strip()
        except Exception:
            summary = ""
        if not summary:
            summary = "\n".join(
                f"- {m['content'][:200]}" for m in history
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            )[:2500]
        self.messages[1:] = [
            {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary}"},
            {"role": "assistant", "content": "Understood. Continuing from the summary."},
        ]
        self.refresh_system_message()
        after = self.budget.usage(self.messages, self.tools())
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

    def _maybe_compact(self, tools: list[dict[str, Any]]) -> None:
        if not self.budget.needs_compaction(self.messages, tools):
            return

        def summarize(transcript: str) -> str:
            return self.llm.complete_text(
                [{"role": "system", "content": SUMMARY_INSTRUCTIONS}, {"role": "user", "content": transcript}],
                max_tokens=min(700, self.budget.max_output_tokens),
            )

        report = compact(self.messages, self.budget, summarize, tools)
        if not report:
            return
        self.host.emit(
            "compaction", agent=self.agent_id, before=report["before"], after=report["after"],
            elided=report["elided"], summarized=bool(report["summary"]),
        )
        if report["summary"] and self.is_main:
            self.host.emit("summary", agent=self.agent_id, text=report["summary"])

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
        return parse_arguments(function.get("arguments")).get("role") in roles.PARALLEL_SAFE_ROLES

    def _run_parallel_delegations(self, group: list[dict[str, Any]]) -> None:
        workers = max(1, int(self.config.agents.get("max_parallel", 2)))
        for call in group:
            self._announce(call)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dcode-agent") as pool:
            outputs = list(pool.map(lambda c: self._safe_execute(c), group))
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
        if name in COMPACT_ARG_TOOLS:
            # The big payload now lives on disk; keep only a stub in context.
            args = parse_arguments(function.get("arguments"))
            for key in ("content", "instructions"):
                value = args.get(key)
                if isinstance(value, str) and len(value) > 240:
                    args[key] = f"[{len(value)} chars — saved, see tool result]"
            function["arguments"] = json.dumps(args)
        self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name, "content": output})
        self.host.emit(
            "tool_result", agent=self.agent_id, id=call.get("id", ""), name=name, output=output[:PREVIEW_CHARS]
        )

    def _safe_execute(self, call: dict[str, Any]) -> str:
        function = call.get("function") or {}
        name = function.get("name", "")
        raw = function.get("arguments")
        args = parse_arguments(raw)
        if raw and raw.strip() not in ("", "{}") and not args:
            return "Invalid JSON arguments. Call the tool again with valid JSON."
        signature = f"{name}:{json.dumps(args, sort_keys=True)}"
        self._repeat = (self._repeat + [signature])[-REPEAT_LIMIT:]
        if len(self._repeat) == REPEAT_LIMIT and len(set(self._repeat)) == 1 and name not in ("run_command",):
            return (
                f"You called {name} with identical arguments {REPEAT_LIMIT} times; the result will not change. "
                "Use the information you have or try a different approach."
            )
        if name not in self.tool_names():
            return f"Tool '{name}' is not available. Available: {', '.join(self.tool_names())}"
        try:
            return self._dispatch(name, args)
        except Interrupted:
            raise
        except Exception as exc:  # a tool must never kill the loop
            return f"Tool error: {exc}"

    def _approve(self, kind: str, detail: str) -> bool:
        return self.host.request_approval(kind, detail, self.agent_id)

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        experimental = self.config.experimental
        if name == "delegate":
            return self._delegate(str(args.get("role") or "explore"), str(args.get("task") or ""))
        if name == "artifact_save":
            meta = self.artifacts.save(
                title=str(args.get("title") or "Untitled"),
                content=str(args.get("content") or ""),
                kind=str(args.get("kind") or ""),
                language=str(args.get("language") or ""),
                artifact_id=str(args.get("id") or "") or None,
                cwd=str(workspace_root()),
                thread=str(getattr(self.host, "thread_name", "") or ""),
            )
            self.host.emit("artifact_saved", agent=self.agent_id, **meta)
            return (f"Saved artifact '{meta['title']}' (id: {meta['id']}, kind: {meta['kind']}, "
                    f"version {meta['versions']}). The user can open it in the web UI.")
        if name == "artifact_get":
            artifact_id = str(args.get("id") or "").strip()
            if not artifact_id:
                items = self.artifacts.list()[:20]
                return "\n".join(f"- {m['id']}: {m['title']} ({m['kind']})" for m in items) or "(no artifacts yet)"
            record = self.artifacts.get(artifact_id)
            if record is None:
                return f"No artifact with id '{artifact_id}'. Call artifact_get with no id to list them."
            return _truncate(f"[{record['title']} · {record['kind']}]\n{record['content']}")
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

        mode = self.mode
        if mode == "plan" and name in roles.PLAN_BLOCKED_TOOLS:
            return f"Plan mode: {name} is disabled. Finish the plan and ask the user to approve it."
        auto = mode == "auto"

        if name == "run_command":
            command = str(args.get("command", ""))
            if not (auto or experimental.get("auto_approve_commands")) and not self._approve("run_command", command):
                return "Command cancelled by user."
            return execute_tool(name, args)

        if name == "delete_file":
            target = str(args.get("path", ""))
            if not auto and not self._approve("delete_file", target):
                return f"Deletion cancelled by user for {target}"
            self._checkpoint(target)
            return execute_tool(name, args)

        if name == "git_commit":
            explicit = "commit" in self.current_task.lower()
            if not experimental.get("auto_commit") and not explicit:
                return "git_commit blocked: auto_commit is disabled and the user did not explicitly request a commit."
            message = str(args.get("message", ""))
            if not auto and not self._approve("git_commit", message):
                return "Git commit cancelled by user."
            output = execute_tool(name, args)
            self.git = git_summary()
            return output

        if name in ("write_file", "edit_file", "create_directory"):
            if mode == "manual" and not self._approve("edit_file", self._edit_detail(name, args)):
                return f"{name} cancelled by user for {args.get('path', '')}"
            self._checkpoint(str(args.get("path", "")))

        output = execute_tool(name, args)
        if name in ("write_file", "edit_file") and experimental.get("auto_commit") and self.is_main:
            auto_message = f"Auto commit after write: {Path(str(args.get('path', 'file'))).name}"
            if self._approve("git_commit", auto_message):
                execute_tool("git_commit", {"message": auto_message})
                self.git = git_summary()
        return output

    # --- sub-agents ---------------------------------------------------------------------
    def _delegate(self, role: str, task: str) -> str:
        if not self.is_main:
            return "Sub-agents cannot delegate further."
        if role not in roles.ROLE_TOOLS:
            role = "explore"
        if self.mode == "plan" and role not in roles.PARALLEL_SAFE_ROLES:
            return "Plan mode: only explore and review helpers are available until the plan is approved."
        if not task.strip():
            return "delegate needs a non-empty task."
        agent_id = f"{role}-{next(_agent_counter)}"
        self.host.emit("agent_start", agent=agent_id, parent=self.agent_id, role=role, task=task)
        sub = Agent(self.config, self.llm, self.host, self.memory, role=role, agent_id=agent_id, git=self.git)
        try:
            result = sub.run_turn(task)
            report = result.text or "(sub-agent returned no report)"
        except Interrupted:
            self.host.emit("agent_end", agent=agent_id, role=role, ok=False, report="interrupted")
            raise
        except Exception as exc:
            report = f"Sub-agent failed: {exc}"
        # Cap what flows back into the (small) main context.
        report = _truncate(report, int(self.config.context_window * 3.5 * 0.08))
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

    def emit(self, type: str, **data: Any) -> None:
        with self._lock:
            self.events.append({"type": type, **data})

    def request_approval(self, kind: str, detail: str, agent: str) -> bool:
        return self._approve

    def should_stop(self) -> bool:
        return self.stop
