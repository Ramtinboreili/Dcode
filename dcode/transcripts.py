"""Saved chats as files other tools can read: Markdown, JSON, or one self-contained HTML page.

Also the way back in: ``import_chat`` checks an exported JSON file and saves it as a chat that
``dcode --resume`` (or /sessions) continues. An imported file is untrusted, so only the fields
and message shapes Dcode itself writes are kept. Standard library only: the hub uses this too.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_thread, save_thread, slugify_thread_name, thread_path, threads_dir

FORMATS = {"md": "Markdown", "html": "HTML page", "json": "JSON (can be imported again)"}
CONTENT_TYPES = {"md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8",
                 "json": "application/json"}
EXPORT_VERSION = 1
MAX_IMPORT_BYTES = 30 * 1024 * 1024
MAX_IMPORT_MESSAGES = 20_000
OUTPUT_LINES = 60          # of each tool result kept in the Markdown and HTML exports
# How a command the user ran themselves ("!ls") is written into the chat for the model.
SHELL_NOTE = "[I ran this shell command myself: "


class TranscriptError(ValueError):
    """A chat can't be exported or imported; the message is for the user."""


# --- messages -> events ---------------------------------------------------------------------

def transcript_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten stored chat messages into UI events (for resumed / closed threads)."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        if role == "user":
            if content.startswith(SHELL_NOTE) and content.endswith("]"):
                content = "!" + content[len(SHELL_NOTE):-1]
            items.append({"type": "user_message", "text": content, "source": "history"})
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append({"type": "tool_call", "agent": "main", "name": function.get("name", ""),
                              "args": function.get("arguments", ""), "id": call.get("id", "")})
            if content:
                items.append({"type": "assistant_message", "agent": "main", "text": content})
        elif role == "tool":
            items.append({"type": "tool_result", "agent": "main", "name": message.get("name", ""),
                          "output": content[:1500], "id": message.get("tool_call_id", "")})
    return items


# --- export ---------------------------------------------------------------------------------

def find_thread(wanted: str = "", cwd: str = "") -> Path:
    """The saved chat called ``wanted`` (id, exact name, or part of a name); else ``cwd``'s newest."""
    from .config import list_threads

    threads = list_threads()  # newest first
    if wanted:
        slug, low = slugify_thread_name(wanted), wanted.lower()
        match = (next((t for t in threads if t["id"] == slug), None)
                 or next((t for t in threads if low in str(t["name"]).lower()), None))
        if match is None:
            raise TranscriptError(f"No saved chat matches “{wanted}”.")
        return match["path"]
    match = next((t for t in threads if not cwd or t["cwd"] == cwd), None)
    if match is None:
        raise TranscriptError("No saved chat for this folder yet.")
    return match["path"]


def export_name(name: str, fmt: str) -> str:
    return f"dcode-{slugify_thread_name(name)[:60]}.{fmt}"


def export_thread(path: Path, fmt: str) -> tuple[str, str]:
    """(file name, text) for the saved chat at ``path``."""
    try:
        payload = load_thread(path)
    except (OSError, ValueError) as exc:
        raise TranscriptError(f"Couldn't read that chat: {exc}") from exc
    return export_name(str(payload.get("name") or path.stem), fmt), export_text(payload, fmt)


def export_text(payload: dict[str, Any], fmt: str) -> str:
    if fmt not in FORMATS:
        raise TranscriptError(f"Unknown format “{fmt}”. Choose from: {', '.join(FORMATS)}.")
    if fmt == "json":
        return json.dumps(_portable(payload), indent=2, ensure_ascii=False) + "\n"
    return _markdown(payload) if fmt == "md" else _html(payload)


