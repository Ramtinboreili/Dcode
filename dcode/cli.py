"""Interactive terminal front-end for a :class:`~dcode.session.Session`.

The loop is state-driven rather than prompt-driven: at every iteration it
looks at the session (pending approval? busy? idle?) and shows the matching
UI. The web UI can change that state at any time; when it does, the session
fires an "attention" callback that closes the current prompt so the loop
re-evaluates.
"""

from __future__ import annotations

import time
from importlib import import_module
from typing import Any

from . import ui
from .config import REASONING_EFFORTS, list_skill_files, list_threads, save_config
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

    # --- main loop -------------------------------------------------------------------------
    def run(self) -> None:
        cfg = self.session.config
        ui.show_welcome(
            cfg.model,
            base_url=cfg.base_url,
            reasoning_effort=cfg.reasoning_effort,
            git_summary=self.session.git,
            ui_url=self.ui_url,
            context_window=cfg.context_window,
            agents_enabled=bool(cfg.agents.get("enabled", True)),
        )
        self.session.bus.subscribe(self._render)
        self.session.on_attention(ui.interrupt_prompt)

        while True:
            approvals = self.session.pending_approvals()
            if approvals:
                self._ask_approval(approvals[0])
                continue
            if not self.session.idle:
                self._wait_while_busy()
                continue
            try:
                text = ui.prompt_input("", complete_commands=True)
            except ui.PromptInterrupted:
                continue
            except KeyboardInterrupt:
                ui.notice("Type /exit to quit.", "info")
                continue
            except EOFError:
                break
            if not text:
                continue
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
        word = {"allow": "Allowed", "session": "Allowed for this session", "deny": "Denied"}[choice]
        ui.notice(word, "success" if choice != "deny" else "warning")

    # --- event rendering --------------------------------------------------------------------
    def _render(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        agent = event.get("agent", "main")
        experimental = self.session.config.experimental
        if kind == "user_message" and event.get("source") not in ("cli", "history"):
            ui.show_user_echo(event.get("text", ""), event.get("source", "web"))
        elif kind == "assistant_note" and agent == "main":
            ui.show_note(event.get("text", ""))
        elif kind == "assistant_message" and agent == "main":
            ui.show_assistant(event.get("text", ""))
        elif kind == "tool_call":
            self.activity = f"{agent + ' · ' if agent != 'main' else ''}{event.get('name')}"
            ui.show_tool_call(event.get("name", ""), event.get("args"), agent)
        elif kind == "tool_result":
            if experimental.get("streaming_tools"):
                ui.show_tool_output(event.get("name", ""), event.get("output", ""))
            else:
                ui.show_tool_result_brief(event.get("output", ""), agent)
            self.activity = "Thinking"
        elif kind == "agent_start":
            ui.show_agent_start(agent, event.get("role", ""), event.get("task", ""))
        elif kind == "agent_end":
            ui.show_agent_end(agent, bool(event.get("ok")))
        elif kind == "approval_resolved" and event.get("by") == "web":
            ui.notice(f"{'Approved' if event.get('approved') else 'Denied'} from the web UI", "info")
        elif kind == "usage" and agent == "main":
            ui.show_done(float(event.get("elapsed", 0.0)))
        elif kind == "approval_auto":
            ui.notice(f"Auto-allowed for this session: {ui._short_args(event.get('detail', ''), 90)}", "info")
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
        elif kind == "thread":
            ui.notice(f"Thread {event.get('action')}: {event.get('name')}", "info")

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
            elif command == "/guide":
                if self.ui_url:
                    ui.notice(f"Guide (English / فارسی): {self.ui_url.split('/?')[0]}/guide", "info")
                else:
                    ui.notice("The guide is served by the web UI; start dcode without --no-ui, "
                              "or open dcode/web/guide.html from the source folder.", "warning")
            elif command == "/ui":
                if self.ui_url:
                    ui.notice(f"Web UI: {self.ui_url}", "info")
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
