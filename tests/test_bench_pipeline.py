"""The benchmark runner and analysis. A fake dcode stands in for the agent, so a run can pass, hang, crash or leak on demand."""

from __future__ import annotations

import csv
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

BENCH = Path(__file__).parent.parent / "bench"
sys.path[:0] = [str(BENCH), str(BENCH / "lib")]
import analyze  # noqa: E402
import common  # noqa: E402
import run as runner  # noqa: E402
import smoke  # noqa: E402

FAKE_DCODE = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time

args = sys.argv[1:]
flag = lambda name: args[args.index(name) + 1]
prompt = sys.stdin.read()
os.chdir(flag("--cwd"))
trace, summary = flag("--trace"), flag("--json-summary")


def finish(reason, code):
    with open(trace, "a") as out:
        out.write(json.dumps({"type": "llm_call", "counted_prompt_tokens": 100, "counted_completion_tokens": 10, "cached_tokens": 40, "total_ms": 500}) + "\n")
        out.write(json.dumps({"type": "task_end", "reason": reason, "steps": 3, "calls": 1, "estimated_calls": 0, "tool_calls": 2, "model_ms": 500, "tool_ms": 250,
                              "wall_ms": 900, "prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 40}) + "\n")
    with open(summary, "w") as out:
        json.dump({"end_reason": reason, "steps": 3}, out)
    sys.exit(code)


if prompt.startswith("solve"):
    open("x.txt", "w").write("solved\n")
    finish("final_answer", 0)
if prompt.startswith("wrong"):
    finish("final_answer", 0)
if prompt.startswith("hang"):
    signal.signal(signal.SIGINT, lambda *a: finish("interrupted", 1))
    open("x.txt", "w").write("solved\n")
    time.sleep(120)
if prompt.startswith("config"):
    sys.exit(2)
if prompt.startswith("servererror"):
    finish("error", 1)
if prompt.startswith("overflow"):
    finish("context_overflow", 1)
if prompt.startswith("leak"):
    print("key is " + os.environ["DCODE_API_KEY"])
    open("leak.txt", "w").write(os.environ["DCODE_API_KEY"])
    finish("final_answer", 0)
if prompt.startswith("env"):
    print(json.dumps(args))
    json.dump({"env": dict(os.environ), "argv": args, "cwd": os.getcwd()}, open("env.json", "w"))
    finish("final_answer", 0)
if prompt.startswith("background"):
    subprocess.Popen(["sleep", "31337"])
    finish("final_answer", 0)
finish("final_answer", 0)
'''


def make_task(root: Path, name: str, prompt: str, requires: list[str] | None = None, category: str = "coding", timeout: float = 60) -> None:
    folder = root / name
    (folder / "repo").mkdir(parents=True)
    (folder / "check").mkdir()
    (folder / "repo" / "x.txt").write_text("start\n")
    (folder / "check" / "check.sh").write_text("grep -q solved x.txt\n")
    (folder / "solution.patch").write_text("not used by the runner\n")
    (folder / "task.json").write_text(json.dumps({"id": name, "category": category, "difficulty": "easy", "prompt": prompt, "timeout_s": timeout,
                                                  "requires": requires or [], "tags": []}))


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Two arms, one model, the fake agent, and a way to run the runner."""
    monkeypatch.setenv("PATH", os.environ["PATH"])  # the runner cleans it for its children
    folder = tmp_path / "lab"
    folder.mkdir()
    fake = folder / "fake-dcode"
    fake.write_text(FAKE_DCODE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (folder / "arms.json").write_text(json.dumps({"arms": [{"name": "baseline", "experiment": {}}, {"name": "other", "experiment": {"delegation": False}}]}))
    (folder / "models.json").write_text(json.dumps({"m": {"base_url": "http://user:pw@model-host:9/v1", "model": "x", "api_key_env": None, "context_window": 8000}}))

    class Lab:
        root = folder / "tasks"
        out = folder / "out"
        path = folder

        def run(self, *extra: str) -> int:
            return runner.main(["--root", str(self.root), "--arms", str(folder / "arms.json"), "--models", str(folder / "models.json"), "--out", str(self.out),
                                "--workdir", str(folder / "work"), "--dcode", str(fake), *extra])

        def result(self, task: str, arm: str = "baseline", repeat: int = 0) -> dict:
            return json.loads((self.out / "runs" / "m" / arm / task / f"r{repeat}" / "result.json").read_text())

    Lab.root.mkdir()
    return Lab()


def simple_task(name: str, requires: list[str] | None = None) -> common.Task:
    return common.Task(name, Path("/nowhere") / name, {"id": name, "category": "coding", "difficulty": "easy", "prompt": "p", "timeout_s": 1, "requires": requires or [], "tags": []})


# --- planning ------------------------------------------------------------------------------------------------------
def plan(monkeypatch, tasks: int, arms: int, repeats: int, parallel: int, models: int = 1) -> list[runner.Run]:
    monkeypatch.setattr(common, "tree_hash", lambda path: "h")
    return runner.build_plan([simple_task(f"t{i}") for i in range(tasks)], [{"name": f"a{i}"} for i in range(arms)],
                             {f"m{i}": {"base_url": "u", "model": "x"} for i in range(models)}, repeats, parallel)


def test_plan_covers_every_combination_once_in_order(monkeypatch):
    runs = plan(monkeypatch, 4, 3, 2, 2, models=2)
    assert len(runs) == 4 * 3 * 2 * 2
    assert len({(r.task.id, r.arm["name"], r.model_name, r.repeat) for r in runs}) == len(runs)
    assert [r.order_index for r in runs] == list(range(len(runs)))


def test_no_arm_is_always_first(monkeypatch):
    runs = plan(monkeypatch, 6, 3, 1, 1)
    firsts = [runs[i].arm["name"] for i in range(0, len(runs), 3)]
    assert firsts == ["a0", "a1", "a2", "a0", "a1", "a2"]
    second_repeat = plan(monkeypatch, 3, 3, 2, 1)
    assert second_repeat[0].arm["name"] != second_repeat[9].arm["name"]


def test_arms_of_a_task_stay_together_and_on_one_slot(monkeypatch):
    runs = plan(monkeypatch, 5, 4, 3, 2)
    slots = {}
    for run in runs:
        slots.setdefault(run.task.id, set()).add(run.slot)
    assert all(len(found) == 1 for found in slots.values())
    assert {run.slot for run in runs} == {0, 1}
    for start in range(0, len(runs), 4):
        assert len({r.task.id for r in runs[start:start + 4]}) == 1


def test_spec_hash_follows_what_a_result_depends_on():
    base = runner.spec_hash("t", {"name": "a", "experiment": {}}, {"model": "x"})
    assert base == runner.spec_hash("t", {"name": "a", "experiment": {}}, {"model": "x"})
    assert base != runner.spec_hash("t2", {"name": "a", "experiment": {}}, {"model": "x"})
    assert base != runner.spec_hash("t", {"name": "a", "experiment": {"seed": 1}}, {"model": "x"})
    assert base != runner.spec_hash("t", {"name": "a", "experiment": {}}, {"model": "y"})
    assert base != runner.spec_hash("t", {"name": "a", "experiment": {}}, {"model": "x"}, "seedhash")


@pytest.mark.parametrize("timed_out, passed, code, reason, want", [
    (True, True, 1, "interrupted", "timeout"), (True, False, None, None, "timeout"),
    (False, True, 0, "final_answer", "pass"), (False, True, 1, "step_limit", "pass"), (False, True, 3, "final_answer", "pass"),
    (False, False, 0, "final_answer", "fail"), (False, False, 1, "step_limit", "fail"), (False, False, 1, "context_overflow", "fail"), (False, False, 1, "stuck_guard", "fail"),
    (False, False, 3, "final_answer", "fail"),
    (False, False, 2, None, "error"), (False, False, 1, "error", "error"), (False, False, 1, None, "error"), (False, False, -9, None, "error"), (False, False, None, None, "error"),
])
def test_classify(timed_out, passed, code, reason, want):
    assert runner.classify(timed_out, passed, code, reason) == want


# --- the shipped files ---------------------------------------------------------------------------------------------
def test_the_arms_file_is_valid_and_has_every_arm():
    arms = {arm["name"]: arm for arm in runner.load_arms(BENCH / "arms.json")}
    assert set(arms) >= {"baseline", "no_earlier_elision", "no_summarize", "no_payload_stub", "stub_write_args", "no_delegation", "recall_user", "no_reasoning_cutoff",
                         "small_output_caps", "window_16k", "window_32k"}
    assert arms["window_16k"]["context_window"] == 16384 and arms["window_32k"]["context_window"] == 32768
    assert arms["recall_user"]["control"] == "recall_system"
    assert arms["recall_user"]["seed_home"] == arms["recall_system"]["seed_home"]


def test_seeded_memories_are_found_by_dcode(tmp_path, monkeypatch):
    from dcode.memory import MemoryStore

    arms = {arm["name"]: arm for arm in runner.load_arms(BENCH / "arms.json")}
    shutil.copytree(BENCH / arms["recall_user"]["seed_home"], tmp_path / "home", dirs_exist_ok=True)
    monkeypatch.setenv("DCODE_HOME", str(tmp_path / "home"))
    assert len(MemoryStore(tmp_path).list("global")) >= 3


def test_shipped_models_need_no_key_and_the_example_names_its_variable():
    assert runner.resolve_keys(runner.load_models(BENCH / "models.json"))
    assert runner.load_models(BENCH / "models.example.json")["qwen-32k"]["api_key_env"] == "BENCH_API_KEY"


@pytest.mark.parametrize("body, message", [
    ({"arms": [{"name": "a", "experiment": {"no_such_switch": True}}]}, "no_such_switch"),
    ({"arms": [{"name": "a", "experiment": {"summarization": "yes"}}]}, "summarization"),
    ({"arms": [{"name": "a"}, {"name": "a"}]}, "twice"),
    ({"arms": [{"name": "a", "control": "zzz"}]}, "control"),
    ({"arms": [{"name": "a", "seed_home": "nope"}]}, "seed"),
    ({"arms": [{"name": "a", "contxt_window": 5}]}, "contxt_window"),
    ({"arms": []}, "arms"),
])
def test_a_bad_arm_stops_before_anything_runs(tmp_path, body, message):
    path = tmp_path / "arms.json"
    path.write_text(json.dumps(body))
    with pytest.raises(runner.BenchError, match=message):
        runner.load_arms(path)


# --- runs, with the fake agent -------------------------------------------------------------------------------------
def test_a_solved_task_passes_and_the_record_is_complete(lab):
    make_task(lab.root, "good", "solve it")
    assert lab.run() == 0
    result = lab.result("good")
    assert result["status"] == "pass" and result["check_passed"] is True and result["exit_code"] == 0 and result["end_reason"] == "final_answer"
    assert (result["prompt_tokens"], result["completion_tokens"], result["total_tokens"], result["cached_tokens"]) == (100, 10, 110, 40)
    assert (result["calls"], result["steps"], result["tool_calls"], result["usage_source"]) == (1, 3, 2, "task_end")
    assert result["model_s"] == 0.5 and result["tool_s"] == 0.25 and result["wall_s"] > 0
    folder = lab.out / "runs" / "m" / "baseline" / "good" / "r0"
    assert "+solved" in (folder / "diff.patch").read_text()
    assert result["diff_bytes"] > 0
    assert (folder / "experiment.json").read_text().strip() == "{}"
    assert json.loads((lab.out / "runs" / "m" / "other" / "good" / "r0" / "experiment.json").read_text()) == {"delegation": False}


def test_an_unsolved_task_fails(lab):
    make_task(lab.root, "bad", "wrong answer")
    assert lab.run() == 0
    assert lab.result("bad")["status"] == "fail"
    assert lab.result("bad")["check_passed"] is False


def test_a_missing_tool_skips_and_spends_nothing(lab):
    make_task(lab.root, "go", "solve", requires=["surely-no-such-tool-xyz"])
    assert lab.run() == 0
    result = lab.result("go")
    assert result["status"] == "skipped" and result["missing_tools"] == ["surely-no-such-tool-xyz"]
    assert result["calls"] == 0
    assert sorted(p.name for p in (lab.out / "runs" / "m" / "baseline" / "go" / "r0").iterdir()) == ["result.json"]


def test_a_timeout_is_scored_and_keeps_the_agents_records(lab):
    make_task(lab.root, "slow", "hang", timeout=1)
    assert lab.run("--timeout-scale", "0.5", "--tasks", "slow") == 0
    result = lab.result("slow")
    assert result["status"] == "timeout" and result["timed_out"] is True
    assert result["check_passed"] is True  # it had done the work, and analysis can see that
    assert result["end_reason"] == "interrupted"  # the interrupt reached it, and it wrote its end
    assert result["timeout_s"] == 0.5
    assert result["wall_s"] < 20


def test_a_config_failure_or_server_error_is_an_error_and_a_context_overflow_is_a_result(lab):
    make_task(lab.root, "broken", "config problem")
    make_task(lab.root, "server", "servererror")
    make_task(lab.root, "full", "overflow")
    assert lab.run() == 0
    assert lab.result("broken")["status"] == "error" and lab.result("broken")["exit_code"] == 2
    assert lab.result("server")["status"] == "error"
    assert lab.result("full")["status"] == "fail"  # the arm's own doing, so it must count against the arm


def test_the_agent_gets_no_credentials_and_a_fixed_workspace(lab, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/root/.kube/admin.conf")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("OPENROUTER_KEY_FOR_TEST", "or-secret")
    monkeypatch.setenv("VIRTUAL_ENV", "/some/venv")
    monkeypatch.setenv("PATH", "/some/venv/bin:" + os.environ["PATH"])
    make_task(lab.root, "probe", "env dump")

    captured = []
    real_capture = common.capture_diff

    def capture(workspace, base):
        captured.append((workspace, json.loads((workspace / "env.json").read_text())))
        return real_capture(workspace, base)

    monkeypatch.setattr(common, "capture_diff", capture)
    assert lab.run("--arms", str(lab.path / "arms.json")) == 0
    assert len(captured) == 2
    assert captured[0][0] == captured[1][0], "every arm of a task gets the same workspace path"
    workspace, seen = captured[0]
    assert workspace.name == "repo" and workspace.parent.name == "w0"
    env = seen["env"]
    assert env["KUBECONFIG"] == "/dev/null" and env["DOCKER_HOST"] == "unix:///nonexistent"
    assert "SSH_AUTH_SOCK" not in env and "AWS_SECRET_ACCESS_KEY" not in env and "OPENROUTER_KEY_FOR_TEST" not in env
    assert "/some/venv/bin" not in env["PATH"].split(":")
    assert env["HOME"] != os.environ.get("HOME") and env["DCODE_HOME"].endswith("/w0/home")
    assert env["DCODE_BASE_URL"] == "http://user:pw@model-host:9/v1" and env["DCODE_CONTEXT_WINDOW"] == "8000"
    argv = seen["argv"]
    assert argv[:3] == ["run", "--mode", "auto"]
    assert "--task-id" in argv and "--trace" in argv and "--experiment" in argv and "--json-summary" in argv


def test_arm_window_overrides_the_model_and_is_recorded(lab):
    (lab.path / "arms.json").write_text(json.dumps({"arms": [{"name": "baseline", "experiment": {}}, {"name": "w16", "experiment": {}, "context_window": 16384}]}))
    make_task(lab.root, "t", "solve")
    assert lab.run() == 0
    assert lab.result("t")["context_window"] == 8000 and lab.result("t", "w16")["context_window"] == 16384


def test_max_steps_and_seed_reach_the_agent(lab):
    make_task(lab.root, "probe", "env dump")
    meta = json.loads((lab.root / "probe" / "task.json").read_text())
    (lab.root / "probe" / "task.json").write_text(json.dumps({**meta, "max_steps": 40}))
    assert lab.run("--seed", "100", "--repeats", "2") == 0
    folder = lab.out / "runs" / "m" / "other" / "probe"
    assert lab.result("probe", "other", 1)["max_steps"] == 40
    argv = json.loads((folder / "r0" / "stdout.log").read_text().splitlines()[0])
    assert argv[argv.index("--max-steps") + 1] == "40"
    assert json.loads((folder / "r0" / "experiment.json").read_text()) == {"delegation": False, "seed": 100}
    assert json.loads((folder / "r1" / "experiment.json").read_text()) == {"delegation": False, "seed": 101}
    assert lab.result("probe", "baseline", 1)["seed"] == 101, "the arms of a repeat share a seed"


def test_the_key_is_never_written(lab, monkeypatch):
    secret = "sk-very-secret-value-123456"
    monkeypatch.setenv("MY_BENCH_KEY", secret)
    models = json.loads((lab.path / "models.json").read_text())
    models["m"]["api_key_env"] = "MY_BENCH_KEY"
    (lab.path / "models.json").write_text(json.dumps(models))
    make_task(lab.root, "clean", "solve")
    make_task(lab.root, "leaky", "leak the key")
    assert lab.run() == 0
    assert lab.result("clean")["key_seen"] is False
    assert lab.result("leaky")["key_seen"] is True
    for path in lab.out.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), str(path)
    plan_file = json.loads((lab.out / "plan.json").read_text())
    assert plan_file["models"]["m"]["host"] == "model-host"
    assert "base_url" not in plan_file["models"]["m"] and "pw" not in (lab.out / "plan.json").read_text()


def test_a_missing_key_fails_before_anything_runs(lab, monkeypatch, capsys):
    monkeypatch.delenv("MY_BENCH_KEY", raising=False)
    models = json.loads((lab.path / "models.json").read_text())
    models["m"]["api_key_env"] = "MY_BENCH_KEY"
    (lab.path / "models.json").write_text(json.dumps(models))
    make_task(lab.root, "t", "solve")
    assert lab.run() == 2
    assert "MY_BENCH_KEY" in capsys.readouterr().err
    assert not lab.out.exists()


def test_agents_background_processes_are_killed(lab):
    make_task(lab.root, "bg", "background")
    (lab.path / "arms.json").write_text(json.dumps({"arms": [{"name": "baseline", "experiment": {}}]}))
    assert lab.run() == 0
    time.sleep(0.2)
    assert subprocess.run(["pgrep", "-f", "^sleep 31337$"], capture_output=True).returncode == 1


# --- resume --------------------------------------------------------------------------------------------------------
def test_results_arent_overwritten_without_resume(lab, capsys):
    make_task(lab.root, "t", "solve")
    assert lab.run() == 0
    assert lab.run() == 2
    assert "--resume" in capsys.readouterr().err


def test_resume_skips_what_is_done_and_redoes_what_is_not(lab):
    make_task(lab.root, "t", "solve")
    assert lab.run() == 0
    done = lab.out / "runs" / "m" / "baseline" / "t" / "r0" / "result.json"
    stamp = done.stat().st_mtime_ns
    partial = lab.out / "runs" / "m" / "other" / "t" / "r0"
    (partial / "result.json").unlink()
    (partial / "leftover").write_text("half done")
    assert lab.run("--resume") == 0
    assert done.stat().st_mtime_ns == stamp
    assert lab.result("t", "other")["status"] == "pass"
    assert not (partial / "leftover").exists()


def test_resume_adds_repeats(lab):
    make_task(lab.root, "t", "solve")
    assert lab.run() == 0
    assert lab.run("--resume", "--repeats", "2") == 0
    with open(lab.out / "results.csv", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 4


def test_errors_stick_unless_retried(lab):
    make_task(lab.root, "t", "config problem")
    assert lab.run() == 0
    stamp = (lab.out / "runs" / "m" / "baseline" / "t" / "r0" / "result.json").stat().st_mtime_ns
    assert lab.run("--resume") == 0
    assert (lab.out / "runs" / "m" / "baseline" / "t" / "r0" / "result.json").stat().st_mtime_ns == stamp
    assert lab.run("--resume", "--retry-errors") == 0
    assert (lab.out / "runs" / "m" / "baseline" / "t" / "r0" / "result.json").stat().st_mtime_ns != stamp


def test_skipped_runs_are_redone_once_the_tool_exists(lab):
    make_task(lab.root, "t", "solve", requires=["fake-tool-for-bench"])
    assert lab.run() == 0
    assert lab.result("t")["status"] == "skipped"
    tool = lab.path / "bin" / "fake-tool-for-bench"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    os.environ["PATH"] = f"{tool.parent}:{os.environ['PATH']}"
    assert lab.run("--resume") == 0
    assert lab.result("t")["status"] == "pass"


def test_resume_refuses_a_changed_task(lab, capsys):
    make_task(lab.root, "t", "solve")
    assert lab.run() == 0
    (lab.root / "t" / "repo" / "x.txt").write_text("a different start\n")
    assert lab.run("--resume") == 2
    assert "different" in capsys.readouterr().err


def test_resume_refuses_a_changed_arm(lab, capsys):
    make_task(lab.root, "t", "solve")
    assert lab.run() == 0
    (lab.path / "arms.json").write_text(json.dumps({"arms": [{"name": "baseline", "experiment": {"summarization": False}}, {"name": "other", "experiment": {"delegation": False}}]}))
    assert lab.run("--resume") == 2
    assert "different" in capsys.readouterr().err


def test_a_second_runner_on_the_same_workdir_is_refused(lab, capsys):
    import fcntl

    make_task(lab.root, "t", "solve")
    (lab.path / "work").mkdir()
    with open(lab.path / "work" / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert lab.run() == 2
    assert "another runner" in capsys.readouterr().err


def test_ctrl_c_stops_cleanly_and_resume_finishes(lab):
    make_task(lab.root, "first", "solve")
    make_task(lab.root, "slow", "hang")
    (lab.path / "arms.json").write_text(json.dumps({"arms": [{"name": "baseline", "experiment": {}}]}))
    code = (f"import sys; sys.path[:0] = [{str(BENCH)!r}]; import run; "
            f"sys.exit(run.main(['--root', {str(lab.root)!r}, '--arms', {str(lab.path / 'arms.json')!r}, '--models', {str(lab.path / 'models.json')!r}, "
            f"'--out', {str(lab.out)!r}, '--workdir', {str(lab.path / 'work')!r}, '--dcode', {str(lab.path / 'fake-dcode')!r}]))")
    process = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).parent.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.time()
    target = lab.out / "runs" / "m" / "baseline" / "slow" / "r0" / "stdout.log"
    while not target.exists() and time.time() - started < 30:
        time.sleep(0.05)
    assert target.exists()
    time.sleep(0.3)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=30) == 130
    assert lab.result("first")["status"] == "pass"
    assert not (lab.out / "runs" / "m" / "baseline" / "slow" / "r0" / "result.json").exists()
    with open(lab.out / "results.csv", newline="") as handle:
        assert [row["task"] for row in csv.DictReader(handle)] == ["first"]
    assert subprocess.run(["pgrep", "-f", str(lab.path / "fake-dcode")], capture_output=True).returncode == 1, "the agent was left running"
    (lab.root / "slow" / "task.json").write_text(json.dumps({"id": "slow", "category": "coding", "difficulty": "easy", "prompt": "solve", "timeout_s": 60, "requires": [], "tags": []}))
    assert lab.run("--resume") == 0
    assert lab.result("slow")["status"] == "pass"


def test_parallel_slots_run_at_once_and_stay_apart(lab):
    make_task(lab.root, "a", "solve")
    make_task(lab.root, "b", "solve")
    make_task(lab.root, "c", "solve")
    assert lab.run("--parallel", "2") == 0
    assert {lab.result(name)["slot"] for name in "abc"} == {0, 1}
    assert all(lab.result(name)["status"] == "pass" for name in "abc")
    assert lab.result("a")["slot"] == lab.result("a", "other")["slot"]


def test_trace_totals_from_a_killed_run_come_from_its_records(tmp_path):
    path = tmp_path / "t.jsonl"
    rows = [{"type": "llm_call", "counted_prompt_tokens": 100, "counted_completion_tokens": 5, "cached_tokens": None, "total_ms": 1000},
            {"type": "llm_call", "counted_prompt_tokens": 300, "counted_completion_tokens": 7, "cached_tokens": 50, "total_ms": 500, "usage_estimated": True},
            {"type": "tool_call", "name": "read_file", "duration_ms": 200}, {"type": "tool_call", "name": "delegate", "duration_ms": 9000},
            {"type": "compaction", "elided": 2}, {"type": "elision", "count": 3}, {"type": "elision_regret"}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + '\n{"type": "llm_ca')
    got = runner.read_trace(path)
    assert (got["calls"], got["prompt_tokens"], got["completion_tokens"], got["cached_tokens"], got["estimated_calls"]) == (2, 400, 12, 50, 1)
    assert (got["tool_calls"], got["tool_s"], got["model_s"]) == (2, 0.2, 1.5)
    assert (got["compactions"], got["elided"], got["elision_count"], got["elision_regrets"], got["max_prompt_tokens"]) == (1, 2, 3, 1, 300)
    assert got["usage_source"] == "records" and got["end_reason"] is None
    assert runner.read_trace(tmp_path / "missing.jsonl")["usage_source"] == "none"


# --- analysis ------------------------------------------------------------------------------------------------------
def row(task="t", arm="baseline", repeat=0, status="pass", tokens=100, **extra) -> dict:
    base = {"model": "m", "arm": arm, "task": task, "repeat": repeat, "category": "coding", "difficulty": "easy", "context_window": 8000, "status": status, "end_reason": "final_answer",
            "prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens, "cached_tokens": 10, "calls": 1, "steps": 1, "wall_s": 1.0, "model_s": 0.5, "tool_s": 0.1,
            "elided": 0, "elision_count": 0, "elision_regrets": 0, "estimated_calls": 0, "dcode_commit": "abc"}
    return {**base, **extra}


def test_bootstrap_is_reproducible_and_covers_the_mean():
    clusters = {f"t{i}": [float(i % 2)] * 3 for i in range(20)}
    first = analyze.bootstrap_ci(clusters, "label", 500)
    assert first == analyze.bootstrap_ci(clusters, "label", 500)
    assert first[0] < 0.5 < first[1]
    assert analyze.bootstrap_ci({"only": [1.0, 0.0]}, "label") is None
    assert analyze.bootstrap_ci({f"t{i}": [1.0] for i in range(5)}, "label", 100) == (1.0, 1.0)


def test_bootstrap_resamples_tasks_not_repeats():
    """Ten repeats of each task must not look like ten times the evidence."""
    few = {f"t{i}": [float(i % 2)] for i in range(10)}
    repeated = {f"t{i}": [float(i % 2)] * 10 for i in range(10)}
    low, high = analyze.bootstrap_ci(few, "x", 1000)
    low2, high2 = analyze.bootstrap_ci(repeated, "x", 1000)
    assert (low, high) == (low2, high2)


def test_pass_rate_leaves_out_errors_and_skips_and_counts_timeouts_as_failures():
    rows = [row("a"), row("b", status="fail"), row("c", status="timeout"), row("d", status="error"), row("e", status="skipped")]
    summary = analyze.arm_summary(rows, 100)[0]
    assert (summary["counted"], summary["passed"], summary["errors"], summary["skipped"], summary["timeouts"]) == (3, 1, 1, 1, 1)
    assert summary["pass_rate"] == pytest.approx(1 / 3)


def test_tokens_per_pass_includes_the_failures():
    rows = [row("a", tokens=100), row("b", status="fail", tokens=300)]
    assert analyze.arm_summary(rows, 50)[0]["tokens_per_pass"] == 400
    assert analyze.arm_summary([row("a", status="fail")], 50)[0]["tokens_per_pass"] is None


def test_windows_are_separate_groups():
    rows = [row("a", context_window=8000), row("a", context_window=16384)]
    assert len(analyze.arm_summary(rows, 50)) == 2


def test_cached_share_ignores_runs_without_a_report():
    rows = [row("a", tokens=100, cached_tokens=50), row("b", tokens=900, cached_tokens=None)]
    assert analyze.arm_summary(rows, 50)[0]["cached_ratio"] == 0.5
    assert analyze.arm_summary([row("a", cached_tokens=None)], 50)[0]["cached_ratio"] is None


def test_paired_differences_use_the_control_and_skip_broken_pairs():
    rows = [row("a", tokens=100), row("a", "x", tokens=150), row("b", tokens=100), row("b", "x", status="fail", tokens=50), row("c"), row("c", "x", status="error"),
            row("a", "y", tokens=999), row("a", "z", tokens=120)]
    pairs = {p["arm"]: p for p in analyze.paired(rows, {"x": "baseline", "y": "z", "z": "baseline"}, 100)}
    assert pairs["x"]["pairs"] == 2 and pairs["x"]["tasks"] == 2
    assert pairs["x"]["delta_pass"] == -0.5 and pairs["x"]["delta_tokens"] == 0
    assert pairs["y"]["control"] == "z" and pairs["y"]["delta_tokens"] == 879
    assert "baseline" not in pairs


def test_regret_rate():
    rows = [row("a", elided=3, elision_count=1, elision_regrets=2), row("b", elided=4), row("c", status="error", elision_regrets=9)]
    got = analyze.regret_summary(rows)[0]
    assert (got["stubbed_outputs"], got["regrets"], got["regret_rate"], got["runs"], got["runs_with_regret"]) == (8, 2, 0.25, 2, 1)
    assert analyze.regret_summary([row("a")])[0]["regret_rate"] is None


def test_prefix_summary(tmp_path):
    folder = tmp_path / "runs" / "m" / "baseline" / "t" / "r0"
    folder.mkdir(parents=True)
    calls = [{"type": "llm_call", "prev_messages": None, "estimated_prompt_tokens": 100, "lcp_tokens": 0},
             {"type": "llm_call", "prev_messages": 2, "estimated_prompt_tokens": 200, "lcp_tokens": 200, "prefix_intact": True},
             {"type": "llm_call", "prev_messages": 4, "estimated_prompt_tokens": 400, "lcp_tokens": 100, "prefix_intact": False, "first_changed_role": "tool"},
             {"type": "llm_call", "prev_messages": 6, "estimated_prompt_tokens": 100, "lcp_tokens": 50, "prefix_intact": False, "first_changed_role": None, "tools_changed": True}]
    (folder / "trace.jsonl").write_text("\n".join(json.dumps(call) for call in calls) + '\n{"torn')
    got = analyze.prefix_summary(tmp_path, [row()])[0]
    assert got["calls"] == 3
    assert got["mean_lcp_ratio"] == pytest.approx((1 + 0.25 + 0.5) / 3)
    assert got["intact_share"] == pytest.approx(1 / 3) and got["tools_changed"] == 1
    assert got["breaks_by_role"] == {"tool": 1, "got shorter": 1}


def test_the_report_warns_about_what_changes_the_reading():
    rows = [row("a", status="error"), row("a", "x"), row("b", "x", estimated_calls=2, dcode_commit="def+dirty", key_seen="True", cached_tokens=None)]
    text = " ".join(analyze.warnings(rows, analyze.arm_summary(rows, 50)))
    assert "1 run(s) ended in `error`" in text and "differ between arms" in text
    assert "2 model call(s)" in text and "different Dcode commits" in text and "uncommitted" in text and "replace the key" in text
    assert analyze.warnings([row("a")], analyze.arm_summary([row("a")], 50)) == []
    assert "no cached tokens" in " ".join(analyze.warnings([row("a", cached_tokens=None)], analyze.arm_summary([row("a", cached_tokens=None)], 50)))


class FakeAxis:
    def __init__(self, log):
        self.log = log
        self.spines = {"top": self}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.log.append((name, args, kwargs))

        return record


def test_plots_are_skipped_without_matplotlib_and_drawn_with_it(tmp_path, monkeypatch):
    summary = analyze.arm_summary([row("a"), row("b", status="fail"), row("a", "x"), row("b", "x")], 50)
    pairs = analyze.paired([row("a"), row("a", "x", tokens=150), row("b"), row("b", "x")], {"x": "baseline"}, 50)
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    assert analyze.plots(tmp_path, summary, pairs) == []

    # matplotlib isn't a dependency: a stand-in runs the code path. How a chart looks is checked by eye where it's installed.
    log: list = []
    import types

    fake = types.ModuleType("matplotlib")
    fake.use = lambda name: None
    pyplot = types.ModuleType("matplotlib.pyplot")
    figure = types.SimpleNamespace(tight_layout=lambda: None, savefig=lambda path, **kw: Path(path).write_bytes(b"png"))
    pyplot.subplots = lambda **kw: (figure, FakeAxis(log))
    pyplot.close = lambda fig: None
    fake.pyplot = pyplot
    monkeypatch.setitem(sys.modules, "matplotlib", fake)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", pyplot)
    drawn = analyze.plots(tmp_path, summary, pairs)
    assert sorted(path.name for path in drawn) == ["delta_tokens_m.png", "pass_rate_m.png"]


# --- the whole pipeline --------------------------------------------------------------------------------------------
def test_smoke_pipeline_against_the_mock(tmp_path, monkeypatch):
    """3 tasks × 2 arms × 1 repeat, a real dcode, a real mock server. The mock can't solve tasks: every run is an expected fail."""
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.delenv("BENCH_NO_REAL_TOOLS", raising=False)
    out = tmp_path / "smoke"
    problems = smoke.run_smoke(out, tmp_path / "work")
    assert problems == []
    report = (out / "report.md").read_text()
    assert "a 0% pass rate is expected" in report
    with open(out / "results.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["status"] for row in rows} == {"fail"}
    assert (out / "plan.json").exists()
    # The totals match the trace's own records.
    baseline = json.loads((out / "runs" / "mock" / "baseline" / "smoke-long" / "r0" / "result.json").read_text())
    calls = [json.loads(line) for line in (out / "runs" / "mock" / "baseline" / "smoke-long" / "r0" / "trace.jsonl").read_text().splitlines() if '"llm_call"' in line]
    assert baseline["calls"] == len(calls)
    assert baseline["prompt_tokens"] == sum(call["counted_prompt_tokens"] for call in calls)


def test_validate_reports_a_broken_smoke_folder(tmp_path):
    out = tmp_path / "x"
    (out / "runs").mkdir(parents=True)
    (out / "results.csv").write_text("task,arm,order_index\na,b,3\n")
    with pytest.raises((FileNotFoundError, KeyError)):
        smoke.verify(out)
