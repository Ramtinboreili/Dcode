"""A helper's own steps are kept, saved with the chat and served, so its session can be opened."""

from __future__ import annotations

import json
import time

from conftest import FakeLLM, call, make_config

from dcode.config import load_thread, thread_path
from dcode.llm import AssistantTurn
from dcode.session import Session
from dcode.transcripts import HELPER_ARG, HELPER_KEEP, HelperLog


def _wait(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _router(messages, tools):
    usage = {"prompt": 50, "completion": 5, "total": 55}
    if "exploration sub-agent" in messages[0]["content"]:
        if not any(m["role"] == "tool" for m in messages):
            return AssistantTurn(content="Let me look.", reasoning="where would it be?", usage=usage,
                                 tool_calls=[call("list_files", '{"path": "."}', "h1")])
        return AssistantTurn(content="REPORT: one file, a.txt", usage=usage)
    if not any(m["role"] == "tool" for m in messages):
        return AssistantTurn(tool_calls=[call("delegate", json.dumps({"role": "explore", "task": "map the folder"}), "d1")])
    return AssistantTurn(content="The folder has one file.")


def test_helper_steps_are_kept_and_saved_with_the_chat(isolated):
    (isolated / "a.txt").write_text("x")
    config = make_config()
    session = Session(config, llm=FakeLLM(_router, config))
    try:
        session.submit("what is in here?")
        assert _wait(lambda: session.idle and len(session.agent.messages) > 2)
        (summary,) = session.helpers.summaries()
        assert summary["role"] == "explore" and summary["task"] == "map the folder" and summary["status"] == "done"
        assert summary["steps"] == 1 and summary["tokens_in"] == 100 and "items" not in summary
        assert summary["report"] == "REPORT: one file, a.txt"
        run = session.helpers.get(summary["id"])
        assert [i["type"] for i in run["items"]] == ["thinking", "assistant_note", "tool_call", "tool_result"]
        assert run["items"][2]["name"] == "list_files" and "a.txt" in run["items"][3]["output"]
        # The main context still holds only the report.
        assert not any("Let me look." in str(m.get("content")) for m in session.agent.messages)

        saved = load_thread(thread_path(session.thread_name))
        assert saved["helpers"][0]["items"][2]["name"] == "list_files"
        name = session.thread_name
        session.new_thread()
        assert session.helpers.summaries() == []
        assert session.resume(thread_path(name).stem)
        assert session.helpers.summaries()[0]["task"] == "map the folder"
        resumed = [e for e in session.bus.since(0) if e["type"] == "thread" and e["action"] == "resume"][-1]
        assert resumed["helpers"][0]["report"] == "REPORT: one file, a.txt"
    finally:
        session.close()


def test_helper_log_limits_and_reused_ids():
    log = HelperLog()
    log.feed({"type": "tool_call", "agent": "explore-1", "name": "read_file"})  # no start seen: ignored
    assert log.summaries() == []
    log.feed({"type": "agent_start", "agent": "code-1", "role": "code", "task": "write it", "ts": 10.0})
    log.feed({"type": "tool_call", "agent": "code-1", "name": "write_file", "id": "w",
              "args": {"path": "big.txt", "content": "x" * 5000}})
    log.feed({"type": "tool_result", "agent": "code-1", "name": "write_file", "id": "w", "output": "y" * 9000})
    log.feed({"type": "agent_end", "agent": "code-1", "ok": False, "report": "interrupted", "ts": 12.5})
    log.feed({"type": "tool_call", "agent": "code-1", "name": "late"})  # after the end: ignored
    run = log.get("code-1")
    assert run["status"] == "stopped" and run["seconds"] == 2.5 and len(run["items"]) == 2
    content = run["items"][0]["args"]["content"]
    assert len(content) < HELPER_ARG + 40 and content.endswith("(5,000 characters)")
    assert len(run["items"][1]["output"]) == 1500

    log.feed({"type": "agent_start", "agent": "code-1", "role": "code", "task": "again", "ts": 20.0})
    assert log.get("code-1")["task"] == "again" and log.get("code-1.1")["task"] == "write it"  # the same id after a restart

    for index in range(HELPER_KEEP + 5):
        log.feed({"type": "agent_start", "agent": f"explore-{index}", "role": "explore", "task": str(index)})
    assert len(log.summaries()) == HELPER_KEEP and log.get("code-1") is None

    fresh = HelperLog()
    fresh.load([{"id": "x-1", "status": "running", "items": []}, "junk", {"id": 5}])
    assert fresh.summaries() == [{"id": "x-1", "status": "stopped"}]  # whoever ran it is gone
