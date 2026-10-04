from __future__ import annotations

import time

from conftest import FakeLLM, call, make_config, scripted

from dcode.config import list_threads
from dcode.llm import AssistantTurn
from dcode.session import Session, transcript_from_messages


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _session(router):
    config = make_config()
    session = Session(config, llm=FakeLLM(router, config))
    return session


def test_submit_runs_and_autosaves():
    session = _session(scripted(AssistantTurn(content="hi there")))
    try:
        session.submit("hello", source="web")
        assert _wait(lambda: any(e["type"] == "assistant_message" for e in session.bus.since(0)))
        assert _wait(lambda: session.idle)
        users = [e for e in session.bus.since(0) if e["type"] == "user_message"]
        assert users[0]["source"] == "web"
        assert _wait(lambda: list_threads() and list_threads()[0]["name"] == session.thread_name)
    finally:
        session.close()


def test_approval_from_another_surface():
    session = _session(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo approved-run"}')]),
        AssistantTurn(content="done"),
    ))
    try:
        session.submit("run it")
        assert _wait(lambda: session.pending_approvals())
        approval = session.pending_approvals()[0]
        assert approval["kind"] == "run_command"
        assert session.resolve_approval(approval["id"], True, by="web")
        assert not session.resolve_approval(approval["id"], True, by="cli")  # first answer wins
        assert _wait(lambda: session.idle)
        tool = [m for m in session.agent.messages if m["role"] == "tool"][0]
        assert "approved-run" in tool["content"]
    finally:
        session.close()


def test_interrupt_cancels_and_repairs_history():
    session = _session(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo x"}', "c1")]),
        AssistantTurn(content="never"),
    ))
    try:
        session.submit("run it")
        assert _wait(lambda: session.pending_approvals())
        session.interrupt(source="web")
        assert _wait(lambda: session.idle)
        tool_ids = {m.get("tool_call_id") for m in session.agent.messages if m["role"] == "tool"}
        assert "c1" in tool_ids  # dangling call answered → history stays valid
        assert not session.pending_approvals()
    finally:
        session.close()


def test_new_thread_and_resume():
    session = _session(scripted(AssistantTurn(content="first answer")))
    try:
        session.submit("first")
        assert _wait(lambda: session.idle and len(session.agent.messages) > 2)
        old_name = session.thread_name
        session.thread_name = "named-thread"
        session.save()
        session.new_thread()
        assert len(session.agent.messages) == 1
        assert session.resume("named-thread")
        assert any(m.get("content") == "first answer" for m in session.agent.messages)
        resumed = [e for e in session.bus.since(0) if e["type"] == "thread" and e["action"] == "resume"]
        assert resumed and resumed[-1]["items"]
        assert old_name
    finally:
        session.close()


def test_state_shape():
    session = _session(scripted())
    try:
        state = session.state()
        for key in ("id", "model", "busy", "approvals", "context_used", "context_window", "thread_name", "cwd"):
            assert key in state
    finally:
        session.close()


def test_transcript_from_messages():
    items = transcript_from_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [call("read_file", '{"path":"a"}')]},
        {"role": "tool", "tool_call_id": "call_read_file", "name": "read_file", "content": "1| x"},
        {"role": "assistant", "content": "a"},
    ])
    assert [i["type"] for i in items] == ["user_message", "tool_call", "tool_result", "assistant_message"]


def test_allow_for_session_skips_later_prompts():
    session = _session(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo one"}', "c1")]),
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo two"}', "c2")]),
        AssistantTurn(content="done"),
    ))
    try:
        session.submit("run twice")
        assert _wait(lambda: session.pending_approvals())
        first = session.pending_approvals()[0]
        assert session.resolve_approval(first["id"], True, by="web", scope="session")
        assert _wait(lambda: session.idle)
        outputs = [m["content"] for m in session.agent.messages if m["role"] == "tool"]
        assert "one" in outputs[0] and "two" in outputs[1]
        assert any(e["type"] == "approval_auto" for e in session.bus.since(0))
    finally:
        session.close()


def test_first_message_names_the_chat():
    from dcode.session import clean_title, heuristic_title

    assert heuristic_title("please can you fix the nginx ansible inventory and rerun it") == "Fix the nginx ansible inventory and"
    assert clean_title('Title: "Fix Nginx Inventory".') == "Fix Nginx Inventory"
    assert clean_title("way too long " * 10) == ""

    session = _session(scripted(AssistantTurn(content="hi")))
    session.llm.complete_text = lambda messages, max_tokens=60: "- Deploy Pipeline Overview"
    try:
        session.submit("hey, explain the deploy pipeline")
        assert _wait(lambda: session.thread_name == "Deploy Pipeline Overview")
        titles = [e["name"] for e in session.bus.since(0) if e["type"] == "thread" and e["action"] == "title"]
        assert titles[0] == "Explain the deploy pipeline"  # instant heuristic first, model title after
        assert _wait(lambda: session.idle)
    finally:
        session.close()
