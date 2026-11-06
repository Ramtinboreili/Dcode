"""Helpers for task checks. A check runs in a clean copy of the agent's final workspace, with its files in ``check/``.

    from benchcheck import ok, finish, load_yaml
    ok(load_yaml("svc.yaml")["spec"]["port"] == 80, "the service listens on 80")
    finish()

Every ``ok`` is reported, so a log shows all that is wrong at once. Checks must be deterministic and offline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, os.getcwd())  # the workspace's own modules
import miniyaml  # noqa: E402

_failures: list[str] = []


def ok(condition: Any, message: str) -> bool:
    print(("PASS " if condition else "FAIL ") + message, flush=True)
    if not condition:
        _failures.append(message)
    return bool(condition)


def finish() -> None:
    print(f"{len(_failures)} failed", flush=True)
    sys.exit(1 if _failures else 0)


def read(path: str) -> str:
    """The file's text, or an empty string if it is missing (the assertions then say what's wrong)."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def exists(path: str) -> bool:
    return Path(path).exists()


def load_yaml(path: str) -> Any:
    return miniyaml.load(read(path))


def load_yaml_all(path: str) -> list[Any]:
    return miniyaml.load_all(read(path))


def load_json(path: str) -> Any:
    return json.loads(read(path))


def dig(value: Any, *keys: Any, default: Any = None) -> Any:
    """``dig(doc, "spec", "ports", 0, "port")``: the nested value, or ``default`` if any step is missing."""
    for key in keys:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            return default
    return value


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def run(*command: str, timeout: float = 60, input: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a command, never raising for its exit code. A timeout comes back as exit code 124."""
    full = {**os.environ, **(env or {})}
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, input=input, cwd=cwd, env=full,
                              errors="replace")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, str(exc.stdout or ""), f"timed out after {timeout}s")


def tool_enabled(binary: str) -> bool:
    """An optional real tool: present, and not switched off with BENCH_NO_REAL_TOOLS=1."""
    return have(binary) and os.environ.get("BENCH_NO_REAL_TOOLS") != "1"


def docker_build_if_possible(base_image: str, context: str = ".", dockerfile: str = "Dockerfile") -> bool | None:
    """Build for real when that can work offline: no network, never pulling, base image already on this host.

    True or False is the build's result. None means it couldn't be tried, and says why, so the static asserts decide.
    """
    if not tool_enabled("docker"):
        print("SKIP real build: docker isn't available", flush=True)
        return None
    if run("docker", "image", "inspect", base_image, timeout=30).returncode != 0:
        print(f"SKIP real build: {base_image} isn't on this host and a check may not pull it", flush=True)
        return None
    tag = f"dcode-bench-check:{uuid.uuid4().hex[:12]}"
    built = run("docker", "build", "--pull=false", "--network=none", "-q", "-t", tag, "-f", dockerfile, context, timeout=180)
    if built.returncode == 0:
        run("docker", "image", "rm", "-f", tag, timeout=60)
        return True
    print(built.stderr[-1500:], flush=True)
    return False


def nginx_parse(text: str) -> list[tuple[str, list[str], Any]]:
    """nginx config as (directive, args, children or None). Raises ValueError for unbalanced braces or a missing
    final semicolon. A semicolon missing in the middle shows up as extra args, which a check's asserts catch."""
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
        elif char == "#":
            while index < len(text) and text[index] != "\n":
                index += 1
        elif char in "{};":
            tokens.append(char)
            index += 1
        elif char in "\"'":
            end = text.find(char, index + 1)
            if end < 0:
                raise ValueError("unterminated string")
            tokens.append(text[index + 1:end])
            index = end + 1
        else:
            start = index
            while index < len(text) and not text[index].isspace() and text[index] not in "{};":
                index += 1
            tokens.append(text[start:index])

    def block(position: int, nested: bool) -> tuple[list[tuple[str, list[str], Any]], int]:
        out: list[tuple[str, list[str], Any]] = []
        words: list[str] = []
        while position < len(tokens):
            token = tokens[position]
            position += 1
            if token == ";":
                if not words:
                    raise ValueError("a stray ;")
                out.append((words[0], words[1:], None))
                words = []
            elif token == "{":
                if not words:
                    raise ValueError("a block without a name")
                children, position = block(position, True)
                out.append((words[0], words[1:], children))
                words = []
            elif token == "}":
                if not nested or words:
                    raise ValueError("unexpected }, or a directive before it has no ;")
                return out, position
            else:
                words.append(token)
        if nested or words:
            raise ValueError("unexpected end of file: a missing } or ;")
        return out, position

    return block(0, False)[0]


def nginx_find(tree: list[tuple[str, list[str], Any]], name: str) -> list[tuple[str, list[str], Any]]:
    """Every directive with this name, at any depth."""
    found = []
    for entry in tree:
        if entry[0] == name:
            found.append(entry)
        if entry[2]:
            found += nginx_find(entry[2], name)
    return found


def nginx_test_if_possible(config: str) -> bool | None:
    """``nginx -t`` when nginx is here. None if it can't be tried."""
    if not tool_enabled("nginx"):
        print("SKIP nginx -t: nginx isn't available", flush=True)
        return None
    prefix = os.path.abspath(os.path.dirname(config) or ".")
    result = run("nginx", "-t", "-e", "stderr", "-p", prefix + "/", "-c", os.path.abspath(config), "-g", f"pid {prefix}/nginx.pid;", timeout=30)
    print(result.stderr[-1000:], flush=True)
    return result.returncode == 0
