from __future__ import annotations

import os
from typing import List, Optional, Sequence

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel

from code_one import __version__

ORANGE = "#FF6B00"
WHITE = "white"
MUTED = "#888888"
_INPUT_BG = "#1a1a1a"

console = Console()

_PROMPT_SESSION = PromptSession()


class _SlashCommandCompleter(Completer):
    def __init__(self, commands: List[str]):
        self._commands = commands

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for command in self._commands:
            if command.startswith(text):
                yield Completion(command, start_position=-len(text))


_COMMAND_COMPLETER = _SlashCommandCompleter(
    [
        "/exit",
        "/reset",
        "/model",
        "/setup",
        "/permissions",
        "/experimental",
        "/skills",
        "/review",
        "/rename",
        "/new",
        "/resume",
        "/help",
    ]
)

_PROMPT_STYLE = Style.from_dict(
    {
        "": f"bg:{_INPUT_BG} {WHITE}",
        "prompt": f"bold fg:{ORANGE}",
        "completion-menu.completion": f"bg:{_INPUT_BG} fg:{ORANGE}",
        "completion-menu.completion.current": f"bg:{_INPUT_BG} bold fg:{ORANGE}",
        "completion-menu.meta": f"bg:{_INPUT_BG} {WHITE}",
    }
)


def show_welcome(
    current_model: str,
    base_url: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    git_summary: Optional[dict] = None,
) -> None:
    cwd = os.getcwd()
    logo = "\n".join(
        [
            "   ██████╗ ██╗",
            "  ██╔════╝ ██║",
            "  ██║      ██║",
            "  ██╚════╝ ██║",
            "   ██████╝ ╚═╝",
        ]
    )
    left_lines = [
        f"[{WHITE}]{logo}[/]",
        "",
        f"[bold {WHITE}]code one[/]",
        f"[dim {WHITE}]{current_model}[/]",
        f"[dim {ORANGE}]reasoning: {reasoning_effort or 'medium'}[/]",
        f"[dim {ORANGE}]{cwd}[/]",
    ]
    if base_url:
        left_lines.append(f"[dim {WHITE}]{base_url}[/]")
    left_body = "\n".join(left_lines)
    left_panel = Panel(left_body, border_style=ORANGE)

    right_lines = [
        "/exit → Exit the program",
        "/reset → Clear conversation history",
        "/model → Model + reasoning",
        "/permissions → Session tool access",
        "/experimental → Feature flags",
        "/skills → Session skills",
        "/review → Review git diff",
        "/rename → Rename thread",
        "/new → Save and start fresh",
        "/resume → Load saved thread",
    ]
    if git_summary and git_summary.get("is_repo"):
        right_lines.extend(
            [
                "-" * 26,
                f"Branch: {git_summary.get('branch', '-')}",
                f"Last: {git_summary.get('last_commit', '-')}",
                f"Modified: {git_summary.get('modified_files', 0)} files",
            ]
        )
    right_panel = Panel(
        "\n".join(right_lines),
        title="[bold #FF6B00]Available Commands[/bold #FF6B00]",
        title_align="left",
        border_style=ORANGE,
    )
    console.print(
        Panel(
            Columns([left_panel, right_panel], expand=True),
            title=f"[bold #FF6B00]code-one v{__version__}[/bold #FF6B00]",
            border_style=ORANGE,
            expand=True,
        )
    )


def show_help() -> None:
    lines = [
        "/exit - Exit the program",
        "/reset - Clear conversation history",
        "/model - Switch model and reasoning effort",
        "/setup - Re-run setup wizard",
        "/permissions - Toggle tool permissions",
        "/experimental - Toggle experimental features",
        "/skills - Enable session skills from ~/.code-one/skills",
        "/review - Review git-modified files",
        "/rename - Rename current thread",
        "/new - Save current thread and start a new one",
        "/resume - Resume a saved thread",
    ]
    console.print(Panel("\n".join(lines), title="Help", border_style=ORANGE))


def _render_prompt_input(field: Optional[str], default: str, password: bool, completer: Optional[Completer]) -> str:
    if field:
        console.print(f"[{ORANGE}]{field}[/]")
    complete = completer is not None
    return _PROMPT_SESSION.prompt(
        [("class:prompt", "> ")],
        default=default,
        is_password=password,
        style=_PROMPT_STYLE,
        completer=completer,
        complete_while_typing=complete,
        mouse_support=False,
    )


def show_setup_prompt(field: str, default: str = "", password: bool = False) -> str:
    return _render_prompt_input(field=field, default=default, password=password, completer=None).strip()


def prompt_input(field: str, default: str = "", password: bool = False, complete_commands: bool = False) -> str:
    completer = _COMMAND_COMPLETER if complete_commands else None
    return _render_prompt_input(field=field, default=default, password=password, completer=completer).strip()


def _selector(
    title: str,
    rows_builder,
    item_count: int,
    initial_index: int = 0,
    on_enter=None,
    cancel_result: Optional[object] = None,
):
    selected_index = [max(0, min(initial_index, max(item_count - 1, 0)))]
    kb = KeyBindings()

    @kb.add("up")
    def _move_up(event) -> None:
        if item_count:
            selected_index[0] = (selected_index[0] - 1) % item_count
            event.app.invalidate()

    @kb.add("down")
    def _move_down(event) -> None:
        if item_count:
            selected_index[0] = (selected_index[0] + 1) % item_count
            event.app.invalidate()

    @kb.add("enter")
    def _confirm(event) -> None:
        if on_enter is None:
            event.app.exit(result=selected_index[0])
            return
        on_enter(event, selected_index[0])

    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result=cancel_result)

    header = Window(height=1, content=FormattedTextControl(text=[(f"bold fg:{ORANGE}", title)]))
    body = Window(content=FormattedTextControl(text=lambda: rows_builder(selected_index[0])))
    hint = Window(height=1, content=FormattedTextControl(text=[("fg:#AA6A3C", "↑↓ navigate enter select")]))
    app = Application(layout=Layout(HSplit([header, body, hint])), key_bindings=kb, full_screen=False)
    return app.run()


