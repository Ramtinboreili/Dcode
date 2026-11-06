#!/usr/bin/env python3
"""The benchmark runner: every task × arm × model × repeat, each in a fresh home and a workspace at a fixed path.

    python bench/run.py --out results/a --repeats 3 --models my-models.json
    python bench/run.py --out results/a --resume

The agent runs with `--mode auto`, so it runs commands the model chose. Its environment has no kubeconfig, Docker
socket or SSH agent, but that isn't a sandbox: run this inside a VM or container. See docs/research/benchmark.md.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEFAULT_WORKDIR = Path("/tmp/dcode-bench")
DUMMY_KEY = "bench-no-key"
INTERRUPT_GRACE = 15.0  # a timed-out agent gets this long to write its task_end and summary
STOP_GRACE = 3.0
# -I keeps the workspace's own modules and any PYTHONPATH out of the agent, and the path this checkout's dcode ahead of
# whatever copy the virtualenv's site-packages holds.
LAUNCHER = "import sys; sys.path.insert(0, {repo!r}); sys.argv = ['dcode', *sys.argv[1:]]; from dcode.__main__ import main; sys.exit(main())"
STATUSES = ("pass", "fail", "timeout", "error", "skipped")
CSV_FIELDS = ("task", "category", "difficulty", "arm", "model", "context_window", "repeat", "order_index", "slot", "status",
              "check_passed", "check_exit_code", "exit_code", "timed_out", "end_reason", "steps", "calls", "estimated_calls",
              "tool_calls", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "compactions", "elided",
              "elision_count", "elision_regrets", "max_prompt_tokens", "wall_s", "model_s", "tool_s", "check_s", "diff_bytes",
              "usage_source", "key_seen", "dcode_commit", "spec_hash", "error")


class BenchError(Exception):
    """Something wrong with the inputs: reported before anything has run."""


@dataclass(frozen=True)
class Run:
    task: common.Task
    arm: dict[str, Any]
    model_name: str
    model: dict[str, Any]
    repeat: int
    order_index: int
    slot: int
    spec_hash: str

    @property
    def context_window(self) -> int | None:
        return self.arm.get("context_window") or self.model.get("context_window")

    @property
    def max_output_tokens(self) -> int | None:
        return self.arm.get("max_output_tokens") or self.model.get("max_output_tokens")

    def folder(self, out: Path) -> Path:
        return out / "runs" / self.model_name / self.arm["name"] / self.task.id / f"r{self.repeat}"


@dataclass
class Context:
    out: Path
    workdir: Path
    command: list[str]
    commit: str | None
    stop: threading.Event
    timeout_scale: float = 1.0
    max_steps: int | None = None
    seed: int | None = None


# --- inputs -------------------------------------------------------------------------------------------------------
def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError(f"{what}: can't read {path}: {exc}") from exc


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_arms(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path, "arms")
    arms = data.get("arms") if isinstance(data, dict) else None
    if not isinstance(arms, list) or not arms:
        raise BenchError(f"{path}: expected {{\"arms\": [ ... ]}}")
    sys.path.insert(0, str(common.REPO))
    from dcode import experiment  # the same rule the run will apply: a typo must not quietly run the baseline

    names = set()
    known = {"name", "experiment", "context_window", "max_output_tokens", "seed_home", "control", "note"}
    for arm in arms:
        name = arm.get("name") if isinstance(arm, dict) else None
        if not isinstance(name, str) or not name or "/" in name:
            raise BenchError(f"{path}: an arm needs a simple name: {arm!r}")
        if name in names:
            raise BenchError(f"{path}: arm {name} appears twice")
        names.add(name)
        if set(arm) - known:
            raise BenchError(f"arm {name}: unknown field(s) {sorted(set(arm) - known)}")
        try:
            experiment.clean(arm.get("experiment") or {})
        except experiment.ExperimentError as exc:
            raise BenchError(f"arm {name}: {exc}") from exc
        for field in ("context_window", "max_output_tokens"):
            if field in arm and not _positive(arm[field]):
                raise BenchError(f"arm {name}: {field} must be a positive whole number")
        seed = arm.get("seed_home")
        if seed and not (common.BENCH / seed).is_dir():
            raise BenchError(f"arm {name}: no seed folder {seed} under bench/")
    for arm in arms:
        if arm.get("control") and arm["control"] not in names:
            raise BenchError(f"arm {arm['name']}: its control {arm['control']} isn't an arm")
    return arms


def load_models(path: Path) -> dict[str, dict[str, Any]]:
    models = _read_json(path, "models")
    if not isinstance(models, dict) or not models:
        raise BenchError(f"{path}: expected an object of profile name -> settings")
    known = {"base_url", "model", "api_key_env", "context_window", "max_output_tokens", "family", "reasoning_effort"}
    for name, model in models.items():
        if not isinstance(model, dict) or "/" in name:
            raise BenchError(f"{path}: bad profile {name!r}")
        if not model.get("base_url") or not model.get("model"):
            raise BenchError(f"model {name}: base_url and model are required")
        if set(model) - known:
            raise BenchError(f"model {name}: unknown field(s) {sorted(set(model) - known)}")
        for field in ("context_window", "max_output_tokens"):
            if field in model and not _positive(model[field]):
                raise BenchError(f"model {name}: {field} must be a positive whole number")
    return models


def resolve_keys(models: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Each profile's key, from the variable it names. Keys stay in memory and in the agent's environment."""
    keys, missing = {}, []
    for name, model in models.items():
        variable = model.get("api_key_env")
        if not variable:
            keys[name] = DUMMY_KEY
        elif os.environ.get(variable):
            keys[name] = os.environ[variable]
        else:
            missing.append(f"{name} ({variable})")
    if missing:
        raise BenchError("no API key in the environment for: " + ", ".join(missing))
    return keys


