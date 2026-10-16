"""run_command, made the same for every model.

Models trained on different agents send a command in different shapes (``["bash", "-lc", "ls"]``,
a fenced block, ``$ ls``, ``cmd``/``workdir`` keys) and expect a real terminal. ``clean_command``
turns each shape into one command line; ``refusal`` stops commands that would wait for a keypress
or never end (without a terminal they only burn the timeout); ``run`` executes with no stdin, no
pager and no colours, in bash, and kills the whole process group on timeout.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_SHELL_FLAGS = {"-c", "-lc", "-cl", "-ic", "-ci", "-lic", "-ilc", "-ec", "-xc", "-euc"}
_FENCE_RE = re.compile(r"^\s*```[\w-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)
_LONE_AMP_RE = re.compile(r"(?<![&|>])&\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]")
MAX_TIMEOUT = 600

_STEP = r"[^|;&\n]*"  # within one command of a pipeline or list
# (pattern, why it can't run, what to do instead). Pagers and top are left alone: without a terminal
# they print once (or fail at once). These wait for a keypress, or never end and only burn the timeout.
_ANYWHERE = [
    (rf"\btail\b{_STEP}\s-[a-zA-Z]*[fF]", "tail -f never ends", "use tail -n 200, or background=true"),
    (rf"\bjournalctl\b{_STEP}(\s-[a-zA-Z]*f\b|--follow)", "journalctl -f never ends",
     "use journalctl -n 200 --no-pager, or background=true"),
    (rf"\b(kubectl|oc|docker|podman|nerdctl|crictl)\b{_STEP}\blogs\b{_STEP}(\s-[a-zA-Z]*f\b|--follow)",
     "logs -f never ends", "use --tail=200 without -f, or background=true"),
    (rf"\b(kubectl|oc)\b{_STEP}\s(-w|--watch)\b", "a watch never ends", "run it once without -w"),
    (r"^\s*(sudo\s+)?watch\b", "watch never ends", "run the command once"),
    (rf"^\s*(sudo\s+)?ping6?\b(?!{_STEP}\s-[a-zA-Z]*[cw]\s*\d)", "ping never ends", "use ping -c 4"),
    (rf"\b(kubectl|oc)\b{_STEP}\bedit\b", "kubectl edit opens an editor", "use kubectl patch or kubectl apply"),
]
_FIRST = [  # only where nothing is piped in: "curl … | sh" is fine
    (r"^\s*(sudo\s+)?(vi|vim|nvim|nano|emacs|pico|joe|mcedit|micro)\b", "an editor waits for keys",
     "use edit_file or write_file"),
    (r"^\s*(python3?|node|irb|ipython|bash|sh|zsh|ssh\s+\S+|telnet|mysql|psql|redis-cli|mongo|mongosh|"
     r"sqlite3(\s+\S+)?|R|julia|ghci|lua)\s*$", "an interactive shell waits for input",
     "pass the script or query as arguments (python3 -c '…', psql -c '…', ssh host 'cmd')"),
]
_ANYWHERE_RE = [(re.compile(p), why, fix) for p, why, fix in _ANYWHERE]
_FIRST_RE = [(re.compile(p), why, fix) for p, why, fix in _FIRST]
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_LIST_RE = re.compile(r"&&|\|\||;|\n")


def clean_command(args: dict[str, Any]) -> dict[str, Any]:
    """run_command arguments in Dcode's shape: ``command`` is one string, ``timeout`` in seconds."""
    args = dict(args)
    command = args.get("command")
    if command is None:
        for key in ("cmd", "script", "bash", "shell", "commands", "command_line", "shell_command", "code", "input"):
            if args.get(key) is not None:
                command = args.pop(key)
                break
    if isinstance(command, list):
        parts = [str(p) for p in command]
        if len(parts) >= 3 and Path(parts[0]).name in SHELLS and parts[1] in _SHELL_FLAGS:
            command = parts[2]  # ["bash", "-lc", "ls -la"] (GPT/Codex style)
        elif parts and all(" " in p or "&&" in p for p in parts):
            command = " && ".join(parts)  # a list of whole commands
        else:
            command = shlex.join(parts)
    command = str(command or "").strip()
    fenced = _FENCE_RE.match(command)
    if fenced:
        command = fenced.group(1).strip()
    lines = command.splitlines()
    if lines and all(line.startswith("$ ") for line in lines if line.strip()):
        command = "\n".join(line[2:] for line in lines)  # a pasted prompt
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    if len(words) == 3 and Path(words[0]).name in SHELLS and words[1] in _SHELL_FLAGS:
        command = words[2]  # bash -lc '…' around the whole thing: we already run bash
    workdir = next((args.pop(k) for k in ("workdir", "cwd", "working_directory", "directory", "dir")
                    if args.get(k)), None)
    if workdir and str(workdir).strip() not in (".", "./", str(_root())):
        command = f"cd {shlex.quote(str(workdir).strip())} && {command}"
    for key in ("background", "run_in_background", "is_background", "detach", "async", "daemon"):
        if key in args:
            value = args.pop(key)
            args["background"] = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes")
            break
    if _LONE_AMP_RE.search(command):
        # "server &": a job, not a hang. Strip it even with background=true (Qwen3-Coder sends both): left in,
        # bash detaches the server and exits, so the job looks finished and job_stop can't reach it.
        command, args["background"] = _LONE_AMP_RE.sub("", command).rstrip(), True
    if "timeout_ms" in args:
        ms = args.pop("timeout_ms")
        args["timeout"] = float(ms) / 1000 if _number(ms) else None
    if "timeout" in args:
        value = args.pop("timeout")
        if _number(value):
            args["timeout"] = max(1, min(MAX_TIMEOUT, int(float(value))))
    if command:
        args["command"] = command
    return args


