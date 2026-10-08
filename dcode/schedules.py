"""Scheduled jobs (cron): a shell command or a Dcode prompt, run on a schedule, with a log per run.

Jobs live in ``~/.dcode/cron/jobs.json`` and are yours alone: nothing in a project folder
can add one, and the model has no tool for it. The per-user hub runs them (see
:class:`Scheduler`), so they fire even when no terminal is open; the hub stays alive
while at least one job is enabled.

* **command** jobs run through the shell, like crontab.
* **prompt** jobs start an unattended Dcode (``dcode run``) in the job's folder. Nobody is
  there to answer, so anything that would ask for approval is denied: pick the mode that
  allows what the job needs.

Schedules are standard five-field cron expressions in the machine's local time. Runs that
were due while the hub was down are not caught up.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import home_dir, write_private_json

KINDS = ("command", "prompt")
PROMPT_MODES = ("plan", "accept_edits", "auto")  # manual would deny everything: nobody can answer
DEFAULT_TIMEOUT = 3600        # seconds one run may take
MAX_TIMEOUT = 24 * 3600
KEEP_RUNS = 50                # runs (and their logs) kept per job
MAX_LOG_BYTES = 5_000_000     # a run's log is cut here; the job keeps running
TICK = 5.0                    # seconds between scheduler checks

_MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))
_NAMES = ({}, {}, {},
          {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)},
          {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())})
_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


class ScheduleError(ValueError):
    """A schedule or job that can't be saved; the text is for the user."""


# --- cron expressions ----------------------------------------------------------------------

def _field(text: str, index: int) -> set[int]:
    label, low, high = _FIELDS[index]
    values: set[int] = set()
    for part in text.lower().split(","):
        body, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if step < 1:
                raise ValueError
            if body in ("*", "?"):
                start, end = low, high
            else:
                first, dash, last = body.partition("-")
                start = _NAMES[index].get(first, None)
                start = int(first) if start is None else start
                end = (_NAMES[index].get(last) if last in _NAMES[index] else int(last)) if dash else (high if step_text else start)
        except (ValueError, TypeError):
            raise ScheduleError(f"Can't read '{part}' in the {label} field.") from None
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise ScheduleError(f"The {label} field must be between {low} and {high} (got '{part}').")
        values.update(range(start, end + 1, step))
    if index == 4 and 7 in values:  # 0 and 7 are both Sunday
        values = (values - {7}) | {0}
    return values


