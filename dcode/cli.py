"""Interactive terminal front-end for a :class:`~dcode.session.Session`.

The loop is state-driven rather than prompt-driven: at every iteration it
looks at the session (pending approval? busy? idle?) and shows the matching
UI. The web UI can change that state at any time; when it does, the session
fires an "attention" callback that closes the current prompt so the loop
re-evaluates.
"""

from __future__ import annotations

import platform
import shutil
import time
from importlib import import_module
from typing import Any

from . import __version__, ui
from .agents import MODE_LABELS, MODE_SHORT, MODES
from .config import REASONING_EFFORTS, list_skill_files, list_threads, save_config
from .engine import git_summary
from .session import Session
from .theme import MUTED, PRIMARY

PERMISSION_LABELS = {
    "read_file": "read files",
    "write_file": "write / edit files",
    "run_command": "run shell commands",
    "delete_file": "delete files",
    "create_directory": "create directories",
    "search_in_files": "search files",
}
EXPERIMENTAL_LABELS = {
    "multi_file_edit": "multi_file_edit",
    "auto_commit": "auto_commit",
    "streaming_tools": "streaming_tools (show full tool output)",
    "auto_approve_commands": "auto_approve_commands",
}
APPROVAL_LABELS = {"run_command": "Dcode wants to run a command", "delete_file": "Dcode wants to delete a file",
                   "git_commit": "Dcode wants to make a git commit"}
APPROVAL_KINDS = {"run_command": "commands", "delete_file": "deletions", "git_commit": "commits"}
CONTEXT_CHOICES = ["4096", "8192", "16384", "32768", "65536", "131072"]