def select_model(models: List[str], selected: Optional[str] = None) -> str:
    if not models:
        return selected or ""
    initial_index = models.index(selected) if selected in models else 0

    def render_rows(current_index: int) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for index, model in enumerate(models):
            style = f"bold fg:{ORANGE}" if index == current_index else f"fg:{MUTED}"
            prefix = "❯ " if index == current_index else "  "
            rows.append((style, f"{prefix}{model}"))
            rows.append(("", "\n"))
        return rows[:-1] if rows else []

    result = _selector("Select Model", render_rows, len(models), initial_index=initial_index, cancel_result=selected or "")
    return models[result] if isinstance(result, int) else str(result or "")


def list_models_prompt(models: List[str], selected: Optional[str] = None) -> str:
    return select_model(models=models, selected=selected)


def select_one(title: str, options: Sequence[str], selected: Optional[str] = None) -> str:
    normalized = list(options)
    if not normalized:
        return selected or ""
    initial_index = normalized.index(selected) if selected in normalized else 0

    def render_rows(current_index: int) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for index, option in enumerate(normalized):
            style = f"bold fg:{ORANGE}" if index == current_index else f"fg:{MUTED}"
            prefix = "❯ " if index == current_index else "  "
            rows.append((style, f"{prefix}{option}"))
            rows.append(("", "\n"))
        return rows[:-1] if rows else []

    result = _selector(title, render_rows, len(normalized), initial_index=initial_index, cancel_result=selected or "")
    return normalized[result] if isinstance(result, int) else str(result or "")


def checklist_prompt(title: str, items: Sequence[tuple[str, bool]], hint_text: str = "↑↓ move space toggle enter save") -> dict[str, bool]:
    labels = [label for label, _ in items]
    values = [bool(enabled) for _, enabled in items]
    selected_index = [0]
    kb = KeyBindings()

    @kb.add("up")
    def _move_up(event) -> None:
        selected_index[0] = (selected_index[0] - 1) % len(labels)
        event.app.invalidate()

    @kb.add("down")
    def _move_down(event) -> None:
        selected_index[0] = (selected_index[0] + 1) % len(labels)
        event.app.invalidate()

    @kb.add(" ")
    def _toggle(event) -> None:
        values[selected_index[0]] = not values[selected_index[0]]
        event.app.invalidate()

    @kb.add("enter")
    def _confirm(event) -> None:
        event.app.exit(result={label: values[index] for index, label in enumerate(labels)})

    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result={label: enabled for label, enabled in items})

    def render_rows() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for index, label in enumerate(labels):
            active = index == selected_index[0]
            box = "[x]" if values[index] else "[ ]"
            style = f"bold fg:{ORANGE}" if active else f"fg:{MUTED}"
            prefix = "❯ " if active else "  "
            rows.append((style, f"{prefix}{box} {label}"))
            rows.append(("", "\n"))
        return rows[:-1] if rows else []

    header = Window(height=1, content=FormattedTextControl(text=[(f"bold fg:{ORANGE}", title)]))
    body = Window(content=FormattedTextControl(text=render_rows))
    hint = Window(height=1, content=FormattedTextControl(text=[("fg:#AA6A3C", hint_text)]))
    app = Application(layout=Layout(HSplit([header, body, hint])), key_bindings=kb, full_screen=False)
    return app.run()


def pick_thread(threads: Sequence[dict]) -> Optional[dict]:
    if not threads:
        return None
    labels = [f"{item['name']}  {item.get('created_at', '')}" for item in threads]

    def render_rows(current_index: int) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for index, label in enumerate(labels):
            style = f"bold fg:{ORANGE}" if index == current_index else f"fg:{MUTED}"
            prefix = "❯ " if index == current_index else "  "
            rows.append((style, f"{prefix}{label}"))
            rows.append(("", "\n"))
        return rows[:-1] if rows else []

    result = _selector("Resume Thread", render_rows, len(labels), cancel_result=None)
    return threads[result] if isinstance(result, int) else None


def show_tool_call(tool: str, args: str) -> None:
    console.print(f"[dim {ORANGE}]⚡ {tool}({args})[/]")


def show_tool_output(tool: str, output: str) -> None:
    if output:
        console.print(Panel(output, title=f"{tool} output", border_style=ORANGE))


def show_assistant(message: str) -> None:
    if message:
        console.print(message)


def show_stream_chunk(text: str) -> None:
    if text:
        console.print(text, end="")


def show_summary(model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int, elapsed: float) -> None:
    summary = "\n".join(
        [
            f"Elapsed: {elapsed:.2f}s",
            f"Prompt tokens: {prompt_tokens}",
            f"Completion tokens: {completion_tokens}",
            f"Total tokens: {total_tokens}",
            f"Model used: {model}",
        ]
    )
    console.print(Panel(summary, title="Summary", border_style=ORANGE))