def parse(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Five sets (minute, hour, day, month, weekday) + whether day and weekday are restricted."""
    text = " ".join(str(expression or "").split())
    text = _MACROS.get(text.lower(), text)
    parts = text.split(" ")
    if len(parts) != 5:
        raise ScheduleError("A schedule has five fields: minute hour day-of-month month day-of-week "
                            "(for example '*/15 * * * *' or '30 2 * * 1-5'), or @hourly, @daily, @weekly, @monthly.")
    sets = [_field(part, index) for index, part in enumerate(parts)]
    return (sets[0], sets[1], sets[2], sets[3], sets[4], parts[2] not in ("*", "?"), parts[4] not in ("*", "?"))


def _day_matches(spec: tuple, moment: datetime) -> bool:
    _, _, days, months, weekdays, day_set, weekday_set = spec
    if moment.month not in months:
        return False
    by_day = moment.day in days
    by_weekday = (moment.isoweekday() % 7) in weekdays
    # Cron's rule: with both restricted, either one is enough.
    return (by_day or by_weekday) if (day_set and weekday_set) else (by_day and by_weekday)


def matches(expression: str, moment: datetime) -> bool:
    spec = parse(expression)
    return moment.minute in spec[0] and moment.hour in spec[1] and _day_matches(spec, moment)


def next_run(expression: str, after: datetime | None = None) -> datetime | None:
    """The first matching minute after ``after`` (local time), or None if there is none within 5 years."""
    spec = parse(expression)
    moment = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = moment + timedelta(days=366 * 5)
    while moment < limit:
        if not _day_matches(spec, moment):
            moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
        elif moment.hour not in spec[1]:
            moment = (moment + timedelta(hours=1)).replace(minute=0)
        elif moment.minute not in spec[0]:
            moment += timedelta(minutes=1)
        else:
            return moment
    return None


def explain(expression: str) -> str:
    """A schedule in words for the common shapes; the expression itself otherwise."""
    text = " ".join(str(expression or "").split())
    text = _MACROS.get(text.lower(), text)
    minute, hour, day, month, weekday = (text.split(" ") + [""] * 5)[:5]
    every = re.fullmatch(r"\*/(\d+)", minute)
    if (hour, day, month, weekday) == ("*", "*", "*", "*"):
        if minute == "*":
            return "every minute"
        if every:
            return f"every {every.group(1)} minutes"
        if minute.isdigit():
            return f"every hour at :{int(minute):02d}"
    hours = re.fullmatch(r"\*/(\d+)", hour)
    if minute.isdigit() and hours and (day, month, weekday) == ("*", "*", "*"):
        return f"every {hours.group(1)} hours at :{int(minute):02d}"
    if minute.isdigit() and hour.isdigit() and month == "*":
        at = f"{int(hour):02d}:{int(minute):02d}"
        if (day, weekday) == ("*", "*"):
            return f"every day at {at}"
        if day == "*" and weekday in ("1-5", "mon-fri"):
            return f"Monday to Friday at {at}"
        if day == "*" and weekday.isdigit():
            return f"every {_DAYS[int(weekday) % 7]} at {at}"
        if day.isdigit() and weekday == "*":
            return f"on day {int(day)} of every month at {at}"
    return text


# --- jobs on disk -----------------------------------------------------------------------------

def cron_dir() -> Path:
    return home_dir() / "cron"


def _jobs_file() -> Path:
    return cron_dir() / "jobs.json"


def _job_dir(job_id: str) -> Path:
    return cron_dir() / "logs" / clean_id(job_id)


def clean_id(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(text or "").strip().lower()).strip("-")[:40]


def _read_json(path: Path, default: Any) -> Any:
    import json

    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def load_jobs() -> dict[str, dict[str, Any]]:
    data = _read_json(_jobs_file(), {})
    items = data.get("jobs") if isinstance(data, dict) else None
    return {j["id"]: j for j in items or [] if isinstance(j, dict) and j.get("id")}


_store_lock = threading.Lock()


def _store(jobs: dict[str, dict[str, Any]]) -> None:
    write_private_json(_jobs_file(), {"jobs": list(jobs.values())})


def has_enabled_jobs() -> bool:
    return any(job.get("enabled") for job in load_jobs().values())


def save_job(data: dict[str, Any]) -> dict[str, Any]:
    """Create a job, or update the one whose ``id`` is given."""
    name = " ".join(str(data.get("name") or "").split())[:80]
    command = str(data.get("command") or "").strip()
    kind = str(data.get("kind") or "command")
    if kind not in KINDS:
        raise ScheduleError("A job is either a 'command' or a 'prompt'.")
    if not command:
        raise ScheduleError("Give the command to run." if kind == "command" else "Write what Dcode should do.")
    schedule = " ".join(str(data.get("schedule") or "").split())
    parse(schedule)
    if next_run(schedule) is None:
        raise ScheduleError("That schedule never comes up (check the day and month).")
    cwd = str(data.get("cwd") or "").strip()
    if cwd and not Path(cwd).expanduser().is_dir():
        raise ScheduleError(f"The folder {cwd} doesn't exist.")
    if kind == "prompt" and not cwd:
        raise ScheduleError("A Dcode prompt needs the project folder to work in.")
    mode = str(data.get("mode") or "accept_edits")
    try:
        timeout = min(MAX_TIMEOUT, max(10, int(data.get("timeout") or DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    with _store_lock:
        jobs = load_jobs()
        job_id = clean_id(data.get("id") or "")
        previous = jobs.get(job_id)
        if previous is None:
            base = clean_id(name or command.split()[0]) or "job"
            job_id, count = base, 1
            while job_id in jobs:
                count += 1
                job_id = f"{base}-{count}"
        job = {
            "id": job_id, "name": name or command[:60], "kind": kind, "schedule": schedule, "command": command,
            "cwd": str(Path(cwd).expanduser()) if cwd else "", "mode": mode if mode in PROMPT_MODES else "accept_edits",
            "timeout": timeout, "enabled": bool(data.get("enabled", True)),
            "created": (previous or {}).get("created") or datetime.now().isoformat(timespec="seconds"),
        }
        jobs[job_id] = job
        _store(jobs)
    return job


def update_job(job_id: str, **changes: Any) -> dict[str, Any] | None:
    """Flip small flags (enabled, run_requested) without rewriting the rest."""
    with _store_lock:
        jobs = load_jobs()
        job = jobs.get(clean_id(job_id))
        if job is None:
            return None
        job.update(changes)
        _store(jobs)
        return job


def delete_job(job_id: str) -> bool:
    import shutil

    with _store_lock:
        jobs = load_jobs()
        if jobs.pop(clean_id(job_id), None) is None:
            return False
        _store(jobs)
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return True


def describe_job(job: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """A job as the UIs show it: with its schedule in words, next run and last run."""
    upcoming = next_run(job["schedule"], now) if job.get("enabled") else None
    runs = load_runs(job["id"])
    return {**{k: v for k, v in job.items() if k != "run_requested"},
            "schedule_text": explain(job["schedule"]),
            "next_run": upcoming.isoformat(timespec="minutes") if upcoming else None,
            "last_run": runs[-1] if runs else None,
            "running": bool(runs and runs[-1]["status"] == "running")}


# --- runs and logs --------------------------------------------------------------------------------

def load_runs(job_id: str) -> list[dict[str, Any]]:
    runs = _read_json(_job_dir(job_id) / "runs.json", [])
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def _save_runs(job_id: str, runs: list[dict[str, Any]]) -> None:
    folder = _job_dir(job_id)
    for old in runs[:-KEEP_RUNS]:
        (folder / str(old.get("log") or "-")).unlink(missing_ok=True)
    write_private_json(folder / "runs.json", runs[-KEEP_RUNS:])


def _record(job_id: str, run: dict[str, Any]) -> None:
    runs = load_runs(job_id)
    for index, existing in enumerate(runs):
        if existing.get("id") == run["id"]:
            runs[index] = run  # a finished run keeps its place in the history
            break
    else:
        runs.append(run)
    _save_runs(job_id, runs)


def read_log(job_id: str, run_id: str, max_bytes: int = 400_000) -> str:
    """The end of a run's log (a job that prints for hours must not flood the page)."""
    path = _job_dir(job_id) / f"{clean_id(run_id)}.log"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return (f"… ({size - max_bytes:,} earlier bytes not shown)\n" if size > max_bytes else "") + text


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def settle_stale_runs() -> None:
    """Runs left 'running' by a hub that went away: mark them once their process is gone."""
    for job_id in load_jobs():
        runs = load_runs(job_id)
        changed = False
        for run in runs:
            if run.get("status") == "running" and not _pid_alive(int(run.get("pid") or 0)):
                run.update(status="interrupted", finished=datetime.now().isoformat(timespec="seconds"))
                changed = True
        if changed:
            _save_runs(job_id, runs)


def self_command() -> list[str]:
    """How to start another Dcode process (the packaged binary, or this Python)."""
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "dcode"]