def spec_hash(task_hash: str, arm: dict[str, Any], model: dict[str, Any], seed_hash: str = "") -> str:
    """What a result depends on, for refusing to mix results of different setups in one folder."""
    body = json.dumps({"task": task_hash, "arm": arm, "model": model, "seed": seed_hash}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def build_plan(tasks: list[common.Task], arms: list[dict[str, Any]], models: dict[str, dict[str, Any]], repeats: int, parallel: int) -> list[Run]:
    """All runs in order. A task stays on one slot, so its workspace path is the same for every arm: the path is part
    of the system prompt. The arm order rotates with task and repeat, so no arm always runs first against a server's
    prefix cache, or last."""
    task_hashes = {task.id: common.tree_hash(task.path) for task in tasks}
    seed_hashes = {arm["name"]: common.tree_hash(common.BENCH / arm["seed_home"]) if arm.get("seed_home") else "" for arm in arms}
    runs: list[Run] = []
    for repeat in range(repeats):
        for model_name, model in models.items():
            for index, task in enumerate(tasks):
                shift = (index + repeat) % len(arms)
                for arm in arms[shift:] + arms[:shift]:
                    runs.append(Run(task, arm, model_name, model, repeat, len(runs), index % parallel,
                                    spec_hash(task_hashes[task.id], arm, model, seed_hashes[arm["name"]])))
    return runs


def existing_result(run: Run, out: Path) -> dict[str, Any] | None:
    path = run.folder(out) / "result.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pending_runs(runs: list[Run], out: Path, resume: bool, retry_errors: bool = False) -> list[Run]:
    """What's left to do. Raises rather than mix setups, or overwrite results nobody asked to resume."""
    pending, conflicts, found = [], [], 0
    for run in runs:
        result = existing_result(run, out)
        if result is None:
            pending.append(run)
            continue
        found += 1
        if result.get("spec_hash") != run.spec_hash:
            conflicts.append(f"{run.model_name}/{run.arm['name']}/{run.task.id}/r{run.repeat}")
        elif result.get("status") == "error" and retry_errors:
            pending.append(run)
        elif result.get("status") == "skipped" and not common.missing_tools(run.task):
            pending.append(run)  # the tool is here now, and a skipped run spent nothing
    if found and not resume:
        raise BenchError(f"{out} already holds {found} result(s): pass --resume, or choose another --out")
    if conflicts:
        raise BenchError(f"{len(conflicts)} result(s) in {out} came from a different task, arm or model (first: {conflicts[0]}). "
                         "Use another --out.")
    return pending


# --- one run ------------------------------------------------------------------------------------------------------
def clean_path() -> str:
    """PATH without this Python's virtualenv: the agent's `python3` and `pip` shouldn't be the harness's."""
    venv = {os.path.join(sys.prefix, "bin")} if sys.prefix != sys.base_prefix else set()
    if os.environ.get("VIRTUAL_ENV"):
        venv.add(os.path.join(os.environ["VIRTUAL_ENV"], "bin"))
    return os.pathsep.join(p for p in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep) if p and p not in venv)


