"""What the validator and the runner share: tasks, workspaces and scoring. Standard library only."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
TASKS = BENCH / "tasks"
LIB = BENCH / "lib"
CATEGORIES = ("coding", "devops", "long")
DIFFICULTIES = ("easy", "medium", "hard")
REQUIRED = {"id": str, "category": str, "difficulty": str, "prompt": str, "timeout_s": (int, float), "requires": list, "tags": list}
# Same bytes, same commit hash, in every run: the git line is part of the system prompt.
GIT_ENV = {"GIT_AUTHOR_NAME": "Bench", "GIT_AUTHOR_EMAIL": "bench@example.invalid", "GIT_AUTHOR_DATE": "2025-01-01T00:00:00+0000",
           "GIT_COMMITTER_NAME": "Bench", "GIT_COMMITTER_EMAIL": "bench@example.invalid", "GIT_COMMITTER_DATE": "2025-01-01T00:00:00+0000",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
GIT_EXCLUDES = "__pycache__/\n*.pyc\n.pytest_cache/\nnode_modules/\n"
NOT_COPIED = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", "node_modules")
CHECK_TIMEOUT = 180.0


class TaskError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    path: Path
    meta: dict[str, Any]

    @property
    def category(self) -> str:
        return self.meta["category"]

    @property
    def difficulty(self) -> str:
        return self.meta["difficulty"]

    @property
    def prompt(self) -> str:
        return self.meta["prompt"]

    @property
    def timeout_s(self) -> float:
        return float(self.meta["timeout_s"])

    @property
    def requires(self) -> list[str]:
        return list(self.meta["requires"])

    @property
    def repo(self) -> Path:
        return self.path / "repo"

    @property
    def check(self) -> Path:
        return self.path / "check"

    @property
    def solution(self) -> Path:
        return self.path / "solution.patch"


def load_task(path: Path) -> Task:
    """Read one task and refuse anything malformed: a broken task must not look like a failing agent."""
    meta_file = path / "task.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaskError(f"{path.name}: can't read task.json: {exc}") from exc
    for key, kind in REQUIRED.items():
        if not isinstance(meta.get(key), kind):
            raise TaskError(f"{path.name}: task.json needs {key}")
    if meta["id"] != path.name:
        raise TaskError(f"{path.name}: its id is {meta['id']!r}; they must match")
    if meta["category"] not in CATEGORIES:
        raise TaskError(f"{path.name}: category must be one of {', '.join(CATEGORIES)}")
    if meta["difficulty"] not in DIFFICULTIES:
        raise TaskError(f"{path.name}: difficulty must be one of {', '.join(DIFFICULTIES)}")
    if not meta["prompt"].strip() or meta["timeout_s"] <= 0:
        raise TaskError(f"{path.name}: needs a prompt and a positive timeout_s")
    if not all(isinstance(item, str) and item for item in meta["requires"] + meta["tags"]):
        raise TaskError(f"{path.name}: requires and tags hold names")
    if "max_steps" in meta and (not isinstance(meta["max_steps"], int) or meta["max_steps"] < 1):
        raise TaskError(f"{path.name}: max_steps must be a positive whole number")
    task = Task(path.name, path, meta)
    if not task.repo.is_dir():
        raise TaskError(f"{path.name}: has no repo/")
    if not (task.check / "check.sh").is_file():
        raise TaskError(f"{path.name}: has no check/check.sh")
    if not task.solution.is_file() or not task.solution.stat().st_size:
        raise TaskError(f"{path.name}: has no solution.patch")
    if (task.repo / "check").exists():
        raise TaskError(f"{path.name}: repo/ may not have a check/ folder; scoring puts its own there")
    return task


def load_tasks(root: Path = TASKS) -> list[Task]:
    if not root.is_dir():
        return []
    return [load_task(path) for path in sorted(root.iterdir()) if path.is_dir()]


def select_tasks(tasks: list[Task], patterns: list[str] | None) -> list[Task]:
    """Ids, globs or categories, comma or space separated; nothing means every task. A pattern that matches nothing is
    an error, since a typo would otherwise quietly shrink a study."""
    if not patterns:
        return tasks
    wanted = [word for pattern in patterns for word in pattern.split(",") if word]
    chosen = []
    for word in wanted:
        hits = [t for t in tasks if t.category == word or fnmatch.fnmatchcase(t.id, word)]
        if not hits:
            raise TaskError(f"no task matches {word!r}")
        chosen += hits
    return [t for t in tasks if t in chosen]


def missing_tools(task: Task) -> list[str]:
    return [name for name in task.requires if shutil.which(name) is None]


def tree_hash(path: Path) -> str:
    """Of a folder's names and contents: says whether two runs used the same task."""
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git(workspace: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=workspace, env={**(env or _plain_env()), **GIT_ENV}, capture_output=True,
                          text=True, errors="replace", check=check)


def _plain_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8", "HOME": tempfile.gettempdir()}


def prepare_workspace(task: Task, workspace: Path) -> str:
    """Copy the starting repo and make its one commit. Returns the commit, the same in every run."""
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(task.repo, workspace, symlinks=True, ignore=NOT_COPIED)
    git(workspace, "init", "-q", "-b", "main")
    (workspace / ".git" / "info" / "exclude").write_text(GIT_EXCLUDES, encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "Initial commit")
    return git(workspace, "rev-parse", "HEAD").stdout.strip()


def capture_diff(workspace: Path, base: str) -> str:
    """Everything the agent changed since the first commit, new files and its own commits included."""
    git(workspace, "add", "-A")
    return git(workspace, "diff", "--cached", "--binary", base).stdout


def kill_group(process: subprocess.Popen, grace: float = 0.0, first: int = signal.SIGTERM) -> None:
    """End a process and everything it started. With a grace period it may finish its records first."""
    try:
        os.killpg(process.pid, first)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)  # the agent's own leftovers, even if it exited
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def score(final_workspace: Path, task: Task, timeout: float = CHECK_TIMEOUT, keep_log: Path | None = None) -> dict[str, Any]:
    """Run the check in a clean copy of the workspace. The check sees the files as they ended, nothing else."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="dcode-bench-check-") as scratch:
        clean = Path(scratch) / "workspace"
        shutil.copytree(final_workspace, clean, symlinks=True, ignore=NOT_COPIED)
        target = clean / "check"
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)  # an agent's own check/ folder must not stand in for the real one
        shutil.copytree(task.check, target)
        home = Path(scratch) / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C.UTF-8", "TZ": "UTC",
               "PYTHONPATH": str(LIB), "BENCH_LIB": str(LIB), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
               "TMPDIR": scratch, "NO_COLOR": "1"}
        for name in ("BENCH_NO_REAL_TOOLS", "DOCKER_HOST"):
            if name in os.environ:
                env[name] = os.environ[name]
        process = subprocess.Popen(["bash", "check/check.sh"], cwd=clean, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", start_new_session=True)
        timed_out = False
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(process)
            output, _ = process.communicate()
        finally:
            if process.poll() is None:
                kill_group(process)
    if keep_log is not None:
        keep_log.write_text(output, encoding="utf-8")
    return {"passed": process.returncode == 0 and not timed_out, "exit_code": process.returncode, "timed_out": timed_out,
            "seconds": round(time.perf_counter() - started, 3), "output": output}
