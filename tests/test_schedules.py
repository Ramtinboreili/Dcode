from __future__ import annotations

import sys
import threading
import time
from datetime import datetime

import pytest
from conftest import FakeLLM, call, make_config, scripted
from test_server import req, stack  # noqa: F401  (stack is a fixture)

from dcode import headless, schedules, server
from dcode.config import save_config
from dcode.llm import AssistantTurn

PY = sys.executable


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --- cron expressions ---------------------------------------------------------------------------

def test_cron_expressions_match_like_cron():
    at = datetime(2026, 10, 8, 14, 30)  # a Thursday
    for expression in ("* * * * *", "30 14 * * *", "*/15 * * * *", "0-45/15 14 8 10 *", "30 14 * * thu", "30 14 * oct 4",
                       "30 14 1 * 4", "30 14 8 * 1"):  # day and weekday both set: either is enough
        assert schedules.matches(expression, at), expression
    for expression in ("31 14 * * *", "30 15 * * *", "30 14 * * 1-3", "30 14 9 * *", "30 14 * 11 *", "30 14 1 * 1"):
        assert not schedules.matches(expression, at), expression
    assert schedules.matches("0 0 * * 7", datetime(2026, 10, 11)) and schedules.matches("@weekly", datetime(2026, 10, 11))
    assert schedules.matches("@hourly", datetime(2026, 1, 1, 5, 0)) and not schedules.matches("@daily", datetime(2026, 1, 1, 5, 0))


def test_next_run_and_plain_words():
    after = datetime(2026, 10, 8, 14, 30, 20)
    assert schedules.next_run("*/15 * * * *", after) == datetime(2026, 10, 8, 14, 45)
    assert schedules.next_run("30 2 * * *", after) == datetime(2026, 10, 9, 2, 30)
    assert schedules.next_run("0 9 * * 1-5", datetime(2026, 10, 9, 9, 0)) == datetime(2026, 10, 12, 9, 0)  # Fri → Mon
    assert schedules.next_run("0 0 29 2 *", after) == datetime(2028, 2, 29)
    assert schedules.next_run("0 0 31 2 *", after) is None  # never
    assert schedules.explain("*/5 * * * *") == "every 5 minutes"
    assert schedules.explain("30 2 * * *") == "every day at 02:30"
    assert schedules.explain("0 9 * * 1-5") == "Monday to Friday at 09:00"
    assert schedules.explain("15 * * * *") == "every hour at :15"
    assert schedules.explain("@daily") == "every day at 00:00"
    assert schedules.explain("0 3 1 * *") == "on day 1 of every month at 03:00"
    assert schedules.explain("@yearly") == "every year on January 1 at 00:00"
    assert schedules.explain("5 4 * 2 1,3") == "5 4 * 2 1,3"  # unusual: shown as written


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *", "*/0 * * * *", "a b c d e", "5-1 * * * *"])
def test_bad_schedules_are_explained(bad):
    with pytest.raises(schedules.ScheduleError):
        schedules.parse(bad)


# --- jobs -----------------------------------------------------------------------------------------

