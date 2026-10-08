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

from . import __version__, mentions
from .theme import ACCENT, DANGER, LOGO_LINES, MUTED, PLAN, PRIMARY, PRIMARY_SOFT, SUCCESS, SURFACE_2, TEXT, WARNING
from .tools import workspace_root

MODE_COLORS = {"manual": MUTED, "accept_edits": PRIMARY, "auto": SUCCESS, "plan": PLAN}

# Kept for code that still refers to the old name.
ORANGE = PRIMARY

console = Console(
    theme=Theme(
        {
            "markdown.code": "bold",
            "markdown.code_block": TEXT,
            "markdown.h1": "bold underline",
            "markdown.h2": "bold",
            "markdown.h3": "bold",
            "markdown.link": PRIMARY_SOFT,
            "markdown.item.bullet": PRIMARY,
            "markdown.block_quote": MUTED,
        }
    ),
    highlight=False,
)

# Dcode's own spinner: the prompt chevrons marching, like the logo's cursor.
try:
    from rich.spinner import SPINNERS

    SPINNERS["dcode"] = {"interval": 110, "frames": ["❯    ", "❯❯   ", "❯❯❯  ", " ❯❯❯ ", "  ❯❯❯", "   ❯❯", "    ❯", "     "]}
    SPINNER = "dcode"
except Exception:  # pragma: no cover - very old rich
    SPINNER = "dots"

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
    "/clear": "Clear the screen and start a fresh chat (the old one is saved)",
    "/artifacts": "Browse artifacts and open one in your browser",
    "/jobs": "Background jobs: see output, stop them",
    "/cron": "Scheduled jobs: /cron · /cron add · /cron run|on|off|rm|logs <id>",
    "/db": "Databases: /db · /db add · /db <name> [table | SQL | summary | export …]",
    "/editor": "Write a long message in your editor ($EDITOR)",
    "/thinking": "Show or hide the model's reasoning",
    "/details": "Show or hide tool calls and their results",
    "/notify": "Ring or notify when a long task ends: /notify off|bell|desktop",
    "/version": "Show the Dcode version",
    "/mode": "Switch mode: manual · accept edits · auto · plan",
    "/init": "Write AGENTS.md: project commands, layout and conventions for Dcode",
    "/compact": "Summarize this session to free context: /compact [focus]",
    "/undo": "Undo Dcode's file changes from the last task",
    "/diff": "Show uncommitted git changes",
    "/commit": "Commit with a generated message: /commit [message]",
    "/guide": "Open the user guide in your browser (English / فارسی)",
    "/ui": "Open the web UI in your browser",
    "/model": "Switch model + reasoning effort",
    "/agents": "List helpers (use one with @name) · /agents settings",
    "/context": "Context window + usage",
    "/memory": "List saved memories",
    "/remember": "Save a memory: /remember <text>",
    "/permissions": "Tool permissions",
    "/rules": "Allow / ask / deny commands and files by pattern",
    "/hooks": "Show your hooks (shell commands at key moments)",
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


# Commands added at runtime (custom commands from .dcode/commands); shown after the built-ins.
EXTRA_COMMANDS: dict[str, str] = {}
# Helpers (name -> description) offered when a message starts with "@".
HELPERS: dict[str, str] = {}


def command_items() -> dict[str, str]:
    return {**COMMANDS, **{k: v for k, v in EXTRA_COMMANDS.items() if k not in COMMANDS}}


