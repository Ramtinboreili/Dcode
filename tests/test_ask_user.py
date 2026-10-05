from __future__ import annotations

import json
import time

from conftest import FakeLLM, call, make_config, scripted

from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn
from dcode.session import Session

ASK = call("ask_user", json.dumps({"question": "Which database?", "options": ["Postgres", "SQLite", " "]}))


def test_answer_and_skip_reach_the_model():
    for answer, expected in (("SQLite", "The user answered: SQLite"), ("", "The user skipped the question")):
        host = NullHost()
        host.answer = answer
        config = make_config()
        agent = Agent(config, FakeLLM(scripted(AssistantTurn(tool_calls=[ASK]), AssistantTurn(content="ok")), config), host)
        agent.run_turn("set up storage")
        result = next(m["content"] for m in agent.messages if m["role"] == "tool")
        assert result.startswith(expected)
        assert next(e for e in host.events if e["type"] == "question")["options"] == ["Postgres", "SQLite"]


def test_helpers_cannot_ask():
    config = make_config()
    sub = Agent(config, FakeLLM(scripted(), config), NullHost(), role="explore", agent_id="explore-1")
    assert "ask_user" not in sub.tool_names()


def test_session_question_round_trip():
    config = make_config()
    session = Session(config, llm=FakeLLM(scripted(AssistantTurn(tool_calls=[ASK]), AssistantTurn(content="using it")), config))
    try:
        session.mode = "auto"  # questions are asked even in auto mode
        session.submit("set up storage")
        deadline = time.time() + 5
        while not session.pending_approvals() and time.time() < deadline:
            time.sleep(0.02)
        pending = session.pending_approvals()[0]
        assert pending["kind"] == "question" and pending["options"] == ["Postgres", "SQLite"]
        assert session.resolve_approval(pending["id"], True, by="web", answer="Postgres")
        while not session.idle and time.time() < deadline:
            time.sleep(0.02)
        tool = next(m for m in session.agent.messages if m["role"] == "tool")
        assert tool["content"] == "The user answered: Postgres"
        resolved = next(e for e in session.bus.since(0) if e["type"] == "approval_resolved")
        assert resolved["answer"] == "Postgres"
        assert "question" not in session.allowed_kinds
    finally:
        session.close()