def _portable(payload: dict[str, Any]) -> dict[str, Any]:
    """The chat without the system prompt (it holds this machine's memory and project notes)."""
    messages = [m for m in payload.get("messages") or [] if isinstance(m, dict) and m.get("role") != "system"]
    return {
        "dcode_export": EXPORT_VERSION, "dcode_version": __version__,
        "name": str(payload.get("name") or ""), "created_at": str(payload.get("created_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""), "cwd": str(payload.get("cwd") or ""),
        "model": str(payload.get("model") or ""), "summary": str(payload.get("summary") or ""),
        "todos": [t for t in payload.get("todos") or [] if isinstance(t, dict)],
        "messages": messages,
    }


def _args_text(raw: Any) -> str:
    """The one argument worth showing for a tool call (the command, the path, the query…)."""
    args = raw
    if isinstance(raw, str):
        try:
            args = json.loads(raw or "{}")
        except ValueError:
            return raw[:200]
    if not isinstance(args, dict):
        return str(args)[:200]
    for key in ("command", "path", "sql", "task", "title", "name", "query", "pattern", "question"):
        if args.get(key):
            return " ".join(str(args[key]).split())[:200]
    return ""


def _clip(text: str, lines: int = OUTPUT_LINES) -> str:
    rows = (text or "").splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "\n".join(rows[:lines]) + f"\n… {len(rows) - lines} more lines"


def _turns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Events grouped for reading: each tool call carries its result."""
    events = transcript_from_messages([m for m in payload.get("messages") or [] if isinstance(m, dict)])
    results = {e["id"]: e["output"] for e in events if e["type"] == "tool_result" and e.get("id")}
    out = []
    for event in events:
        if event["type"] == "tool_result":
            continue
        if event["type"] == "tool_call":
            event = dict(event, output=results.get(event.get("id"), ""))
        out.append(event)
    return out


def _header(payload: dict[str, Any]) -> list[str]:
    bits = [str(payload.get("cwd") or ""), str(payload.get("model") or ""), str(payload.get("updated_at") or "")[:16].replace("T", " ")]
    return [b for b in bits if b]


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [f"# {payload.get('name') or 'Dcode chat'}", ""]
    header = _header(payload)
    if header:
        lines += ["*" + " · ".join(header) + f" · exported from Dcode {__version__}*", ""]
    for event in _turns(payload):
        kind = event["type"]
        if kind == "user_message":
            lines += ["## You", "", event["text"], ""]
        elif kind == "assistant_message":
            lines += ["## Dcode", "", event["text"], ""]
        else:
            detail = _args_text(event.get("args"))
            output = _clip(str(event.get("output") or ""))
            fence = _fence(output)
            lines += [f"> **{event.get('name')}**" + (f" `{detail}`" if detail else ""), ""]
            if output:
                lines += [fence, output, fence, ""]
    return "\n".join(lines).rstrip() + "\n"


def _render_markdown(text: str) -> str:
    """Markdown as HTML with raw HTML switched off; plain escaped text if the renderer is missing."""
    try:
        from markdown_it import MarkdownIt  # ships with rich; pure Python

        return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)
    except Exception:
        return f"<pre class=\"plain\">{html.escape(text)}</pre>"


_PAGE_CSS = """
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:24px;margin:0 0 4px}.meta{color:var(--muted);font-size:13px;margin:0 0 28px}
.user{display:flex;justify-content:flex-end;margin:22px 0 14px}
.user div{background:var(--user-bubble);border-radius:18px;padding:10px 16px;max-width:78%;white-space:pre-wrap;word-break:break-word}
.answer{margin:8px 0 18px;border-left:3px solid var(--primary);padding-left:16px}
.answer p{margin:.5em 0}.answer h1,.answer h2,.answer h3{font-size:1.05em;margin:1em 0 .3em}
code{font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--surface-2);padding:1px 5px;border-radius:5px}
pre{background:var(--code-bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px;overflow:auto;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre code{background:none;padding:0}pre.plain{white-space:pre-wrap;border:0;background:none;padding:0;font:inherit}
details{margin:4px 0;color:var(--muted);font-size:13.5px}summary{cursor:pointer;padding:3px 0}
summary b{color:var(--text-2);font-weight:600}details pre{margin:6px 0 10px}
table{border-collapse:collapse}td,th{border:1px solid var(--border);padding:4px 10px}
a{color:var(--primary-text)}footer{margin-top:40px;color:var(--muted);font-size:12.5px}
"""


def _html(payload: dict[str, Any]) -> str:
    from .theme import css_variables

    title = html.escape(str(payload.get("name") or "Dcode chat"))
    parts = [f"<h1>{title}</h1>", f"<p class=\"meta\">{html.escape(' · '.join(_header(payload)))}</p>"]
    for event in _turns(payload):
        kind = event["type"]
        if kind == "user_message":
            parts.append(f"<div class=\"user\"><div>{html.escape(event['text'])}</div></div>")
        elif kind == "assistant_message":
            parts.append(f"<div class=\"answer\">{_render_markdown(event['text'])}</div>")
        else:
            detail = html.escape(_args_text(event.get("args")))
            output = html.escape(_clip(str(event.get("output") or "")) or "(no output)")
            parts.append(f"<details><summary><b>{html.escape(str(event.get('name')))}</b> "
                         f"<code>{detail}</code></summary><pre>{output}</pre></details>")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(f"<footer>Exported from Dcode {__version__} on {stamp}. This page has no scripts and loads nothing.</footer>")
    # No scripts and no outside requests: the page opens the same on a machine with no network.
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            f"<title>{title}</title><style>{css_variables()}{_PAGE_CSS}</style></head>\n"
            f"<body><main>\n{chr(10).join(parts)}\n</main></body></html>\n")


# --- import ---------------------------------------------------------------------------------

def _clean_message(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("role") not in ("user", "assistant", "tool"):
        return None
    role = message["role"]
    content = message.get("content")
    clean: dict[str, Any] = {"role": role, "content": content if isinstance(content, str) else ""}
    if role == "assistant":
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                continue
            arguments = function.get("arguments")
            calls.append({"id": str(call.get("id") or ""), "type": "function", "function": {
                "name": function["name"][:80], "arguments": arguments if isinstance(arguments, str) else "{}"}})
        if calls:
            clean["tool_calls"] = calls
        elif not clean["content"]:
            return None
    elif role == "tool":
        clean["tool_call_id"] = str(message.get("tool_call_id") or "")
        clean["name"] = str(message.get("name") or "")[:80]
    return clean


def _valid_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only tool results that answer a call, and answer every call (servers reject gaps)."""
    out: list[dict[str, Any]] = []
    pending: list[str] = []

    def close() -> None:
        for call_id in pending:
            out.append({"role": "tool", "tool_call_id": call_id, "name": "", "content": "(result not in the imported file)"})
        pending.clear()

    for message in messages:
        if message["role"] == "tool":
            if message["tool_call_id"] in pending:
                pending.remove(message["tool_call_id"])
                out.append(message)
            continue
        close()
        out.append(message)
        if message["role"] == "assistant":
            pending.extend(call["id"] for call in message.get("tool_calls") or [])
    close()
    return out


def import_chat(raw: str | bytes | dict[str, Any]) -> dict[str, str]:
    """Save an exported chat (or a copied thread file) as a new chat. Returns its id and name."""
    data = raw
    if not isinstance(raw, dict):
        if len(raw) > MAX_IMPORT_BYTES:
            raise TranscriptError("That file is too big to be a Dcode chat.")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise TranscriptError("That isn't a Dcode chat export (the JSON format is the one that imports).") from exc
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise TranscriptError("That isn't a Dcode chat export: it has no messages.")
    if len(data["messages"]) > MAX_IMPORT_MESSAGES:
        raise TranscriptError("That chat has too many messages to import.")
    messages = _valid_history([m for m in map(_clean_message, data["messages"]) if m])
    if not any(m["role"] == "user" for m in messages):
        raise TranscriptError("That chat has no messages to import.")
    name = " ".join(str(data.get("name") or "").split())[:80] or "Imported chat"
    base, counter = name, 2
    while thread_path(name).exists():  # never overwrite a chat that is already here
        name = f"{base} ({counter})"
        counter += 1
    todos = [{"text": str(t.get("text", ""))[:300], "status": str(t.get("status", "pending"))}
             for t in data.get("todos") or [] if isinstance(t, dict)][:50]
    path = save_thread(
        name, [{"role": "system", "content": ""}, *messages],  # the prompt is rebuilt when the chat is continued
        created_at=str(data.get("created_at") or "") or None,
        extra={"cwd": str(data.get("cwd") or "")[:500], "model": str(data.get("model") or "")[:120],
               "summary": str(data.get("summary") or "")[:4000], "todos": todos, "imported": True},
    )
    return {"id": path.stem, "name": name, "messages": str(len(messages))}


def thread_file(thread_id: str) -> Path:
    """The file of a chat id (ids are file stems; anything else is dropped from the name)."""
    safe = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "_"
    return threads_dir() / f"{safe}.json"