class _SlashCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for command, meta in command_items().items():
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
        "input": "",
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
    mode: str = "accept_edits",
) -> None:
    """Compact Claude-Code-style header: small mark + three lines, then ▎ notices."""
    from urllib.parse import urlsplit

    from .agents import MODE_SHORT

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    shown_cwd = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    context = f"{context_window // 1024}K context" if context_window else ""
    host = urlsplit(base_url).netloc if base_url else ""
    line2 = " · ".join(x for x in (current_model, context, host) if x)
    git = ""
    if git_summary and git_summary.get("is_repo"):
        modified = git_summary.get("modified_files", 0)
        git = f"  ⎇ {git_summary.get('branch', '-')}" + (f" · {modified} modified" if modified else "")
    rows = [
        (f"[bold]Dcode[/] [{MUTED}]v{__version__}[/]"),
        (f"[{MUTED}]{_escape(line2)}[/]"),
        (f"[{MUTED}]{_escape(shown_cwd)}{_escape(git)}[/]"),
    ]
    mark = logo_text().split("\n")
    console.print()
    for index, row in enumerate(rows):
        logo_line = mark[index] if index < len(mark) else Text(" " * 9)
        line = Text("  ")
        line.append_text(logo_line)
        line.append("   ")
        line.append_text(Text.from_markup(row))
        console.print(line, overflow="ellipsis", no_wrap=True)
    console.print()
    if ui_url:
        # Own line, unwrapped: terminals detect it; OSC 8 makes it clickable too.
        console.print(f"[{PRIMARY}]▎[/] [{MUTED}]Web UI[/]  {link(ui_url)}  [{MUTED}](/ui opens it)[/]", soft_wrap=True)
    tips = (f"[{PRIMARY}]▎[/] [{MUTED}]Mode: {MODE_SHORT.get(mode, mode)} · Shift+Tab to switch · "
            f"/help for commands · helpers {'on' if agents_enabled else 'off'}[/]")
    console.print(tips)


def push_to_bottom(used_lines: int) -> None:
    """Pad with blank lines so the input box sits at the bottom of a fresh screen (like Claude Code).

    ``used_lines`` = lines already printed (header) + lines the input box will take.
    """
    height = shutil.get_terminal_size((80, 24)).lines
    blank = height - used_lines
    if blank > 0:
        console.print("\n" * (blank - 1))


def show_help() -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=f"bold {PRIMARY}", no_wrap=True)
    table.add_column(style=TEXT)
    for command, meta in command_items().items():
        table.add_row(command, meta)
    console.print(Panel(table, title="[bold]Commands[/]", title_align="left", border_style=MUTED))
    console.print(f"{PAD}[{MUTED}]!command runs a shell command yourself (Dcode sees the output) · "
                  f"@file attaches a file · @helper task runs a helper[/]")


def notice(text: str, level: str = "info") -> None:
    dot = {"info": PRIMARY, "warning": WARNING, "error": DANGER, "success": SUCCESS}.get(level, PRIMARY)
    body = {"warning": WARNING, "error": DANGER}.get(level, TEXT)  # info/success text stays white
    console.print(f"{PAD}[{dot}]●[/] [{body}]{_escape(text)}[/]")


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
        console.print(f"[bold]{field}[/]")
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


class ExitRequested(Exception):
    """Ctrl+C pressed twice (or Ctrl+D) on an empty input box."""


class ExpandRequested(Exception):
    """Ctrl+O: show the full output of the last task."""


_HISTORY = None
_EXIT_WINDOW = 2.0


PLACEHOLDERS = [
    'Try "edit <filepath> to..."',
    'Try "fix the failing test"',
    'Try "explain how this project is structured"',
    'Try "add input validation to <file>"',
    'Try "review my uncommitted changes"',
    'Try "write tests for <module>"',
]


