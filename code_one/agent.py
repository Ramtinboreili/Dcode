from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from openai import OpenAI

from . import ui
from .config import AppConfig, list_skill_files, list_threads, load_thread, save_config, save_thread
from .tools import execute_tool, parse_arguments, tool_definitions


SYSTEM_PROMPT = """You are an expert AI coding assistant running in the terminal (code-one).

You have tools to read/write files, run shell commands, inspect git state, and manage the filesystem.

Be concise. Read files before editing. Explain actions briefly. Ask before destructive ops."""

REASONING_EFFORTS = ["none", "low", "medium", "high"]
PERMISSION_LABELS = {
    "read_file": "read files",
    "write_file": "write files",
    "run_command": "run shell commands",
    "delete_file": "delete files",
    "create_directory": "create directories",
    "search_in_files": "search files",
}
EXPERIMENTAL_LABELS = {
    "multi_file_edit": "multi_file_edit",
    "auto_commit": "auto_commit",
    "streaming_tools": "streaming_tools",
}


class CodeOneAgent:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self.model = config.model
        self.git_summary = self._get_git_summary()
        self.thread_name = self._timestamp_name()
        self.thread_created_at = datetime.now(timezone.utc).isoformat()
        self.thread_renamed = False
        self.messages: List[Dict[str, Any]] = []
        self._reset_messages()
        self._refresh_tools()

    def _timestamp_name(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    def _system_prompt(self) -> str:
        lines = [SYSTEM_PROMPT]
        lines.append(f"Current reasoning effort: {self.config.reasoning_effort}.")
        if self.config.experimental.get("multi_file_edit"):
            lines.append("Experimental feature enabled: multi_file_edit. You may edit multiple files in one response.")
        if self.git_summary.get("is_repo"):
            lines.append(
                "Current git branch: "
                f"{self.git_summary.get('branch', '-')}. "
                f"Last commit: {self.git_summary.get('last_commit', '-')}. "
                f"Modified files: {self.git_summary.get('modified_files', 0)}."
            )
        skill_text = self._load_enabled_skills()
        if skill_text:
            lines.append("Enabled skills:")
            lines.append(skill_text)
        return "\n\n".join(lines)

    def _reset_messages(self) -> None:
        self.messages = [{"role": "system", "content": self._system_prompt()}]

    def _refresh_system_message(self) -> None:
        system_message = {"role": "system", "content": self._system_prompt()}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = system_message
        else:
            self.messages.insert(0, system_message)

    def _refresh_tools(self) -> None:
        self.tools = tool_definitions(
            permissions=self.config.permissions,
            include_git_tools=True,
            auto_commit_enabled=self.config.experimental.get("auto_commit", False),
        )

    def reset(self) -> None:
        self._reset_messages()
        self.thread_name = self._timestamp_name()
        self.thread_created_at = datetime.now(timezone.utc).isoformat()
        self.thread_renamed = False
        ui.console.print("")
        ui.console.print(f"[{ui.ORANGE}]Conversation reset[/{ui.ORANGE}]")

    def run(self) -> None:
        ui.show_welcome(
            self.model,
            base_url=self.config.base_url,
            reasoning_effort=self.config.reasoning_effort,
            git_summary=self.git_summary,
        )
        while True:
            user_text = ui.prompt_input("Send a task or command", complete_commands=True)
            if not user_text:
                continue
            if user_text == "/exit":
                break
            if user_text == "/help":
                ui.show_help()
                continue
            if user_text == "/reset":
                self.reset()
                continue
            if user_text == "/model":
                self._switch_model()
                continue
            if user_text == "/setup":
                self._setup()
                continue
            if user_text == "/permissions":
                self._edit_permissions()
                continue
            if user_text == "/experimental":
                self._edit_experimental()
                continue
            if user_text == "/skills":
                self._edit_skills()
                continue
            if user_text == "/review":
                self._run_review()
                continue
            if user_text == "/rename":
                self._rename_thread()
                continue
            if user_text == "/new":
                self._new_thread()
                continue
            if user_text == "/resume":
                self._resume_thread()
                continue

            self._handle_user_task(user_text)

    def fetch_models(self) -> List[str]:
        main = import_module("code_one.main")
        with ui.console.status(f"[{ui.ORANGE}]Fetching models...[/]", spinner="dots", spinner_style=ui.ORANGE):
            return main._fetch_models(self.config.base_url, self.config.api_key)

    def _switch_model(self) -> None:
        ui.console.print(
            f"[{ui.ORANGE}]Current model:[/] {self.model}  "
            f"[{ui.ORANGE}]Reasoning:[/] {self.config.reasoning_effort}"
        )
        try:
            models = self.fetch_models()
        except Exception as exc:
            ui.console.print(f"[{ui.ORANGE}]Could not load models: {exc}[/]")
            models = []

        if models:
            selection = ui.list_models_prompt(models, selected=self.model).strip()
            if selection in models:
                self.model = selection
                self.config.model = selection
        else:
            selection = ui.prompt_input("Model", default=self.model).strip()
            if selection:
                self.model = selection
                self.config.model = selection

        reasoning = ui.select_one("Reasoning Effort", REASONING_EFFORTS, selected=self.config.reasoning_effort)
        if reasoning in REASONING_EFFORTS:
            self.config.reasoning_effort = reasoning

        save_config(self.config)
        self._refresh_system_message()
        ui.console.print(
            f"[{ui.ORANGE}]Using model:[/] {self.model}  "
            f"[{ui.ORANGE}]Reasoning:[/] {self.config.reasoning_effort}"
        )

    def _setup(self) -> None:
        main = import_module("code_one.main")
        config = main.run_setup(model=self.model)
        config.permissions = dict(self.config.permissions)
        config.experimental = dict(self.config.experimental)
        config.enabled_skills = list(self.config.enabled_skills)
        config.reasoning_effort = self.config.reasoning_effort
        self.config = config
        self.model = config.model
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self._refresh_tools()
        self._refresh_system_message()
        ui.console.print(f"[{ui.ORANGE}]✓ Setup complete. Now using model: {self.model}[/]")

    def _edit_permissions(self) -> None:
        items = [(label, self.config.permissions.get(name, True)) for name, label in PERMISSION_LABELS.items()]
        result = ui.checklist_prompt("Permissions", items)
        for key, label in PERMISSION_LABELS.items():
            self.config.permissions[key] = bool(result.get(label, True))
        save_config(self.config)
        self._refresh_tools()
        ui.console.print(f"[{ui.ORANGE}]Permissions updated for this session[/]")

    def _edit_experimental(self) -> None:
        items = [(label, self.config.experimental.get(key, False)) for key, label in EXPERIMENTAL_LABELS.items()]
        result = ui.checklist_prompt("Experimental Features", items)
        for key, label in EXPERIMENTAL_LABELS.items():
            self.config.experimental[key] = bool(result.get(label, False))
        save_config(self.config)
        self._refresh_tools()
        self._refresh_system_message()
        ui.console.print(f"[{ui.ORANGE}]Experimental features updated[/]")

    def _edit_skills(self) -> None:
        skill_paths = list_skill_files()
        if not skill_paths:
            ui.console.print(f"[{ui.ORANGE}]No skills found in ~/.code-one/skills[/]")
            return
        items = [(path.name, path.name in self.config.enabled_skills) for path in skill_paths]
        result = ui.checklist_prompt("Skills", items)
        self.config.enabled_skills = [name for name in result if result[name]]
        self._refresh_system_message()
        ui.console.print(f"[{ui.ORANGE}]Loaded skills for this session: {', '.join(self.config.enabled_skills) or '(none)'}[/]")

    def _run_review(self) -> None:
        if not self.git_summary.get("is_repo"):
            ui.console.print(f"[{ui.ORANGE}]Current directory is not a git repository[/]")
            return
        diff = self._run_git_command(["git", "diff", "HEAD"])
        if not diff.strip():
            ui.console.print(f"[{ui.ORANGE}]No git changes to review[/]")
            return

        review_messages = [
            {"role": "system", "content": self._system_prompt()},
            {
                "role": "user",
                "content": "Review these changes. Find bugs, issues, and improvements.\n\n" + diff,
            },
        ]
        started_at = time.perf_counter()
        collected = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        try:
            with ui.console.status(f"[{ui.ORANGE}]Reviewing changes...[/]", spinner="dots", spinner_style=ui.ORANGE):
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=review_messages,
                    stream=True,
                )
            for chunk in stream:
                choice = chunk.choices[0] if getattr(chunk, "choices", None) else None
                delta = getattr(choice, "delta", None)
                content = getattr(delta, "content", None) if delta else None
                if content:
                    collected.append(content)
                    ui.show_stream_chunk(content)
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    prompt_tokens += getattr(usage, "prompt_tokens", 0)
                    completion_tokens += getattr(usage, "completion_tokens", 0)
                    total_tokens += getattr(usage, "total_tokens", 0)
            ui.console.print("")
        except Exception as exc:
            ui.console.print(f"[{ui.ORANGE}]Review failed: {exc}[/]")
            return
        elapsed = time.perf_counter() - started_at
        review_text = "".join(collected).strip()
        if review_text:
            self.messages.append({"role": "user", "content": "[built-in review request]"})
            self.messages.append({"role": "assistant", "content": review_text})
        ui.show_summary(self.model, prompt_tokens, completion_tokens, total_tokens, elapsed)

    def _rename_thread(self) -> None:
        new_name = ui.prompt_input("Thread name", default=self.thread_name).strip()
        if not new_name:
            return
        self.thread_name = new_name
        self.thread_renamed = True
        ui.console.print(f"[{ui.ORANGE}]Thread renamed to {self.thread_name}[/]")

    def _new_thread(self) -> None:
        self._save_current_thread()
        self._reset_messages()
        self.thread_name = self._timestamp_name()
        self.thread_created_at = datetime.now(timezone.utc).isoformat()
        self.thread_renamed = False
        ui.console.print(f"[{ui.ORANGE}]Started a new thread[/]")

    def _resume_thread(self) -> None:
        threads = list_threads()
        choice = ui.pick_thread(threads)
        if not choice:
            ui.console.print(f"[{ui.ORANGE}]No thread selected[/]")
            return
        payload = load_thread(choice["path"])
        loaded_messages = payload.get("messages")
        if not isinstance(loaded_messages, list):
            ui.console.print(f"[{ui.ORANGE}]Thread file is invalid[/]")
            return
        self.messages = loaded_messages
        self._refresh_system_message()
        self.thread_name = str(payload.get("name") or choice["name"])
        self.thread_created_at = str(payload.get("created_at") or datetime.now(timezone.utc).isoformat())
        self.thread_renamed = True
        ui.console.print(f"[{ui.ORANGE}]Resumed thread: {self.thread_name}[/]")

    def _save_current_thread(self) -> None:
        if len(self.messages) <= 1:
            return
        path = save_thread(self.thread_name, self.messages, created_at=self.thread_created_at)
        ui.console.print(f"[{ui.ORANGE}]Saved thread:[/] {path}")

    def _load_enabled_skills(self) -> str:
        if not self.config.enabled_skills:
            return ""
        loaded: List[str] = []
        for path in list_skill_files():
            if path.name not in self.config.enabled_skills:
                continue
            try:
                loaded.append(f"## {path.name}\n{path.read_text(encoding='utf-8').strip()}")
            except Exception:
                continue
        return "\n\n".join(loaded)

    def _run_git_command(self, command: List[str]) -> str:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        output = (result.stdout or "") + (result.stderr or "")
        return output.strip()

    def _get_git_summary(self) -> Dict[str, Any]:
        probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=False, capture_output=True, text=True)
        if probe.returncode != 0:
            return {"is_repo": False}
        branch = self._run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "-"
        last_commit = self._run_git_command(["git", "log", "-1", "--pretty=format:%h %s"]) or "-"
        modified = self._run_git_command(["git", "status", "--porcelain"])
        modified_files = len([line for line in modified.splitlines() if line.strip()])
        return {
            "is_repo": True,
            "branch": branch,
            "last_commit": last_commit,
            "modified_files": modified_files,
        }

    def _append_tool_result(self, tool_call: Union[Dict[str, Any], Any], name: str, content: str) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": self._tool_call_id(tool_call),
                "name": name,
                "content": content,
            }
        )
        if self.config.experimental.get("streaming_tools"):
            ui.show_tool_output(name, content)

    def _is_explicit_commit_request(self, user_text: str) -> bool:
        lowered = user_text.lower()
        return "commit" in lowered or "git_commit" in lowered

    def _handle_user_task(self, user_text: str) -> None:
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        started_at = time.perf_counter()

        self._refresh_system_message()
        self.messages.append({"role": "user", "content": user_text})

        final_response: Optional[str] = None
        try:
            while True:
                with ui.console.status(f"[{ui.ORANGE}]Thinking...[/]", spinner="dots", spinner_style=ui.ORANGE):
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=self.messages,
                        tools=self.tools,
                        tool_choice="auto",
                    )

                usage = getattr(response, "usage", None)
                if usage is not None:
                    prompt_tokens += getattr(usage, "prompt_tokens", 0)
                    completion_tokens += getattr(usage, "completion_tokens", 0)
                    total_tokens += getattr(usage, "total_tokens", 0)

                message = response.choices[0].message
                tool_calls = self._normalize_tool_calls(message)

                if not tool_calls:
                    assistant_content = getattr(message, "content", "") or ""
                    final_response = assistant_content
                    self.messages.append(self._serialize_message(message))
                    break

                self.messages.append(self._serialize_message(message))
                for tool_call in tool_calls:
                    function = tool_call.get("function") if isinstance(tool_call, dict) else getattr(tool_call, "function", None)
                    if not function:
                        continue

                    if isinstance(function, dict):
                        name = function.get("name", "")
                        raw_args = function.get("arguments", "{}")
                    else:
                        name = getattr(function, "name", "")
                        raw_args = getattr(function, "arguments", "{}")

                    args = parse_arguments(raw_args)
                    ui.show_tool_call(name, json.dumps(args))

                    if name == "delete_file":
                        target = args.get("path", "")
                        confirm = ui.prompt_input(f"Confirm delete for: {target} (y/N)", default="n").lower()
                        if not confirm.startswith("y"):
                            self._append_tool_result(tool_call, name, f"Deletion cancelled by user for {target}")
                        else:
                            self._append_tool_result(tool_call, name, execute_tool(name, args))
                        continue

                    if name == "git_commit":
                        if not self.config.experimental.get("auto_commit") and not self._is_explicit_commit_request(user_text):
                            self._append_tool_result(tool_call, name, "git_commit blocked: auto_commit is disabled and the user did not explicitly request a commit.")
                            continue
                        message_text = args.get("message", "")
                        confirm = ui.prompt_input(f"Confirm git commit: {message_text} (y/N)", default="n").lower()
                        if not confirm.startswith("y"):
                            self._append_tool_result(tool_call, name, "Git commit cancelled by user.")
                        else:
                            self._append_tool_result(tool_call, name, execute_tool(name, args))
                            self.git_summary = self._get_git_summary()
                        continue

                    tool_output = execute_tool(name, args)
                    self._append_tool_result(tool_call, name, tool_output)

                    if name == "write_file" and self.config.experimental.get("auto_commit"):
                        auto_message = f"Auto commit after write: {Path(str(args.get('path', 'file'))).name}"
                        confirm = ui.prompt_input(f"Confirm git commit: {auto_message} (y/N)", default="n").lower()
                        if confirm.startswith("y"):
                            commit_output = execute_tool("git_commit", {"message": auto_message})
                            self.git_summary = self._get_git_summary()
                            if self.config.experimental.get("streaming_tools"):
                                ui.show_tool_output("git_commit", commit_output)

        except Exception as exc:
            final_response = f"Task failed: {exc}"

        elapsed = time.perf_counter() - started_at
        ui.show_assistant(final_response or "")
        ui.show_summary(self.model, prompt_tokens, completion_tokens, total_tokens, elapsed)

    @staticmethod
    def _serialize_message(message: Any) -> Dict[str, Any]:
        if hasattr(message, "model_dump"):
            return message.model_dump(exclude_none=True)
        if isinstance(message, dict):
            return message
        return json.loads(message.model_dump_json(exclude_none=True))

    @staticmethod
    def _normalize_tool_calls(message: Any) -> List[Dict[str, Any]]:
        calls = getattr(message, "tool_calls", None)
        normalized = []
        if not calls:
            return normalized
        for call in calls:
            if isinstance(call, dict):
                normalized.append(call)
            else:
                normalized.append(call.model_dump(exclude_none=True))
        return normalized

    @staticmethod
    def _tool_call_id(tool_call: Union[Dict[str, Any], Any]) -> str:
        if isinstance(tool_call, dict):
            return tool_call.get("id", "")
        return getattr(tool_call, "id", "")
