"""``dcode run``: one task, unattended, output as plain text (used by scheduled jobs).

There is no terminal and nobody to answer, so every approval is denied and every question
is skipped. What the task may do without asking is decided by ``--mode`` (and the user's
rules): ``plan`` reads only, ``accept_edits`` may edit files, ``auto`` may also run commands.

The study flags are the command-line form of what the environment can already say (``DCODE_TASK_ID``,
``DCODE_TRACE``, ``DCODE_EXPERIMENT``) plus a step limit and a one-file summary for a harness to read.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

USAGE = ("usage: dcode run [--mode plan|accept_edits|auto] [--cwd FOLDER] [--task-id ID] [--trace PATH] "
         "[--experiment FILE] [--max-steps N] [--json-summary PATH] PROMPT   (PROMPT '-' reads standard input)")
# Flag -> the variable it sets. Set before the config loads, so a flag and its variable can't disagree.
ENV_FLAGS = {"--task-id": "DCODE_TASK_ID", "--trace": "DCODE_TRACE", "--experiment": "DCODE_EXPERIMENT"}
VALUE_FLAGS = (*ENV_FLAGS, "--max-steps", "--json-summary")
PATH_FLAGS = ("--trace", "--experiment", "--json-summary")


def _say(text: str = "") -> None:
    print(text, flush=True)


def _args(args: Any) -> str:
    if not isinstance(args, dict):
        return str(args)[:160]
    for key in ("command", "path", "sql", "task", "title", "name", "query", "pattern"):
        if args.get(key):
            return " ".join(str(args[key]).split())[:160]
    return ""


def _write_summary(path: str, summary: dict[str, Any]) -> None:
    """Whole or not at all, and never the reason a finished task reports a failure."""
    target = Path(path)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)
    except OSError as exc:
        _say(f"! Could not write the summary to {path}: {exc}")


def _summary(session: Any, usage: dict[str, Any] | None, failed: list[Any], code: int, wall: float, task_id: str) -> dict[str, Any]:
    """What a harness needs without parsing a trace. Tokens of a task that failed are still in the session's
    ledger, since no usage event took them."""
    from .trace import add_usage

    totals: dict[str, Any] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cached": None}
    if usage:
        add_usage(totals, usage)
    add_usage(totals, session._side_jobs.drain())
    reason = session.agent._end_reason
    if failed and reason == "final_answer":  # it stopped before the agent's loop could say why
        reason = "error"
    return {"task_id": task_id, "end_reason": reason, "exit_code": code, "steps": session.agent._steps_done,
            "calls": totals["calls"], "prompt_tokens": totals["prompt"], "completion_tokens": totals["completion"],
            "total_tokens": totals["total"], "cached_tokens": totals["cached"],
            "usage_estimated": bool(usage and usage.get("usage_estimated")), "wall_s": round(wall, 3),
            "model": session.config.model}


def main(argv: list[str]) -> int:
    mode, cwd, words = "accept_edits", "", []
    flags: dict[str, str] = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--mode", "--cwd") and index + 1 < len(argv):
            index += 1
            mode, cwd = (argv[index], cwd) if arg == "--mode" else (mode, argv[index])
        elif arg in VALUE_FLAGS:
            index += 1
            if index >= len(argv):
                _say(f"{arg} needs a value\n{USAGE}")
                return 2
            # Named from where the command was started: --cwd below moves the process.
            flags[arg] = os.path.abspath(os.path.expanduser(argv[index])) if arg in PATH_FLAGS else argv[index]
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
    max_steps = flags.get("--max-steps")
    if max_steps is not None and not max_steps.isdigit():
        _say(f"--max-steps needs a whole number, not {max_steps!r}\n{USAGE}")
        return 2
    if cwd:
        os.chdir(os.path.expanduser(cwd))

    from . import experiment, trace
    from .agents import MODES
    from .config import load_config
    from .session import Session

    for flag, variable in ENV_FLAGS.items():
        if flag in flags:
            os.environ[variable] = flags[flag]
    summary_path = flags.get("--json-summary")
    if summary_path and not os.environ.get("DCODE_TASK_ID"):
        os.environ["DCODE_TASK_ID"] = trace.new_task_id()  # so the summary can name the task the trace holds

    if mode not in MODES:
        _say(f"Unknown mode {mode!r}. Choose from: {', '.join(MODES)}.")
        return 2
    config = load_config()
    if config is None:
        _say("Dcode is not set up yet: run `dcode` once in a terminal, or set DCODE_BASE_URL, DCODE_API_KEY and DCODE_MODEL.")
        return 2
    if max_steps is not None:
        config.experiment = experiment.clean({**config.experiment, "main_max_steps": int(max_steps)})
    started = time.perf_counter()
    session = Session(config)
    session.set_mode(mode, source="run")
    failed: list[Any] = []
    denied: list[str] = []
    final_usage: dict[str, Any] = {}
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
            final_usage.update(event)
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
    code = 1 if failed else 0
    if denied and not failed:
        _say(f"# {len(denied)} action(s) needed approval and were denied ({', '.join(sorted(set(denied)))}). "
             "Nobody answers an unattended run: give the job a mode that allows them.")
        code = 3
    if summary_path:
        _write_summary(summary_path, _summary(session, final_usage, failed, code, time.perf_counter() - started,
                                              os.environ.get("DCODE_TASK_ID", "")))
    return code