def chat_input(
    status: Any = "",
    placeholder: str = "",
    mode: Any = None,
    on_cycle_mode: Any = None,
) -> str:
    """Full-width input box with a rounded border that follows the content.

    * Enter sends · Alt/Option+Enter (or "\\" then Enter) inserts a newline · ↑/↓ history
    * Ctrl+C clears the text; on an empty box, press it twice to exit
    * The border is blue, cyan while typing a /command, amber when exit is armed
    """
    import time as _time

    from prompt_toolkit.filters import Condition
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import TextArea

    global _HISTORY
    if _HISTORY is None:
        _HISTORY = InMemoryHistory()
    if not placeholder:
        import random

        placeholder = random.choice(PLACEHOLDERS)
    state = {"armed": 0.0}
    area = TextArea(
        multiline=True, wrap_lines=True,
        history=_HISTORY, height=Dimension(min=1, max=12), dont_extend_height=True,
        prompt=[("class:prompt", "❯ ")], style="class:input",
        get_line_prefix=lambda line, wrap: [("", "  ")] if line or wrap else [],
    )

    def armed() -> bool:
        return _time.monotonic() - state["armed"] < _EXIT_WINDOW

    def border() -> str:
        if armed():
            return f"fg:{WARNING}"
        if area.text.startswith(("/", "!")):
            return f"fg:{PRIMARY}"  # a hint that this is a command
        return f"fg:{MUTED}"

    def rule() -> Window:
        return Window(height=1, char="─", style=border)

    def top_rule() -> Window:
        """Dcode's signature: the brand on the left, the mode badge on the right."""
        from prompt_toolkit.application.current import get_app

        from .agents import MODE_SHORT

        def fragments() -> list:
            try:
                cols = get_app().output.get_size().columns
            except Exception:
                cols = shutil.get_terminal_size((100, 24)).columns
            current = mode() if callable(mode) else (mode or "")
            badge = f" ◆ {MODE_SHORT.get(current, current)} " if current else ""
            left, brand = "── ", "❯ dcode "
            fill = max(1, cols - len(left) - len(brand) - len(badge) - 2)
            line = border()
            color = f"bold fg:{MODE_COLORS.get(current, PRIMARY)}" if not armed() else f"bold fg:{WARNING}"
            return [(line, left), (f"bold fg:{PRIMARY}", "❯ "), ("bold", "dcode "), (line, "─" * fill),
                    (color, badge), (line, "──")]

        return Window(FormattedTextControl(fragments), height=1)

    placeholder_win = ConditionalContainer(
        Window(FormattedTextControl([(f"fg:{MUTED}", placeholder)]), height=1),
        filter=Condition(lambda: not area.text),
    )
    body = FloatContainer(area, floats=[Float(content=placeholder_win, left=2, top=0, transparent=True)])

    state["sel"] = 0

    def suggestions() -> list[tuple[str, str]]:
        """/commands while typing one; project files after an "@" (fuzzy)."""
        text = area.text
        if text.startswith("/") and " " not in text and "\n" not in text:
            return [(command, meta) for command, meta in command_items().items() if command.startswith(text)]
        token = mentions.typing_token(area.buffer.document.text_before_cursor)
        if token is None:
            return []
        if state.get("files_for") != token:  # cache per token: this runs on every repaint
            state["files_for"] = token
            helpers = []
            if text.lstrip() == "@" + token:  # a message starting with @name runs that helper
                helpers = [("@" + name, f"helper · {desc[:60]}") for name, desc in HELPERS.items()
                           if name.startswith(token.lower())]
            try:
                files = [("@" + f, "folder" if f.endswith("/") else "file")
                         for f in mentions.complete(token, workspace_root())]
            except OSError:
                files = []
            state["files"] = (helpers + files)[:8]
        return state["files"]

    def accept_suggestion(item: str) -> None:
        buffer = area.buffer
        if item.startswith("@"):
            token = mentions.typing_token(buffer.document.text_before_cursor) or ""
            buffer.delete_before_cursor(len(token) + 1)
            buffer.insert_text(item + ("" if item.endswith("/") else " "))
        else:
            buffer.text = item + " "
            buffer.cursor_position = len(buffer.text)

    def on_change(_buffer) -> None:
        state["sel"] = 0

    area.buffer.on_text_changed += on_change
    showing = Condition(lambda: bool(suggestions()))

    def render_suggestions() -> list:
        items = suggestions()[:8]
        selected = min(state["sel"], len(items) - 1)
        width = max((len(c) for c, _ in items), default=8) + 4
        rows: list = []
        for index, (command, meta) in enumerate(items):
            active = index == selected
            rows.append((f"bold fg:{PRIMARY}" if active else "", f"  {command.ljust(width)}"))
            rows.append(("" if active else f"fg:{MUTED}", meta + "\n"))
        return rows

    def status_line() -> list:
        if armed():
            return [(f"fg:{WARNING}", "  Press Ctrl+C again to exit")]
        text = status() if callable(status) else status
        return [(f"fg:{MUTED}", f"  {text}")] if text else []

    kb = KeyBindings()

    # Enter sends. Ctrl+J sends too: keys typed while Dcode was busy arrive with
    # Enter translated to a line feed by the terminal (it was not in raw mode yet).
    @kb.add("up", filter=showing)
    def _sugg_up(event) -> None:
        state["sel"] = (state["sel"] - 1) % len(suggestions()[:8])

    @kb.add("down", filter=showing)
    def _sugg_down(event) -> None:
        state["sel"] = (state["sel"] + 1) % len(suggestions()[:8])

    @kb.add("tab", filter=showing)
    def _sugg_complete(event) -> None:
        items = suggestions()[:8]
        accept_suggestion(items[min(state["sel"], len(items) - 1)][0])

    @kb.add("enter")
    @kb.add("c-j")
    def _submit(event) -> None:
        buffer = area.buffer
        items = suggestions()[:8]
        if items and items[0][0].startswith("@"):  # Enter picks the highlighted file
            accept_suggestion(items[min(state["sel"], len(items) - 1)][0])
            return
        if items:  # run the highlighted command
            buffer.text = items[min(state["sel"], len(items) - 1)][0]
        if buffer.document.text_before_cursor.endswith("\\"):  # "\" + Enter → newline
            buffer.delete_before_cursor(1)
            buffer.insert_text("\n")
            return
        if area.text.strip():
            buffer.append_to_history()
            event.app.exit(result=area.text)

    @kb.add("escape", "enter")
    def _newline(event) -> None:
        area.buffer.insert_text("\n")

    @kb.add("c-c")
    def _ctrl_c(event) -> None:
        if area.text:
            area.buffer.reset()
        elif armed():
            event.app.exit(exception=ExitRequested())
        else:
            state["armed"] = _time.monotonic()

            def _disarm() -> None:  # repaint once the window passes
                _time.sleep(_EXIT_WINDOW + 0.05)
                event.app.invalidate()

            threading.Thread(target=_disarm, daemon=True).start()

    @kb.add("c-d")
    def _ctrl_d(event) -> None:
        if not area.text:
            event.app.exit(exception=ExitRequested())

    @kb.add("s-tab")
    def _cycle(event) -> None:
        if on_cycle_mode is not None:
            on_cycle_mode()
            event.app.invalidate()

    @kb.add("c-o")
    def _expand(event) -> None:
        event.app.exit(exception=ExpandRequested())

    root = HSplit([
        top_rule(), body, rule(),
        ConditionalContainer(Window(FormattedTextControl(render_suggestions), dont_extend_height=True), filter=showing),
        ConditionalContainer(Window(FormattedTextControl(status_line), height=1), filter=~showing),
    ])
    app = Application(
        layout=Layout(root, focused_element=area), key_bindings=kb, style=_PROMPT_STYLE,
        full_screen=False, erase_when_done=True, mouse_support=False,
    )
    console.print()
    with _prompt_lock, patch_stdout(raw=True):
        text = _run_app(app)
    return (text or "").strip()


