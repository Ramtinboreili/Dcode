"""Your own slash commands, as Markdown prompt templates.

``~/.dcode/commands/review-pr.md`` (all projects) or
``<project>/.dcode/commands/review-pr.md`` (that project; wins on name clashes)
becomes ``/review-pr``::

    ---
    description: Review my branch against main
    agent: review            # optional: run it as this helper instead of in the chat
    ---
    Review these changes for bugs and missing tests. Focus: $ARGUMENTS
    !`git diff main...HEAD --stat`
    Conventions are in @CONTRIBUTING.md

* ``$ARGUMENTS`` is everything after the command; ``$1``…``$9`` are its words.
* ``!`cmd``` is replaced by the command's output when you run it. Those
  commands follow the same mode and rules as the agent's own (they ask first
  unless allowed).
* ``@path`` attaches a file, like in a normal message.

Built-in commands keep their names; a custom command can't replace ``/exit``.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import home_dir
from .skills import parse_frontmatter

SHELL = re.compile(r"!`([^`\n]+)`")
_NAME = re.compile(r"[^a-z0-9_-]+")
SHELL_TIMEOUT = 30
SHELL_OUTPUT_CHARS = 4000


@dataclass
class Command:
    name: str
    description: str
    template: str
    agent: str = ""
    scope: str = "user"
    path: Path | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "agent": self.agent, "scope": self.scope}


def commands_dir() -> Path:
    return home_dir() / "commands"


def normalize_name(name: str) -> str:
    return _NAME.sub("-", name.lower().removesuffix(".md").lstrip("/")).strip("-")[:40]


def load_commands(workspace: Path | None = None) -> dict[str, Command]:
    found: dict[str, Command] = {}
    folders = [(commands_dir(), "user")]
    if workspace is not None:
        folders.append((workspace / ".dcode" / "commands", "project"))
    for folder, scope in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = normalize_name(path.stem)
            if not name or not body.strip():
                continue
            first = next((ln.strip("#-* ") for ln in body.splitlines() if ln.strip()), "")
            found[name] = Command(name=name, description=(meta.get("description") or first)[:100],
                                  template=body.strip(), agent=meta.get("agent", "").strip().lower(),
                                  scope=scope, path=path)
    return found


def fill_arguments(template: str, arguments: str) -> str:
    try:
        words = shlex.split(arguments)
    except ValueError:
        words = arguments.split()
    text = template
    for index in range(9, 0, -1):  # $10 isn't supported; go high to low so $1 doesn't eat $12
        text = text.replace(f"${index}", words[index - 1] if index <= len(words) else "")
    text = text.replace("$ARGUMENTS", arguments.strip())
    if arguments.strip() and "$ARGUMENTS" not in template and not re.search(r"\$[1-9]", template):
        text += f"\n\n{arguments.strip()}"  # don't silently drop what the user typed
    return text


def run_shell(command: str, cwd: Path) -> str:
    try:
        result = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True,
                                timeout=SHELL_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return f"(timed out after {SHELL_TIMEOUT}s)"
    except OSError as exc:
        return f"(could not run: {exc})"
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if len(output) > SHELL_OUTPUT_CHARS:
        output = output[:SHELL_OUTPUT_CHARS] + f"\n… (truncated, {len(output) - SHELL_OUTPUT_CHARS} more chars)"
    return output or "(no output)"


def expand(command: Command, arguments: str, cwd: Path, may_run: Callable[[str], str | None]) -> str:
    """The prompt to send: arguments filled in, ``!`cmd``` replaced by output.

    ``may_run(cmd)`` returns None to run it, or the text to put there instead (denied).
    """
    text = fill_arguments(command.template, arguments)

    def shell(match: re.Match) -> str:
        cmd = match.group(1).strip()
        refusal = may_run(cmd)
        output = refusal if refusal is not None else run_shell(cmd, cwd)
        return f"\n```\n$ {cmd}\n{output}\n```\n"

    return SHELL.sub(shell, text).strip()
