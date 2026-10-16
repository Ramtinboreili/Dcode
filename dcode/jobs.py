"""Background jobs: long-running commands (dev servers, watchers, builds).

``run_command`` with ``background: true`` starts a job and returns at once;
the model checks it later with ``job_status`` and stops it with ``job_stop``.
Output goes to a log file per job. All jobs are stopped when the session ends.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import shell

TAIL_LINES = 40


@dataclass
class Job:
    id: int
    command: str
    cwd: str
    log_path: Path
    process: subprocess.Popen
    started: float = field(default_factory=time.time)
    ended: float | None = None
    stopped_by_user: bool = False

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.process.poll()

    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def tail(self, lines: int = TAIL_LINES) -> str:
        try:
            data = self.log_path.read_bytes()[-64_000:].decode("utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(shell.strip_ansi(data).splitlines()[-lines:])

    def state(self) -> str:
        if self.running:
            return "running"
        if self.stopped_by_user:
            return "stopped"
        return f"exited ({self.exit_code})"

    def as_dict(self) -> dict:
        return {"id": self.id, "command": self.command, "state": self.state(), "running": self.running,
                "exit_code": self.exit_code, "elapsed": round(self.elapsed(), 1), "pid": self.process.pid}


class JobManager:
    def __init__(self, log_dir: Path, on_exit: Callable[[Job], None] | None = None) -> None:
        self.log_dir = log_dir
        self.jobs: dict[int, Job] = {}
        self._next = 1
        self._lock = threading.Lock()
        self._on_exit = on_exit

    def start(self, command: str, cwd: str) -> Job:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            job_id = self._next
            self._next += 1
        log_path = self.log_dir / f"job-{os.getpid()}-{job_id}.log"
        log = open(log_path, "wb")
        kwargs: dict = {"shell": True, "cwd": cwd, "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                        "executable": shell.bash(), "env": shell.environment()}
        if os.name == "posix":
            kwargs["start_new_session"] = True  # own process group, so stop() kills its children too
        process = subprocess.Popen(command, **kwargs)
        log.close()
        job = Job(job_id, command, cwd, log_path, process)
        with self._lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._watch, args=(job,), daemon=True, name=f"dcode-job-{job_id}").start()
        return job

    def _watch(self, job: Job) -> None:
        job.process.wait()
        job.ended = time.time()
        if self._on_exit:
            try:
                self._on_exit(job)
            except Exception:
                pass

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.id)

    def running(self) -> list[Job]:
        return [j for j in self.list() if j.running]

    def stop(self, job_id: int, timeout: float = 3.0) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if not job.running:
            return _stop_leftovers(job)
        job.stopped_by_user = True
        try:
            if os.name == "posix":
                os.killpg(job.process.pid, signal.SIGTERM)
            else:
                job.process.terminate()
            job.process.wait(timeout=timeout)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(job.process.pid, signal.SIGKILL)
                else:
                    job.process.kill()
            except (ProcessLookupError, PermissionError):
                pass
        return True

    def stop_all(self) -> None:
        for job in list(self.jobs.values()):
            if job.running:
                self.stop(job.id, timeout=1.5)
            else:
                _stop_leftovers(job)


def _stop_leftovers(job: Any) -> bool:
    """A finished job whose children live on (``server &``, a daemon) still owns its process group: end it."""
    if os.name != "posix":
        return False
    try:
        os.killpg(job.process.pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        os.killpg(job.process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    job.stopped_by_user = True
    return True