def echo_user(text: str) -> None:
    """Print a submitted message into the transcript (the input box erases itself)."""
    lines = text.splitlines() or [""]
    console.print(f"[bold {PRIMARY}]❯[/] [bold]{_escape(lines[0])}[/]")
    for line in lines[1:]:
        console.print(f"  [bold]{_escape(line)}[/]")


# Terminals that render OSC 8 hyperlinks. macOS Terminal doesn't (it detects plain URLs
# on ⌘-double-click instead), and some terminals mangle the escape codes.
_OSC8_TERMINALS = {"vscode", "iTerm.app", "WezTerm", "ghostty", "Hyper", "Tabby", "rio", "WarpTerminal"}


def supports_hyperlinks() -> bool:
    if os.environ.get("DCODE_HYPERLINKS") in ("0", "1"):
        return os.environ["DCODE_HYPERLINKS"] == "1"
    return (os.environ.get("TERM_PROGRAM") in _OSC8_TERMINALS or "KITTY_WINDOW_ID" in os.environ
            or "WT_SESSION" in os.environ or bool(os.environ.get("VTE_VERSION")))


def link(url: str, label: str | None = None) -> str:
    """Clickable link where the terminal supports it; plain (still detectable) text elsewhere."""
    text = _escape(label or url)
    return f"[link={url}]{text}[/link]" if supports_hyperlinks() else text


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the default browser (the web UI and guide run on this machine)."""
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


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
    erase: bool = False,
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
            rows.append((f"bold fg:{PRIMARY}" if active else "", f"  {'❯' if active else ' '} "))
            rows.append(("bold" if active else "", f"{options[index]}\n"))
        remaining = len(items) - (start + len(window))
        if remaining > 0:
            rows.append((f"fg:{MUTED}", f"    ↓ {remaining} more\n"))
        return rows

    keys = hint_text or ("↑↓ move · enter select · esc cancel" + (" · type to search" if filterable else ""))
    header = Window(height=1, content=FormattedTextControl(text=[("bold", title)]))
    body = Window(content=FormattedTextControl(text=render), wrap_lines=True)
    hint = Window(height=1, content=FormattedTextControl(text=[(f"fg:{MUTED}", keys)]))
    if not erase:
        console.print()
    return _run_app(Application(layout=Layout(HSplit([header, body, hint])), key_bindings=kb,
                                full_screen=False, erase_when_done=erase))


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


def approval_prompt(label: str, detail: str, kind_label: str, session_option: bool = True) -> str:
    """Ask to allow an action. Returns 'allow', 'session' or 'deny'.

    Raises PromptInterrupted if the web UI answered first.
    """
    if not session_option:  # some things are asked every single time (database changes)
        return _approve_once(label, detail)
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
        erase=True,  # once answered, only the one-line result stays in the transcript
    )
    return ("allow", "session", "deny")[result if isinstance(result, int) else 2]


def _approve_once(label: str, detail: str) -> str:
    detail_lines = detail.strip().splitlines() or [""]
    shown = "\n".join(f"    {line}" for line in detail_lines[:20])
    if len(detail_lines) > 20:
        shown += f"\n    … {len(detail_lines) - 20} more lines"
    result = _selector(f"⚠  {label}", ["Allow this once", "Deny"], cancel_result=1, filterable=False,
                       body_text=shown + "\n", shortcuts={"y": 0, "n": 1},
                       hint_text="↑↓ move · enter select · y allow · n deny · asked every time", erase=True)
    return "allow" if result == 0 else "deny"


def question_prompt(question: str, options: Sequence[str]) -> str:
    """The model asks something. Returns the answer, or "" if skipped (Esc).

    Raises PromptInterrupted if the web UI answered first.
    """
    typed = "Type my own answer…"
    choices = list(options) + [typed]
    result = _selector(f"?  {question}", choices, cancel_result=None, filterable=False,
                       hint_text="↑↓ move · enter select · esc skip", erase=True)
    if result is None:
        answer = ""
    elif choices[result] == typed:
        answer = prompt_input("Your answer")
    else:
        answer = choices[result]
    shown = answer or "skipped — Dcode will decide"
    console.print(f"{PAD}  [{PRIMARY}]?[/] [{MUTED}]{_escape(_short_args(question, 70))} →[/] {_escape(shown)}")
    return answer


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
            rows.append((f"bold fg:{PRIMARY}" if active else "", f"  {'❯' if active else ' '} {box} "))
            rows.append(("bold" if active else "", f"{labels[index]}\n"))
        if start + limit < len(labels):
            rows.append((f"fg:{MUTED}", f"    ↓ {len(labels) - start - limit} more\n"))
        return rows

    header = Window(height=1, content=FormattedTextControl(text=[("bold", title)]))
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
            text = str(args["command"]) + ("  (background)" if args.get("background") else "")
        elif "path" in args and len(args) <= 3:
            text = str(args["path"]) + (
                f":{args.get('start_line')}-{args.get('end_line', '')}" if args.get("start_line") else ""
            )
        elif "sql" in args:
            text = f"{args.get('connection', '')}: {args['sql']}"
        elif "task" in args:
            text = f"{args.get('role', '')}: {args['task']}"
        elif "title" in args:
            text = str(args["title"])
        elif "content" in args:  # e.g. an untitled artifact_save: don't dump the payload
            text = f"({len(str(args['content']))} chars)"
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
    console.print(f"{_indent(agent)}[{ACCENT}]◇[/] {_agent_tag(agent)}[bold {TEXT}]{tool}[/]  [{MUTED}]{_escape(_short_args(args))}[/]")


def show_tool_output(tool: str, output: str) -> None:
    if output:
        console.print(Padding(Panel(Text(output), title=f"{tool} output", border_style=MUTED, title_align="left"), (0, 0, 0, 2)))


def show_decision(choice: str, detail: str) -> None:
    """One-line record of an approval answer (the menu itself erases)."""
    color, word = {"allow": (SUCCESS, "Allowed"), "session": (SUCCESS, "Allowed for this session"),
                   "deny": (WARNING, "Denied")}.get(choice, (MUTED, choice))
    mark = "✓" if choice != "deny" else "✕"
    console.print(f"{PAD}  [{color}]{mark} {word}[/] [{MUTED}]· {_escape(_short_args(detail, 90))}[/]")


def show_edit_diff(old: str, new: str, agent: str = "main", limit: int = 6) -> None:
    """Compact red/green preview of an edit_file change."""
    pad = _indent(agent) + "     "
    old_lines, new_lines = old.splitlines(), new.splitlines()
    for line in old_lines[:limit]:
        console.print(f"{pad}[{DANGER}]- {_escape(line[:110])}[/]", highlight=False)
    if len(old_lines) > limit:
        console.print(f"{pad}[{MUTED}]  … {len(old_lines) - limit} more removed[/]")
    for line in new_lines[:limit]:
        console.print(f"{pad}[{SUCCESS}]+ {_escape(line[:110])}[/]", highlight=False)
    if len(new_lines) > limit:
        console.print(f"{pad}[{MUTED}]  … {len(new_lines) - limit} more added (ctrl+o to expand)[/]")


def show_tool_result_lines(output: str, agent: str = "main", lines: int = 3) -> None:
    """First few lines of an output, with a hint to expand the rest."""
    text = (output or "").strip() or "(no output)"
    all_lines = text.splitlines()
    pad = _indent(agent)
    for index, line in enumerate(all_lines[:lines]):
        prefix = "╰─ " if index == 0 else "   "
        console.print(f"{pad}  [{MUTED}]{prefix}{_escape(line[:120])}[/]", highlight=False)
    if len(all_lines) > lines:
        console.print(f"{pad}     [{MUTED}]… +{len(all_lines) - lines} lines (ctrl+o to expand)[/]")


def show_diff(diff: str, limit: int = 400) -> None:
    """Colored git diff."""
    lines = diff.splitlines()
    console.print()
    for line in lines[:limit]:
        style = (SUCCESS if line.startswith("+") and not line.startswith("+++") else
                 DANGER if line.startswith("-") and not line.startswith("---") else
                 ACCENT if line.startswith("@@") else
                 f"bold {TEXT}" if line.startswith(("diff --git", "+++", "---")) else MUTED)
        console.print(f"{PAD}[{style}]{_escape(line)}[/]", highlight=False)
    if len(lines) > limit:
        console.print(f"{PAD}[{MUTED}]… {len(lines) - limit} more lines[/]")
    console.print()


def show_expanded(entries: list) -> None:
    """Ctrl+O: full outputs of the last task's tool calls."""
    if not entries:
        notice("Nothing to expand yet.")
        return
    console.print()
    for name, args, output in entries:
        title = f"{name}  {_short_args(args, 90)}"
        console.print(Padding(Panel(Text(output or "(no output)"), title=_escape(title), title_align="left",
                                    border_style=MUTED, padding=(0, 1)), (0, 2, 0, 2)))
    console.print()