def test_jobs_are_validated_and_saved_privately(isolated):
    job = schedules.save_job({"name": "Disk report", "schedule": "*/10  * * * *", "command": "df -h"})
    assert job["id"] == "disk-report" and job["schedule"] == "*/10 * * * *" and job["enabled"] and job["kind"] == "command"
    assert schedules.save_job({"name": "Disk report", "schedule": "@daily", "command": "df"})["id"] == "disk-report-2"
    edited = schedules.save_job({"id": "disk-report", "name": "Disk", "schedule": "@hourly", "command": "df -h /", "enabled": False})
    assert edited["id"] == "disk-report" and edited["created"] == job["created"] and not edited["enabled"]
    assert set(schedules.load_jobs()) == {"disk-report", "disk-report-2"}
    assert oct(schedules.cron_dir().joinpath("jobs.json").stat().st_mode)[-3:] == "600"
    shown = schedules.describe_job(schedules.load_jobs()["disk-report-2"], datetime(2026, 10, 8, 14, 30))
    assert shown["schedule_text"] == "every day at 00:00" and shown["next_run"] == "2026-10-09T00:00" and shown["last_run"] is None
    assert schedules.describe_job(edited)["next_run"] is None  # off: nothing coming
    for bad, why in (({"schedule": "@daily", "command": ""}, "command to run"),
                     ({"schedule": "nope", "command": "x"}, "five fields"),
                     ({"schedule": "0 0 31 2 *", "command": "x"}, "never comes up"),
                     ({"schedule": "@daily", "command": "x", "cwd": "/no/such/folder"}, "doesn't exist"),
                     ({"schedule": "@daily", "command": "tidy up", "kind": "prompt"}, "project folder")):
        with pytest.raises(schedules.ScheduleError, match=why):
            schedules.save_job(bad)
    assert schedules.delete_job("disk-report") and not schedules.delete_job("disk-report")


def test_a_prompt_job_starts_an_unattended_dcode(isolated):
    job = schedules.save_job({"name": "Nightly", "schedule": "@daily", "kind": "prompt", "command": "summarize the logs",
                              "cwd": str(isolated), "mode": "auto"})
    argv, shell = schedules.job_argv(job)
    assert not shell and argv[-4:] == ["run", "--mode", "auto", "summarize the logs"] and argv[:3] == [PY, "-m", "dcode"]
    assert schedules.save_job({**job, "mode": "manual"})["mode"] == "accept_edits"  # manual would deny everything


def test_a_run_is_logged_with_its_result(isolated):
    ok = schedules.save_job({"name": "hello", "schedule": "@daily", "command": "echo out; echo err >&2; pwd", "cwd": str(isolated)})
    run = schedules.run_job(ok, "manual")
    assert (run["status"], run["exit_code"], run["trigger"]) == ("ok", 0, "manual") and run["finished"]
    log = schedules.read_log("hello", run["id"])
    assert "out\nerr\n" in log and str(isolated) in log and "# finished: exit code 0" in log and log.startswith("# hello · manual")
    bad = schedules.save_job({"name": "broken", "schedule": "@daily", "command": "echo nope; exit 7"})
    run = schedules.run_job(bad)
    assert (run["status"], run["exit_code"]) == ("failed", 7)
    assert schedules.describe_job(bad)["last_run"]["status"] == "failed"
    assert [r["status"] for r in schedules.load_runs("broken")] == ["failed"]


def test_a_run_that_takes_too_long_is_stopped_with_its_children(isolated):
    job = {"id": "slow", "name": "slow", "kind": "command", "command": "sleep 30 & echo started; wait", "cwd": "", "timeout": 1}
    began = time.time()
    run = schedules.run_job(job)
    assert run["status"] == "timeout" and time.time() - began < 10
    assert "stopped after the 1s time limit" in schedules.read_log("slow", run["id"])


def test_old_runs_and_long_logs_are_trimmed(isolated, monkeypatch):
    monkeypatch.setattr(schedules, "KEEP_RUNS", 2)
    monkeypatch.setattr(schedules, "MAX_LOG_BYTES", 2000)
    job = {"id": "chatty", "name": "chatty", "kind": "command", "command": f"{PY} -c \"print('x' * 9000)\"", "cwd": "", "timeout": 20}
    runs = [schedules.run_job(job) for _ in range(3)]
    kept = schedules.load_runs("chatty")
    assert [r["id"] for r in kept] == [r["id"] for r in runs[1:]]
    assert not (schedules.cron_dir() / "logs" / "chatty" / runs[0]["log"]).exists()
    log = schedules.read_log("chatty", runs[2]["id"])
    assert "log cut at 2,000 bytes" in log and len(log) < 2400 and runs[2]["status"] == "ok"
    assert schedules.read_log("chatty", "../../jobs") == ""  # only logs of this job