class TerminalApp:
    def __init__(self, session: Session, ui_url: str | None = None) -> None:
        self.session = session
        self.ui_url = ui_url
        self.activity = "Thinking"
        self.last_outputs: list[tuple[str, Any, str]] = []  # (tool, args, output) of the last task, for Ctrl+O
        self._calls: dict[str, tuple[str, Any]] = {}

    # --- main loop -------------------------------------------------------------------------
    def status_text(self) -> str:
        """Line under the input box: chat · model · context on the left, key hints on the right."""
        session = self.session
        cfg = session.config
        try:
            used = session.agent.budget.usage(session.agent.messages, session.agent.tools())
            pct = min(100, int(100 * used / max(1, cfg.context_window)))
        except Exception:
            pct = 0
        name = session.thread_name
        name = "new chat" if name[:8].isdigit() and "-" in name else name
        left = f"{name[:40]} · ctx {pct}%"
        mode_part = len(f"  ⏵⏵ {MODE_SHORT.get(session.mode, session.mode)} on (shift+tab to cycle) · ")
        width = shutil.get_terminal_size((100, 24)).columns - 2 - mode_part
        hints = ["ctrl+c ×2 exit", "ctrl+o expand", "⌥/alt+enter newline"]
        while hints and len(left) + 4 + len(" · ".join(hints)) > width:
            hints.pop()  # drop the least important hint first
        right = " · ".join(hints)
        if not right:
            return left[:max(0, width)]
        return left + " " * max(2, width - len(left) - len(right)) + right

    def run(self) -> None:
        self._draw_home()
        self.session.bus.subscribe(self._render)
        self.session.on_attention(ui.interrupt_prompt)
        self._loop()

    def _draw_home(self) -> None:
        """Clear the screen and draw the header with the input at the bottom (start and /clear)."""
        cfg = self.session.config
        ui.console.clear()
        ui.show_welcome(
            cfg.model,
            base_url=cfg.base_url,
            reasoning_effort=cfg.reasoning_effort,
            git_summary=self.session.git,
            ui_url=self.ui_url,
            context_window=cfg.context_window,
            agents_enabled=bool(cfg.agents.get("enabled", True)),
            mode=self.session.mode,
        )
        # header (≈5) + notices (1–2) + input box with its blank line and status (5)
        ui.push_to_bottom(5 + (2 if self.ui_url else 1) + 5)

    def _loop(self) -> None:
        while True:
            approvals = self.session.pending_approvals()
            if approvals:
                self._ask_approval(approvals[0])
                continue
            if not self.session.idle:
                self._wait_while_busy()
                continue
            if self.session.plan_pending:
                self._ask_plan()
                continue
            try:
                text = ui.chat_input(self.status_text, mode=lambda: self.session.mode,
                                     on_cycle_mode=self.session.cycle_mode)
            except ui.PromptInterrupted:
                continue
            except ui.ExpandRequested:
                ui.show_expanded(self.last_outputs)
                continue
            except (ui.ExitRequested, EOFError):
                break
            except KeyboardInterrupt:
                continue
            if not text:
                continue
            ui.echo_user(text)
            if text.startswith("/"):
                if self._command(text) == "exit":
                    break
                continue
            ui.console.print()
            self.session.submit(text, source="cli")
            self._await_start()

    def _await_start(self, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.session.busy and not self.session.idle:
            time.sleep(0.02)

    def _wait_while_busy(self) -> None:
        try:
            with ui.console.status(f"[{PRIMARY}]{self.activity}…[/]", spinner="dots", spinner_style=PRIMARY) as status:
                while (not self.session.idle) and not self.session.pending_approvals():
                    status.update(f"[{PRIMARY}]{self.activity}…[/]  [{MUTED}]ctrl-c to interrupt[/]")
                    time.sleep(0.1)
        except KeyboardInterrupt:
            self.session.interrupt(source="cli")
            # Give the worker a moment to unwind before re-rendering.
            deadline = time.time() + 5
            while self.session.busy and time.time() < deadline:
                time.sleep(0.05)

    def _ask_approval(self, approval: dict[str, Any]) -> None:
        label = APPROVAL_LABELS.get(approval["kind"], approval["kind"])
        agent = approval.get("agent") or "main"
        if agent != "main":
            label += f" ({agent})"
        try:
            choice = ui.approval_prompt(label, approval["detail"], APPROVAL_KINDS.get(approval["kind"], "of these"))
        except ui.PromptInterrupted:
            return  # answered from the web UI (or the task was stopped)
        except KeyboardInterrupt:
            choice = "deny"
        self.session.resolve_approval(
            approval["id"], choice != "deny", by="cli", scope="session" if choice == "session" else "once"
        )
        ui.show_decision(choice, approval["detail"].splitlines()[0] if approval["detail"] else "")

    def _ask_plan(self) -> None:
        """Plan mode finished a plan: run it, or keep planning (like Claude Code's plan approval)."""
        options = ["Yes, and auto-accept edits", "Yes, and ask before each edit (manual)", "No, keep planning"]
        try:
            index = ui._selector("Ready to implement this plan?", options, cancel_result=2, filterable=False,
                                 shortcuts={"y": 0, "m": 1, "n": 2},
                                 hint_text="↑↓ move · enter select · y yes · m manual · n keep planning", erase=True)
        except ui.PromptInterrupted:
            return
        except KeyboardInterrupt:
            index = 2
        decision = ("accept_edits", "manual", "keep")[index if isinstance(index, int) else 2]
        if decision == "keep":
            self.session.resolve_plan("keep")
            ui.notice("Still in plan mode — tell Dcode what to change in the plan.", "info")
        else:
            ui.notice(f"Plan approved — switching to {MODE_SHORT[decision]} and implementing.", "success")
            self.session.resolve_plan(decision)
            self._await_start()

    # --- event rendering --------------------------------------------------------------------
    def _render(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        agent = event.get("agent", "main")
        experimental = self.session.config.experimental
        if kind == "user_message":
            self.last_outputs = []
            if event.get("source") not in ("cli", "history"):
                ui.show_user_echo(event.get("text", ""), event.get("source", "web"))
        elif kind == "assistant_note" and agent == "main":
            ui.show_note(event.get("text", ""))
        elif kind == "assistant_message" and agent == "main":
            ui.show_assistant(event.get("text", ""))
        elif kind == "tool_call":
            self.activity = f"{agent + ' · ' if agent != 'main' else ''}{event.get('name')}"
            self._calls[f"{agent}/{event.get('id', '')}"] = (event.get("name", ""), event.get("args") or {})
            ui.show_tool_call(event.get("name", ""), event.get("args"), agent)
        elif kind == "tool_result":
            name, args = self._calls.pop(f"{agent}/{event.get('id', '')}", (event.get("name", ""), {}))
            output = event.get("output", "")
            self.last_outputs.append((name, args, output))
            if experimental.get("streaming_tools"):
                ui.show_tool_output(name, output)
            elif name == "edit_file" and output.startswith("Edited") and isinstance(args, dict):
                ui.show_tool_result_brief(output, agent)
                ui.show_edit_diff(str(args.get("old_text", "")), str(args.get("new_text", "")), agent)
            elif name in ("run_command", "git_diff", "git_status"):
                ui.show_tool_result_lines(output, agent, lines=3)
            else:
                ui.show_tool_result_brief(output, agent)
            self.activity = "Thinking"
        elif kind == "agent_start":
            ui.show_agent_start(agent, event.get("role", ""), event.get("task", ""))
        elif kind == "agent_end":
            ui.show_agent_end(agent, bool(event.get("ok")))
        elif kind == "approval_resolved" and event.get("by") == "web":
            ui.notice(f"{'Approved' if event.get('approved') else 'Denied'} from the web UI", "info")
        elif kind == "usage" and agent == "main":
            ui.show_done(float(event.get("elapsed", 0.0)), report=event)
        elif kind == "approval_auto":
            ui.console.print(f"{ui.PAD}  [{MUTED}]✓ allowed (session rule)[/]")
        elif kind == "mode" and event.get("by") != "cli":
            ui.notice(f"Mode changed from the web UI: {MODE_SHORT.get(event.get('mode'), event.get('mode'))}")
        elif kind == "undo":
            ui.notice(f"Undid “{event.get('label')}”: restored {', '.join(event.get('files') or []) or 'nothing'}", "success")
        elif kind == "artifact_saved":
            ui.notice(f"Artifact saved: {event.get('title')} — open it in the web UI", "success")
        elif kind == "skill_saved":
            ui.notice(f"Skill saved: {event.get('name')}", "success")
        elif kind == "compaction":
            ui.notice(
                f"Context compacted ({agent}): ~{event.get('before', 0):,} → ~{event.get('after', 0):,} tokens"
                + (" · summarized older turns" if event.get("summarized") else ""), "info")
        elif kind == "memory_saved":
            ui.notice(f"Memory saved: {event.get('name')} ({event.get('scope')})", "success")
        elif kind == "notice":
            ui.notice(event.get("text", ""), event.get("level", "info"))
        elif kind == "error":
            ui.notice(event.get("text", ""), "error")
        elif kind == "config":
            ui.notice(f"Model: {event.get('model')} · reasoning: {event.get('reasoning_effort')}", "info")
        elif kind == "thread" and event.get("action") != "title":  # titles show in the status line
            ui.notice(f"Chat {event.get('action')}: {event.get('name')}", "info")

    # --- slash commands -----------------------------------------------------------------------
    def _command(self, text: str) -> str | None:
        command, _, arg = text.partition(" ")
        session = self.session
        cfg = session.config
        try:
            if command == "/exit":
                return "exit"
            if command == "/help":
                ui.show_help()
            elif command == "/clear":
                session.new_thread()
                self.last_outputs = []
                self._draw_home()
            elif command == "/version":
                ui.console.print(f"{ui.PAD}[bold]Dcode[/] {__version__}  [{MUTED}]· {cfg.model} · {cfg.base_url} · "
                                 f"Python {platform.python_version()} · {platform.system()} {platform.machine()}[/]")
            elif command == "/mode":
                wanted = arg.strip().lower().replace(" ", "_").replace("-", "_")
                aliases = {"accept": "accept_edits", "edits": "accept_edits", "default": "accept_edits"}
                wanted = aliases.get(wanted, wanted)
                if not wanted:
                    labels = [MODE_LABELS[m] for m in MODES]
                    picked = ui.select_one("Mode", labels, MODE_LABELS[session.mode])
                    wanted = next((m for m in MODES if MODE_LABELS[m] == picked), session.mode)
                session.set_mode(wanted)
                ui.notice(f"Mode: {MODE_LABELS[session.mode]}", "success")
            elif command == "/compact":
                session.compact(arg.strip())
                self._await_start()
            elif command == "/undo":
                result = session.undo()
                if result is None:
                    ui.notice("Nothing to undo — Dcode hasn't changed any files in this session.")
            elif command == "/diff":
                from .tools import execute_tool

                stat = execute_tool("git_diff", {}) if session.git.get("is_repo") else ""
                if not session.git.get("is_repo"):
                    ui.notice("Not a git repository. /undo still works for Dcode's own changes.", "warning")
                elif stat in ("", "(no output)"):
                    ui.notice("No uncommitted changes.")
                else:
                    ui.show_diff(stat)
            elif command == "/commit":
                self._commit(arg.strip())
            elif command == "/guide":
                if self.ui_url:
                    guide = self.ui_url.split("/?")[0] + "/guide"
                    opened = ui.open_in_browser(guide)
                    ui.console.print(f"{ui.PAD}[{MUTED}]Guide (English / فارسی){' — opened in your browser' if opened else ''}[/]"
                                     f"  {ui.link(guide)}", soft_wrap=True)
                else:
                    ui.notice("The guide is served by the web UI; start dcode without --no-ui, "
                              "or open dcode/web/guide.html from the source folder.", "warning")
            elif command == "/ui":
                if self.ui_url:
                    opened = ui.open_in_browser(self.ui_url)
                    ui.console.print(f"{ui.PAD}[{MUTED}]Web UI{' — opened in your browser' if opened else ''}[/]"
                                     f"  {ui.link(self.ui_url)}", soft_wrap=True)
                else:
                    ui.notice("Web UI is disabled (enable it in settings or drop --no-ui).", "warning")
            elif command in ("/reset", "/new"):
                session.new_thread()
            elif command == "/rename":
                name = arg.strip() or ui.prompt_input("Thread name", default=session.thread_name)
                session.rename(name)
            elif command == "/resume":
                choice = ui.pick_thread(list_threads())
                if not choice:
                    ui.notice("No thread selected")
                elif session.resume(choice["id"]):
                    ui.notice(f"Resumed {session.thread_name} ({len(session.agent.messages)} messages)", "success")
            elif command == "/review":
                session.review(source="cli")
                self._await_start()
            elif command == "/model":
                self._switch_model()
            elif command == "/setup":
                main = import_module("dcode.main")
                new = main.run_setup(model=cfg.model)
                for key in ("permissions", "experimental", "agents", "memory", "ui", "enabled_skills",
                            "reasoning_effort"):
                    setattr(new, key, getattr(cfg, key))
                save_config(new)
                session.apply_config(new)
            elif command == "/permissions":
                result = ui.checklist_prompt("Permissions", [(label, cfg.permissions.get(k, True))
                                                             for k, label in PERMISSION_LABELS.items()])
                for key, label in PERMISSION_LABELS.items():
                    cfg.permissions[key] = bool(result.get(label, True))
                self._save()
            elif command == "/experimental":
                result = ui.checklist_prompt("Experimental", [(label, cfg.experimental.get(k, False))
                                                              for k, label in EXPERIMENTAL_LABELS.items()])
                for key, label in EXPERIMENTAL_LABELS.items():
                    cfg.experimental[key] = bool(result.get(label, False))
                self._save()
            elif command == "/skills":
                paths = list_skill_files()
                if not paths:
                    ui.notice("No skills found in ~/.dcode/skills")
                else:
                    result = ui.checklist_prompt("Skills", [(p.name, p.name in cfg.enabled_skills) for p in paths])
                    cfg.enabled_skills = [name for name, on in result.items() if on]
                    self._save()
            elif command == "/agents":
                result = ui.checklist_prompt("Sub-agents", [("enabled", bool(cfg.agents.get("enabled", True)))])
                cfg.agents["enabled"] = bool(result.get("enabled"))
                cfg.agents["max_parallel"] = int(ui.select_one(
                    "Max parallel read-only agents", ["1", "2", "3", "4"], str(cfg.agents.get("max_parallel", 2))))
                cfg.agents["max_steps"] = int(ui.select_one(
                    "Max steps per sub-agent", ["8", "16", "24", "32", "48"], str(cfg.agents.get("max_steps", 16))))
                self._save()
            elif command == "/context":
                used = session.agent.budget.usage(session.agent.messages, session.agent.tools())
                ui.notice(f"Context: ~{used:,} / {cfg.context_window:,} tokens used "
                          f"(reply budget {session.agent.budget.max_output_tokens:,})")
                choice = ui.select_one("Context window (tokens)", CONTEXT_CHOICES + ["custom"], str(cfg.context_window))
                if choice == "custom":
                    choice = ui.prompt_input("Context window", default=str(cfg.context_window))
                if choice.isdigit():
                    cfg.context_window = max(2048, int(choice))
                    self._save()
            elif command == "/memory":
                memories = session.memory.list()
                if not memories:
                    ui.notice("No memories yet. Use /remember <text> or let the agent save them.")
                for memory in memories[:40]:
                    ui.console.print(f"[{PRIMARY}]●[/] [bold]{memory.name}[/] [{MUTED}]({memory.scope}) {ui._escape(memory.description)}[/]")
            elif command == "/remember":
                body = arg.strip() or ui.prompt_input("Memory text")
                if body:
                    name = "-".join(body.lower().split()[:5])
                    memory = session.memory.save(name, body)
                    ui.notice(f"Saved memory {memory.name}", "success")
            else:
                ui.notice(f"Unknown command {command}. /help lists commands.", "warning")
        except RuntimeError as exc:
            ui.notice(str(exc), "warning")
        except (KeyboardInterrupt, ui.PromptInterrupted):
            pass
        return None

    def _commit(self, message: str) -> None:
        from .tools import execute_tool

        session = self.session
        if not session.git.get("is_repo"):
            ui.notice("Not a git repository.", "warning")
            return
        if not message:
            with ui.console.status(f"[{PRIMARY}]Writing a commit message…[/]", spinner="dots", spinner_style=PRIMARY):
                message = session.suggest_commit_message()
            if not message:
                ui.notice("Nothing to commit.")
                return
        ui.console.print(f"{ui.PAD}[{MUTED}]Commit message:[/]")
        for line in message.splitlines():
            ui.console.print(f"{ui.PAD}  [bold]{ui._escape(line)}[/]")
        choice = ui.select_one("Commit all changes with this message?", ["Commit", "Edit message", "Cancel"], "Commit")
        if choice == "Edit message":
            message = ui.prompt_input("Commit message", default=message.splitlines()[0])
            choice = "Commit" if message else "Cancel"
        if choice != "Commit":
            ui.notice("Commit cancelled.")
            return
        output = execute_tool("git_commit", {"message": message})
        session.git = git_summary()
        ui.notice(output.splitlines()[0] if output else "Committed.", "success" if "(return code" not in output else "error")

    def _save(self) -> None:
        save_config(self.session.config)
        self.session.apply_config(self.session.config)

    def _switch_model(self) -> None:
        cfg = self.session.config
        try:
            with ui.console.status(f"[{PRIMARY}]Fetching models…[/]", spinner="dots", spinner_style=PRIMARY):
                models = self.session.llm.list_models()
        except Exception as exc:
            ui.notice(f"Could not load models: {exc}", "warning")
            models = []
        model = ui.select_model(models, cfg.model) if models else ui.prompt_input("Model", default=cfg.model)
        effort = ui.select_one("Reasoning effort", REASONING_EFFORTS, cfg.reasoning_effort)
        self.session.set_model(model or cfg.model, effort if effort in REASONING_EFFORTS else None)
        save_config(cfg)