def show_tool_result_brief(output: str, agent: str = "main") -> None:
    first = (output or "").strip().splitlines()[:1]
    lines = len((output or "").splitlines())
    summary = (first[0][:96] if first else "(no output)") + (f"  [+{lines - 1} lines]" if lines > 1 else "")
    console.print(f"{_indent(agent)}  [{MUTED}]╰─ {_escape(summary)}[/]")


def show_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]], limit: int = 40, cell: int = 48) -> None:
    """A query result as a table: white text, structure in gray."""
    table = Table(box=None, padding=(0, 2, 0, 0), header_style=f"bold {TEXT}", pad_edge=False)
    for column in columns:
        table.add_column(_escape(str(column)), overflow="ellipsis", no_wrap=True, max_width=cell)
    for row in rows[:limit]:
        table.add_row(*[Text("NULL", style=MUTED) if v is None else Text(str(v).replace("\n", "⏎")[:cell * 2]) for v in row])
    console.print(Padding(table, (0, 0, 0, len(PAD) + 2)))


def show_todos(items: list) -> None:
    """The model's checklist: done items dimmed, the current one bold."""
    done = sum(1 for i in items if i.get("status") == "done")
    console.print(f"{PAD}[{PRIMARY}]☰[/] [bold]Checklist[/] [{MUTED}]{done}/{len(items)} done[/]")
    for item in items:
        text = _escape(str(item.get("text", "")))
        status = item.get("status")
        if status == "done":
            console.print(f"{PAD}  [{SUCCESS}]✓[/] [{MUTED} strike]{text}[/]")
        elif status == "in_progress":
            console.print(f"{PAD}  [{PRIMARY}]▸[/] [bold]{text}[/]")
        else:
            console.print(f"{PAD}  [{MUTED}]○[/] {text}")


