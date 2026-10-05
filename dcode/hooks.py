"""Hooks: your shell commands at key moments of a session.

Configured in ``~/.dcode/config.json`` only — never from a project folder, so
opening a repository can't make Dcode run its code::

    "hooks": [
      {"on": "before_tool", "match": "run_command", "command": "~/bin/check-cmd.sh"},
      {"on": "after_tool", "match": "*_file", "command": "logger -t dcode \\"$DCODE_TOOL $DCODE_PATH\\""},
      {"on": "task_done", "command": "notify-send Dcode 'Task finished'"},
      {"on": "approval_needed", "command": "printf '\\\\a'"}
    ]

Events: ``before_tool`` (exit non-zero to block the tool; what the hook prints
is shown to the model as the reason), ``after_tool``, ``task_done`` and
``approval_needed`` (handy for desktop notifications or a terminal bell).
``match`` is a tool-name pattern (``*`` = all; default all).

Each hook gets details as environment variables (``DCODE_EVENT``,
``DCODE_TOOL``, ``DCODE_PATH``, ``DCODE_COMMAND``, ``DCODE_ARGS`` as JSON,
``DCODE_OUTPUT``, ``DCODE_TEXT``, ``DCODE_DETAIL``, ``DCODE_SESSION``) and the same as
one JSON object on stdin. ``timeout`` (seconds, default 10) is per hook.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path
from typing import Any

EVENTS = ("before_tool", "after_tool", "task_done", "approval_needed")
DEFAULT_TIMEOUT = 10.0
MAX_ENV_CHARS = 8000


def clean(hooks: Any) -> list[dict[str, Any]]:
    """Keep well-formed hooks only (bad config must never break a session)."""
    out = []
    for hook in hooks if isinstance(hooks, list) else []:
        if not isinstance(hook, dict):
            continue
        event, command = str(hook.get("on") or "").strip(), str(hook.get("command") or "").strip()
        if event in EVENTS and command:
            item: dict[str, Any] = {"on": event, "command": command}
            if str(hook.get("match") or "").strip():
                item["match"] = str(hook["match"]).strip()
            try:
                if hook.get("timeout") is not None:
                    item["timeout"] = max(1.0, min(300.0, float(hook["timeout"])))
            except (TypeError, ValueError):
                pass
            out.append(item)
    return out


def _matching(hooks: list[dict[str, Any]], event: str, tool: str) -> list[dict[str, Any]]:
    return [h for h in clean(hooks) if h["on"] == event
            and (not tool or fnmatch.fnmatchcase(tool, h.get("match") or "*"))]


def _run(hook: dict[str, Any], payload: dict[str, Any], cwd: Path) -> tuple[int, str]:
    env = dict(os.environ)
    for key, value in payload.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        env[f"DCODE_{key.upper()}"] = text[:MAX_ENV_CHARS]
    try:
        result = subprocess.run(
            hook["command"], shell=True, cwd=str(cwd), env=env, input=json.dumps(payload, default=str),
            capture_output=True, text=True, timeout=float(hook.get("timeout") or DEFAULT_TIMEOUT), check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"hook timed out: {hook['command']}"
    except OSError as exc:
        return 127, f"hook could not run: {exc}"
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()[:2000]


def run(hooks: list[dict[str, Any]], event: str, cwd: Path, tool: str = "", **payload: Any) -> list[tuple[dict, int, str]]:
    """Run every hook for ``event`` (and ``tool``); returns (hook, exit code, output) for each."""
    if not hooks:
        return []
    data = {"event": event, "tool": tool, **payload}
    return [(hook, *_run(hook, data, cwd)) for hook in _matching(hooks, event, tool)]


def tool_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {"args": args, "path": str(args.get("path") or ""), "command": str(args.get("command") or "")}


def before_tool(hooks: list[dict[str, Any]], tool: str, args: dict[str, Any], cwd: Path, session: str = "") -> str:
    """A refusal message if a before_tool hook blocks this call, else ""."""
    for hook, code, output in run(hooks, "before_tool", cwd, tool, session=session, **tool_payload(args)):
        if code != 0:
            return output or f"blocked by the hook `{hook['command']}`"
    return ""