def job_argv(job: dict[str, Any]) -> tuple[Any, bool]:
    """(what to execute, through the shell?)"""
    if job["kind"] == "prompt":
        return self_command() + ["run", "--mode", job.get("mode") or "accept_edits", job["command"]], False
    return job["command"], True


def run_job(job: dict[str, Any], trigger: str = "schedule", env: dict[str, str] | None = None,
            on_start: Any = None) -> dict[str, Any]:
    """Run one job to the end (blocking) and return its run record. Output goes to the run's log."""
    folder = _job_dir(job["id"])
    folder.mkdir(parents=True, exist_ok=True)
    started = datetime.now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    while (folder / f"{run_id}.log").exists():
        run_id += "x"
    log_path = folder / f"{run_id}.log"
    run: dict[str, Any] = {"id": run_id, "job": job["id"], "trigger": trigger, "status": "running",
                           "started": started.isoformat(timespec="seconds"), "finished": None, "exit_code": None,
                           "duration": None, "pid": 0, "log": log_path.name}
    argv, shell = job_argv(job)
    cwd = job.get("cwd") or str(Path.home())
    child_env = dict(env if env is not None else os.environ)
    child_env.update(DCODE_CRON_JOB=job["id"], DCODE_CRON_RUN=run_id, DCODE_NO_UI="1")
    begun = time.monotonic()
    with log_path.open("wb") as log:
        os.chmod(log_path, 0o600)
        shown = job["command"] if shell else f"dcode run --mode {job.get('mode')} (in {cwd})\n# prompt: {job['command']}"
        log.write(f"# {job['name']} · {trigger} · {run['started']}\n# {shown}\n\n".encode())
        log.flush()
        try:
            process = subprocess.Popen(argv, shell=shell, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            log.write(f"Could not start: {exc}\n".encode())
            run.update(status="failed", finished=datetime.now().isoformat(timespec="seconds"), duration=0.0)
            _record(job["id"], run)
            return run
        run["pid"] = process.pid
        _record(job["id"], run)
        if on_start:
            on_start(run, process)
        timer = threading.Timer(int(job.get("timeout") or DEFAULT_TIMEOUT), _time_out, [process])
        timer.daemon = True
        timer.start()
        written, cut = 0, False
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read1(65536), b""):  # type: ignore[union-attr]
            if written < MAX_LOG_BYTES:
                log.write(chunk[:MAX_LOG_BYTES - written])
            written += len(chunk)
            if written > MAX_LOG_BYTES and not cut:
                cut = True
                log.write(f"\n# log cut at {MAX_LOG_BYTES:,} bytes; the job keeps running\n".encode())
            log.flush()
        code = process.wait()
        timer.cancel()
        duration = round(time.monotonic() - begun, 1)
        status = getattr(process, "dcode_ended", "") or ("ok" if code == 0 else "failed")
        note = {"timeout": f"stopped after the {job.get('timeout')}s time limit", "stopped": "stopped by you"}.get(status, f"exit code {code}")
        log.write(f"\n# finished: {note} · {duration}s\n".encode())
    run.update(status=status, exit_code=code, duration=duration, finished=datetime.now().isoformat(timespec="seconds"))
    _record(job["id"], run)
    return run


