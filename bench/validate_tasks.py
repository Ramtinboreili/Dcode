#!/usr/bin/env python3
"""The oracle test: every task's check must FAIL on the untouched repo and PASS with solution.patch applied.

    python bench/validate_tasks.py [--tasks PATTERN ...] [--jobs N] [--verbose]

A task whose required tools are missing is reported as skipped, not failed, and the summary says it wasn't verified.
Exit code: 0 when every task that could be checked is right.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def validate(task: common.Task, verbose: bool = False) -> dict:
    missing = common.missing_tools(task)
    if missing:
        return {"task": task, "state": "skipped", "detail": "missing " + ", ".join(missing)}
    with tempfile.TemporaryDirectory(prefix="dcode-bench-validate-") as scratch:
        workspace = Path(scratch) / "workspace"
        common.prepare_workspace(task, workspace)
        untouched = common.score(workspace, task)
        if untouched["passed"]:
            return {"task": task, "state": "bad", "detail": "the check passes on the untouched repo"}
        if untouched["timed_out"]:
            return {"task": task, "state": "bad", "detail": "the check timed out on the untouched repo"}
        applied = common.git(workspace, "apply", "--whitespace=nowarn", str(task.solution), check=False)
        if applied.returncode != 0:
            return {"task": task, "state": "bad", "detail": "solution.patch doesn't apply: " + applied.stderr.strip()[:300]}
        results = [common.score(workspace, task) for _ in range(2)]  # twice: a flaky check is a broken check
        if not all(r["passed"] for r in results):
            log = next(r for r in results if not r["passed"])["output"]
            return {"task": task, "state": "bad", "detail": "the check fails with the solution applied:\n" + log[-1500:]}
        detail = f"check {results[0]['seconds']:.1f}s"
        if verbose:
            detail += "\n--- on the untouched repo ---\n" + untouched["output"][-1200:]
        return {"task": task, "state": "ok", "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", nargs="*", help="ids, globs or categories (default: every task)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--root", type=Path, default=common.TASKS, help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="show why each check fails on the untouched repo")
    args = parser.parse_args(argv)
    try:
        tasks = common.select_tasks(common.load_tasks(args.root), args.tasks)
    except common.TaskError as exc:
        print(f"bad task: {exc}")
        return 1
    if not tasks:
        print("no tasks found")
        return 1
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        rows = list(pool.map(lambda task: validate(task, args.verbose), tasks))
    for row in rows:
        print(f"{row['state'].upper():8} {row['task'].id:34} {row['task'].category:7} {row['task'].difficulty:7} {row['detail']}")
    counts = {state: sum(1 for row in rows if row["state"] == state) for state in ("ok", "bad", "skipped")}
    print(f"\n{len(rows)} tasks: {counts['ok']} ok, {counts['bad']} bad, {counts['skipped']} skipped (not verified here)")
    return 1 if counts["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())