def show_agent_start(agent: str, role: str, task: str, model: str = "") -> None:
    console.print()
    on = f" [{MUTED}]on {_escape(model)}[/]" if model else ""
    console.print(f"{PAD}[{ACCENT}]◆ {agent}[/]{on}  [{MUTED}]{_escape(_short_args(task, 110))}[/]")


def show_agent_end(agent: str, ok: bool, elapsed: float | None = None) -> None:
    color = SUCCESS if ok else WARNING
    took = f" [{MUTED}]· {_fmt_elapsed(elapsed)}[/]" if elapsed is not None else ""
    console.print(f"{PAD}[{color}]◆ {agent} {'finished' if ok else 'stopped'}[/]{took}")
    console.print()


def show_assistant(message: str) -> None:
    """Answers get a blue gutter — easy to spot when scrolling back through tool output."""
    if not message:
        return
    width = max(40, console.width - 6)
    lines = console.render_lines(Markdown(message, code_theme="monokai"), console.options.update(width=width), pad=False)
    console.print()
    for line in lines:
        row = Text()
        row.append("  ▎ ", style=PRIMARY)  # only the gutter is blue; the answer stays white
        row.append_text(_segments_text(line))
        console.print(row, soft_wrap=True)  # one print per line: partial lines break under live spinners


