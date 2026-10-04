from __future__ import annotations

import json
import time

from conftest import FakeLLM, call, make_config, scripted
from test_session import _session, _wait

from dcode.artifacts import ArtifactStore
from dcode.engine import Agent, NullHost
from dcode.jobs import JobManager
from dcode.llm import AssistantTurn


def _agent(router, host=None):
    config = make_config()
    return Agent(config, FakeLLM(router, config), host or NullHost())


def test_announced_artifact_without_tool_call_is_followed_up():
    """Reproduces the reported bug: 'Building the artifact now.' and then nothing."""
    html = json.dumps({"title": "Project overview", "kind": "html", "content": "<h1>Overview</h1>"})
    agent = _agent(scripted(
        AssistantTurn(content="I have the full picture. Building the artifact now."),
        AssistantTurn(tool_calls=[call("artifact_save", html)]),
        AssistantTurn(content="Saved the overview page."),
    ))
    result = agent.run_turn("make an html and tell me all about this project // make an artifact out of it")
    assert result.text == "Saved the overview page."
    assert [a["title"] for a in ArtifactStore().list()] == ["Project overview"]
    assert any(e["type"] == "notice" and "follow through" in e["text"] for e in agent.host.events)


def test_follow_ups_are_capped():
    agent = _agent(scripted(*[AssistantTurn(content="Doing it now.")] * 6))
    result = agent.run_turn("make an artifact of the readme")
    assert result.text == "Doing it now."  # gave up after the cap instead of looping
    reminders = [m for m in agent.messages if m["role"] == "user" and "artifact_save NOW" in m["content"]]
    assert len(reminders) == 2


def test_plain_answers_are_not_nagged():
    agent = _agent(scripted(AssistantTurn(content="The project is an Ansible role for nginx.")))
    assert agent.run_turn("what is this project?").text.startswith("The project is")
    assert not [m for m in agent.messages if m["role"] == "user" and "Remind" in m.get("content", "")]


def test_cut_off_reply_is_continued():
    agent = _agent(scripted(
        AssistantTurn(content="Part one of a long answer", finish_reason="length"),
        AssistantTurn(content="…and the end."),
    ))
    assert agent.run_turn("explain everything in detail").text == "…and the end."
    assert any("cut off" in m.get("content", "") for m in agent.messages if m["role"] == "user")


def test_artifact_append_builds_in_parts():
    first = json.dumps({"title": "Big page", "kind": "html", "content": "<h1>Part 1</h1>"})
    agent = _agent(scripted(AssistantTurn(tool_calls=[call("artifact_save", first, "c1")]), AssistantTurn(content="ok")))
    agent.run_turn("make an html artifact")
    aid = ArtifactStore().list()[0]["id"]
    more = json.dumps({"title": "Big page", "kind": "html", "content": "<p>Part 2</p>", "id": aid, "append": True})
    agent2 = _agent(scripted(AssistantTurn(tool_calls=[call("artifact_save", more, "c2")]), AssistantTurn(content="ok")))
    agent2.run_turn("add part 2 to the artifact")
    record = ArtifactStore().get(aid)
    assert record["content"] == "<h1>Part 1</h1><p>Part 2</p>" and len(record["history"]) == 0


def test_job_manager_lifecycle(tmp_path):
    exited = []
    jobs = JobManager(tmp_path, on_exit=exited.append)
    quick = jobs.start("echo hello-job", str(tmp_path))
    assert _wait(lambda: not quick.running)
    assert "hello-job" in quick.tail() and quick.state() == "exited (0)"
    slow = jobs.start("sleep 30", str(tmp_path))
    time.sleep(0.2)
    assert slow.running and jobs.running() == [slow]
    assert jobs.stop(slow.id) and _wait(lambda: not slow.running)
    assert slow.state() == "stopped"
    assert _wait(lambda: len(exited) == 2)


def test_background_command_through_the_session():
    session = _session(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "sleep 30", "background": true}', "c1")]),
        AssistantTurn(tool_calls=[call("job_status", '{"id": 1}', "c2")]),
        AssistantTurn(content="Server is running."),
    ))
    session.set_mode("auto")
    try:
        session.submit("start the dev server")
        assert _wait(lambda: session.idle)
        outputs = [m["content"] for m in session.agent.messages if m["role"] == "tool"]
        assert outputs[0].startswith("Started background job #1") and "[running" in outputs[1]
        assert [j["running"] for j in session.state()["jobs"]] == [True]
        assert any(e["type"] == "job_started" for e in session.bus.since(0))
    finally:
        session.close()
    assert not session.jobs.running()  # closing the session stops its jobs


def test_artifact_resave_loop_is_stopped_and_untitled_saves_update():
    """Reproduces the reported loop: the model kept re-saving and then created 'Untitled' copies."""
    page = "<!DOCTYPE html><html><body>" + "x" * 3000 + "</body></html>"
    titled = json.dumps({"title": "Repo report", "kind": "html", "content": page})
    untitled = json.dumps({"content": page})
    agent = _agent(scripted(
        AssistantTurn(tool_calls=[call("artifact_save", titled, "c1")]),
        AssistantTurn(tool_calls=[call("artifact_save", titled, "c2")]),
        AssistantTurn(tool_calls=[call("artifact_save", untitled, "c3")]),
        AssistantTurn(tool_calls=[call("artifact_save", untitled, "c4")]),
        AssistantTurn(content="It's ready."),
    ))
    agent.run_turn("make an html report artifact")
    items = ArtifactStore().list()
    assert len(items) == 1 and items[0]["title"] == "Repo report"  # no "Untitled" copies
    assert items[0]["versions"] == 3  # third save capped the rest
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert f"Received and stored {len(page)} chars" in results[0] and "don't save it again" in results[0]
    assert results[-1].startswith("Not saved again") and "IS stored" in results[-1]
    updates = [e.get("update") for e in agent.host.events if e["type"] == "artifact_saved"]
    assert updates == [False, True, True]


def test_one_artifact_per_chat_across_tasks():
    """Reproduces the screenshot: each new request in the same chat created another 'Untitled'."""
    host = NullHost()
    host.thread_name = "Ok make an artifact out all"
    page = "<html><body>report</body></html>"
    turns = [
        AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"title": "nginx-setup — Repo Activity Report", "kind": "html", "content": page}), "a")]),
        AssistantTurn(content="Saved."),
        AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"content": page + "<p>v2</p>"}), "b")]),  # untitled, next task
        AssistantTurn(content="Updated."),
        AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"title": "Nginx setup: repo activity report", "content": page + "<p>v3</p>"}), "c")]),
        AssistantTurn(content="Updated again."),
        AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"title": "Deploy checklist", "kind": "markdown", "content": "- [ ] tag"}), "d")]),
        AssistantTurn(content="New one."),
    ]
    agent = _agent(scripted(*turns), host=host)
    for request in ("make an artifact of the repo", "where is it? save it", "update the report", "also a deploy checklist artifact"):
        agent.run_turn(request)
    items = ArtifactStore().list()
    assert len(items) == 2 and not any(a["title"] == "Untitled" for a in items)
    report = next(a for a in items if "repo activity report" in a["title"].lower())
    assert report["versions"] == 3 and ArtifactStore().get(report["id"])["content"].endswith("<p>v3</p>")
