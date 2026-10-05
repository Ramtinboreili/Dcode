"""Check (and optionally format) a file right after Dcode writes or edits it.

Small models often leave a syntax error or an undefined name behind and only
notice several steps later — if at all. Running a fast checker after every
edit and appending its errors to the tool result lets the model fix the
problem in its very next step, without spending a turn (and context) on
running a linter itself.

* Built-in checks cost nothing extra: Python, JSON and TOML are checked
  in-process; the others run only if their tool is installed (ruff, bash,
  node, gofmt, ruby, php). No network, no new dependencies.
* Formatting is off by default (``checks.format``): a formatter rewrites the
  file, and the model must then re-read it before editing nearby lines.
* Your own commands per extension win over the built-ins (``$FILE`` is the
  file path)::

      "checks": {"commands": {".ts": "npx --no-install tsc --noEmit -p ."},
                 "formatters": {".py": "black -q $FILE"}}
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

MAX_REPORT_LINES = 20
MAX_REPORT_CHARS = 1500
MAX_CHECK_BYTES = 1024 * 1024

# Extension -> shell commands; the first whose program is installed runs.
# A non-zero exit code means "problems found" and its output is reported.
CHECKERS: dict[str, list[str]] = {
    # Syntax errors and undefined names only — never style nits.
    ".py": ["ruff check --quiet --no-cache --output-format concise --select E9,F63,F7,F82 $FILE"],
    ".sh": ["bash -n $FILE"],
    ".bash": ["bash -n $FILE"],
    ".js": ["node --check $FILE"],
    ".mjs": ["node --check $FILE"],
    ".cjs": ["node --check $FILE"],
    ".go": ["gofmt -e $FILE >/dev/null"],
    ".rb": ["ruby -c $FILE >/dev/null"],
    ".php": ["php -l $FILE"],
}
FORMATTERS: dict[str, list[str]] = {
    ".py": ["ruff format --quiet --no-cache $FILE", "black -q $FILE"],
    ".go": ["gofmt -w $FILE"],
    ".rs": ["rustfmt $FILE"],
}
# Formatted with the project's own prettier only (never fetched from the network).
PRETTIER_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".scss", ".vue", ".svelte"}
# JSON-with-comments files that a strict parser would wrongly reject.
_JSONC_NAMES = {"tsconfig.json", "jsconfig.json", "devcontainer.json"}


def after_edit(path: Path, settings: dict[str, Any], root: Path) -> tuple[str, str]:
    """Format (if enabled) and check ``path``.

    Returns ``(note, problems)``: a note for the model (``""`` when all is well)
    and the problem text alone, for the UI.
    """
    if not settings.get("enabled", True) or not path.is_file():
        return "", ""
    try:
        if path.stat().st_size > MAX_CHECK_BYTES:
            return "", ""
    except OSError:
        return "", ""
    ext = path.suffix.lower()
    timeout = _timeout(settings)
    notes: list[str] = []
    if settings.get("format"):
        tool = _format(path, ext, settings, root, timeout)
        if tool:
            notes.append(f"Auto-formatted with {tool}; re-read the file before editing nearby lines.")
    tool, problems = _check(path, ext, settings, root, timeout)
    if problems:
        notes.append(f"⚠ {tool} found problems in this file — fix them now:\n{problems}")
    return "\n".join(notes), problems


def _timeout(settings: dict[str, Any]) -> float:
    try:
        return max(1.0, float(settings.get("timeout", 15)))
    except (TypeError, ValueError):
        return 15.0


def _custom(settings: dict[str, Any], key: str, ext: str) -> str:
    table = settings.get(key)
    if not isinstance(table, dict):
        return ""
    value = table.get(ext) or table.get(ext.lstrip("."))
    return str(value).strip() if value else ""


def _installed(command: str) -> bool:
    try:
        program = shlex.split(command)[0]
    except (ValueError, IndexError):
        return False
    return bool(shutil.which(program)) or Path(program).is_file()


def _run(command: str, path: Path, root: Path, timeout: float) -> tuple[int, str] | None:
    """Run ``command`` with ``$FILE`` filled in; None if it could not run in time."""
    filled = command.replace("$FILE", shlex.quote(str(path)))
    try:
        result = subprocess.run(filled, shell=True, cwd=str(root), capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()


def _name(command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        return "checker"
    if not words:
        return "checker"
    if Path(words[0]).name in ("npx", "uvx", "pnpm", "yarn") and len(words) > 1:
        rest = [w for w in words[1:] if not w.startswith("-")]
        return rest[0] if rest else Path(words[0]).name
    return Path(words[0]).name


def _tidy(text: str, path: Path, root: Path) -> str:
    """Short, relative paths and a hard cap: this text goes into a small context."""
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    text = text.replace(str(path), rel)
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > MAX_REPORT_LINES:
        lines = lines[:MAX_REPORT_LINES] + [f"… {len(lines) - MAX_REPORT_LINES} more lines"]
    out = "\n".join(lines)
    return out if len(out) <= MAX_REPORT_CHARS else out[:MAX_REPORT_CHARS] + " …"


def _builtin_check(path: Path, ext: str, root: Path) -> tuple[str, str]:
    """Checks that need nothing installed. ('', '') when fine or not applicable."""
    if ext not in (".py", ".json", ".toml"):
        return "", ""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    rel = _tidy(str(path), path, root)
    if ext == ".py":
        try:
            compile(source, rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            where = f"{rel}:{exc.lineno or '?'}:{exc.offset or '?'}"
            line = (exc.text or "").rstrip()
            return "python", f"{where}: SyntaxError: {exc.msg}" + (f"\n    {line.strip()}" if line else "")
        except ValueError as exc:  # e.g. null bytes
            return "python", f"{rel}: {exc}"
        return "", ""
    if ext == ".json":
        if path.name in _JSONC_NAMES or path.name.startswith("tsconfig.") or ".vscode" in path.parts:
            return "", ""
        try:
            json.loads(source)
        except json.JSONDecodeError as exc:
            return "json", f"{rel}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}"
        return "", ""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return "", ""
    try:
        tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        return "toml", f"{rel}: invalid TOML: {exc}"
    return "", ""


def _check(path: Path, ext: str, settings: dict[str, Any], root: Path, timeout: float) -> tuple[str, str]:
    custom = _custom(settings, "commands", ext)
    if custom:
        result = _run(custom, path, root, timeout)
        if result and result[0] != 0:
            return _name(custom), _tidy(result[1] or f"exit code {result[0]}", path, root)
        return "", ""
    tool, problems = _builtin_check(path, ext, root)
    if problems:
        return tool, problems
    for command in CHECKERS.get(ext, []):
        if not _installed(command):
            continue
        result = _run(command, path, root, timeout)
        if result and result[0] != 0 and result[1]:
            return _name(command), _tidy(result[1], path, root)
        break
    return "", ""


def _format(path: Path, ext: str, settings: dict[str, Any], root: Path, timeout: float) -> str:
    """Run the first available formatter; return its name if the file changed."""
    candidates = [_custom(settings, "formatters", ext)] if _custom(settings, "formatters", ext) else []
    if not candidates:
        candidates = list(FORMATTERS.get(ext, []))
        prettier = root / "node_modules" / ".bin" / "prettier"
        if ext in PRETTIER_EXTS and prettier.is_file():
            candidates.insert(0, f"{shlex.quote(str(prettier))} --write --log-level warn $FILE")
    try:
        before = path.read_bytes()
    except OSError:
        return ""
    for command in candidates:
        if not _installed(command):
            continue
        result = _run(command, path, root, timeout)
        if result is None or result[0] != 0:
            return ""
        try:
            changed = path.read_bytes() != before
        except OSError:
            return ""
        return _name(command) if changed else ""
    return ""