def _segments_text(segments: list) -> Text:
    text = Text()
    for segment in segments:
        if not segment.control:
            text.append(segment.text, style=segment.style)
    return text


def show_thinking(text: str, agent: str = "main", limit: int = 24) -> None:
    """The model's reasoning (/thinking): dim, so it never reads like the answer."""
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    if not lines:
        return
    pad = _indent(agent)
    console.print(f"{pad}[{MUTED}]✻ {_agent_tag_plain(agent)}thinking[/]")
    for line in lines[:limit]:
        console.print(f"{pad}  [{MUTED} italic]{_escape(line)}[/]", highlight=False, soft_wrap=True)
    if len(lines) > limit:
        console.print(f"{pad}  [{MUTED}]… +{len(lines) - limit} lines[/]")


def _agent_tag_plain(agent: str) -> str:
    return "" if agent in ("main", "", None) else f"{agent} "


NOTIFY_CHOICES = ["off", "bell", "desktop"]


def ring(kind: str, message: str = "") -> None:
    """Get the user's attention: a terminal bell, or a desktop notification where the terminal has one.

    "desktop" sends OSC 9 (iTerm2, kitty, WezTerm, ghostty, Windows Terminal) and OSC 777 (others);
    terminals that know neither ignore them, so the bell is sent too.
    """
    import sys

    stream = sys.__stdout__
    if kind not in ("bell", "desktop") or stream is None or not stream.isatty():
        return
    text = "".join(ch for ch in message if ch.isprintable() and ch != ";")[:120]
    codes = "\a"
    if kind == "desktop" and text:
        codes = f"\x1b]9;{text}\x07\x1b]777;notify;Dcode;{text}\x07" + codes
    try:
        stream.write(codes)
        stream.flush()
    except OSError:
        pass


