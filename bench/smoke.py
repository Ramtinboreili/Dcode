#!/usr/bin/env python3
"""The whole pipeline against the mock server: 3 tasks × 2 arms × 1 repeat, then checks on what came out.

    python bench/smoke.py [--out DIR]

The mock can't solve anything, so every run is expected to fail its check. This tests the pipeline, not Dcode's
ability. One task of the three is made up to make the mock work through many steps in a small window, so that the
compaction and prefix sections of the report hold something.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
import common  # noqa: E402
import run as runner  # noqa: E402

REAL_TASKS = ("py-duration-parse", "shell-backup-quoting")
ARMS = ("baseline", "small_output_caps")
LONG_TASK = "smoke-long"
NOTE = "Smoke run against the mock server. Its replies can't solve tasks, so a 0% pass rate is expected here and says nothing about Dcode."
RESULT_FIELDS = ("task", "arm", "model", "repeat", "status", "check_passed", "exit_code", "end_reason", "prompt_tokens", "completion_tokens", "cached_tokens",
                 "calls", "steps", "wall_s", "model_s", "tool_s", "order_index", "spec_hash", "dcode_commit")
REPORT_SECTIONS = ("# Benchmark report", "## Pass rate and cost", "## How runs ended", "## Against the control", "## Prefix stability", "## Elision regret", "## Pass rate by category")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(port: int, process: subprocess.Popen) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("the mock server exited")
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("the mock server didn't start")


def _make_inputs(folder: Path, port: int) -> tuple[Path, Path, Path]:
    root = folder / "tasks"
    for name in REAL_TASKS:
        shutil.copytree(common.TASKS / name, root / name)
    made_up = root / LONG_TASK
    (made_up / "repo").mkdir(parents=True)
    (made_up / "check").mkdir()
    (made_up / "repo" / "README.md").write_text("A smoke test.\n", encoding="utf-8")
    (made_up / "check" / "check.sh").write_text("exit 1\n", encoding="utf-8")
    (made_up / "solution.patch").write_text("nothing solves this one\n", encoding="utf-8")
    (made_up / "task.json").write_text(json.dumps({"id": LONG_TASK, "category": "long", "difficulty": "easy", "prompt": "long task 14", "timeout_s": 120,
                                                   "requires": [], "tags": ["smoke"]}), encoding="utf-8")
    all_arms = json.loads((common.BENCH / "arms.json").read_text(encoding="utf-8"))["arms"]
    arms = folder / "arms.json"
    arms.write_text(json.dumps({"arms": [arm for arm in all_arms if arm["name"] in ARMS]}), encoding="utf-8")
    models = folder / "models.json"
    models.write_text(json.dumps({"mock": {"base_url": f"http://127.0.0.1:{port}/v1", "model": "mock", "api_key_env": None,
                                           "context_window": 8000, "max_output_tokens": 512}}), encoding="utf-8")
    return root, arms, models


def verify(out: Path) -> list[str]:
    """What's wrong with a finished smoke folder. A failed check is expected; a missing or malformed file isn't."""
    problems = []
    tasks = (*REAL_TASKS, LONG_TASK)
    with open(out / "results.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != len(tasks) * len(ARMS):
        problems.append(f"results.csv has {len(rows)} rows")
    if [int(row["order_index"]) for row in rows] != list(range(len(rows))):
        problems.append("results.csv isn't in run order")
    for task in tasks:
        for arm in ARMS:
            folder = out / "runs" / "mock" / arm / task / "r0"
            for name in ("result.json", "trace.jsonl", "stdout.log", "stderr.log", "diff.patch", "summary.json", "check.log", "experiment.json"):
                if not (folder / name).exists():
                    problems.append(f"{task}/{arm}: no {name}")
            if not (folder / "result.json").exists():
                continue
            result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            problems += [f"{task}/{arm}: result.json has no {field}" for field in RESULT_FIELDS if field not in result]
            if result.get("status") != "fail":
                problems.append(f"{task}/{arm}: expected fail, got {result.get('status')}: {result.get('error')}")
            if not result.get("calls") or not result.get("prompt_tokens"):
                problems.append(f"{task}/{arm}: the model was never called")
            if result.get("cached_tokens") is None:
                problems.append(f"{task}/{arm}: no cached tokens from --simulate-cache")
            if result.get("usage_source") != "task_end":
                problems.append(f"{task}/{arm}: totals didn't come from task_end")
    header = json.loads((out / "runs" / "mock" / "small_output_caps" / LONG_TASK / "r0" / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    if header.get("experiment_overrides", {}).get("output_cap_max") != 8000:
        problems.append(f"the arm's settings didn't reach Dcode: {header.get('experiment_overrides')}")
    long_run = json.loads((out / "runs" / "mock" / "baseline" / LONG_TASK / "r0" / "result.json").read_text(encoding="utf-8"))
    if long_run.get("steps", 0) < 10 or not long_run.get("compactions"):
        problems.append(f"the long task didn't exercise compaction: {long_run.get('steps')} steps, {long_run.get('compactions')} compactions")
    report = (out / "report.md").read_text(encoding="utf-8")
    problems += [f"report.md has no section {section!r}" for section in REPORT_SECTIONS if section not in report]
    for name in ("arm_summary.csv", "paired.csv", "prefix.csv", "regret.csv"):
        with open(out / name, newline="", encoding="utf-8") as handle:
            if len(list(csv.DictReader(handle))) == 0:
                problems.append(f"{name} is empty")
    with open(out / "paired.csv", newline="", encoding="utf-8") as handle:
        pairs = list(csv.DictReader(handle))
    if not pairs or pairs[0]["arm"] != "small_output_caps" or int(pairs[0]["pairs"]) != len(tasks):
        problems.append(f"paired.csv is wrong: {pairs}")
    with open(out / "prefix.csv", newline="", encoding="utf-8") as handle:
        if not any(int(row["calls"]) > 0 and row["mean_lcp_ratio"] for row in csv.DictReader(handle)):
            problems.append("prefix.csv has no calls")
    return problems


def run_smoke(out: Path, workdir: Path) -> list[str]:
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="dcode-bench-smoke-") as scratch:
        root, arms, models = _make_inputs(Path(scratch), port)
        mock = subprocess.Popen([sys.executable, str(common.REPO / "docs" / "dev" / "mock_llm.py"), str(port), "--simulate-cache", "--long-chars", "2400"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_for(port, mock)
            code = runner.main(["--root", str(root), "--arms", str(arms), "--models", str(models), "--out", str(out), "--workdir", str(workdir)])
        finally:
            mock.terminate()
            mock.wait(timeout=10)
    if code != 0:
        return [f"the runner exited with {code}"]
    analyze.analyze(out, NOTE)
    return verify(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="default: a new temporary folder")
    args = parser.parse_args(argv)
    out = args.out or Path(tempfile.mkdtemp(prefix="dcode-bench-smoke-out-"))
    with tempfile.TemporaryDirectory(prefix="dcode-bench-smoke-work-") as workdir:
        problems = run_smoke(out, Path(workdir))
    if (out / "report.md").exists():
        print("\n" + (out / "report.md").read_text(encoding="utf-8"))
    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    print(f"{'Smoke test failed' if problems else 'Smoke test passed'}. Output in {out}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
