"""`dcode run`'s study flags, in a real process against a scripted server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_trace import Model, text, tools_

REPO = Path(__file__).parent.parent


def _run(args, folder, url, env=None):
    full = {key: value for key, value in os.environ.items() if not key.startswith(("DCODE_", "CODE_ONE_"))}
    full.update({"PYTHONPATH": str(REPO), "DCODE_HOME": str(folder / "home"), "BROWSER": "true", "DCODE_BASE_URL": url,
                 "DCODE_API_KEY": "sk-test", "DCODE_MODEL": "mock-model", **(env or {})})
    return subprocess.run([sys.executable, "-m", "dcode", "run", *args], cwd=folder, env=full, capture_output=True, text=True, timeout=120)


@pytest.fixture
def server():
    made = []

    def make(*script):
        made.append(Model(*script))
        return made[-1]

    yield make
    for item in made:
        item.close()


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_every_flag_together(server, tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "exp.json").write_text(json.dumps({"seed": 3}))
    model = server(tools_(("write_file", {"path": "out.txt", "content": "hello"})), text("all done"))
    # Relative paths are from where the command started, not from --cwd.
    run = _run(["--mode", "auto", "--cwd", "work", "--task-id", "t-9", "--trace", "t.jsonl", "--experiment", "exp.json",
                "--max-steps", "7", "--json-summary", "out/s.json", "write it"], tmp_path, model.url)
    assert run.returncode == 0, run.stdout + run.stderr
    assert (tmp_path / "work" / "out.txt").read_text() == "hello"
    rows = _rows(tmp_path / "t.jsonl")
    assert {r["task_id"] for r in rows} == {"t-9"}
    assert rows[0]["experiment_overrides"] == {"seed": 3, "main_max_steps": 7}
    assert all(body["seed"] == 3 for body in model.seen)
    assert all(head["x-dcode-task"] == "t-9" for head in model.headers)
    summary = json.loads((tmp_path / "out" / "s.json").read_text())
    end = rows[-1]
    assert summary["task_id"] == "t-9" and summary["end_reason"] == "final_answer" == end["reason"]
    assert summary["exit_code"] == 0 and summary["steps"] == end["steps"] == 1 and summary["calls"] == end["calls"] == 2
    assert summary["prompt_tokens"] == end["prompt_tokens"] > 0 and summary["completion_tokens"] == end["completion_tokens"] > 0
    assert summary["total_tokens"] == summary["prompt_tokens"] + summary["completion_tokens"]
    assert summary["cached_tokens"] is None and summary["wall_s"] > 0 and summary["model"] == "mock-model"


def test_without_the_flags_nothing_new_happens(server, tmp_path):
    model = server(text("hello there"))
    run = _run(["say hello"], tmp_path, model.url)
    assert run.returncode == 0 and "hello there" in run.stdout
    assert not [p.name for p in tmp_path.iterdir() if p.is_file()]  # no trace, no summary


def test_max_steps_ends_the_task_at_the_limit(server, tmp_path):
    model = server(*[tools_(("list_files", {"path": f"dir{n}"})) for n in range(5)], text("stopped"))
    run = _run(["--mode", "auto", "--max-steps", "2", "--json-summary", "s.json", "go"], tmp_path, model.url)
    assert run.returncode == 0, run.stdout + run.stderr
    summary = json.loads((tmp_path / "s.json").read_text())
    assert summary["end_reason"] == "step_limit" and summary["steps"] == 2


def test_a_failed_task_still_reports_what_it_spent(server, tmp_path):
    thinking = {"content": "", "reasoning": "hmm " * 100, "finish": "length",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
    model = server(thinking, {"status": 401, "message": "the key was revoked"})
    run = _run(["--json-summary", "s.json", "--trace", "t.jsonl", "go"], tmp_path, model.url,
               {"DCODE_MODEL": "Qwen/Qwen3-32B", "DCODE_REASONING_EFFORT": "medium"})
    assert run.returncode == 1
    summary = json.loads((tmp_path / "s.json").read_text())
    assert summary["end_reason"] == "error" and summary["exit_code"] == 1
    assert summary["prompt_tokens"] == 100 and summary["completion_tokens"] == 50 and summary["calls"] == 2
    end = _rows(tmp_path / "t.jsonl")[-1]
    assert end["type"] == "task_end" and end["prompt_tokens"] == 100 and end["task_id"] == summary["task_id"] != ""


@pytest.mark.parametrize("args,says", [
    (["--max-steps", "many", "go"], "whole number"),
    (["go", "--trace"], "needs a value"),
])
def test_a_bad_flag_stops_with_exit_2(tmp_path, args, says):
    run = _run(args, tmp_path, "http://127.0.0.1:9/v1")
    assert run.returncode == 2 and says in run.stdout


def test_a_step_limit_of_zero_is_refused_like_any_bad_setting(tmp_path):
    run = _run(["--max-steps", "0", "go"], tmp_path, "http://127.0.0.1:9/v1")
    assert run.returncode == 2 and "main_max_steps" in run.stderr


def test_an_unwritable_summary_doesnt_change_the_exit_code(server, tmp_path):
    (tmp_path / "a-file").write_text("x")
    model = server(text("hello"))
    run = _run(["--json-summary", "a-file/s.json", "go"], tmp_path, model.url)
    assert run.returncode == 0 and "Could not write the summary" in run.stdout
