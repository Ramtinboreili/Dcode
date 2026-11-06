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
