from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import threading
import traceback
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import shell

# --- Safety limits -----------------------------------------------------------
# Cap outputs handed back to the model so a single tool call cannot blow up the
# context window. ``configure_limits`` rescales these to the model's window.
MAX_READ_BYTES = 512 * 1024          # read_file: refuse files larger than this
MAX_OUTPUT_CHARS = 16_000            # truncate any tool output beyond this
MAX_READ_LINES = 400                 # read_file: default lines per call
MAX_LIST_ENTRIES = 400               # cap directory listing entries
MAX_SEARCH_MATCHES = 120             # cap search hits
COMMAND_TIMEOUT = 120                # seconds for run_command / git

# Directories we never descend into for listing/search — noisy and huge.
_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".idea", ".vscode", ".tox", ".next", "target",
}

# "12| code" — the prefix read_file adds; stripped if a model pastes it into edits.
_LINE_PREFIX = re.compile(r"^\s*\d+\| ?", re.MULTILINE)


def configure_limits(context_window: int, share: float = 0.12, low: int = 3_000, high: int = 60_000) -> None:
    """Scale output caps so one tool result uses at most ~12% of the window."""
    global MAX_OUTPUT_CHARS, MAX_READ_LINES, MAX_LIST_ENTRIES, MAX_SEARCH_MATCHES
    chars = int(context_window * 3.5 * share)  # ~3.5 chars per token
    MAX_OUTPUT_CHARS = max(low, min(chars, high))
    MAX_READ_LINES = max(80, min(MAX_OUTPUT_CHARS // 45, 1_500))
    MAX_LIST_ENTRIES = max(80, min(MAX_OUTPUT_CHARS // 40, 2_000))
    MAX_SEARCH_MATCHES = max(30, min(MAX_OUTPUT_CHARS // 120, 500))


class WorkspaceError(Exception):
    """Raised when a path escapes the allowed workspace root."""


def _env_flag(name: str) -> str:
    return os.environ.get(f"DCODE_{name}") or os.environ.get(f"CODE_ONE_{name}") or ""


def workspace_root() -> Path:
    """Root that file tools are confined to (defaults to the launch cwd)."""
    override = _env_flag("WORKSPACE")
    base = Path(override) if override else Path.cwd()
    return base.expanduser().resolve()


def _allow_outside_workspace() -> bool:
    if getattr(_local, "outside_ok", False):  # approved for this one tool call (rules)
        return True
    return _env_flag("ALLOW_OUTSIDE_WORKSPACE").lower() in {"1", "true", "yes"}


_local = threading.local()
# Set by the engine from the permission rules: rel_path -> True if the file is denied.
_file_guard: Callable[[str], bool] | None = None


def set_file_guard(guard: Callable[[str], bool] | None) -> None:
    global _file_guard
    _file_guard = guard


def _denied(path: Path) -> bool:
    if _file_guard is None:
        return False
    try:
        rel = str(path.relative_to(workspace_root()))
    except ValueError:
        rel = str(path)
    try:
        return rel != "." and _file_guard(rel)
    except Exception:
        return False


@contextmanager
def outside_workspace_allowed(allowed: bool = True):
    """Let this thread's next file tools reach outside the workspace (after a rule or approval)."""
    previous = getattr(_local, "outside_ok", False)
    _local.outside_ok = allowed
    try:
        yield
    finally:
        _local.outside_ok = previous


def _safe_path(value: str) -> Path:
    """Resolve ``value`` and ensure it stays inside the workspace root.

    Set DCODE_ALLOW_OUTSIDE_WORKSPACE=1 to disable this sandbox.
    """
    resolved = Path(str(value or ".")).expanduser()
    if not resolved.is_absolute():
        resolved = workspace_root() / resolved
    resolved = resolved.resolve()
    if _denied(resolved):
        raise WorkspaceError(f"'{_rel(resolved)}' is protected by a permission rule. Don't read or change it.")
    if _allow_outside_workspace():
        return resolved
    root = workspace_root()
    if resolved != root and root not in resolved.parents:
        raise WorkspaceError(
            f"Path '{value}' is outside the workspace ({root}). "
            "Set DCODE_ALLOW_OUTSIDE_WORKSPACE=1 to override."
        )
    return resolved


def _rel(path: Path) -> str:
    """Short workspace-relative display path (saves tokens vs. absolute paths)."""
    try:
        return str(path.relative_to(workspace_root())) or "."
    except ValueError:
        return str(path)


def _note_raw(chars: int) -> None:
    """What this tool produced before a cap cut it, for the research trace."""
    _local.raw_chars = max(chars, getattr(_local, "raw_chars", 0))


def take_raw_chars() -> int | None:
    """The size noted since the last ask on this thread; None when nothing was cut."""
    chars = getattr(_local, "raw_chars", None)
    _local.raw_chars = 0
    return chars or None


def _truncate(text: str, limit: int | None = None) -> str:
    limit = MAX_OUTPUT_CHARS if limit is None else limit
    if len(text) <= limit:
        return text
    _note_raw(len(text))
    return text[:limit] + f"\n... (truncated, {len(text) - limit} more chars)"


def _skip_dir(item: Path) -> bool:
    return any(part in _IGNORE_DIRS for part in item.parts)


# --- Tool schemas ----------------------------------------------------------------
# Descriptions are deliberately terse: every schema is re-sent on every request,
# and on a 32K model each word here competes with the actual code.

def _fn(name: str, description: str, properties: dict[str, Any], required: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


_S = {"type": "string"}
_I = {"type": "integer"}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "read_file": _fn(
        "read_file",
        "Read a text file. Returns numbered lines ('N| text'). Use start_line/end_line for big files.",
        {"path": _S, "start_line": _I, "end_line": _I},
        ["path"],
    ),
    "edit_file": _fn(
        "edit_file",
        "Replace exact text in a file. old_text must match once (or set replace_all). Prefer this over write_file.",
        {"path": _S, "old_text": _S, "new_text": _S, "replace_all": {"type": "boolean"}},
        ["path", "old_text", "new_text"],
    ),
    "write_file": _fn(
        "write_file",
        "Create a file or fully replace its content.",
        {"path": _S, "content": _S},
        ["path", "content"],
    ),
    "list_files": _fn(
        "list_files",
        "List files under a directory (default depth 2).",
        {"path": _S, "max_depth": _I},
        ["path"],
    ),
    "search_in_files": _fn(
        "search_in_files",
        "Search file contents (regex). Optional glob filter like '*.py'.",
        {"pattern": _S, "path": _S, "glob": _S},
        ["pattern"],
    ),
    "create_directory": _fn("create_directory", "Create a directory (and parents).", {"path": _S}, ["path"]),
    "delete_file": _fn("delete_file", "Delete a file or empty directory.", {"path": _S}, ["path"]),
    "run_command": _fn(
        "run_command",
        "Run a bash command. Each call starts in the workspace root (cd doesn't carry over); there is no "
        "terminal or input. Output is truncated. background=true starts a long-running process (server, "
        "watcher) and returns at once.",
        {"command": _S, "background": {"type": "boolean"}},
        ["command"],
    ),
    "git_status": _fn("git_status", "git status --short.", {}, []),
    "git_diff": _fn("git_diff", "git diff HEAD (optionally for one path).", {"path": _S}, []),
    "git_log": _fn("git_log", "Last 10 commits, one line each.", {}, []),
    "git_commit": _fn(
        "git_commit",
        "Stage all changes and commit. Only when the user asked to commit.",
        {"message": _S},
        ["message"],
    ),
}

BASE_TOOL_NAMES = [
    "read_file", "edit_file", "write_file", "list_files", "create_directory",
    "delete_file", "search_in_files", "run_command",
]
GIT_TOOL_NAMES = ["git_status", "git_diff", "git_log", "git_commit"]
READ_ONLY_TOOL_NAMES = ["read_file", "list_files", "search_in_files", "git_status", "git_diff", "git_log"]

# Which permission switch governs each tool.
_PERMISSION_FOR = {"edit_file": "write_file"}


def allowed_tool_names(permissions: dict[str, bool] | None = None, auto_commit_enabled: bool = False) -> list[str]:
    permission_map = permissions or {}
    names = []
    for name in BASE_TOOL_NAMES + GIT_TOOL_NAMES:
        key = _PERMISSION_FOR.get(name, name)
        if name == "list_files" or permission_map.get(key, True):
            names.append(name)
    return names


def tool_definitions(
    permissions: dict[str, bool] | None = None,
    include_git_tools: bool = True,
    auto_commit_enabled: bool = False,
    names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    selected = list(names) if names is not None else allowed_tool_names(permissions, auto_commit_enabled)
    if not include_git_tools:
        selected = [name for name in selected if name not in GIT_TOOL_NAMES]
    return [TOOL_SCHEMAS[name] for name in selected if name in TOOL_SCHEMAS]


# --- Execution -------------------------------------------------------------------

def _ensure_parent(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def _run(command: list[str], timeout: int = COMMAND_TIMEOUT) -> str:
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=timeout, cwd=str(workspace_root())
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        output += f"\n(return code: {result.returncode})"
    return _truncate(output.strip()) or "(no output)"


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_file(arguments: dict[str, Any]) -> str:
    path = _safe_path(arguments["path"])
    if not path.exists():
        return f"File does not exist: {_rel(path)}"
    if path.is_dir():
        return f"Path is a directory: {_rel(path)}"
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        return (
            f"File is too large to read ({size} bytes, limit {MAX_READ_BYTES}). "
            "Use search_in_files or run_command with head/sed to read part of it."
        )
    lines = path.read_text(errors="replace").splitlines()
    total = len(lines)
    start = max(1, _to_int(arguments.get("start_line")) or 1)
    end = _to_int(arguments.get("end_line")) or (start + MAX_READ_LINES - 1)
    end = min(total, end, start + MAX_READ_LINES - 1)
    if total == 0:
        return "(empty file)"
    if start > total:
        return f"start_line {start} is past end of file ({total} lines)."
    width = len(str(end))
    body = "\n".join(f"{str(n).rjust(width)}| {lines[n - 1]}" for n in range(start, end + 1))
    body = _truncate(body)
    if start > 1 or end < total:
        _note_raw(sum(len(line) + 1 for line in lines))
        body += f"\n[lines {start}-{end} of {total}; use start_line/end_line for more]"
    return body


def _unshift(text: str) -> str | None:
    """Drop one leading space from every line; None unless each non-blank line has one."""
    lines = text.split("\n")
    if any(line.strip() and not line.startswith(" ") for line in lines):
        return None
    return "\n".join(line[1:] if line.startswith(" ") else line for line in lines)


def _closest_line(text: str, old: str) -> str:
    """A hint for a failed edit: the file line that old_text's first line most likely meant."""
    wanted = next((line.strip() for line in old.split("\n") if line.strip()), "")
    lines = text.split("\n")
    if not wanted or not lines:
        return ""
    same = [n for n, line in enumerate(lines, 1) if line.strip() == wanted]
    if same:
        n = same[0]
        return f" Line {n} has those words with other spacing: {json.dumps(lines[n - 1][:160])}."
    import difflib

    near = difflib.get_close_matches(wanted, [line.strip() for line in lines], n=1, cutoff=0.6)
    if near:
        n = next(i for i, line in enumerate(lines, 1) if line.strip() == near[0])
        return f" The closest is line {n}: {json.dumps(lines[n - 1][:160])}."
    return ""


def _edit_file(arguments: dict[str, Any]) -> str:
    path = _safe_path(arguments["path"])
    if not path.exists():
        return f"File does not exist: {_rel(path)}. Use write_file to create it."
    old = str(arguments.get("old_text", ""))
    new = str(arguments.get("new_text", ""))
    if not old:
        return "old_text must not be empty."
    text = path.read_text(errors="replace")
    count = text.count(old)
    if count == 0:
        # Small models often paste read_file's "N| " prefixes back in.
        stripped_old = _LINE_PREFIX.sub("", old)
        if stripped_old != old and text.count(stripped_old):
            old, new = stripped_old, _LINE_PREFIX.sub("", new)
            count = text.count(old)
    if count == 0 and old.startswith(" "):
        # ...or copy the space after "N|" as if it were indentation: every line has one space too many.
        shifted_old = _unshift(old)
        if shifted_old is not None and text.count(shifted_old):
            old, new = shifted_old, _unshift(new) or new
            count = text.count(old)
    if count == 0:
        return ("old_text not found." + _closest_line(text, old) +
                " Copy the text exactly, without the 'N| ' line numbers (whitespace matters).")
    if count > 1 and not arguments.get("replace_all"):
        return f"old_text matches {count} places. Add surrounding lines to make it unique, or set replace_all."
    updated = text.replace(old, new) if arguments.get("replace_all") else text.replace(old, new, 1)
    path.write_text(updated)
    return f"Edited {_rel(path)} ({count if arguments.get('replace_all') else 1} replacement)."


def _list_files(arguments: dict[str, Any]) -> str:
    base = _safe_path(arguments.get("path") or ".")
    if not base.exists():
        return f"Path does not exist: {_rel(base)}"
    if not base.is_dir():
        return f"Not a directory: {_rel(base)}"
    max_depth = _to_int(arguments.get("max_depth")) or 2
    items: list[str] = []
    for root, dirs, files in os.walk(base):
        root_path = Path(root)
        depth = len(root_path.relative_to(base).parts)
        dirs[:] = sorted(d for d in dirs if d not in _IGNORE_DIRS)
        if depth + 1 >= max_depth:
            items.extend(str((root_path / d).relative_to(base)) + "/ ..." for d in dirs)
            dirs[:] = []
        else:
            items.extend(str((root_path / d).relative_to(base)) + "/" for d in dirs)
        items.extend(str((root_path / f).relative_to(base)) for f in sorted(files))
        if len(items) >= MAX_LIST_ENTRIES:
            break
    items.sort()
    if len(items) > MAX_LIST_ENTRIES:
        items = items[:MAX_LIST_ENTRIES] + [f"... (truncated at {MAX_LIST_ENTRIES} entries; narrow the path)"]
    return "\n".join(items) if items else "(empty)"


def _search(arguments: dict[str, Any]) -> str:
    base = _safe_path(arguments.get("path") or ".")
    if not base.exists():
        return f"Path does not exist: {_rel(base)}"
    pattern = str(arguments.get("pattern", ""))
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))
    glob = arguments.get("glob") or None
    candidates = [base] if base.is_file() else (p for p in base.rglob("*") if not _skip_dir(p.relative_to(base)))
    matches: list[str] = []
    for item in candidates:
        if not item.is_file() or (glob and not fnmatch.fnmatch(item.name, glob)) or _denied(item):
            continue
        try:
            if item.stat().st_size > MAX_READ_BYTES:
                continue
            text = item.read_text(errors="ignore")
        except Exception:
            continue
        if not regex.search(text):
            continue
        for line_num, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{_rel(item)}:{line_num}: {line.strip()[:160]}")
                if len(matches) >= MAX_SEARCH_MATCHES:
                    matches.append(f"... (truncated at {MAX_SEARCH_MATCHES} matches; refine the pattern)")
                    return _truncate("\n".join(matches))
    return _truncate("\n".join(matches)) if matches else "(no matches)"


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    try:
        if name == "read_file":
            return _read_file(arguments)

        if name == "edit_file":
            return _edit_file(arguments)

        if name == "write_file":
            path = _safe_path(arguments["path"])
            if "content" not in arguments:
                return "Missing required argument: 'content'. Pass the complete file content."
            content = str(arguments.get("content") or "")
            if not content.strip() and path.is_file() and path.stat().st_size > 0:
                # Small models sometimes drop the payload; never let that erase a file.
                return (f"Refused: content is empty and would erase {_rel(path)}. "
                        "Pass the complete new content, or use edit_file for a partial change.")
            _ensure_parent(path)
            path.write_text(content)
            return f"Wrote {len(content)} chars to {_rel(path)}"

        if name == "list_files":
            return _list_files(arguments)

        if name == "create_directory":
            path = _safe_path(arguments["path"])
            path.mkdir(parents=True, exist_ok=True)
            return f"Created directory {_rel(path)}"

        if name == "delete_file":
            path = _safe_path(arguments["path"])
            if not path.exists():
                return f"Path does not exist: {_rel(path)}"
            if path.is_dir():
                if any(path.iterdir()):
                    return f"Directory not empty: {_rel(path)}"
                path.rmdir()
            else:
                path.unlink()
            return f"Deleted {_rel(path)}"

        if name == "search_in_files":
            return _search(arguments)

        if name == "run_command":
            command = str(arguments["command"])
            refused = shell.refusal(command)
            if refused:
                return refused
            timeout = int(arguments.get("timeout") or COMMAND_TIMEOUT)
            output, code = shell.run(command, workspace_root(), timeout)
            output = output.strip()
            if code is None:
                output += (f"\n(stopped after {timeout}s. A server or watcher needs background=true; "
                           "otherwise bound it, e.g. tail -n, ping -c, --tail=200.)")
            elif code != 0:
                output += f"\n(return code: {code})"
            # Keep the tail too: errors usually appear at the end of build/test output.
            output = output.strip()
            if len(output) > MAX_OUTPUT_CHARS:
                _note_raw(len(output))
                half = MAX_OUTPUT_CHARS // 2
                output = output[:half] + f"\n... (truncated {len(output) - 2 * half} chars) ...\n" + output[-half:]
            return output or "(no output)"

        if name == "git_status":
            return _run(["git", "status", "--short", "--branch"])

        if name == "git_diff":
            command = ["git", "diff", "HEAD"]
            if arguments.get("path"):
                command += ["--", str(_safe_path(arguments["path"]))]
            return _run(command)

        if name == "git_log":
            return _run(["git", "log", "--oneline", "-10"])

        if name == "git_commit":
            message = str(arguments.get("message", "")).strip()
            if not message:
                return "Commit message is required."
            add_output = _run(["git", "add", "-A"])
            if "(return code:" in add_output:
                return add_output
            return _run(["git", "commit", "-m", message], timeout=120)

        return f"Unknown tool: {name}"
    except WorkspaceError as exc:
        return f"Blocked: {exc}"
    except KeyError as exc:
        return f"Missing required argument: {exc}"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {COMMAND_TIMEOUT}s."
    except Exception as exc:
        return "".join(traceback.format_exception_only(type(exc), exc)).strip()


# Other agents' names for these arguments; a model trained on them keeps using them.
_ARG_ALIASES = {
    "path": ("file_path", "filepath", "file", "filename", "target_file", "path_to_file", "absolute_path",
             "relative_path", "dir_path", "directory", "dir", "folder", "target_directory", "relative_workspace_path"),
    "content": ("contents", "text", "file_text", "data", "body", "code", "file_content", "new_content"),
    "old_text": ("old_string", "old_str", "search", "find", "original", "old", "old_content", "search_text"),
    "new_text": ("new_string", "new_str", "replace", "replacement", "new", "new_content", "replace_text"),
    "replace_all": ("all", "global", "replace_every"),
    "pattern": ("query", "regex", "search", "text", "search_term", "keyword", "term"),
    "glob": ("include", "file_pattern", "glob_pattern", "includes", "file_glob"),
    "start_line": ("offset", "line_start", "from_line", "start", "begin_line"),
    "end_line": ("line_end", "to_line", "end", "stop_line"),
    "max_depth": ("depth", "levels", "recursion_depth"),
    "message": ("commit_message", "msg"),
}


def normalize_call(call: dict[str, Any]) -> None:
    """Rewrite a built-in tool call into Dcode's argument names, in place (the history keeps the clean form)."""
    function = call.get("function") or {}
    name = function.get("name", "")
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return
    args = parse_arguments(function.get("arguments"))
    if not args:
        return
    before = json.dumps(args, sort_keys=True)
    if name == "run_command":
        args = shell.clean_command(args)
    props = schema["function"]["parameters"]["properties"]
    for key, aliases in _ARG_ALIASES.items():
        if key in props and key not in args:
            alias = next((a for a in aliases if a in args and a not in props), None)
            if alias:
                args[key] = args.pop(alias)
    if name == "read_file":
        view = args.pop("view_range", None) or args.pop("lines", None)
        if isinstance(view, list) and len(view) == 2:
            args.setdefault("start_line", view[0])
            if view[1] not in (-1, None):
                args.setdefault("end_line", view[1])
        limit = _to_int(args.pop("limit", None))
        start = _to_int(args.get("start_line"))
        if limit and "end_line" not in args:
            args["end_line"] = (start or 1) + limit - 1
    if json.dumps(args, sort_keys=True) != before:
        function["arguments"] = json.dumps(args, ensure_ascii=False)


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def tool_names() -> list[str]:
    return BASE_TOOL_NAMES + GIT_TOOL_NAMES
