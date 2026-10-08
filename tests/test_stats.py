from __future__ import annotations

import json
import time

from click.testing import CliRunner
from conftest import FakeLLM, call, make_config

from dcode import stats
from dcode.llm import AssistantTurn
from dcode.session import Session


def _wait(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_finished_task_is_counted_with_its_helpers_and_tools(isolated):
    (isolated / "a.txt").write_text("x")

    def router(messages, tools):
        usage = {"prompt": 100, "completion": 10, "total": 110}
        if "exploration sub-agent" in messages[0]["content"]:
            return AssistantTurn(content="REPORT", usage=usage)
        kinds = [m.get("name") for m in messages if m["role"] == "tool" and m.get("name") != "run_command"]
        if not kinds:
            return AssistantTurn(tool_calls=[call("read_file", '{"path": "a.txt"}', "c1")], usage=usage)
        if kinds == ["read_file"]:
            return AssistantTurn(tool_calls=[call("delegate", '{"role": "explore", "task": "look"}', "c2")], usage=usage)
        return AssistantTurn(content="done", usage=usage)

    config = make_config()
    session = Session(config, llm=FakeLLM(router, config))
    try:
        session.run_shell("echo not-counted")  # your own command is not a model tool call
        session.submit("read it and explore")
        assert _wait(lambda: session.idle and stats.load())
    finally:
        session.close()
    rows = stats.load()
    assert len(rows) == 1
    row = rows[0]
    assert row["tools"] == {"read_file": 1, "delegate": 1} and row["helpers"] == 1
    assert row["prompt"] == 400 and row["completion"] == 40  # three main calls + the helper's
    assert row["model"] == "qwen2.5-coder" and row["cwd"] == str(isolated) and row["session"] == session.id
    assert "read it and explore" not in stats.stats_file().read_text()  # counts only, never the prompt
    assert oct(stats.stats_file().stat().st_mode & 0o777) == "0o600"


def test_summary_groups_and_survives_a_broken_line():
    for model, cwd, tokens in (("small", "/a", 1000), ("small", "/a", 2000), ("big", "/b", 50_000)):
        stats.record({"session": "s1", "chat": f"chat {cwd}", "cwd": cwd, "model": model, "prompt": tokens,
                      "completion": 100, "elapsed": 30.0, "files": 1, "commands": 2, "helpers": 0,
                      "tools": {"read_file": 3, "run_command": 2}})
    with stats.stats_file().open("a") as handle:
        handle.write('{"t": "2026-10-08T00:00:00+00:00", "prompt": 5')  # cut off by a crash
    summary = stats.summarize(30)
    assert summary["tasks"] == 3 and summary["chats"] == 2
    assert summary["tokens_in"] == 53_000 and summary["tokens_out"] == 300 and summary["seconds"] == 90.0
    assert summary["models"][0] == {"name": "small", "tasks": 2, "tokens_in": 3000, "tokens_out": 200, "seconds": 60.0}
    assert [p["name"] for p in summary["projects"]] == ["/a", "/b"]
    assert summary["tools"][0] == {"name": "read_file", "calls": 9} and summary["tool_calls"] == 15
    assert len(summary["daily"]) == 14 and summary["daily"][-1]["tasks"] == 3
    text = stats.render_text(summary)
    assert "3 in 2 chats" in text and "53K in · 300 out" in text and "By model" in text and "read_file" in text


def test_old_records_fall_outside_the_period_and_the_log_is_capped(monkeypatch):
    stats.stats_file().parent.mkdir(parents=True, exist_ok=True)
    stats.stats_file().write_text('{"t": "2020-01-01T00:00:00+00:00", "prompt": 7, "model": "old"}\n')
    stats.record({"model": "new", "prompt": 1})
    assert stats.summarize(30)["tasks"] == 1 and stats.summarize(0)["tasks"] == 2
    assert "No tasks recorded yet" in stats.render_text(stats.summarize(1) | {"tasks": 0})

    monkeypatch.setattr(stats, "MAX_LOG_BYTES", 2000)
    for index in range(60):
        stats.record({"model": "m", "n": index})
    rows = stats.load()
    assert 0 < len(rows) < 60 and rows[-1]["n"] == 59  # the oldest half went, the newest stayed


def test_stats_command(isolated):
    from dcode.main import cli

    runner = CliRunner()
    assert "No tasks recorded yet" in runner.invoke(cli, ["stats"]).output
    stats.record({"model": "m", "cwd": str(isolated), "prompt": 1200, "completion": 30, "elapsed": 5, "tools": {"x": 1}})
    out = runner.invoke(cli, ["stats", "--days", "7"])
    assert out.exit_code == 0 and "last 7 days" in out.output and "1.2K in" in out.output
    data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
    assert data["tasks"] == 1 and data["tokens_in"] == 1200