def agent_env(run: Run, key: str, slot_dir: Path) -> dict[str, str]:
    """Built from nothing, so the caller's credentials and cluster access don't reach the agent's commands."""
    env = {"PATH": clean_path(), "HOME": str(slot_dir / "user"), "TMPDIR": str(slot_dir / "tmp"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "TZ": "UTC", "TERM": "dumb", "NO_COLOR": "1", "KUBECONFIG": "/dev/null", "DOCKER_HOST": "unix:///nonexistent",
           **common.GIT_ENV, "DCODE_HOME": str(slot_dir / "home"), "DCODE_NO_UI": "1",
           "DCODE_BASE_URL": run.model["base_url"], "DCODE_API_KEY": key, "DCODE_MODEL": run.model["model"]}
    if run.context_window:
        env["DCODE_CONTEXT_WINDOW"] = str(run.context_window)
    if run.max_output_tokens:
        env["DCODE_MAX_OUTPUT_TOKENS"] = str(run.max_output_tokens)
    if run.model.get("family"):
        env["DCODE_FAMILY"] = run.model["family"]
    if run.model.get("reasoning_effort"):
        env["DCODE_REASONING_EFFORT"] = run.model["reasoning_effort"]
    return env


def read_trace(path: Path) -> dict[str, Any]:
    """Totals from the trace: task_end when the run got that far, else the records of a run that was killed."""
    out: dict[str, Any] = {"end_reason": None, "steps": None, "calls": 0, "estimated_calls": 0, "tool_calls": 0, "prompt_tokens": 0,
                           "completion_tokens": 0, "cached_tokens": None, "compactions": 0, "elided": 0, "elision_count": 0,
                           "elision_regrets": 0, "max_prompt_tokens": 0, "wall_s": None, "model_s": 0.0, "tool_s": 0.0, "usage_source": "none"}
    end = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # the last line of a killed process
        kind = record.get("type")
        if kind == "llm_call":
            out["calls"] += 1
            out["estimated_calls"] += 1 if record.get("usage_estimated") else 0
            prompt = record.get("counted_prompt_tokens") or 0
            out["prompt_tokens"] += prompt
            out["completion_tokens"] += record.get("counted_completion_tokens") or 0
            out["max_prompt_tokens"] = max(out["max_prompt_tokens"], prompt)
            if record.get("cached_tokens") is not None:
                out["cached_tokens"] = (out["cached_tokens"] or 0) + record["cached_tokens"]
            out["model_s"] += (record.get("total_ms") or 0) / 1000
            out["usage_source"] = "records"
        elif kind == "tool_call":
            out["tool_calls"] += 1
            if record.get("name") != "delegate":
                out["tool_s"] += (record.get("duration_ms") or 0) / 1000
        elif kind == "compaction":
            out["compactions"] += 1
            out["elided"] += record.get("elided") or 0
        elif kind == "elision":
            out["elision_count"] += record.get("count") or 0
        elif kind == "elision_regret":
            out["elision_regrets"] += 1
        elif kind == "task_end":
            end = record
    if end:
        out.update(end_reason=end.get("reason"), steps=end.get("steps"), usage_source="task_end", calls=end.get("calls", out["calls"]),
                   estimated_calls=end.get("estimated_calls", out["estimated_calls"]), tool_calls=end.get("tool_calls", out["tool_calls"]),
                   prompt_tokens=end.get("prompt_tokens", out["prompt_tokens"]), completion_tokens=end.get("completion_tokens", out["completion_tokens"]),
                   cached_tokens=end.get("cached_tokens"), wall_s=(end.get("wall_ms") or 0) / 1000,
                   model_s=(end.get("model_ms") or 0) / 1000, tool_s=(end.get("tool_ms") or 0) / 1000)
    return out


def classify(timed_out: bool, check_passed: bool | None, exit_code: int | None, end_reason: str | None) -> str:
    """`error` means the benchmark learned nothing about the arm: Dcode or the server failed, and no work came of it."""
    if timed_out:
        return "timeout"
    if check_passed:
        return "pass"
    if exit_code is None or exit_code == 2 or exit_code < 0:
        return "error"
    if exit_code != 0 and end_reason in (None, "error", "interrupted"):
        return "error"
    return "fail"


def _atomic_text(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _blank_result(run: Run, ctx: Context) -> dict[str, Any]:
    return {"task": run.task.id, "category": run.task.category, "difficulty": run.task.difficulty, "arm": run.arm["name"], "model": run.model_name,
            "context_window": run.context_window, "repeat": run.repeat, "order_index": run.order_index, "slot": run.slot, "status": None,
            "check_passed": None, "check_exit_code": None, "exit_code": None, "timed_out": False, "end_reason": None, "steps": None, "calls": 0,
            "estimated_calls": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": None, "compactions": 0,
            "elided": 0, "elision_count": 0, "elision_regrets": 0, "max_prompt_tokens": 0, "wall_s": None, "model_s": 0.0, "tool_s": 0.0, "check_s": None,
            "diff_bytes": 0, "usage_source": "none", "key_seen": False, "dcode_commit": ctx.commit, "spec_hash": run.spec_hash, "error": None,
            "missing_tools": [], "max_steps": None, "timeout_s": None, "seed": None}


def _redact(folder: Path, key: str) -> bool:
    """The agent's commands can read the key from their environment, and a model can print what they read."""
    if key == DUMMY_KEY:
        return False
    seen = False
    needle = key.encode()
    for path in folder.iterdir():
        if path.is_file() and needle in path.read_bytes():
            path.write_bytes(path.read_bytes().replace(needle, b"[REDACTED]"))
            seen = True
    return seen


def execute(run: Run, key: str, ctx: Context) -> dict[str, Any] | None:
    """One run, to a result.json written last and whole. None when interrupted: the folder is redone on resume."""
    folder = run.folder(ctx.out)
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    result = _blank_result(run, ctx)
    missing = common.missing_tools(run.task)
    if missing:
        result.update(status="skipped", missing_tools=missing)
        _atomic_text(folder / "result.json", json.dumps(result, indent=2) + "\n")
        return result
    try:
        finished = _run_agent(run, key, ctx, folder, result)
    except Exception as exc:  # one broken run must not take the rest of the benchmark with it
        result.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
        finished = True
    if not finished:
        return None
    if _redact(folder, key):
        result["key_seen"] = True
    _atomic_text(folder / "result.json", json.dumps(result, indent=2) + "\n")
    return result


def _run_agent(run: Run, key: str, ctx: Context, folder: Path, result: dict[str, Any]) -> bool:
    slot_dir = ctx.workdir / f"w{run.slot}"
    for name in ("home", "user", "tmp"):
        shutil.rmtree(slot_dir / name, ignore_errors=True)
        (slot_dir / name).mkdir(parents=True)
    if run.arm.get("seed_home"):
        shutil.copytree(common.BENCH / run.arm["seed_home"], slot_dir / "home", dirs_exist_ok=True)
    workspace = slot_dir / "repo"
    base = common.prepare_workspace(run.task, workspace)

    section = dict(run.arm.get("experiment") or {})
    if ctx.seed is not None:
        section["seed"] = ctx.seed + run.repeat  # the same for all arms of a repeat, different between repeats
        result["seed"] = section["seed"]
    (folder / "experiment.json").write_text(json.dumps(section, indent=2) + "\n", encoding="utf-8")
    trace_path, summary_path = folder / "trace.jsonl", folder / "summary.json"
    task_id = f"{run.task.id}.{run.arm['name']}.{run.model_name}.r{run.repeat}"
    max_steps = ctx.max_steps or run.task.meta.get("max_steps")
    timeout = run.task.timeout_s * ctx.timeout_scale
    result.update(max_steps=max_steps, timeout_s=timeout)
    arguments = ["run", "--mode", "auto", "--cwd", str(workspace), "--task-id", task_id, "--trace", str(trace_path),
                 "--experiment", str(folder / "experiment.json"), "--json-summary", str(summary_path)]
    if max_steps:
        arguments += ["--max-steps", str(max_steps)]
    arguments.append("-")

    started = time.perf_counter()
    with open(folder / "stdout.log", "wb") as stdout, open(folder / "stderr.log", "wb") as stderr:
        process = subprocess.Popen([*ctx.command, *arguments], cwd=slot_dir, env=agent_env(run, key, slot_dir), stdin=subprocess.PIPE,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            process.stdin.write(run.task.prompt.encode())
            process.stdin.close()
        except OSError:
            pass  # it died at once: the exit code and stderr say why
        timed_out = False
        while process.poll() is None:
            if ctx.stop.is_set():
                common.kill_group(process, grace=STOP_GRACE, first=signal.SIGINT)
                return False
            if time.perf_counter() - started > timeout:
                timed_out = True
                common.kill_group(process, grace=INTERRUPT_GRACE, first=signal.SIGINT)
                break
            time.sleep(0.1)
        common.kill_group(process)  # whatever the agent left running in the background
    wall = time.perf_counter() - started
    if ctx.stop.is_set():
        return False

    diff = common.capture_diff(workspace, base)
    (folder / "diff.patch").write_text(diff, encoding="utf-8")
    scored = common.score(workspace, run.task, keep_log=folder / "check.log")
    shutil.rmtree(workspace, ignore_errors=True)

    totals = read_trace(trace_path)
    summary: dict[str, Any] = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            summary = {}
    result.update(totals)
    result.update(check_passed=scored["passed"], check_exit_code=scored["exit_code"], check_s=scored["seconds"], exit_code=process.returncode,
                  timed_out=timed_out, diff_bytes=len(diff.encode()), wall_s=round(wall, 3), model_s=round(totals["model_s"], 3), tool_s=round(totals["tool_s"], 3))
    result["end_reason"] = summary.get("end_reason") or totals["end_reason"]
    if result["steps"] is None:
        result["steps"] = summary.get("steps")
    result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    result["status"] = classify(timed_out, scored["passed"], process.returncode, result["end_reason"])
    if result["status"] == "error":
        tail = (folder / "stderr.log").read_text(encoding="utf-8", errors="replace")[-300:] or (folder / "stdout.log").read_text(encoding="utf-8", errors="replace")[-300:]
        result["error"] = tail.strip() or f"exit {process.returncode}"
    return True


# --- all runs -----------------------------------------------------------------------------------------------------
def aggregate(out: Path) -> list[dict[str, Any]]:
    """results.csv from every result.json there is, so it's right after a stop, a resume or a crash."""
    rows = []
    for path in sorted((out / "runs").glob("*/*/*/r*/result.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    rows.sort(key=lambda row: row.get("order_index", 0))
    temp = out / f".results.csv.{os.getpid()}.tmp"
    with open(temp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, out / "results.csv")
    return rows


def dcode_command(choice: str) -> tuple[list[str], str | None]:
    if choice != "source":
        path = shutil.which(choice) or choice
        if not os.access(path, os.X_OK):
            raise BenchError(f"--dcode: {choice} isn't an executable")
        return [os.path.abspath(path)], None
    venv = common.REPO / ".venv" / "bin" / "python"
    python = str(venv) if venv.exists() else sys.executable
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=common.REPO, capture_output=True, text=True).stdout.strip() or None
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "dcode"], cwd=common.REPO, capture_output=True, text=True).stdout.strip()
    return [python, "-I", "-c", LAUNCHER.format(repo=str(common.REPO))], f"{commit}+dirty" if commit and dirty else commit


def write_plan(out: Path, runs: list[Run], arms: list[dict[str, Any]], models: dict[str, dict[str, Any]], args: argparse.Namespace, commit: str | None) -> None:
    public = {name: {**{k: v for k, v in model.items() if k != "base_url"}, "host": urlparse(model["base_url"]).hostname} for name, model in models.items()}
    plan = {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "dcode": args.dcode, "dcode_commit": commit, "repeats": args.repeats,
            "parallel": args.parallel, "timeout_scale": args.timeout_scale, "max_steps": args.max_steps, "seed": args.seed, "arms": arms, "models": public,
            "tasks": sorted({run.task.id for run in runs}),
            "runs": [{"order_index": run.order_index, "task": run.task.id, "arm": run.arm["name"], "model": run.model_name, "repeat": run.repeat, "slot": run.slot} for run in runs]}
    _atomic_text(out / "plan.json", json.dumps(plan, indent=2) + "\n")


def execute_all(pending: list[Run], keys: dict[str, str], ctx: Context, total: int, say: Any = print) -> None:
    queues: dict[int, list[Run]] = {}
    for run in pending:
        queues.setdefault(run.slot, []).append(run)
    lock = threading.Lock()
    done = [total - len(pending)]

    def worker(queue: list[Run]) -> None:
        for run in queue:
            if ctx.stop.is_set():
                return
            result = execute(run, keys[run.model_name], ctx)
            if result is None:
                return
            with lock:
                done[0] += 1
                say(f"[{done[0]}/{total}] {result['status']:<8} {run.task.id} · {run.arm['name']} · {run.model_name} · r{run.repeat} · "
                    f"{result['wall_s'] or 0:.0f}s · {result['total_tokens']} tokens · {result['end_reason'] or '-'}", flush=True)

    threads = [threading.Thread(target=worker, args=(queue,), daemon=True) for queue in queues.values()]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(0.2)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Dcode benchmark.")
    parser.add_argument("--tasks", nargs="*", help="ids, globs or categories (default: every task)")
    parser.add_argument("--arms", type=Path, default=common.BENCH / "arms.json")
    parser.add_argument("--models", type=Path, default=common.BENCH / "models.json")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parallel", type=int, default=1, help="worker slots; each runs one agent at a time")
    parser.add_argument("--timeout-scale", type=float, default=1.0, help="multiplies every task's timeout_s (slow servers)")
    parser.add_argument("--dcode", default="source", help="'source' (this checkout), or the path of a dcode binary")
    parser.add_argument("--resume", action="store_true", help="skip runs that have a result.json")
    parser.add_argument("--retry-errors", action="store_true", help="with --resume: also redo runs whose status is error")
    parser.add_argument("--max-steps", type=int, help="overrides each task's max_steps")
    parser.add_argument("--seed", type=int, help="send a sampling seed: this number + the repeat")
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR, help="holds the fixed workspace paths; one runner at a time")
    parser.add_argument("--root", type=Path, default=common.TASKS, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.repeats < 1 or args.parallel < 1 or args.timeout_scale <= 0:
            raise BenchError("--repeats and --parallel must be at least 1, --timeout-scale above 0")
        tasks = common.select_tasks(common.load_tasks(args.root), args.tasks)
        if not tasks:
            raise BenchError("no tasks match")
        arms, models = load_arms(args.arms), load_models(args.models)
        keys = resolve_keys(models)
        command, commit = dcode_command(args.dcode)
        os.environ["PATH"] = clean_path()  # checks and agents see the same tools
        runs = build_plan(tasks, arms, models, args.repeats, args.parallel)
        args.out.mkdir(parents=True, exist_ok=True)
        pending = pending_runs(runs, args.out, args.resume, args.retry_errors)
    except (BenchError, common.TaskError) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2

    args.workdir.mkdir(parents=True, exist_ok=True)
    with open(args.workdir / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"bench: another runner is using {args.workdir}: wait for it, or pass another --workdir", file=sys.stderr)
            return 2
        write_plan(args.out, runs, arms, models, args, commit)
        ctx = Context(out=args.out.resolve(), workdir=args.workdir.resolve(), command=command, commit=commit, stop=threading.Event(),
                      timeout_scale=args.timeout_scale, max_steps=args.max_steps, seed=args.seed)

        def stop(signum: int, frame: Any) -> None:
            if not ctx.stop.is_set():
                print("\nStopping: killing the running agents. Finished runs are kept, --resume continues.", flush=True)
            ctx.stop.set()

        handlers = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
        print(f"{len(runs)} runs planned, {len(pending)} to do, {args.parallel} slot(s), output in {args.out}", flush=True)
        try:
            execute_all(pending, keys, ctx, len(runs))
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
        rows = aggregate(args.out)
        for slot in {run.slot for run in runs}:
            shutil.rmtree(ctx.workdir / f"w{slot}", ignore_errors=True)
    counts = {status: sum(1 for row in rows if row.get("status") == status) for status in STATUSES}
    print(" · ".join(f"{count} {status}" for status, count in counts.items()) + f" · {args.out / 'results.csv'}", flush=True)
    return 130 if ctx.stop.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
