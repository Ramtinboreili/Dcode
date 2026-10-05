from __future__ import annotations

import json
import time

import pytest
from conftest import FakeLLM, call, make_config

from dcode.llm import AssistantTurn
from dcode.session import Session


def _wait(session, timeout=5.0):
    time.sleep(0.05)
    deadline = time.time() + timeout
    while not session.idle and time.time() < deadline:
        time.sleep(0.02)


def test_init_writes_agents_md_and_it_is_used(isolated):
    (isolated / "CLAUDE.md").write_text("old notes")
    prompts = []

    def router(messages, tools):
        if messages[-1]["role"] == "tool":
            return AssistantTurn(content="Wrote AGENTS.md.")
        prompts.append(messages[-1]["content"])
        content = "# Agents\n- test: pytest -q\n"
        return AssistantTurn(tool_calls=[call("write_file", json.dumps({"path": "AGENTS.md", "content": content}))])

    config = make_config()
    session = Session(config, llm=FakeLLM(router, config))
    try:
        assert session.init_project() == "AGENTS.md"
        _wait(session)
        assert "Create AGENTS.md" in prompts[0] and "CLAUDE.md exists" in prompts[0]
        assert (isolated / "AGENTS.md").read_text().startswith("# Agents")
        assert any(e["type"] == "user_message" and e["text"] == "/init" for e in session.bus.since(0))
        session.agent.refresh_system_message()
        assert "pytest -q" in session.agent.messages[0]["content"]
        assert session.init_project() == "AGENTS.md"  # now it updates
        _wait(session)
        assert prompts[-1].startswith("Update AGENTS.md") and "keep what is still correct" in prompts[-1]
    finally:
        session.close()


def test_init_prefers_dcode_md_and_refuses_in_plan_mode(isolated):
    (isolated / "DCODE.md").write_text("x")
    config = make_config()
    session = Session(config, llm=FakeLLM(lambda m, t: AssistantTurn(content="ok"), config))
    try:
        session.set_mode("plan")
        with pytest.raises(RuntimeError, match="Plan mode"):
            session.init_project()
        session.set_mode("accept_edits")
        assert session.init_project() == "DCODE.md"
        _wait(session)
    finally:
        session.close()