def _time_out(process: subprocess.Popen) -> None:
    process.dcode_ended = "timeout"  # type: ignore[attr-defined]
    _kill_group(process)


def _kill_group(process: subprocess.Popen) -> None:
    """Stop a run and everything it started (it runs in its own process group)."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        process.terminate()
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()


# --- the scheduler (lives in the hub) ---------------------------------------------------------------

class Scheduler:
    """Fires due jobs. One thread watches the clock; each run gets its own thread."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env
        self._fired: dict[str, str] = {}      # job id -> the minute it last fired for
        self._running: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="dcode-cron", daemon=True)

    def start(self) -> Scheduler:
        settle_stale_runs()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def busy(self) -> bool:
        with self._lock:
            return bool(self._running)

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._running

    def _loop(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick(datetime.now())
            except Exception:
                pass  # a broken jobs file must not stop the clock

    def tick(self, now: datetime) -> list[str]:
        """Start whatever is due at ``now``. Returns the ids started (for tests)."""
        minute = now.strftime("%Y-%m-%dT%H:%M")
        started = []
        for job in load_jobs().values():
            manual = bool(job.get("run_requested"))
            if manual:
                update_job(job["id"], run_requested=False)
            due = False
            if job.get("enabled") and self._fired.get(job["id"]) != minute:
                try:
                    due = matches(job["schedule"], now)
                except ScheduleError:
                    due = False
                if due:
                    self._fired[job["id"]] = minute
            if not (due or manual):
                continue
            if self.is_running(job["id"]):
                if due:  # the last run is still going: note it instead of piling up
                    stamp = now.strftime("%Y%m%d-%H%M%S") + "-skipped"
                    _record(job["id"], {"id": stamp, "job": job["id"], "trigger": "schedule", "status": "skipped",
                                        "started": now.isoformat(timespec="seconds"), "finished": None, "exit_code": None,
                                        "duration": None, "pid": 0, "log": ""})
                continue
            self.launch(job, "manual" if manual and not due else "schedule")
            started.append(job["id"])
        return started

    def launch(self, job: dict[str, Any], trigger: str) -> None:
        with self._lock:
            if job["id"] in self._running:
                return
            self._running[job["id"]] = None  # type: ignore[assignment]  # reserved until the process exists

        def work() -> None:
            try:
                run_job(job, trigger, self.env, on_start=lambda run, process: self._running.__setitem__(job["id"], process))
            finally:
                with self._lock:
                    self._running.pop(job["id"], None)

        threading.Thread(target=work, name=f"dcode-cron-{job['id']}", daemon=True).start()

    def stop_job(self, job_id: str) -> bool:
        with self._lock:
            process = self._running.get(job_id)
        if process is None:
            return False
        process.dcode_ended = "stopped"  # type: ignore[attr-defined]
        threading.Thread(target=_kill_group, args=[process], daemon=True).start()
        return True
