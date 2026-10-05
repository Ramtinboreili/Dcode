"""Permission rules: allow, ask or deny by pattern, on top of the modes.

Modes say how much Dcode asks in general; rules make exceptions for specific
commands and files::

    "rules": {
      "commands": {"git status*": "allow", "git push*": "ask", "rm -rf *": "deny"},
      "files": {".env": "deny", "*.pem": "deny", "*.env.example": "allow"},
      "outside_workspace": "ask"
    }

* ``allow`` runs without asking, even in manual mode. ``ask`` always asks, even
  in auto mode. ``deny`` refuses and tells the model not to retry.
* Patterns are shell-style (``*``, ``?``, ``[abc]``); within one list the last
  matching pattern wins. File patterns match the project-relative path or just
  the file name.
* A command chained with ``&&``, ``||``, ``;`` or ``|`` is judged part by part:
  any denied part denies it, any "ask" part asks, and it is allowed only if
  every part is allowed.
* A project can add rules in ``.dcode/rules.json`` (same shape). Those can
  only make things stricter (``ask``/``deny``) — a cloned repo must never be
  able to allow itself to run commands.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DECISIONS = ("allow", "ask", "deny")
_RANK = {None: 0, "allow": 1, "ask": 2, "deny": 3}
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


def _clean(table: Any, allowed: tuple[str, ...] = DECISIONS) -> dict[str, str]:
    if not isinstance(table, dict):
        return {}
    out = {}
    for pattern, decision in table.items():
        decision = str(decision).strip().lower()
        if str(pattern).strip() and decision in allowed:
            out[str(pattern).strip()] = decision
    return out


def _last_match(table: dict[str, str], *candidates: str) -> tuple[str | None, str]:
    decision, matched = None, ""
    for pattern, value in table.items():
        bare = pattern[3:] if pattern.startswith("**/") else pattern
        if any(fnmatch.fnmatchcase(c, pattern) or fnmatch.fnmatchcase(c, bare) for c in candidates if c):
            decision, matched = value, pattern
    return decision, matched


def _strictest(*results: tuple[str | None, str]) -> tuple[str | None, str]:
    return max(results, key=lambda r: _RANK[r[0]])


@dataclass
class RuleSet:
    commands: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    outside_workspace: str = "ask"
    # From the project's .dcode/rules.json: ask/deny only.
    project_commands: dict[str, str] = field(default_factory=dict)
    project_files: dict[str, str] = field(default_factory=dict)

    def command(self, command: str) -> tuple[str | None, str]:
        """(decision, pattern) for a shell command; (None, "") = no rule, the mode decides."""
        parts = [p for p in _SPLIT.split(command) if p.strip()]
        if not parts:
            return None, ""
        results = []
        for part in parts:
            part = " ".join(part.split())
            results.append(_strictest(_last_match(self.commands, part), _last_match(self.project_commands, part)))
        worst = _strictest(*results)
        if worst[0] in ("deny", "ask"):
            return worst
        if all(r[0] == "allow" for r in results):
            return "allow", results[-1][1]
        return None, ""

    def file(self, rel_path: str) -> tuple[str | None, str]:
        rel = rel_path.replace("\\", "/").strip("/") or "."
        name = rel.rsplit("/", 1)[-1]
        return _strictest(_last_match(self.files, rel, name), _last_match(self.project_files, rel, name))

    def denies_file(self, rel_path: str) -> bool:
        return self.file(rel_path)[0] == "deny"


def load(settings: dict[str, Any] | None, workspace: Path | None = None) -> RuleSet:
    settings = settings if isinstance(settings, dict) else {}
    outside = str(settings.get("outside_workspace") or "ask").lower()
    rules = RuleSet(
        commands=_clean(settings.get("commands")),
        files=_clean(settings.get("files")),
        outside_workspace=outside if outside in DECISIONS else "ask",
    )
    project = project_rules(workspace) if workspace is not None else {}
    rules.project_commands = _clean(project.get("commands"), ("ask", "deny"))
    rules.project_files = _clean(project.get("files"), ("ask", "deny"))
    return rules


def project_rules(workspace: Path) -> dict[str, Any]:
    path = workspace / ".dcode" / "rules.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
