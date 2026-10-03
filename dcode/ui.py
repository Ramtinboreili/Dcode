"""Terminal widgets. Colors come from :mod:`dcode.theme`, shared with the web UI."""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Sequence
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import __version__
from .theme import ACCENT, DANGER, LOGO_LINES, MUTED, PRIMARY, PRIMARY_SOFT, SUCCESS, SURFACE_2, TEXT, WARNING

# Kept for code that still refers to the old name.
ORANGE = PRIMARY

console = Console(
    theme=Theme(
        {
            "markdown.code": f"bold {ACCENT}",
            "markdown.code_block": TEXT,
            "markdown.h1": f"bold {PRIMARY_SOFT}",
            "markdown.h2": f"bold {PRIMARY_SOFT}",
            "markdown.h3": f"bold {PRIMARY}",
            "markdown.link": PRIMARY_SOFT,
            "markdown.item.bullet": PRIMARY,
            "markdown.block_quote": MUTED,
        }
    ),
    highlight=False,
)

_PROMPT_SESSION: PromptSession | None = None


def _prompt_session() -> PromptSession:
    # Created on first use: building it at import warns when stdin isn't a TTY
    # (e.g. `dcode --help`, the background hub process).
    global _PROMPT_SESSION
    if _PROMPT_SESSION is None:
        _PROMPT_SESSION = PromptSession()
    return _PROMPT_SESSION
_prompt_lock = threading.Lock()


class PromptInterrupted(Exception):
    """The prompt was closed from another thread (e.g. the web UI took action)."""


COMMANDS: dict[str, str] = {
    "/help": "Show commands",
    "/ui": "Show the web UI link",
    "/model": "Switch model + reasoning effort",
    "/agents": "Sub-agent settings",
    "/context": "Context window + usage",
    "/memory": "List saved memories",
    "/remember": "Save a memory: /remember <text>",
    "/permissions": "Tool permissions",
    "/experimental": "Feature flags",
    "/skills": "Pin skills to every prompt",
    "/review": "Review the git diff (sub-agent)",
    "/rename": "Rename this thread",
    "/new": "Save and start a new thread",
    "/resume": "Resume a saved thread",
    "/reset": "Clear conversation",
    "/setup": "Re-run setup",
    "/exit": "Quit",
}


class _SlashCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for command, meta in COMMANDS.items():
            if command.startswith(text):
                yield Completion(command, start_position=-len(text), display_meta=meta)


_COMMAND_COMPLETER = _SlashCommandCompleter()

_PROMPT_STYLE = Style.from_dict(
    {
        "": TEXT,
        "prompt": f"bold fg:{PRIMARY}",
        "hint": f"fg:{MUTED}",
        "completion-menu.completion": f"bg:{SURFACE_2} fg:#F8FAFC",
        "completion-menu.completion.current": f"bg:{PRIMARY} bold fg:#ffffff",
        "completion-menu.meta.completion": f"bg:{SURFACE_2} fg:{MUTED}",
        "completion-menu.meta.completion.current": f"bg:{PRIMARY} fg:#ffffff",
        "bottom-toolbar": f"bg:{SURFACE_2} fg:{MUTED}",
    }
)


# --- banner & static panels ------------------------------------------------------------

def logo_text() -> Text:
    """The Dcode mark — frame in blue, D in the terminal's foreground, prompt cursor in cyan."""
    text = Text()
    for index, line in enumerate(LOGO_LINES):
        if index:
            text.append("\n")
        for char in line:
            if char == "D":
                text.append(char, style=f"bold {TEXT}")
            elif char == "❯":
                text.append(char, style=f"bold {PRIMARY_SOFT}")
            elif char == "_":
                text.append(char, style=f"bold {ACCENT}")
            else:
                text.append(char, style=PRIMARY)
    return text


