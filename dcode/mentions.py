"""``@path`` mentions: attach files to a message without spending tool calls.

``fix the bug in @src/app.py`` puts the file into the conversation as if the
model had read it itself (a synthetic ``read_file`` call and result). Using a
tool result rather than pasting into the user message matters: compaction
already knows how to shrink old tool results, so a mentioned file never sits
in a small context forever.

Also provides the fuzzy file completion used by the terminal and the web UI.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from .tools import _IGNORE_DIRS

MAX_ATTACH = 3          # files attached per message; further mentions are only named
MAX_INDEX_FILES = 20000
INDEX_TTL = 10.0        # seconds before the file index is rebuilt

# "@src/app.py", "@./notes.md", "@docs/" — not e-mail addresses ("a@b.c") or "@@".
MENTION = re.compile(r"(?<![\w@])@([\w.~/-][^\s,;:!?'\"()\[\]{}<>`]*)")
# The "@partial" being typed just before the cursor.
_TOKEN = re.compile(r"(?:^|\s)@([^\s]*)$")


def find(text: str, root: Path) -> list[str]:
    """Workspace-relative paths of existing files/folders mentioned in ``text`` (in order, no dupes)."""
    found: list[str] = []
    for match in MENTION.finditer(text or ""):
        raw = match.group(1)
        for candidate in (raw, raw.rstrip(".")):  # "see @app.py." ends a sentence
            rel = _resolve(candidate, root)
            if rel is not None:
                if rel not in found:
                    found.append(rel)
                break
    return found


def _resolve(value: str, root: Path) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser()
    path = path if path.is_absolute() else root / path
    try:
        path = path.resolve()
        rel = path.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    if not path.exists():
        return None
    shown = str(rel) or "."
    return shown + "/" if path.is_dir() and shown != "." else shown


def typing_token(text_before_cursor: str) -> str | None:
    """The partial path after an "@" right before the cursor, or None."""
    match = _TOKEN.search(text_before_cursor or "")
    return match.group(1) if match else None


# --- completion -----------------------------------------------------------------

_index: dict[str, tuple[float, list[str]]] = {}
_index_lock = threading.Lock()


def project_files(root: Path) -> list[str]:
    """Relative file and folder paths (folders end with "/"), cached for a few seconds."""
    key = str(root)
    now = time.monotonic()
    with _index_lock:
        cached = _index.get(key)
        if cached and now - cached[0] < INDEX_TTL:
            return cached[1]
    items: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORE_DIRS)
        base = Path(current).relative_to(root)
        for d in dirs:
            items.append(f"{base / d}/" if str(base) != "." else f"{d}/")
        for f in sorted(files):
            items.append(str(base / f) if str(base) != "." else f)
        if len(items) >= MAX_INDEX_FILES:
            break
    with _index_lock:
        _index[key] = (now, items)
    return items


def _score(query: str, path: str) -> int | None:
    """Lower is better; None = no match. Name matches beat path matches beat fuzzy ones."""
    q = query.lower()
    p = path.lower().rstrip("/")
    name = p.rsplit("/", 1)[-1]
    if not q:
        return p.count("/") * 10 + len(p)
    if name.startswith(q):
        return 0 + len(p)
    if q in name:
        return 1000 + len(p)
    if q in p:
        return 2000 + len(p)
    position = 0
    for char in q:  # subsequence: "sapp" matches "src/app.py"
        position = p.find(char, position)
        if position < 0:
            return None
        position += 1
    return 3000 + len(p)


def complete(query: str, root: Path, limit: int = 8) -> list[str]:
    ranked = []
    for path in project_files(root):
        score = _score(query, path)
        if score is not None:
            ranked.append((score, path))
    ranked.sort()
    return [path for _, path in ranked[:limit]]
