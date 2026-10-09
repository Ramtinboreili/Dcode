from __future__ import annotations

import json
import time

from conftest import FakeLLM, call, make_config, scripted

from dcode.custom_agents import agents_dir, load_specs
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn, LLMClient
from dcode.session import Session

ANSIBLE = """---
description: Reviews Ansible playbooks
tools: read_file, search_in_files, delegate, run_command
model: small-7b
steps: 5
---
You review Ansible code for idempotency.
"""


def _write(folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text)


def test_specs_load_and_override(isolated):
    _write(agents_dir(), "ansible", ANSIBLE)
    _write(isolated / ".dcode" / "agents", "explore", "---\nmodel: tiny\n---\n")
    _write(isolated / ".dcode" / "agents", "nodesc", "---\ntools: read_file\n---\nbody")
    specs = load_specs(isolated)
    ansible = specs["ansible"]
    assert ansible.tools == ["read_file", "search_in_files", "run_command"]  # main-only tools dropped
    assert (ansible.model, ansible.steps, ansible.scope) == ("small-7b", 5, "user")
    assert not ansible.read_only
    explore = specs["explore"]  # built-in changed: only the model
    assert explore.model == "tiny" and explore.read_only and explore.description.startswith("find and read")
    assert explore.prompt == ""
    assert "nodesc" not in specs  # no description: the main agent couldn't choose it


def test_main_agent_sees_and_uses_custom_helpers(isolated):
    _write(isolated / ".dcode" / "agents", "ansible", ANSIBLE)
    seen = {}

    def router(messages, tools):
        system = messages[0]["content"]
        if "idempotency" in system:
            seen["helper_tools"] = [t["function"]["name"] for t in tools or []]
            seen["helper_system"] = system
            return AssistantTurn(content="REPORT: all good")
        if messages[-1]["role"] == "tool":
            return AssistantTurn(content="Reviewed.")
        seen["roles"] = next(t for t in tools if t["function"]["name"] == "delegate")["function"]["parameters"]["properties"]["role"]["enum"]
        seen["main_system"] = system
        return AssistantTurn(tool_calls=[call("delegate", json.dumps({"role": "ansible", "task": "review site.yml"}))])

    config = make_config()
    host = NullHost()
    agent = Agent(config, FakeLLM(router, config), host)
    agent.run_turn("review my playbook")
    assert "ansible" in seen["roles"] and "- ansible: Reviews Ansible playbooks" in seen["main_system"]
    assert seen["helper_tools"][:3] == ["read_file", "search_in_files", "run_command"]
    assert "Workspace:" in seen["helper_system"]
    start = next(e for e in host.events if e["type"] == "agent_start")
    assert start["role"] == "ansible" and start["model"] == "small-7b"


def test_helper_model_setting_and_with_model():
    config = make_config(agents={"enabled": True, "max_parallel": 2, "max_steps": 16, "helper_model": "fast-3b"})
    client = LLMClient(config)
    clone = client.with_model("fast-3b")
    assert clone.model == "fast-3b" and client.model == "qwen2.5-coder" and clone.client is client.client
    agent = Agent(config, client, NullHost())
    assert agent._model_for("fast-3b")[1].model == "fast-3b"
    assert agent._model_for("") == (config, client)
    assert agent._model_for("fast-3b")[0].model == "fast-3b" and config.model == "qwen2.5-coder"


def test_plan_mode_blocks_custom_helpers_that_can_change_things(isolated):
    _write(isolated / ".dcode" / "agents", "ansible", ANSIBLE)
    config = make_config()
    host = NullHost()
    host.mode = "plan"
    agent = Agent(config, FakeLLM(scripted(), config), host)
    assert "only read-only helpers" in agent._delegate("ansible", "fix it")


def test_at_name_runs_a_helper_directly(isolated):
    _write(isolated / ".dcode" / "agents", "ansible", ANSIBLE)

    def router(messages, tools):
        return AssistantTurn(content="REPORT: 2 tasks are not idempotent")

    config = make_config()
    session = Session(config, llm=FakeLLM(router, config))
    try:
        session.submit("@ansible check site.yml")
        deadline = time.time() + 5
        while (session.busy or not session.idle) and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        events = session.bus.since(0)
        assert any(e["type"] == "agent_start" and e["role"] == "ansible" for e in events)
        answer = next(e for e in events if e["type"] == "assistant_message")
        assert "not idempotent" in answer["text"]
        assert session.agent.messages[-2]["content"] == "@ansible check site.yml"
    finally:
        session.close()