def show_welcome(
    current_model: str,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    git_summary: dict | None = None,
    ui_url: str | None = None,
    context_window: int | None = None,
    agents_enabled: bool = True,
) -> None:
    grid = Table.grid(padding=(0, 3), expand=True)
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)

    left = Text()
    left.append_text(logo_text())
    left.append("\n\nDcode", style=f"bold {TEXT}")
    left.append(f"  v{__version__}", style=MUTED)

    info = Table.grid(padding=(0, 2))
    info.add_column(style=MUTED, no_wrap=True)
    info.add_column(style=TEXT, overflow="fold")
    info.add_row("model", Text(current_model, style=f"bold {PRIMARY_SOFT}"))
    info.add_row("reasoning", reasoning_effort or "medium")
    if context_window:
        info.add_row("context", f"{context_window:,} tokens")
    info.add_row("agents", "sub-agents on" if agents_enabled else "single agent")
    info.add_row("cwd", os.getcwd())
    if base_url:
        info.add_row("endpoint", base_url)
    if git_summary and git_summary.get("is_repo"):
        info.add_row("git", f"{git_summary.get('branch', '-')} · {git_summary.get('modified_files', 0)} modified")
    if ui_url:
        info.add_row("web ui", Text(ui_url, style=f"underline {ACCENT}"))
    grid.add_row(left, info)

    console.print(
        Panel(
            grid,
            border_style=PRIMARY,
            title=f"[bold {PRIMARY_SOFT}]Dcode[/]",
            title_align="left",
            subtitle=f"[{MUTED}]/help for commands · ctrl-c to interrupt[/]",
            subtitle_align="right",
            padding=(1, 2),
        )
    )


def show_help() -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=f"bold {PRIMARY}", no_wrap=True)
    table.add_column(style=TEXT)
    for command, meta in COMMANDS.items():
        table.add_row(command, meta)
    console.print(Panel(table, title=f"[bold {PRIMARY_SOFT}]Commands[/]", title_align="left", border_style=PRIMARY))


def notice(text: str, level: str = "info") -> None:
    color = {"info": PRIMARY_SOFT, "warning": WARNING, "error": DANGER, "success": SUCCESS}.get(level, PRIMARY_SOFT)
    console.print(f"{PAD}[{color}]● {_escape(text)}[/]")


# --- input -----------------------------------------------------------------------------------

PAD = "  "  # left gutter for everything the agent prints
_ACTIVE_APP: Application | None = None  # selector / checklist currently on screen


def interrupt_prompt() -> None:
    """Close whatever prompt or menu is on screen, from any thread.

    Used when the web UI acts (answers an approval, sends a task) so the
    terminal loop can re-check the session state.
    """
    apps = [a for a in (_ACTIVE_APP, _PROMPT_SESSION.app if _PROMPT_SESSION else None) if a is not None]
    for app in apps:
        loop = getattr(app, "loop", None)
        if not app.is_running or loop is None:
            continue

        def _exit(app: Application = app) -> None:
            if app.is_running and not app.is_done:
                app.exit(exception=PromptInterrupted())

        try:
            loop.call_soon_threadsafe(_exit)
        except RuntimeError:
            pass


def _render_prompt_input(field: str | None, default: str, password: bool, completer: Completer | None) -> str:
    if field:
        console.print(f"[{PRIMARY_SOFT}]{field}[/]")
    with _prompt_lock, patch_stdout(raw=True):
        return _prompt_session().prompt(
            [("class:prompt", "❯ ")],
            default=default,
            is_password=password,
            style=_PROMPT_STYLE,
            completer=completer,
            complete_while_typing=completer is not None,
            mouse_support=False,
        )


def show_setup_prompt(field: str, default: str = "", password: bool = False) -> str:
    return _render_prompt_input(field=field, default=default, password=password, completer=None).strip()


def prompt_input(field: str, default: str = "", password: bool = False, complete_commands: bool = False) -> str:
    completer = _COMMAND_COMPLETER if complete_commands else None
    return _render_prompt_input(field=field, default=default, password=password, completer=completer).strip()