def edit_in_editor(initial: str = "") -> str:
    """Open $VISUAL / $EDITOR on a scratch file and return what was written ("" if nothing)."""
    import shlex
    import subprocess
    import tempfile

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or next(
        (name for name in ("nano", "vim", "vi") if shutil.which(name)), "")
    if not editor:
        raise RuntimeError("No editor found. Set EDITOR, e.g. export EDITOR=nano")
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="dcode-message-", delete=False) as handle:
        handle.write(initial)
        path = handle.name
    try:
        subprocess.run([*shlex.split(editor), path], check=False)
        with open(path, errors="replace") as handle:
            return handle.read().strip()
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Couldn't run the editor ({editor}): {exc}") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def show_note(message: str) -> None:
    if message:
        console.print(Padding(Text(message, style=MUTED), (1, 2, 0, 2)))


def show_user_echo(text: str, source: str) -> None:
    console.print()
    console.print(f"[{PRIMARY}]❯[/] [{TEXT}]{_escape(text)}[/]  [{MUTED}](from the {source} UI)[/]")


def show_stream_chunk(text: str) -> None:
    if text:
        console.print(text, end="")


def show_done(elapsed: float, ok: bool = True, report: dict | None = None) -> None:
    """End-of-turn line: elapsed time plus what changed, with breathing room."""
    report = report or {}
    mark = f"[{SUCCESS}]✓[/]" if ok else f"[{WARNING}]●[/]"
    parts = [f"Done in {_fmt_elapsed(elapsed)}"]
    files = report.get("files_changed") or []
    if files:
        parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''} changed")
    commands = int(report.get("commands") or 0)
    if commands:
        failed = int(report.get("commands_failed") or 0)
        parts.append(f"{commands} command{'s' if commands != 1 else ''}" + (f" ({failed} failed)" if failed else ""))
    helpers = int(report.get("helpers") or 0)
    if helpers:
        parts.append(f"{helpers} helper{'s' if helpers != 1 else ''}")
    console.print()
    console.print(f"{PAD}{mark} [{MUTED}]{' · '.join(parts)}[/]")
    if files:
        shown = ", ".join(files[:8]) + (f" +{len(files) - 8} more" if len(files) > 8 else "")
        console.print(f"{PAD}  [{MUTED}]{_escape(shown)}[/]")
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
