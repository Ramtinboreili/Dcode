"""``dcode run``: one task, unattended, output as plain text (used by scheduled jobs).

There is no terminal and nobody to answer, so every approval is denied and every question
is skipped. What the task may do without asking is decided by ``--mode`` (and the user's
rules): ``plan`` reads only, ``accept_edits`` may edit files, ``auto`` may also run commands.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

USAGE = "usage: dcode run [--mode plan|accept_edits|auto] [--cwd FOLDER] PROMPT   (PROMPT '-' reads standard input)"


def _say(text: str = "") -> None:
    print(text, flush=True)


def _args(args: Any) -> str:
    if not isinstance(args, dict):
        return str(args)[:160]
    for key in ("command", "path", "sql", "task", "title", "name", "query", "pattern"):
        if args.get(key):
            return " ".join(str(args[key]).split())[:160]
    return ""


def main(argv: list[str]) -> int:
    mode, cwd, words = "accept_edits", "", []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--mode", "--cwd") and index + 1 < len(argv):
            index += 1
            mode, cwd = (argv[index], cwd) if arg == "--mode" else (mode, argv[index])
        elif arg in ("-h", "--help"):
            _say(USAGE)
            return 0
        else:
            words.append(arg)
        index += 1
    prompt = sys.stdin.read() if words == ["-"] else " ".join(words)
    if not prompt.strip():
        _say(USAGE)
        return 2
    if cwd:
        os.chdir(os.path.expanduser(cwd))

    from .agents import MODES
    from .config import load_config
    from .session import Session

    if mode not in MODES:
        _say(f"Unknown mode {mode!r}. Choose from: {', '.join(MODES)}.")
        return 2
    config = load_config()
    if config is None:
        _say("Dcode is not set up yet: run `dcode` once in a terminal, or set DCODE_BASE_URL, DCODE_API_KEY and DCODE_MODEL.")
        return 2
    session = Session(config)
    session.set_mode(mode, source="run")
    failed: list[Any] = []
    denied: list[str] = []
    # A name of its own, so runs don't pile into one saved chat (or get an auto title).
    label = os.environ.get("DCODE_CRON_JOB") or "run"
    session.thread_name = f"{label} {time.strftime('%Y-%m-%d %H-%M-%S')}"

    def on_event(event: dict[str, Any]) -> None:
        kind, agent = event["type"], event.get("agent", "main")
        tag = "" if agent == "main" else f"[{agent}] "
        if kind == "tool_call" and event.get("name") != "todo_write":
            _say(f"· {tag}{event.get('name')} {_args(event.get('args'))}".rstrip())
        elif kind == "tool_result":
            lines = str(event.get("output") or "").strip().splitlines() or [""]
            _say(f"    {lines[0][:200]}" + (f"  (+{len(lines) - 1} lines)" if len(lines) > 1 else ""))
        elif kind == "approval_request":
            # Unattended: nobody can answer. Deny, and say so in the log.
            what = "question skipped" if event.get("kind") == "question" else f"needs approval ({event.get('kind')}): denied"
            _say(f"✕ {what} — {' '.join(str(event.get('detail') or '').split())[:200]}")
            denied.append(str(event.get("kind")))
            session.resolve_approval(event["id"], False, by="run")
        elif kind == "assistant_message" and agent == "main" and event.get("text"):
            _say("\n" + event["text"].strip() + "\n")
        elif kind == "error":
            failed.append(event.get("text"))
            _say(f"! {event.get('text')}")
        elif kind == "notice" and event.get("level") in ("warning", "error"):
            _say(f"! {event.get('text')}")
        elif kind == "route":
            _say(f"# reasoning model: {event.get('model')} · {event.get('reason')}")
        elif kind == "usage" and agent == "main":
            changed = ", ".join(event.get("files_changed") or []) or "none"
            _say(f"# done in {event.get('elapsed')}s · {event.get('model')} · files changed: {changed} · "
                 f"commands: {event.get('commands', 0)}")

    session.bus.subscribe(on_event)
    _say(f"# dcode run · mode {mode} · {session.cwd} · {config.model}")
    _say(f"> {prompt.strip()}\n")
    try:
        session.submit(prompt, source="run")
        while not session.idle:
            time.sleep(0.1)
    except KeyboardInterrupt:
        session.interrupt(source="run")
        failed.append("interrupted")
    finally:
        session.close()
    if denied and not failed:
        _say(f"# {len(denied)} action(s) needed approval and were denied ({', '.join(sorted(set(denied)))}). "
             "Nobody answers an unattended run: give the job a mode that allows them.")
        return 3
    return 1 if failed else 0
