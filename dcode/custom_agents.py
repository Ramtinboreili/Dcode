"""Helpers (sub-agents) you define yourself, as Markdown files.

Drop a file in ``~/.dcode/agents/`` (all projects) or ``<project>/.dcode/agents/``
(that project; wins on name clashes). The file name is the helper's name::

    ---
    description: Reviews Ansible playbooks for idempotency and secrets
    tools: read_file, search_in_files, list_files, run_command
    model: qwen2.5-coder:7b
    steps: 20
    ---
    You review Ansible code. Check every task for idempotency, ...

* ``description`` (required) tells the main agent when to use it.
* ``tools``: comma-separated tool names, or ``read-only`` (the default) or ``edit``.
* ``model``: run this helper on another model — e.g. a small fast one for
  searching, while the main agent uses a bigger one.
* ``steps``: how many tool steps it may take before it must report.
* The body is the helper's instructions.

A file named after a built-in helper (explore, code, review, test) changes it:
any field you leave out keeps the built-in value. Invoke a helper yourself by
starting a message with ``@name``, or let the main agent pick it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import agents as roles
from .config import home_dir
from .skills import parse_frontmatter
from .tools import READ_ONLY_TOOL_NAMES, TOOL_SCHEMAS

_HELPER_EXTRAS = ["memory_search", "skill_use"]
# Tools a helper may be given. Main-agent tools (delegate, artifacts, todos) stay out on purpose.
HELPER_TOOLS = set(TOOL_SCHEMAS) | set(_HELPER_EXTRAS)
READ_ONLY_SET = set(READ_ONLY_TOOL_NAMES) | set(_HELPER_EXTRAS)
_NAME = re.compile(r"[^a-z0-9_-]+")


@dataclass
class AgentSpec:
    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    prompt: str = ""        # "" = the built-in prompt for this role
    model: str = ""         # "" = the configured helper model / main model
    steps: int = 0          # 0 = the configured default
    scope: str = "builtin"  # builtin, user or project
    path: Path | None = None

    @property
    def read_only(self) -> bool:
        return set(self.tools) <= READ_ONLY_SET

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "tools": self.tools, "model": self.model,
                "steps": self.steps, "scope": self.scope, "path": str(self.path) if self.path else "",
                "custom_prompt": bool(self.prompt), "read_only": self.read_only}


def agents_dir() -> Path:
    return home_dir() / "agents"


def normalize_name(name: str) -> str:
    return _NAME.sub("-", name.lower().removesuffix(".md")).strip("-")[:40]


def builtin_specs() -> dict[str, AgentSpec]:
    return {name: AgentSpec(name, roles.ROLE_DESCRIPTIONS[name], list(tools))
            for name, tools in roles.ROLE_TOOLS.items()}


def _tools(value: str, fallback: list[str]) -> list[str]:
    value = value.strip().strip("[]")
    if not value:
        return list(fallback)
    if value.lower() in ("read-only", "readonly", "read"):
        return READ_ONLY_TOOL_NAMES + _HELPER_EXTRAS
    if value.lower() in ("edit", "write", "all"):
        return list(roles.ROLE_TOOLS["code"])
    names = [n.strip().strip("'\"") for n in value.split(",")]
    return [n for n in dict.fromkeys(names) if n in HELPER_TOOLS] or list(fallback)


def _steps(value: str) -> int:
    try:
        return max(1, min(200, int(value)))
    except (TypeError, ValueError):
        return 0


def _load(path: Path, scope: str, base: AgentSpec | None) -> AgentSpec | None:
    try:
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    name = normalize_name(path.stem)
    description = " ".join(meta.get("description", "").split())[:200] or (base.description if base else "")
    if not name or not description:
        return None  # the main agent can't choose a helper it can't describe
    default_tools = base.tools if base else READ_ONLY_TOOL_NAMES + _HELPER_EXTRAS
    return AgentSpec(
        name=name, description=description, tools=_tools(meta.get("tools", ""), default_tools),
        prompt=body.strip(), model=meta.get("model", "").strip(), steps=_steps(meta.get("steps", "")),
        scope=scope, path=path,
    )


def load_specs(workspace: Path | None = None) -> dict[str, AgentSpec]:
    """Built-in helpers, then ~/.dcode/agents, then the project's .dcode/agents (later wins)."""
    specs = builtin_specs()
    folders = [(agents_dir(), "user")]
    if workspace is not None:
        folders.append((workspace / ".dcode" / "agents", "project"))
    for folder, scope in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            spec = _load(path, scope, specs.get(normalize_name(path.stem)))
            if spec:
                specs[spec.name] = spec
    return specs


def save_spec(name: str, description: str, prompt: str, tools: str = "", model: str = "",
              folder: Path | None = None) -> Path:
    folder = folder or agents_dir()
    folder.mkdir(parents=True, exist_ok=True)
    slug = normalize_name(name) or "helper"
    lines = ["---", f"description: {' '.join(description.split())[:200]}"]
    if tools.strip():
        lines.append(f"tools: {tools.strip()}")
    if model.strip():
        lines.append(f"model: {model.strip()}")
    path = folder / f"{slug}.md"
    path.write_text("\n".join(lines) + "\n---\n" + prompt.strip() + "\n", encoding="utf-8")
    return path