def _run_app(app: Application):
    global _ACTIVE_APP
    _ACTIVE_APP = app
    try:
        return app.run()
    finally:
        _ACTIVE_APP = None


def _max_rows() -> int:
    return max(5, min(14, shutil.get_terminal_size((80, 24)).lines - 7))


def _selector(
    title: str,
    options: Sequence[str],
    initial_index: int = 0,
    cancel_result: object | None = None,
    filterable: bool | None = None,
    allow_custom: bool = False,
    body_text: str = "",
    shortcuts: dict[str, int] | None = None,
    hint_text: str = "",
):
    """Arrow-key list that scrolls and (for long lists) filters as you type.

    Returns the chosen index, ``("custom", text)`` when ``allow_custom`` and the
    typed text matches nothing, or ``cancel_result``.
    """
    options = list(options)
    filterable = len(options) > 8 if filterable is None else filterable
    state = {"cursor": 0, "query": ""}

    def visible() -> list[int]:
        query = state["query"].lower()
        return [i for i, option in enumerate(options) if query in option.lower()] if query else list(range(len(options)))

    initial = max(0, min(initial_index, len(options) - 1)) if options else 0
    state["cursor"] = visible().index(initial) if options else 0
    kb = KeyBindings()

    def move(delta: int) -> None:
        count = len(visible())
        if count:
            state["cursor"] = (state["cursor"] + delta) % count

    kb.add("up")(lambda e: move(-1))
    kb.add("down")(lambda e: move(1))
    kb.add("pageup")(lambda e: move(-_max_rows()))
    kb.add("pagedown")(lambda e: move(_max_rows()))

    @kb.add("enter")
    def _confirm(event) -> None:
        items = visible()
        if items:
            event.app.exit(result=items[min(state["cursor"], len(items) - 1)])
        elif allow_custom and state["query"].strip():
            event.app.exit(result=("custom", state["query"].strip()))

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _cancel(event) -> None:
        event.app.exit(result=cancel_result)

    if filterable:
        @kb.add("<any>")
        def _type(event) -> None:
            if event.data and event.data.isprintable():
                state["query"] += event.data
                state["cursor"] = 0

        @kb.add("backspace")
        def _erase(event) -> None:
            state["query"] = state["query"][:-1]
            state["cursor"] = 0
    elif shortcuts:
        for key, index in shortcuts.items():
            kb.add(key)(lambda e, index=index: e.app.exit(result=index))

    def render() -> list:
        rows: list = []
        if body_text:
            rows.append((f"fg:{MUTED}", body_text + "\n"))
        if filterable:
            rows.append((f"fg:{MUTED}", "  search: "))
            rows.append(("bold", state["query"]))
            rows.append((f"fg:{PRIMARY}", "▏"))
            rows.append((f"fg:{MUTED}", f"   {len(visible())}/{len(options)}\n"))
        items = visible()
        if not items:
            hint = "  press enter to use this name" if allow_custom and state["query"] else "  no matches"
            rows.append((f"fg:{MUTED}", hint))
            return rows
        limit = _max_rows()
        cursor = min(state["cursor"], len(items) - 1)
        start = max(0, min(cursor - limit // 2, len(items) - limit))
        window = items[start:start + limit]
        if start:
            rows.append((f"fg:{MUTED}", f"    ↑ {start} more\n"))
        for position, index in enumerate(window, start=start):
            active = position == cursor
            rows.append((f"bold fg:{PRIMARY}" if active else "", f"  {'❯' if active else ' '} {options[index]}\n"))
        remaining = len(items) - (start + len(window))
        if remaining > 0:
            rows.append((f"fg:{MUTED}", f"    ↓ {remaining} more\n"))
        return rows

    keys = hint_text or ("↑↓ move · enter select · esc cancel" + (" · type to search" if filterable else ""))
    header = Window(height=1, content=FormattedTextControl(text=[(f"bold fg:{PRIMARY_SOFT}", title)]))
    body = Window(content=FormattedTextControl(text=render), wrap_lines=True)
    hint = Window(height=1, content=FormattedTextControl(text=[(f"fg:{MUTED}", keys)]))
    console.print()
    return _run_app(Application(layout=Layout(HSplit([header, body, hint])), key_bindings=kb, full_screen=False))


def select_one(title: str, options: Sequence[str], selected: str | None = None, allow_custom: bool = False) -> str:
    normalized = list(options)
    if not normalized:
        return selected or ""
    initial = normalized.index(selected) if selected in normalized else 0
    result = _selector(title, normalized, initial_index=initial, cancel_result=None, allow_custom=allow_custom)
    if isinstance(result, tuple):
        return result[1]
    return normalized[result] if isinstance(result, int) else (selected or "")


def select_model(models: list[str], selected: str | None = None) -> str:
    return select_one("Select model", models, selected, allow_custom=True)


def list_models_prompt(models: list[str], selected: str | None = None) -> str:
    return select_model(models=models, selected=selected)


def approval_prompt(label: str, detail: str, kind_label: str) -> str:
    """Ask to allow an action. Returns 'allow', 'session' or 'deny'.

    Raises PromptInterrupted if the web UI answered first.
    """
    options = ["Allow", f"Allow all {kind_label} for this session", "Deny"]
    detail_lines = detail.strip().splitlines() or [""]
    shown = "\n".join(f"    {line}" for line in detail_lines[:12])
    if len(detail_lines) > 12:
        shown += f"\n    … {len(detail_lines) - 12} more lines"
    result = _selector(
        f"⚠  {label}",
        options,
        cancel_result=2,
        filterable=False,
        body_text=shown + "\n",
        shortcuts={"y": 0, "a": 1, "n": 2},
        hint_text="↑↓ move · enter select · y allow · a allow for session · n deny",
    )
    return ("allow", "session", "deny")[result if isinstance(result, int) else 2]


def checklist_prompt(title: str, items: Sequence[tuple], hint_text: str = "↑↓ move · space toggle · enter save") -> dict[str, bool]:
    labels = [label for label, _ in items]
    values = [bool(enabled) for _, enabled in items]
    selected_index = [0]
    kb = KeyBindings()

    @kb.add("up")
    def _move_up(event) -> None:
        selected_index[0] = (selected_index[0] - 1) % len(labels)

    @kb.add("down")
    def _move_down(event) -> None:
        selected_index[0] = (selected_index[0] + 1) % len(labels)

    @kb.add(" ")
    def _toggle(event) -> None:
        values[selected_index[0]] = not values[selected_index[0]]

    @kb.add("enter")
    def _confirm(event) -> None:
        event.app.exit(result={label: values[index] for index, label in enumerate(labels)})

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _cancel(event) -> None:
        event.app.exit(result={label: enabled for label, enabled in items})

    def render_rows() -> list:
        limit = _max_rows()
        cursor = selected_index[0]
        start = max(0, min(cursor - limit // 2, len(labels) - limit))
        rows: list = []
        if start:
            rows.append((f"fg:{MUTED}", f"    ↑ {start} more\n"))
        for index in range(start, min(len(labels), start + limit)):
            active = index == cursor
            box = "◉" if values[index] else "○"
            rows.append((f"bold fg:{PRIMARY}" if active else "", f"  {'❯' if active else ' '} {box} {labels[index]}\n"))
        if start + limit < len(labels):
            rows.append((f"fg:{MUTED}", f"    ↓ {len(labels) - start - limit} more\n"))
        return rows

    header = Window(height=1, content=FormattedTextControl(text=[(f"bold fg:{PRIMARY_SOFT}", title)]))
    body = Window(content=FormattedTextControl(text=render_rows))
    hint = Window(height=1, content=FormattedTextControl(text=[(f"fg:{MUTED}", hint_text)]))
    console.print()
    return _run_app(Application(layout=Layout(HSplit([header, body, hint])), key_bindings=kb, full_screen=False))


def pick_thread(threads: Sequence[dict]) -> dict | None:
    if not threads:
        return None
    labels = [f"{item['name']}  ·  {str(item.get('updated_at', ''))[:16]}  ·  {len(item.get('messages') or [])} msgs" for item in threads]
    result = _selector("Resume thread", labels, cancel_result=None)
    return threads[result] if isinstance(result, int) else None


# --- event rendering ----------------------------------------------------------------------

def _agent_tag(agent: str) -> str:
    return "" if agent in ("main", "", None) else f"[{ACCENT}]{agent}[/] "


def _short_args(args: Any, limit: int = 100) -> str:
    if isinstance(args, dict):
        if "command" in args:
            text = str(args["command"])
        elif "path" in args and len(args) <= 3:
            text = str(args["path"]) + (
                f":{args.get('start_line')}-{args.get('end_line', '')}" if args.get("start_line") else ""
            )
        elif "task" in args:
            text = f"{args.get('role', '')}: {args['task']}"
        elif "title" in args:
            text = str(args["title"])
        elif "name" in args:
            text = str(args["name"])
        else:
            text = json.dumps(args, ensure_ascii=False)
    else:
        text = str(args)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _indent(agent: str) -> str:
    return PAD + ("    " if agent not in ("main", "", None) else "")


def show_tool_call(tool: str, args: Any, agent: str = "main") -> None:
    console.print(f"{_indent(agent)}[{ACCENT}]⏺[/] {_agent_tag(agent)}[bold {TEXT}]{tool}[/]  [{MUTED}]{_escape(_short_args(args))}[/]")


def show_tool_output(tool: str, output: str) -> None:
    if output:
        console.print(Padding(Panel(Text(output), title=f"{tool} output", border_style=MUTED, title_align="left"), (0, 0, 0, 2)))


def show_tool_result_brief(output: str, agent: str = "main") -> None:
    first = (output or "").strip().splitlines()[:1]
    lines = len((output or "").splitlines())
    summary = (first[0][:96] if first else "(no output)") + (f"  [+{lines - 1} lines]" if lines > 1 else "")
    console.print(f"{_indent(agent)}  [{MUTED}]⎿  {_escape(summary)}[/]")


def show_agent_start(agent: str, role: str, task: str) -> None:
    console.print()
    console.print(f"{PAD}[{ACCENT}]◆ {agent}[/]  [{MUTED}]{_escape(_short_args(task, 110))}[/]")


def show_agent_end(agent: str, ok: bool) -> None:
    color = SUCCESS if ok else WARNING
    console.print(f"{PAD}[{color}]◆ {agent} {'finished' if ok else 'stopped'}[/]")
    console.print()


def show_assistant(message: str) -> None:
    if message:
        console.print(Padding(Markdown(message, code_theme="monokai"), (1, 2, 0, 2)))


def show_note(message: str) -> None:
    if message:
        console.print(Padding(Text(message, style=MUTED), (1, 2, 0, 2)))


def show_user_echo(text: str, source: str) -> None:
    console.print()
    console.print(f"[{PRIMARY}]❯[/] [{TEXT}]{_escape(text)}[/]  [{MUTED}](from the {source} UI)[/]")


def show_stream_chunk(text: str) -> None:
    if text:
        console.print(text, end="")


def show_done(elapsed: float, ok: bool = True) -> None:
    """End-of-turn line: just how long it took, with breathing room around it."""
    mark = f"[{SUCCESS}]✓[/]" if ok else f"[{WARNING}]●[/]"
    console.print()
    console.print(f"{PAD}{mark} [{MUTED}]Done in {_fmt_elapsed(elapsed)}[/]")
    console.print()


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def show_summary(model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int, elapsed: float,
                 context_used: int = 0, context_window: int = 0) -> None:
    show_done(elapsed)


def _escape(text: str) -> str:
    return text.replace("[", "\\[")