# --- the scheduler ----------------------------------------------------------------------------------

def test_scheduler_runs_what_is_due_once_per_minute(isolated):
    schedules.save_job({"name": "due", "schedule": "30 14 * * *", "command": "echo due"})
    schedules.save_job({"name": "later", "schedule": "31 14 * * *", "command": "echo later"})
    schedules.save_job({"name": "off", "schedule": "30 14 * * *", "command": "echo off", "enabled": False})
    scheduler = schedules.Scheduler()
    assert scheduler.tick(datetime(2026, 10, 8, 14, 30, 2)) == ["due"]
    assert scheduler.tick(datetime(2026, 10, 8, 14, 30, 8)) == []           # same minute: not again
    assert _wait(lambda: not scheduler.busy())
    assert scheduler.tick(datetime(2026, 10, 8, 14, 31, 1)) == ["later"]
    assert _wait(lambda: not scheduler.busy())
    assert [r["status"] for r in schedules.load_runs("due")] == ["ok"] and schedules.load_runs("off") == []
    # Saved (or the hub started) in the middle of a matching minute: wait for the next one, like cron.
    schedules.save_job({"name": "fresh", "schedule": "* * * * *", "command": "echo fresh"})
    assert scheduler.tick(datetime(2026, 10, 8, 14, 31, 40)) == []
    assert scheduler.tick(datetime(2026, 10, 8, 14, 32, 3)) == ["fresh"] and _wait(lambda: not scheduler.busy())


def test_run_now_and_overlap(isolated):
    schedules.save_job({"name": "long", "schedule": "* * * * *", "command": "sleep 5"})
    schedules.save_job({"name": "manual", "schedule": "0 0 1 1 *", "command": "echo by hand", "enabled": False})
    scheduler = schedules.Scheduler()
    schedules.update_job("manual", run_requested=True)  # what /cron run does
    assert sorted(scheduler.tick(datetime(2026, 10, 8, 14, 30))) == ["long", "manual"]
    assert "run_requested" not in schedules.describe_job(schedules.load_jobs()["manual"])
    assert not schedules.load_jobs()["manual"].get("run_requested")
    assert _wait(lambda: scheduler._running.get("long") is not None)
    # Still running a minute later: noted as skipped, not started twice.
    assert scheduler.tick(datetime(2026, 10, 8, 14, 31)) == []
    assert [r["status"] for r in schedules.load_runs("long")] == ["running", "skipped"]
    assert scheduler.stop_job("long") and _wait(lambda: not scheduler.busy())
    assert schedules.load_runs("long")[0]["status"] == "stopped"
    assert [(r["status"], r["trigger"]) for r in schedules.load_runs("manual")] == [("ok", "manual")]


def test_runs_left_running_by_a_dead_hub_are_settled(isolated):
    schedules.save_job({"name": "ghost", "schedule": "@daily", "command": "true"})
    schedules._record("ghost", {"id": "r1", "job": "ghost", "status": "running", "pid": 2 ** 22 + 12345, "log": "r1.log"})
    schedules.settle_stale_runs()
    assert schedules.load_runs("ghost")[0]["status"] == "interrupted"


# --- unattended runs (dcode run) ----------------------------------------------------------------------

def _headless(monkeypatch, capsys, router, *argv):
    save_config(make_config())
    monkeypatch.setattr("dcode.session.LLMClient", lambda config: FakeLLM(router, config))
    code = headless.main(list(argv))
    return code, capsys.readouterr().out


def test_unattended_run_denies_what_would_ask(isolated, monkeypatch, capsys):
    router = scripted(AssistantTurn(tool_calls=[call("run_command", '{"command": "touch made.txt"}')]),
                      AssistantTurn(content="I could not run it."))
    code, out = _headless(monkeypatch, capsys, router, "make", "a", "file")
    assert code == 3 and not (isolated / "made.txt").exists()
    assert "needs approval (run_command): denied" in out and "I could not run it." in out and "give the job a mode" in out


