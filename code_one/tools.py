from __future__ import annotations

import json
import subprocess
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional


BASE_TOOL_NAMES = [
    "read_file",
    "write_file",
    "list_files",
    "create_directory",
    "delete_file",
    "search_in_files",
    "run_command",
]
GIT_TOOL_NAMES = ["git_status", "git_diff", "git_log", "git_commit"]


def tool_definitions(
    permissions: Optional[Dict[str, bool]] = None,
    include_git_tools: bool = True,
    auto_commit_enabled: bool = False,
) -> List[Dict[str, Any]]:
    permission_map = permissions or {}
    allowed_base_tools = set(
        name for name in BASE_TOOL_NAMES if permission_map.get(name, True)
    )
    if "list_files" not in allowed_base_tools:
        allowed_base_tools.add("list_files")

    tools: List[Dict[str, Any]] = []
    if "read_file" in allowed_base_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a text file and return its full contents.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "Path to the file to read."}},
                        "required": ["path"],
                    },
                },
            }
        )
    if "write_file" in allowed_base_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Create or replace a text file with provided content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Target file path."},
                            "content": {"type": "string", "description": "Content to write."},
                        },
                        "required": ["path", "content"],
                    },
                },
            }
        )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files and folders in a directory recursively.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Directory path to inspect."}},
                    "required": ["path"],
                },
            },
        }
    )
    if "create_directory" in allowed_base_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "create_directory",
                    "description": "Create one directory and any parent directories as needed.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "Directory path to create."}},
                        "required": ["path"],
                    },
                },
            }
        )
    if "delete_file" in allowed_base_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Delete a file or empty folder.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "Path to file or directory to delete."}},
                        "required": ["path"],
                    },
                },
            }
        )
    if "search_in_files" in allowed_base_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "search_in_files",
                    "description": "Search for a text pattern in files under a directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Substring or regex-like text to search."},
                            "path": {"type": "string", "description": "Directory to search in."},
                        },
                        "required": ["pattern", "path"],
                    },
                },
            }
        )
    if "run_command" in allowed_base_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a shell command in the current working directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string", "description": "Command string for shell execution."}},
                        "required": ["command"],
                    },
                },
            }
        )

    if include_git_tools:
        tools.extend(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "git_status",
                        "description": "Run git status and return output from the current repository.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "git_diff",
                        "description": "Run git diff HEAD and return output from the current repository.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "git_log",
                        "description": "Run git log --oneline -10 and return recent history.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "git_commit",
                        "description": (
                            "Stage all changes and create a git commit with the provided message. "
                            "Use only when auto commit is enabled or the user explicitly asks to commit."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string", "description": "Commit message."},
                            },
                            "required": ["message"],
                        },
                    },
                },
            ]
        )
    return tools


def _ensure_parent(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _run(command: List[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    try:
        if name == "read_file":
            path = _path(arguments["path"])
            if not path.exists():
                return f"File does not exist: {path}"
            if path.is_dir():
                return f"Path is a directory: {path}"
            return path.read_text()

        if name == "write_file":
            path = _path(arguments["path"])
            _ensure_parent(path)
            path.write_text(arguments["content"])
            return f"Wrote {len(arguments['content'])} chars to {path}"

        if name == "list_files":
            base = _path(arguments["path"])
            if not base.exists():
                return f"Path does not exist: {base}"
            if not base.is_dir():
                return f"Not a directory: {base}"
            items = sorted([str(item.relative_to(base)) for item in base.rglob("*")])
            return "\n".join(items) if items else "(empty)"

        if name == "create_directory":
            path = _path(arguments["path"])
            path.mkdir(parents=True, exist_ok=True)
            return f"Created directory {path}"

        if name == "delete_file":
            path = _path(arguments["path"])
            if not path.exists():
                return f"Path does not exist: {path}"
            if path.is_dir():
                if any(path.iterdir()):
                    return f"Directory not empty: {path}"
                path.rmdir()
            else:
                path.unlink()
            return f"Deleted {path}"

        if name == "search_in_files":
            base = _path(arguments["path"])
            if not base.exists():
                return f"Path does not exist: {base}"
            if not base.is_dir():
                return f"Not a directory: {base}"
            pattern = arguments["pattern"]
            matches = []
            for item in base.rglob("*"):
                if not item.is_file():
                    continue
                try:
                    text = item.read_text(errors="ignore")
                except Exception:
                    continue
                if pattern in text:
                    for line_num, line in enumerate(text.splitlines(), start=1):
                        if pattern in line:
                            snippet = line.strip()[:200]
                            matches.append(f"{item}:{line_num}:{snippet}")
            return "\n".join(matches) if matches else "(no matches)"

        if name == "run_command":
            result = subprocess.run(
                arguments["command"],
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                output += f"\n(return code: {result.returncode})"
            return output.strip() or "(no output)"

        if name == "git_status":
            result = _run(["git", "status"])
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                output += f"\n(return code: {result.returncode})"
            return output.strip() or "(no output)"

        if name == "git_diff":
            result = _run(["git", "diff", "HEAD"])
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                output += f"\n(return code: {result.returncode})"
            return output.strip() or "(no output)"

        if name == "git_log":
            result = _run(["git", "log", "--oneline", "-10"])
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                output += f"\n(return code: {result.returncode})"
            return output.strip() or "(no output)"

        if name == "git_commit":
            message = str(arguments.get("message", "")).strip()
            if not message:
                return "Commit message is required."
            add_result = _run(["git", "add", "-A"])
            if add_result.returncode != 0:
                output = (add_result.stdout or "") + (add_result.stderr or "")
                return output.strip() or "(git add failed)"
            commit_result = _run(["git", "commit", "-m", message], timeout=120)
            output = (commit_result.stdout or "") + (commit_result.stderr or "")
            if commit_result.returncode != 0:
                output += f"\n(return code: {commit_result.returncode})"
            return output.strip() or "(no output)"

        return f"Unknown tool: {name}"
    except Exception as exc:
        return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def parse_arguments(raw: str) -> Dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def tool_names() -> List[str]:
    return BASE_TOOL_NAMES + GIT_TOOL_NAMES
