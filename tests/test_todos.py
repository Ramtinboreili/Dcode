from __future__ import annotations

import json

from conftest import FakeLLM, call, make_config, scripted

from dcode.agents import normalize_todos
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn


def test_normalize_accepts_sloppy_input():
    items = normalize_todos([{"content": "read code", "status": "completed"}, "write fix",
                             {"text": "test", "status": "In Progress"}, {"text": " ", "status": "done"}, 7])
    assert items == [{"text": "read code", "status": "done"}, {"text": "write fix", "status": "pending"},
                     {"text": "test", "status": "in_progress"}]
    assert normalize_todos("- a\n- b")[1] == {"text": "b", "status": "pending"}


def test_checklist_is_kept_and_survives_in_the_system_prompt(isolated):
    items = [{"text": "Find the bug", "status": "done"}, {"text": "Fix it", "status": "in_progress"}]
    config = make_config()
    host = NullHost()
    agent = Agent(config, FakeLLM(scripted(
        AssistantTurn(tool_calls=[call("todo_write", json.dumps({"items": items}))]),
        AssistantTurn(content="working"),
    ), config), host)
    agent.run_turn("fix the bug")
    result = next(m["content"] for m in agent.messages if m["role"] == "tool")
    assert result.startswith("Checklist saved (1/2 done)") and "[>] Fix it" in result
    assert next(e for e in host.events if e["type"] == "todos")["items"] == items
    agent.refresh_system_message()  # what happens at the next turn or after compaction
    assert "[x] Find the bug" in agent.messages[0]["content"]


def test_sub_agents_do_not_get_the_tool():
    config = make_config()
    sub = Agent(config, FakeLLM(scripted(), config), NullHost(), role="explore", agent_id="explore-1")
    assert "todo_write" not in sub.tool_names()
    assert "todo_write" in Agent(config, FakeLLM(scripted(), config), NullHost()).tool_names()
