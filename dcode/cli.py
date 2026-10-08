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

from rich.text import Text

from . import __version__, ui
from .agents import MODE_LABELS, MODE_SHORT, MODES
from .config import REASONING_EFFORTS, list_skill_files, list_threads, save_config
from .engine import git_summary
from .session import ALWAYS_ASK_KINDS, Session
from .theme import ACCENT, DANGER, MUTED, PRIMARY, SUCCESS, WARNING

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
                   "git_commit": "Dcode wants to make a git commit",
                   "rule": "A rule says to confirm this", "outside_workspace": "Dcode wants a file outside the project",
                   "edit_file": "Dcode wants to change a file",
                   "db_write": "Dcode wants to change a database",
                   "mcp_tool": "Dcode wants to use an external tool"}
APPROVAL_KINDS = {"run_command": "commands", "delete_file": "deletions", "git_commit": "commits",
                  "rule": "rule-matched actions", "outside_workspace": "files outside the project",
                  "edit_file": "file changes", "mcp_tool": "external tools"}
CONTEXT_CHOICES = ["4096", "8192", "16384", "32768", "65536", "131072"]
NOTIFY_MIN_SECONDS = 10.0  # a task shorter than this ends quietly: you were still watching


class TerminalApp:
    def __init__(self, session: Session, ui_url: str | None = None) -> None:
        self.session = session
        self.ui_url = ui_url
        self.activity = "Thinking"
        self.last_outputs: list[tuple[str, Any, str]] = []  # (tool, args, output) of the last task, for Ctrl+O
        self._calls: dict[str, tuple[str, Any]] = {}
        # Live activity: the main agent's current step and every running helper.
        self.main_step: tuple[str, float] | None = None
        self.turn_started: float | None = None
        self.running: dict[str, dict[str, Any]] = {}

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
        filled = round(8 * pct / 100)
        meter = "▰" * filled + "▱" * (8 - filled)
        left = f"{name[:40]}  ·  {cfg.model[:32]}  ·  ctx {meter} {pct}%"
        running_jobs = len(session.jobs.running())
        if running_jobs:
            left += f"  ·  ⚙ {running_jobs} job{'s' if running_jobs > 1 else ''}"
        width = shutil.get_terminal_size((100, 24)).columns - 4
        hints = ["shift+tab mode", "ctrl+c ×2 exit", "ctrl+o expand", "⌥/alt+enter newline"]
        while hints and len(left) + 4 + len(" · ".join(hints)) > width:
            hints.pop()  # drop the least important hint first
        right = " · ".join(hints)
        if not right:
            return left[:max(0, width)]
        return left + " " * max(2, width - len(left) - len(right)) + right

    def run(self, recap: bool = False) -> None:
        self._draw_home()
        if recap:  # started with --continue / --resume
            ui.notice(f"Continuing “{self.session.thread_name}”", "success")
            self.show_recap()
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
            ui.EXTRA_COMMANDS = {f"/{c.name}": f"{c.description}  ({c.scope})"
                                 for c in self.session.custom_commands().values()}
            ui.HELPERS = ({name: spec.description for name, spec in self.session.agent.specs.items()}
                          if self.session.config.agents.get("enabled", True) else {})
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
            if text.startswith("!"):  # your own shell command; Dcode sees it and the output
                if text[1:].strip():
                    self.session.run_shell(text[1:], source="cli")
                    self._await_start()
                else:
                    ui.notice("Type a command after the !, e.g. !git status")
                continue
            ui.console.print()
            self.session.submit(text, source="cli")
            self._await_start()

    def _await_start(self, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.session.busy and not self.session.idle:
            time.sleep(0.02)

    def _activity_view(self):
        """Live panel while Dcode works: the current step, each running helper, background jobs."""
        from rich.console import Group
        from rich.spinner import Spinner
        from rich.table import Table

        now = time.time()

        def ago(since: float) -> str:
            return ui._fmt_elapsed(max(0.0, now - since))

        head = Table.grid(padding=(0, 1))
        head.add_column(no_wrap=True)
        head.add_column(overflow="ellipsis", no_wrap=True)
        label, since = self.main_step or ("Thinking", self.turn_started or now)
        head.add_row(Spinner(ui.SPINNER, style=PRIMARY),
                     Text.from_markup(f"{ui._escape(label)}… [{MUTED}]{ago(since)} · ctrl-c to interrupt[/]"))
        rows = [head]
        for agent, info in list(self.running.items()):
            step = info.get("step") or "thinking"
            rows.append(Text.from_markup(
                f"{ui.PAD}  [{ACCENT}]◆ {agent}[/] [{MUTED}]{ui._escape(step[:90])} · {ago(info['since'])}[/]",
                overflow="ellipsis"))
        for job in self.session.jobs.running():
            rows.append(Text.from_markup(
                f"{ui.PAD}  [{WARNING}]⚙ job #{job.id}[/] [{MUTED}]{ui._escape(job.command[:80])} · running {ago(job.started)}[/]",
                overflow="ellipsis"))
        return Group(*rows)

    def _wait_while_busy(self) -> None:
        from rich.live import Live

        try:
            with Live(self._activity_view(), console=ui.console, refresh_per_second=8, transient=True) as live:
                while (not self.session.idle) and not self.session.pending_approvals():
                    live.update(self._activity_view())
                    time.sleep(0.12)
        except KeyboardInterrupt:
            self.session.interrupt(source="cli")
            # Give the worker a moment to unwind before re-rendering.
            deadline = time.time() + 5
            while self.session.busy and time.time() < deadline:
                time.sleep(0.05)

    def _ask_approval(self, approval: dict[str, Any]) -> None:
        if approval["kind"] == "question":
            try:
                answer = ui.question_prompt(approval["detail"], approval.get("options") or [])
            except ui.PromptInterrupted:
                return  # answered from the web UI
            except KeyboardInterrupt:
                answer = ""
            self.session.resolve_approval(approval["id"], bool(answer), by="cli", answer=answer)
            return
        label = APPROVAL_LABELS.get(approval["kind"], approval["kind"])
        agent = approval.get("agent") or "main"
        if agent != "main":
            label += f" ({agent})"
        try:
            choice = ui.approval_prompt(label, approval["detail"], APPROVAL_KINDS.get(approval["kind"], "of these"),
                                        session_option=approval["kind"] not in ALWAYS_ASK_KINDS)
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
        prefs = self.session.config.ui
        mine = event.get("by") == "user"  # a command the user ran with "!": always shown, and in full
        details = bool(prefs.get("show_details", True)) or mine
        if kind == "user_message":
            self.last_outputs = []
            self.turn_started = time.time()
            self.main_step = None
            if event.get("source") not in ("cli", "history"):
                ui.show_user_echo(event.get("text", ""), event.get("source", "web"))
        elif kind == "assistant_note" and agent == "main":
            ui.show_note(event.get("text", ""))
        elif kind == "thinking":
            if prefs.get("show_thinking"):
                ui.show_thinking(event.get("text", ""), agent)
        elif kind == "approval_request":
            ui.ring(str(prefs.get("notify") or "off"), "Dcode needs your answer")
        elif kind == "assistant_message" and agent == "main":
            ui.show_assistant(event.get("text", ""))
        elif kind == "tool_call":
            self.activity = f"{agent + ' · ' if agent != 'main' else ''}{event.get('name')}"
            self._calls[f"{agent}/{event.get('id', '')}"] = (event.get("name", ""), event.get("args") or {})
            step = f"{event.get('name', '')} {ui._short_args(event.get('args'), 70)}".strip()
            if event.get("name") == "delegate":
                step = "Waiting for helpers"
            if agent == "main":
                self.main_step = (step, time.time())
            elif agent in self.running:
                self.running[agent]["step"] = step
            # artifact_save, todo_write and ask_user print their own line/list
            if details and not mine and event.get("name") not in ("artifact_save", "todo_write", "ask_user"):
                ui.show_tool_call(event.get("name", ""), event.get("args"), agent)
        elif kind == "tool_result":
            name, args = self._calls.pop(f"{agent}/{event.get('id', '')}", (event.get("name", ""), {}))
            if agent == "main":
                self.main_step = None
            elif agent in self.running:
                self.running[agent]["step"] = ""
            output = event.get("output", "")
            self.last_outputs.append((name, args, output))
            if mine:
                ui.show_tool_result_lines(output, agent, lines=40)
            elif not details:
                pass  # /details is off: the live panel and Ctrl+O still show what happened
            elif experimental.get("streaming_tools"):
                ui.show_tool_output(name, output)
            elif name == "edit_file" and output.startswith("Edited") and isinstance(args, dict):
                ui.show_tool_result_brief(output, agent)
                ui.show_edit_diff(str(args.get("old_text", "")), str(args.get("new_text", "")), agent)
            elif name == "todo_write" and output.startswith("Checklist saved"):
                pass  # the "todos" event draws the checklist
            elif name == "ask_user":
                pass  # the question menu already recorded the answer
            elif name == "artifact_save":
                if output.startswith(("Not saved again", "You called artifact_save with identical")):
                    ui.console.print(f"{ui.PAD}  [{MUTED}]↻ already saved — skipped[/]")
                elif not output.startswith("Saved artifact"):  # real failures still show
                    ui.show_tool_call(name, args, agent)
                    ui.show_tool_result_brief(output, agent)
            elif name in ("run_command", "git_diff", "git_status"):
                ui.show_tool_result_lines(output, agent, lines=3)
            else:
                ui.show_tool_result_brief(output, agent)
            self.activity = "Thinking"
        elif kind == "agent_start":
            self.running[agent] = {"role": event.get("role", ""), "task": event.get("task", ""), "since": time.time(), "step": ""}
            model = str(event.get("model") or "")
            ui.show_agent_start(agent, event.get("role", ""), event.get("task", ""),
                                model if model and model != self.session.config.model else "")
        elif kind == "agent_end":
            info = self.running.pop(agent, None)
            ui.show_agent_end(agent, bool(event.get("ok")), time.time() - info["since"] if info else None)
        elif kind == "job_started":
            ui.console.print(f"{ui.PAD}  [{WARNING}]⚙ background job #{event.get('id')} started[/] "
                             f"[{MUTED}]· {ui._escape(str(event.get('command', ''))[:90])} · /jobs to manage[/]")
        elif kind == "job_exited":
            state = event.get("state", "")
            level = "success" if state.startswith("exited (0)") or state == "stopped" else "warning"
            ui.notice(f"Background job #{event.get('id')} {state}: {str(event.get('command', ''))[:80]}", level)
        elif kind == "approval_resolved" and event.get("by") == "web":
            if "answer" in event:
                ui.notice(f"Answered from the web UI: {event.get('answer') or 'skipped'}", "info")
            else:
                ui.notice(f"{'Approved' if event.get('approved') else 'Denied'} from the web UI", "info")
        elif kind == "usage" and agent == "main":
            ui.show_done(float(event.get("elapsed", 0.0)), report=event)
            if float(event.get("elapsed", 0.0)) >= NOTIFY_MIN_SECONDS:
                ui.ring(str(prefs.get("notify") or "off"), f"Dcode finished: {self.session.thread_name}")
        elif kind == "approval_auto":
            ui.console.print(f"{ui.PAD}  [{MUTED}]✓ allowed (session rule)[/]")
        elif kind == "mode" and event.get("by") != "cli":
            ui.notice(f"Mode changed from the web UI: {MODE_SHORT.get(event.get('mode'), event.get('mode'))}")
        elif kind == "undo":
            ui.notice(f"Undid “{event.get('label')}”: restored {', '.join(event.get('files') or []) or 'nothing'}", "success")
        elif kind == "redo":
            ui.notice(f"Brought back “{event.get('label')}”: {', '.join(event.get('files') or []) or 'nothing'}", "success")
        elif kind == "artifact_saved":
            if event.get("update"):  # same artifact again in this task: one quiet line, no link
                ui.console.print(f"{ui.PAD}  [{MUTED}]↻ updated · v{event.get('versions', 1)}[/]")
            else:
                short, full = self._artifact_links(str(event.get("id", "")))
                where = f"  {ui.link(full, short)}" if full else ""
                ui.console.print(f"{ui.PAD}[{SUCCESS}]◆ Artifact[/] [bold]{ui._escape(str(event.get('title', '')))}[/]"
                                 f"[{MUTED}] · v{event.get('versions', 1)}[/]{where}", soft_wrap=True)
        elif kind == "todos" and agent == "main":
            ui.show_todos(event.get("items") or [])
        elif kind == "mentions":
            attached = ", ".join(event.get("attached") or [])
            if attached:
                ui.console.print(f"{ui.PAD}  [{MUTED}]+ attached {ui._escape(attached)}[/]")
        elif kind == "check":
            first = str(event.get("problems", "")).splitlines()[0][:110] if event.get("problems") else ""
            count = int(event.get("count") or 1)
            ui.console.print(f"{ui.PAD}  [{WARNING}]⚠ check found {count} problem{'s' if count > 1 else ''}[/] "
                             f"[{MUTED}]· {ui._escape(first)}[/]", soft_wrap=True)
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
        elif kind == "thread" and event.get("action") == "resume":
            if event.get("by") == "web":  # the terminal's own /sessions prints its line and a recap
                ui.notice(f"Continuing “{event.get('name')}” (picked in the web UI)", "info")
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
            elif command == "/jobs":
                self._jobs()
            elif command == "/artifacts":
                self._artifacts()
            elif command == "/db":
                self._db(arg.strip())
            elif command == "/cron":
                self._cron(arg.strip())
            elif command == "/mcp":
                self._mcp(arg.strip())
            elif command == "/clear":
                session.new_thread()
                self.last_outputs = []
                self._draw_home()
            elif command == "/editor":
                text = ui.edit_in_editor(arg.strip())
                if not text:
                    ui.notice("Nothing written, nothing sent.")
                else:
                    ui.echo_user(text)
                    ui.console.print()
                    session.submit(text, source="cli")
                    self._await_start()
            elif command in ("/thinking", "/details"):
                key = "show_thinking" if command == "/thinking" else "show_details"
                wanted = arg.strip().lower()
                cfg.ui[key] = wanted == "on" if wanted in ("on", "off") else not cfg.ui.get(key, key == "show_details")
                save_config(cfg)
                what = "The model's reasoning is" if key == "show_thinking" else "Tool calls and results are"
                ui.notice(f"{what} {'shown' if cfg.ui[key] else 'hidden'}."
                          + ("" if cfg.ui[key] or key == "show_thinking" else " Ctrl+O still expands the last task."), "success")
            elif command == "/notify":
                wanted = arg.strip().lower() or ui.select_one(
                    "When a long task ends, or Dcode needs an answer", ui.NOTIFY_CHOICES, str(cfg.ui.get("notify") or "off"))
                if wanted not in ui.NOTIFY_CHOICES:
                    ui.notice("Usage: /notify off | bell | desktop", "warning")
                else:
                    cfg.ui["notify"] = wanted
                    save_config(cfg)
                    ui.notice({"off": "Notifications are off.", "bell": "Dcode rings the terminal bell.",
                               "desktop": "Dcode sends a desktop notification (where the terminal supports it) and rings the bell."}[wanted],
                              "success")
                    ui.ring(wanted, "Dcode notifications are on")
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
            elif command == "/init":
                name = session.init_project()
                ui.notice(f"Dcode will look around the project and write {name} (read at the start of every session).")
                self._await_start()
            elif command == "/compact":
                session.compact(arg.strip())
                self._await_start()
            elif command == "/undo":
                result = session.undo()
                if result is None:
                    ui.notice("Nothing to undo — Dcode hasn't changed any files in this session.")
            elif command == "/redo":
                if session.redo() is None:
                    ui.notice("Nothing to redo — use it right after /undo, before Dcode changes files again.")
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
            elif command == "/stats":
                from . import stats

                days = int(arg) if arg.strip().isdigit() else 30
                for line in stats.render_text(stats.summarize(days)).splitlines():
                    bold = line and not line.startswith("    ") and ("usage" in line or line.strip() in (
                        "By model", "By project", "Most used tools", "Last days"))
                    ui.console.print(f"{ui.PAD}[bold]{ui._escape(line)}[/]" if bold else f"{ui.PAD}{ui._escape(line)}",
                                     highlight=False)
            elif command == "/export":
                self._export(arg.strip())
            elif command in ("/sessions", "/resume"):
                self._sessions(arg.strip())
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
            elif command == "/hooks":
                hooks = cfg.hooks
                if not hooks:
                    ui.notice("No hooks. Add them in the web UI (Settings → Permissions) or under \"hooks\" "
                              "in ~/.dcode/config.json.")
                for hook in hooks:
                    match = f" [{MUTED}]({hook['match']})[/]" if hook.get("match") else ""
                    ui.console.print(f"{ui.PAD}  [{ACCENT}]{hook['on']}[/]{match}  {ui._escape(hook['command'])}")
            elif command == "/rules":
                self._rules(arg.strip())
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
                self._list_helpers()
                if arg.strip() not in ("settings", "set"):
                    ui.console.print(f"{ui.PAD}[{MUTED}]Use one with @name <task>. Settings: /agents settings[/]")
                    return None
                result = ui.checklist_prompt("Sub-agents", [("enabled", bool(cfg.agents.get("enabled", True)))])
                cfg.agents["enabled"] = bool(result.get("enabled"))
                cfg.agents["max_parallel"] = int(ui.select_one(
                    "Max parallel read-only agents", ["1", "2", "3", "4"], str(cfg.agents.get("max_parallel", 2))))
                cfg.agents["max_steps"] = int(ui.select_one(
                    "Max steps per sub-agent", ["8", "16", "24", "32", "48"], str(cfg.agents.get("max_steps", 16))))
                cfg.agents["helper_model"] = ui.prompt_input(
                    "Helper model (empty = same as the main model)", default=str(cfg.agents.get("helper_model") or ""))
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
            elif command[1:].lower() in session.custom_commands():
                session.run_custom_command(command[1:], arg.strip())
                self._await_start()
            else:
                ui.notice(f"Unknown command {command}. /help lists commands.", "warning")
        except RuntimeError as exc:
            ui.notice(str(exc), "warning")
        except (KeyboardInterrupt, ui.PromptInterrupted):
            pass
        return None

    def _rules(self, arg: str) -> None:
        """/rules · /rules allow|ask|deny <command pattern> · /rules file allow|ask|deny <pattern> · /rules remove <pattern>"""
        import shlex

        cfg = self.session.config
        rules = cfg.rules
        try:
            words = shlex.split(arg)
        except ValueError as exc:
            ui.notice(f"Couldn't read that: {exc}", "warning")
            return
        table = "commands"
        if words and words[0] in ("file", "files"):
            table, words = "files", words[1:]
        if len(words) >= 2 and words[0] in ("allow", "ask", "deny"):
            pattern = " ".join(words[1:])
            rules.setdefault(table, {})[pattern] = words[0]
            self._save()
            ui.notice(f"Rule saved: {words[0]} {'file' if table == 'files' else 'command'} “{pattern}”", "success")
            return
        if len(words) >= 2 and words[0] == "remove":
            pattern = " ".join(words[1:])
            removed = [t for t in ("commands", "files") if rules.get(t, {}).pop(pattern, None)]
            if removed:
                self._save()
            ui.notice(f"Removed “{pattern}”" if removed else f"No rule “{pattern}”", "success" if removed else "warning")
            return
        if words:
            ui.notice("Usage: /rules · /rules allow|ask|deny <pattern> · /rules file deny <pattern> · /rules remove <pattern>",
                      "warning")
            return
        colors = {"allow": SUCCESS, "ask": WARNING, "deny": DANGER}
        ruleset = self.session.agent.rules
        for title, key, extra in (("Commands", "commands", ruleset.project_commands),
                                  ("Files", "files", ruleset.project_files)):
            ui.console.print(f"{ui.PAD}[bold]{title}[/]")
            entries = list((rules.get(key) or {}).items()) + [(p, d + " · project") for p, d in extra.items()]
            if not entries:
                ui.console.print(f"{ui.PAD}  [{MUTED}]none — the mode decides[/]")
            for pattern, decision in entries:
                color = colors.get(decision.split()[0], MUTED)
                ui.console.print(f"{ui.PAD}  [{color}]{decision:<7}[/] {ui._escape(pattern)}")
        ui.console.print(f"{ui.PAD}[bold]Outside the project[/] [{MUTED}]{rules.get('outside_workspace', 'ask')}[/]")
        ui.console.print(f"{ui.PAD}[{MUTED}]Add: /rules deny \"rm -rf *\" · /rules file ask \"*.pem\" · the last match wins[/]")

    # --- external tools (MCP) ---------------------------------------------------------------------
    def _mcp(self, arg: str) -> None:
        """/mcp · /mcp add <name> <command…> · /mcp rm|on|off|allow|ask|tools <name> · /mcp restart [name]"""
        import shlex
        import time as clock

        from . import mcp

        session, cfg = self.session, self.session.config
        servers = cfg.mcp.setdefault("servers", {})
        word, _, rest = arg.partition(" ")
        rest = rest.strip()
        if not word:
            return self._mcp_list()
        if word == "add":
            name, _, command = rest.partition(" ")
            try:
                parts = shlex.split(command)
            except ValueError as exc:
                ui.notice(f"Couldn't read that command: {exc}", "warning")
                return
            name = mcp.clean_name(name)
            if not name or not parts:
                ui.notice("Usage: /mcp add <name> <command and its arguments>   e.g. /mcp add files npx -y @modelcontextprotocol/server-filesystem .",
                          "warning")
                return
            servers[name] = {"command": parts}
            cfg.mcp = {"servers": mcp.clean_servers(servers)}
            self._save()
            with ui.console.status(f"Starting {name}…", spinner=ui.SPINNER, spinner_style=PRIMARY):
                deadline = clock.time() + mcp.START_TIMEOUT
                while clock.time() < deadline and session.mcp and name in session.mcp.servers \
                        and session.mcp.servers[name].state == "starting":
                    clock.sleep(0.1)
            return self._mcp_list(only=name)
        if word == "restart":
            if session.mcp:
                session.mcp.restart(mcp.clean_name(rest))
                session.mcp.start(wait=5.0)
            return self._mcp_list()
        name = mcp.clean_name(rest)
        if word not in ("rm", "remove", "on", "off", "allow", "ask", "tools") or name not in servers:
            ui.notice(f"Usage: /mcp · /mcp add <name> <command…> · /mcp rm|on|off|allow|ask|tools <name> · /mcp restart   "
                      f"(servers: {', '.join(servers) or 'none yet'})", "warning")
            return
        if word == "tools":
            if not session.mcp or name not in session.mcp.servers:
                ui.notice(f"{name} is off. Turn it on with /mcp on {name}", "warning")
                return
            try:
                text = session.mcp.describe(name)
            except mcp.MCPError as exc:
                ui.notice(str(exc), "error")
                return
            for line in text.splitlines():
                ui.console.print(f"{ui.PAD}  {ui._escape(line)}", highlight=False, soft_wrap=True)
            return
        if word in ("rm", "remove"):
            del servers[name]
        elif word in ("on", "off"):
            servers[name]["enabled"] = word == "on"
        else:
            servers[name]["approve"] = word
        cfg.mcp = {"servers": mcp.clean_servers(servers)}
        self._save()
        ui.notice({"rm": f"Removed the MCP server {name}.", "remove": f"Removed the MCP server {name}.",
                   "on": f"{name} is on.", "off": f"{name} is off.",
                   "allow": f"{name}: its tools now run without asking.",
                   "ask": f"{name}: Dcode asks before each tool call (except in auto mode)."}[word], "success")

    def _mcp_list(self, only: str = "") -> None:
        servers = self.session.config.mcp.get("servers") or {}
        if not servers:
            ui.notice("No MCP servers yet. Add a local one: /mcp add <name> <command…>  "
                      "(it gives Dcode extra tools; see the guide).")
            return
        live = {row["name"]: row for row in (self.session.mcp.status() if self.session.mcp else [])}
        colors = {"ready": SUCCESS, "starting": PRIMARY, "failed": DANGER, "off": MUTED, "stopped": MUTED}
        if not only:
            ui.console.print(f"{ui.PAD}[bold]MCP servers[/]")
        for name, spec in servers.items():
            if only and name != only:
                continue
            row = live.get(name) or {"state": "off", "tools": [], "error": "", "command": " ".join(spec["command"])}
            asks = "runs without asking" if spec.get("approve") == "allow" else "asks first"
            count = f" · {len(row['tools'])} tools" if row["state"] == "ready" else ""
            ui.console.print(f"{ui.PAD}  [{ACCENT}]{name}[/]  [{colors.get(row['state'], WARNING)}]{row['state']}[/]"
                             f"[{MUTED}]{count} · {asks} · {ui._escape(ui._short_args(row['command'], 70))}[/]")
            if row["error"]:
                ui.console.print(f"{ui.PAD}      [{DANGER}]{ui._escape(row['error'][:300])}[/]", soft_wrap=True)
            elif row["tools"]:
                names = ", ".join(t["name"] for t in row["tools"][:12]) + (" …" if len(row["tools"]) > 12 else "")
                ui.console.print(f"{ui.PAD}      [{MUTED}]{ui._escape(names)}[/]", soft_wrap=True)
        if not only:
            ui.console.print(f"{ui.PAD}[{MUTED}]/mcp tools <name> · /mcp on|off|allow|ask|rm <name> · /mcp restart[/]")

    # --- scheduled jobs ---------------------------------------------------------------------------
    def _cron(self, arg: str) -> None:
        """/cron · /cron add ["<schedule>" <command>] · /cron run|on|off|rm|logs <id> · /cron open"""
        from . import schedules

        word, _, rest = arg.partition(" ")
        rest = rest.strip()
        try:
            if not word:
                return self._cron_list(schedules)
            if word == "add":
                return self._cron_add(schedules, rest)
            if word == "open":
                return self._open_web("#cron", "Scheduled jobs")
            job = schedules.load_jobs().get(schedules.clean_id(rest))
            if word not in ("run", "on", "off", "rm", "remove", "logs", "log") or job is None:
                known = ", ".join(schedules.load_jobs()) or "none yet"
                ui.notice(f"Usage: /cron · /cron add · /cron run|on|off|rm|logs <id>   (jobs: {known})", "warning")
                return
            if word in ("rm", "remove"):
                schedules.delete_job(job["id"])
                ui.notice(f"Removed the job {job['id']} and its logs.", "success")
            elif word in ("on", "off"):
                shown = schedules.describe_job(schedules.update_job(job["id"], enabled=word == "on"))
                ui.notice(f"{job['id']} is {word}" + (f" — next run {shown['next_run'].replace('T', ' ')}" if shown["next_run"] else ""),
                          "success")
            elif word == "run":
                schedules.update_job(job["id"], run_requested=True)  # the background service picks it up
                ui.notice(f"Starting {job['id']} now. See the output with /cron logs {job['id']}", "success")
                self._cron_service_hint()
            else:
                self._cron_logs(schedules, job)
        except schedules.ScheduleError as exc:
            ui.notice(str(exc), "error")

    def _cron_service_hint(self) -> None:
        if not self.ui_url:
            ui.notice("Scheduled jobs are run by Dcode's background service, which is off here "
                      "(--no-ui or DCODE_NO_UI). Start dcode without it.", "warning")

    def _cron_list(self, schedules: Any) -> None:
        jobs = [schedules.describe_job(j) for j in schedules.load_jobs().values()]
        if not jobs:
            ui.notice("No scheduled jobs yet. Add one: /cron add \"*/30 * * * *\" df -h   (or /cron add for questions, "
                      "or the Scheduled screen in the web UI).")
            return
        colors = {"ok": SUCCESS, "running": PRIMARY, "failed": DANGER, "timeout": DANGER}
        ui.console.print(f"{ui.PAD}[bold]Scheduled jobs[/]")
        for job in jobs:
            last = job["last_run"]
            status = f"[{colors.get(last['status'], WARNING)}]{last['status']}[/] {last['started'][5:16].replace('T', ' ')}" if last else "never ran"
            upcoming = f"next {job['next_run'][5:].replace('T', ' ')}" if job["next_run"] else "off"
            what = ("Dcode: " if job["kind"] == "prompt" else "") + job["command"]
            ui.console.print(f"{ui.PAD}  [{ACCENT}]{job['id']}[/]  {ui._escape(job['schedule_text'])} [{MUTED}]· {upcoming} · last[/] {status}")
            ui.console.print(f"{ui.PAD}      [{MUTED}]{ui._escape(ui._short_args(what, 96))}[/]")
        ui.console.print(f"{ui.PAD}[{MUTED}]/cron run|on|off|rm|logs <id> · they run in the background, even with no terminal open[/]")
        self._cron_service_hint()

    def _cron_add(self, schedules: Any, rest: str) -> None:
        import shlex

        data: dict[str, Any] = {}
        if rest:  # /cron add "<schedule>" <command…>   or   /cron add @daily <command…>
            try:
                first = shlex.split(rest)[0]
            except (ValueError, IndexError):
                first = ""
            body = rest[rest.index(first) + len(first):] if first else ""
            body = (body[1:] if body[:1] in ("'", '"') else body).strip()  # the schedule's closing quote
            data = {"schedule": first, "command": body, "kind": "command"}
        else:
            kind = ui.select_one("What should run?", ["A shell command", "A Dcode prompt (unattended task)"], "A shell command")
            data["kind"] = "prompt" if kind.startswith("A Dcode") else "command"
            data["schedule"] = ui.prompt_input("Schedule (min hour day month weekday, e.g. */15 * * * *  or  30 2 * * *  or @daily)")
            data["command"] = ui.prompt_input("What should Dcode do" if data["kind"] == "prompt" else "Command")
            data["name"] = ui.prompt_input("Name", default=data["command"][:40])
            if data["kind"] == "prompt":
                modes = {"read only (plan)": "plan", "may edit files (accept edits)": "accept_edits",
                         "may edit files and run commands (auto)": "auto"}
                picked = ui.select_one("Nobody can approve things at run time. The job…", list(modes), list(modes)[1])
                data["mode"] = modes.get(picked, "accept_edits")
        if data["kind"] == "prompt" or not rest:
            data["cwd"] = self.session.cwd
        job = schedules.describe_job(schedules.save_job(data))
        ui.notice(f"Scheduled {job['id']}: {job['schedule_text']} — next run {str(job['next_run']).replace('T', ' ')}", "success")
        self._cron_service_hint()

    def _cron_logs(self, schedules: Any, job: dict[str, Any]) -> None:
        runs = list(reversed(schedules.load_runs(job["id"])))
        if not runs:
            ui.notice(f"{job['id']} hasn't run yet.")
            return
        labels = [f"{r['started'].replace('T', ' ')}  {r['status']:<11} "
                  f"{(str(r['duration']) + 's') if r.get('duration') is not None else '':>8}  {r['trigger']}" for r in runs]
        picked = ui.select_one(f"Runs of {job['id']} — enter shows the log", labels, labels[0])
        if picked not in labels:
            return
        run = runs[labels.index(picked)]
        text = schedules.read_log(job["id"], run["id"], max_bytes=20_000) if run.get("log") else "(skipped: the previous run was still going)"
        ui.show_expanded([(f"{job['id']} · {run['status']}", {"command": job["command"]}, text or "(no output)")])

    # --- databases ------------------------------------------------------------------------------
    def _db(self, arg: str) -> None:
        """/db · /db add [name] [url] · /db rm|test <name> · /db <name> [table | SQL | summary | export <table|schema|all>]"""
        from . import db

        word, _, rest = arg.partition(" ")
        rest = rest.strip()
        try:
            if not word:
                self._db_list(db)
            elif word == "add":
                self._db_add(db, rest)
            elif word in ("rm", "remove", "delete"):
                done = db.delete_connection(rest)
                ui.notice(f"Removed the connection {rest}." if done else f"No connection named {rest}.", "success" if done else "warning")
            elif word == "open":
                self._open_web("#db", "Databases")
            elif word == "test":
                conn = db.get_connection(rest)
                with ui.console.status("Connecting…", spinner=ui.SPINNER, spinner_style=PRIMARY):
                    result = db.test_connection(conn)
                ui.notice(f"Connected to {db.describe(conn)} — {result['version']} · {result['ms']} ms"
                          + (" · encrypted" if result["tls"] else ""), "success")
            else:
                self._db_use(db, db.get_connection(word), rest)
        except db.DBError as exc:
            ui.notice(str(exc), "error")

    def _open_web(self, fragment: str, label: str) -> None:
        if not self.ui_url:
            ui.notice("The web UI is off (start dcode without --no-ui).", "warning")
            return
        url = self.ui_url.split("#")[0] + fragment
        opened = ui.open_in_browser(url)
        ui.console.print(f"{ui.PAD}[{MUTED}]{label}{' — opened in your browser' if opened else ''}[/]  {ui.link(url)}",
                         soft_wrap=True)

    def _db_list(self, db: Any) -> None:
        connections = db.load_connections()
        if not connections:
            ui.notice("No database connections yet. Add one: /db add mydb postgres://user:password@host:5432/dbname "
                      "(or /db add for questions, or the Databases screen in the web UI).")
            return
        ui.console.print(f"{ui.PAD}[bold]Databases[/]")
        for conn in connections.values():
            flag = f" [{MUTED}]· read-only[/]" if conn.get("read_only") else ""
            ui.console.print(f"{ui.PAD}  [{ACCENT}]{conn['name']}[/]  [{MUTED}]{ui._escape(db.describe(conn))}[/]{flag}")
        ui.console.print(f"{ui.PAD}[{MUTED}]/db <name> lists tables · /db <name> <table> · /db <name> SELECT … · "
                         f"/db <name> summary · /db <name> export <table|schema|all>[/]")

    def _db_add(self, db: Any, rest: str) -> None:
        name, _, url = rest.partition(" ")
        if "://" in name:
            name, url = "", name
        data: dict[str, Any] = db.parse_url(url.strip()) if url.strip() else {}
        data["name"] = name or ui.prompt_input("Connection name", default=data.get("database") or "")
        if not data.get("engine"):
            labels = list(db.ENGINE_LABELS.values())
            picked = ui.select_one("Kind of database", labels, labels[0])
            data["engine"] = next(k for k, v in db.ENGINE_LABELS.items() if v == picked)
            if data["engine"] == "sqlite":
                data["database"] = ui.prompt_input("Database file")
            else:
                data["host"] = ui.prompt_input("Host", default="localhost")
                data["port"] = ui.prompt_input("Port", default=str(db.ENGINES[data["engine"]]))
                data["database"] = ui.prompt_input("Database")
                data["user"] = ui.prompt_input("User (Dcode gets exactly this user's rights)")
                data["password"] = ui.prompt_input("Password", password=True)
        conn = db.save_connection(data)
        try:
            with ui.console.status("Connecting…", spinner=ui.SPINNER, spinner_style=PRIMARY):
                result = db.test_connection(conn)
            ui.notice(f"Saved {conn['name']} and connected — {result['version']}. Try /db {conn['name']}", "success")
        except db.DBError as exc:
            ui.notice(f"Saved {conn['name']}, but it doesn't connect yet: {exc}", "warning")

    def _db_use(self, db: Any, conn: dict[str, Any], rest: str) -> None:
        status = ui.console.status("Asking the database…", spinner=ui.SPINNER, spinner_style=PRIMARY)
        action, _, target = rest.partition(" ")
        if not rest or action in ("summary", "export") or (" " not in rest and db.classify(rest) == "write"):
            with status:
                schema = db.read_schema(conn)
            if not rest:
                ui.console.print(f"{ui.PAD}[bold]{ui._escape(schema['target'])}[/] [{MUTED}]{ui._escape(schema['version'])} · "
                                 f"{db.human_size(schema['bytes'])}[/]")
                for line in db.table_lines(schema)[:200]:
                    ui.console.print(f"{ui.PAD}  {ui._escape(line)}")
                if not schema["tables"]:
                    ui.notice("This database user can't read any table here.", "warning")
                return
            if action == "summary":
                facts = db.summarize(schema)
                ui.console.print(f"{ui.PAD}[bold]{ui._escape(facts['target'])}[/] [{MUTED}]{ui._escape(facts['version'])}[/]")
                ui.console.print(f"{ui.PAD}  {facts['size']} · {facts['tables']} tables · {facts['views']} views · "
                                 f"~{db.human_count(facts['rows_estimate'])} rows · {facts['foreign_keys']} foreign keys")
                for item in facts["largest"][:8]:
                    ui.console.print(f"{ui.PAD}  [{MUTED}]{item['size']:>9}  ~{db.human_count(item['rows']):>7} rows[/]  {ui._escape(item['table'])}")
                if facts["heavy"]:
                    ui.notice("Heavy (above the export limits): " + ", ".join(facts["heavy"][:10]), "warning")
                return
            if action == "export":
                return self._db_export(db, conn, schema, target.strip())
            for line in db.schema_text(schema, rest).splitlines():  # one word that isn't SQL: a table
                ui.console.print(f"{ui.PAD}  {ui._escape(line)}")
            return
        try:
            with status:
                result = db.run_query(conn, rest)
        except db.NeedsConfirmation as ask:
            ui.console.print(f"{ui.PAD}[{WARNING}]This changes {ui._escape(db.describe(conn))}:[/]")
            for line in ask.statement.splitlines()[:20]:
                ui.console.print(f"{ui.PAD}    {ui._escape(line)}")
            if ui.select_one("Run this change?", ["Cancel", "Run it"], "Cancel") != "Run it":
                ui.notice("Not run.")
                return
            with status:
                result = db.run_query(conn, rest, confirmed=True)
        if result["kind"] == "write":
            ui.notice(db.result_text(result) + f" {result['elapsed']}s", "success")
            return
        ui.show_rows(result["columns"], result["rows"])
        shown = min(len(result["rows"]), 40)
        more = result["truncated"] or shown < len(result["rows"])
        ui.console.print(f"{ui.PAD}  [{MUTED}]{shown} row{'s' if shown != 1 else ''}"
                         + (" shown — there are more; add LIMIT/WHERE, or /db " + conn["name"] + " export" if more else "")
                         + f" · {result['elapsed']}s[/]")

    def _db_export(self, db: Any, conn: dict[str, Any], schema: dict[str, Any], what: str) -> None:
        if not what:
            ui.notice(f"Usage: /db {conn['name']} export <table> · export schema · export all")
            return

        def run(force: bool) -> dict[str, Any]:
            with ui.console.status("Exporting…", spinner=ui.SPINNER, spinner_style=PRIMARY):
                if what == "schema":
                    return db.export_schema(schema, db.export_name(conn["name"], "schema", "md"))
                if what == "all":
                    return db.export_database(conn, schema, db.export_name(conn["name"], "all-tables", "zip"), force=force)
                return db.export_table(conn, schema, what, db.export_name(conn["name"], what, "csv"), force=force)

        try:
            done = run(False)
        except db.TooHeavy as heavy:
            ui.notice(str(heavy), "warning")
            if ui.select_one("Export anyway, cut at the limits?", ["Cancel", "Export what fits"], "Cancel") != "Export what fits":
                return
            done = run(True)
        unit = "tables" if what == "schema" else "rows"
        ui.notice(f"Exported {done['rows']:,} {unit} ({done['size']}) to {done['path']}"
                  + (f" — incomplete: {done['stopped']}" if done["truncated"] else ""),
                  "warning" if done["truncated"] else "success")

    # --- chats ----------------------------------------------------------------------------------
    def _open_elsewhere(self) -> dict[str, str]:
        """Chats open in another running dcode: thread id -> that session's id."""
        if not self.ui_url:
            return {}
        import json

        from . import server
        from .config import slugify_thread_name

        found: dict[str, str] = {}
        for entry in server.live_sessions():
            if entry.get("id") == self.session.id:
                continue
            name = str(entry.get("thread_name") or "")
            try:  # the registry holds the name at start; the session itself knows the current one
                status, body = server._session_request(entry, "GET", "/state", server.get_token(), timeout=1.0)
                if status == 200:
                    name = str(json.loads(body).get("thread_name") or name)
            except (OSError, ValueError):
                pass
            found[slugify_thread_name(name)] = str(entry.get("id"))
        return found

    def _sessions(self, arg: str) -> None:
        """/sessions [words]: pick a saved chat and continue it. This folder's chats come first."""
        from .config import slugify_thread_name

        session = self.session
        current = slugify_thread_name(session.thread_name)
        elsewhere = self._open_elsewhere()
        threads = [t for t in list_threads() if t["id"] != current]
        if arg:
            words = arg.lower().split()
            threads = [t for t in threads if all(w in f"{t['name']} {t['cwd']}".lower() for w in words)]
        if not threads:
            ui.notice("No other saved chats" + (f" match “{arg}”." if arg else " yet."))
            return
        threads.sort(key=lambda t: t["cwd"] != session.cwd)  # stable: newest first inside each group
        labels = []
        for item in threads:
            folder = "this folder" if item["cwd"] == session.cwd else (ui.short_path(item["cwd"]) or "—")
            count = sum(1 for m in item["messages"] if m.get("role") == "user")
            mark = "  · open in another terminal" if item["id"] in elsewhere else ""
            labels.append(f"{ui.pretty_thread(item['name'])[:44]:<44}  {folder[:28]:<28}  "
                          f"{ui.ago(item['updated_at']):>8}  {count:>3} msg{mark}")
        picked = ui._selector("Chats — enter continues one", labels, cancel_result=None)
        if not isinstance(picked, int):
            return
        choice = threads[picked]
        if choice["id"] in elsewhere:
            ui.notice(f"“{choice['name']}” is open in another terminal (session {elsewhere[choice['id']]}). "
                      f"Join it with: dcode attach {elsewhere[choice['id']]}", "warning")
            return
        if session.resume(choice["id"]):
            ui.notice(f"Continuing “{session.thread_name}”" + (
                "" if choice["cwd"] in ("", session.cwd) else f" — it was started in {ui.short_path(choice['cwd'])}"), "success")
            self.show_recap()

    def _export(self, arg: str) -> None:
        """/export [md|html|json] [file]: write this chat to a file."""
        from pathlib import Path

        from . import transcripts
        from .config import thread_path

        session = self.session
        fmt, _, target = arg.partition(" ")
        if fmt and fmt.lower().lstrip(".") not in transcripts.FORMATS:
            fmt, target = "", arg  # only a file name was given: its ending decides
        fmt = fmt.lower().lstrip(".") or Path(target).suffix.lower().lstrip(".")
        if fmt not in transcripts.FORMATS:
            labels = [f"{key:<5} {label}" for key, label in transcripts.FORMATS.items()]
            picked = ui.select_one("Export this chat as", labels, labels[0])
            fmt = picked.split()[0] if picked in labels else ""
        if not fmt:
            return
        if len(session.agent.messages) <= 1:
            ui.notice("Nothing to export yet: this chat is empty.")
            return
        session.save()
        try:
            name, text = transcripts.export_thread(thread_path(session.thread_name), fmt)
            path = Path(target.strip()).expanduser() if target.strip() else Path(session.cwd) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except (transcripts.TranscriptError, OSError) as exc:
            ui.notice(f"Couldn't export: {exc}", "error")
            return
        note = " — bring it back on any machine with: dcode import <file>" if fmt == "json" else ""
        shown = path.name if path.parent == Path(session.cwd) else str(path)  # short when it's right here
        ui.notice(f"Exported this chat ({len(text):,} characters) to {shown}{note or '.'}", "success")

    def show_recap(self) -> None:
        """Where a continued chat left off: the last request and the last answer, dimmed."""
        messages = self.session.agent.messages
        asked = next((m for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)
                      and not m["content"].startswith("[")), None)
        answered = next((m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")), None)
        if asked:
            ui.console.print(f"{ui.PAD}[{MUTED}]❯ {ui._escape(ui._short_args(asked['content'], 200))}[/]")
        if answered:
            lines = str(answered["content"]).strip().splitlines()
            for line in lines[:8]:
                ui.console.print(f"{ui.PAD}[{MUTED}]▎ {ui._escape(line[:200])}[/]", highlight=False)
            if len(lines) > 8:
                ui.console.print(f"{ui.PAD}[{MUTED}]▎ … +{len(lines) - 8} lines[/]")

    def _list_helpers(self) -> None:
        specs = self.session.agent.specs
        helper_model = str(self.session.config.agents.get("helper_model") or "")
        ui.console.print(f"{ui.PAD}[bold]Helpers[/]" + (f" [{MUTED}]· default model {ui._escape(helper_model)}[/]"
                                                         if helper_model else ""))
        for spec in specs.values():
            where = "" if spec.scope == "builtin" else f" · {spec.scope}"
            model = f" · {spec.model}" if spec.model else ""
            access = "read-only" if spec.read_only else "can change files"
            ui.console.print(f"{ui.PAD}  [{ACCENT}]@{spec.name}[/] [{MUTED}]{access}{model}{where}[/]  "
                             f"{ui._escape(spec.description[:90])}")
        ui.console.print(f"{ui.PAD}[{MUTED}]Add your own: ~/.dcode/agents/<name>.md or .dcode/agents/<name>.md[/]")

    def _jobs(self) -> None:
        jobs = self.session.jobs.list()
        if not jobs:
            ui.notice("No background jobs. Dcode starts one when a command should keep running (servers, watchers).")
            return
        labels = [f"#{j.id}  {j.state():<12}  {ui._fmt_elapsed(j.elapsed()):>7}  {j.command[:70]}" for j in jobs]
        picked = ui.select_one("Background jobs", labels)
        if picked not in labels:
            return
        job = jobs[labels.index(picked)]
        ui.show_expanded([(f"job #{job.id} · {job.state()}", {"command": job.command}, job.tail(60) or "(no output yet)")])
        if job.running:
            choice = ui.select_one(f"Job #{job.id} is running", ["Keep it running", "Stop it"], "Keep it running")
            if choice == "Stop it":
                self.session.jobs.stop(job.id)
                ui.notice(f"Stopped job #{job.id}.", "success")

    def _artifact_link(self, artifact_id: str) -> str:
        """Full link (logs the browser in on first use) — for opening in the browser."""
        if not self.ui_url or not artifact_id:
            return ""
        from .server import artifact_url

        return artifact_url(self.ui_url.split("/?")[0], artifact_id)

    def _artifact_links(self, artifact_id: str) -> tuple[str, str]:
        """(short display link, link to follow). Short = http://host/a/<id>; the login cookie lasts 30 days."""
        full = self._artifact_link(artifact_id)
        if not full:
            return "", ""
        short = f"{self.ui_url.split('/?')[0]}/a/{artifact_id}"
        # Terminals with clickable links show the short text but follow the full (login) link;
        # elsewhere print the short link, which works once the browser has opened the web UI.
        return short, (full if ui.supports_hyperlinks() else short)

    def _artifacts(self) -> None:
        from .artifacts import ArtifactStore

        items = ArtifactStore().list()[:60]
        if not items:
            ui.notice("No artifacts yet. Ask for a page, diagram or report and Dcode saves it as one.")
            return
        kinds = {"html": "page", "svg": "diagram", "markdown": "doc", "csv": "table", "code": "code"}
        labels = [f"{a['title']}  ·  {kinds.get(a['kind'], a['kind'])} · v{a['versions']}" for a in items]
        picked = ui.select_one("Artifacts — enter opens in your browser", labels)
        if picked not in labels:
            return
        artifact = items[labels.index(picked)]
        link = self._artifact_link(artifact["id"])
        if not link:
            ui.notice("The web UI is off, so artifacts can't be opened from here. Start dcode without --no-ui.", "warning")
            return
        opened = ui.open_in_browser(link)
        ui.console.print(f"{ui.PAD}[{MUTED}]{'Opened' if opened else 'Open'}[/] [bold]{ui._escape(artifact['title'])}[/]  "
                         f"{ui.link(link)}", soft_wrap=True)

    def _commit(self, message: str) -> None:
        from .tools import execute_tool

        session = self.session
        if not session.git.get("is_repo"):
            ui.notice("Not a git repository.", "warning")
            return
        if not message:
            with ui.console.status("Writing a commit message…", spinner=ui.SPINNER, spinner_style=PRIMARY):
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
            with ui.console.status("Fetching models…", spinner=ui.SPINNER, spinner_style=PRIMARY):
                models = self.session.llm.list_models()
        except Exception as exc:
            ui.notice(f"Could not load models: {exc}", "warning")
            models = []
        model = ui.select_model(models, cfg.model) if models else ui.prompt_input("Model", default=cfg.model)
        effort = ui.select_one("Reasoning effort", REASONING_EFFORTS, cfg.reasoning_effort)
        self.session.set_model(model or cfg.model, effort if effort in REASONING_EFFORTS else None)
        save_config(cfg)