def test_unattended_run_in_auto_mode_does_the_work(isolated, monkeypatch, capsys):
    router = scripted(AssistantTurn(tool_calls=[call("run_command", '{"command": "touch made.txt"}')]),
                      AssistantTurn(content="Created made.txt."))
    monkeypatch.setenv("DCODE_CRON_JOB", "nightly")
    code, out = _headless(monkeypatch, capsys, router, "--mode", "auto", "make a file")
    assert code == 0 and (isolated / "made.txt").exists()
    assert "· run_command touch made.txt" in out and "Created made.txt." in out and "mode auto" in out
    from dcode.config import list_threads

    assert [t["name"][:8] for t in list_threads()] == ["nightly "]  # saved as its own chat
    assert headless.main([]) == 2 and headless.main(["--mode", "nope", "x"]) == 2


# --- the hub ----------------------------------------------------------------------------------------------

def test_hub_stays_up_for_enabled_jobs(isolated, monkeypatch):
    monkeypatch.setattr(server, "default_port", lambda configured=0: 0)
    monkeypatch.setattr(server, "HUB_IDLE_GRACE", 0.5)
    schedules.save_job({"name": "keep", "schedule": "0 0 1 1 *", "command": "true"})
    server.register_session({"id": "me", "pid": __import__("os").getpid(), "port": 1})
    hub = server.Hub()
    thread = threading.Thread(target=hub.serve, daemon=True)
    thread.start()
    time.sleep(1.2)
    server.unregister_session("me")
    time.sleep(2.5)
    assert thread.is_alive() and server.hub_file().exists()   # no terminal open, but a job is scheduled
    schedules.update_job("keep", enabled=False)
    thread.join(timeout=6)
    assert not thread.is_alive() and not server.hub_file().exists()


def test_cron_api(stack, isolated):  # noqa: F811
    status, preview, _ = req(stack, "GET", "/api/cron/preview?schedule=*/5%20*%20*%20*%20*")
    assert status == 200 and preview["text"] == "every 5 minutes" and len(preview["next"]) == 3
    assert req(stack, "GET", "/api/cron/preview?schedule=nope")[0] == 400
    status, job, _ = req(stack, "POST", "/api/cron", {"name": "Say hi", "schedule": "0 0 1 1 *", "command": "echo hi from cron"})
    assert status == 200 and job["id"] == "say-hi" and job["schedule_text"] and job["next_run"]
    assert req(stack, "POST", "/api/cron", {"name": "x", "schedule": "bad", "command": "x"})[0] == 400
    assert req(stack, "POST", "/api/cron/say-hi/run", {})[1]["ok"]
    assert _wait(lambda: (req(stack, "GET", "/api/cron/say-hi/runs")[1] or [{}])[0].get("status") == "ok")
    run = req(stack, "GET", "/api/cron/say-hi/runs")[1][0]
    assert run["trigger"] == "manual" and run["exit_code"] == 0
    assert "hi from cron" in req(stack, "GET", f"/api/cron/say-hi/log?run={run['id']}")[1]["text"]
    listed = req(stack, "GET", "/api/cron")[1]
    assert listed["jobs"][0]["last_run"]["status"] == "ok" and not listed["jobs"][0]["running"]
    assert req(stack, "POST", "/api/cron/say-hi/toggle", {"enabled": False})[1]["next_run"] is None
    assert req(stack, "POST", "/api/cron/say-hi/run", {}, csrf=False)[0] == 401
    assert req(stack, "DELETE", "/api/cron/say-hi")[1]["ok"] and req(stack, "GET", "/api/cron")[1]["jobs"] == []
    assert req(stack, "GET", "/api/cron/say-hi/runs")[0] == 404