def _number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _root() -> Path:
    from .tools import workspace_root  # local import: tools imports this module

    return workspace_root()


def refusal(command: str) -> str:
    """Why this command can't run without a terminal, and what to run instead ('' = fine)."""
    unquoted = _QUOTED_RE.sub("''", command)  # words inside quotes are data: echo 'tail -f' is fine
    for listed in _LIST_RE.split(unquoted):
        pipeline = listed.split("|")
        checks = [(_FIRST_RE, pipeline[0])] + [(_ANYWHERE_RE, part) for part in pipeline]
        for rules, part in checks:
            for pattern, why, fix in rules:
                if pattern.search(part):
                    return f"Not run: {why} and there is no terminal here. Instead, {fix}."
    return ""


def environment() -> dict[str, str]:
    """The environment for a model's commands: nothing waits for input, nothing pages, no colours."""
    env = dict(os.environ)
    env.update({
        "PAGER": "cat", "GIT_PAGER": "cat", "MANPAGER": "cat", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0",
        "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "EDITOR": "true", "VISUAL": "true",
        "DEBIAN_FRONTEND": "noninteractive", "PIP_NO_INPUT": "1", "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb",
    })
    return env


def bash() -> str | None:
    return shutil.which("bash") or None  # models write bash; /bin/sh is dash on Debian and Ubuntu


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r\n", "\n")


def run(command: str, cwd: Path, timeout: int) -> tuple[str, int | None]:
    """(output, return code; None = timed out). Never reads the user's terminal."""
    process = subprocess.Popen(
        command, shell=True, executable=bash(), cwd=str(cwd), env=environment(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,  # its own process group: a timeout stops the children too
    )
    try:
        out, _ = process.communicate(timeout=timeout)
        code: int | None = process.returncode
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a child left the group and still holds the pipe
            process.kill()
            out = exc.output or b""
        code = None
    return strip_ansi((out or b"").decode("utf-8", errors="replace")), code
