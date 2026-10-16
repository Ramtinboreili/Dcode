from __future__ import annotations

import json
import threading

from conftest import FakeLLM, call, make_config, scripted

from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn
from dcode.memory import MemoryStore


def _agent(router, host=None, memory=None, **cfg):
    config = make_config(**cfg)
    llm = FakeLLM(router, config)
    return Agent(config, llm, host or NullHost(), memory), llm


def test_tool_then_answer(isolated):
    (isolated / "a.txt").write_text("hello")
    agent, llm = _agent(scripted(
        AssistantTurn(tool_calls=[call("read_file", '{"path": "a.txt"}')]),
        AssistantTurn(content="The file says hello."),
    ))
    result = agent.run_turn("what is in a.txt?")
    assert result.text == "The file says hello."
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "1| hello"
    types = [e["type"] for e in agent.host.events]
    assert types.index("tool_call") < types.index("tool_result") < types.index("assistant_message")
    assert "usage" in types


def test_run_command_requires_approval():
    host = NullHost(approve=False)
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "touch x"}')]),
        AssistantTurn(content="ok"),
    ), host=host)
    agent.run_turn("make a file")
    assert any(m["role"] == "tool" and "cancelled" in m["content"] for m in agent.messages)


def test_auto_approve_flag_skips_prompt(isolated):
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "touch made.txt"}')]),
        AssistantTurn(content="ok"),
    ), experimental={"auto_approve_commands": True, "auto_commit": False, "multi_file_edit": False, "streaming_tools": False})
    agent.run_turn("make a file")
    assert (isolated / "made.txt").exists()


def test_parallel_read_only_delegation():
    barrier = threading.Barrier(2, timeout=5)  # both explorers must be in flight at once

    def router(messages, tools):
        system = messages[0]["content"]
        if "exploration sub-agent" in system:
            barrier.wait()
            return AssistantTurn(content=f"REPORT for: {messages[1]['content']}")
        if not any(m["role"] == "tool" for m in messages):
            return AssistantTurn(tool_calls=[
                call("delegate", json.dumps({"role": "explore", "task": "find auth code"}), "d1"),
                call("delegate", json.dumps({"role": "explore", "task": "find db code"}), "d2"),
            ])
        return AssistantTurn(content="Both explored.")

    agent, _ = _agent(router)
    result = agent.run_turn("map the codebase")
    assert result.text == "Both explored."
    reports = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "find auth code" in reports[0] and "find db code" in reports[1]  # order preserved
    starts = [e for e in agent.host.events if e["type"] == "agent_start"]
    assert {e["role"] for e in starts} == {"explore"} and len(starts) == 2
    # Sub-agent transcripts never enter the main context — only the report does.
    assert not any("exploration sub-agent" in str(m.get("content")) for m in agent.messages)


def test_sub_agents_get_role_tools_only():
    agent, _ = _agent(scripted())
    sub = Agent(agent.config, agent.llm, agent.host, None, role="explore", agent_id="explore-x")
    names = sub.tool_names()
    assert "read_file" in names and "write_file" not in names and "delegate" not in names
    assert "delegate" in agent.tool_names()


def test_delegation_disabled_hides_tool():
    agent, _ = _agent(scripted(), agents={"enabled": False, "max_parallel": 2, "max_steps": 16})
    assert "delegate" not in agent.tool_names()
    assert "delegate" not in agent.system_prompt()


def test_a_model_stuck_on_one_failing_edit_is_stopped(isolated):
    """Reported from real use (gemma4 editing a YAML file): two "old_text not found", then the same
    call again and again under "You called edit_file with identical arguments 3 times" until Ctrl+C."""
    (isolated / "deploy.yaml").write_text("spec:\n  replicas: 1\n")
    asked: list[bool] = []

    def router(messages, tools):
        asked.append(bool(tools))
        if not tools:
            return AssistantTurn(content="I could not find that text in deploy.yaml.")
        return AssistantTurn(tool_calls=[call("edit_file", json.dumps(
            {"path": "deploy.yaml", "old_text": "replicas: 7", "new_text": "replicas: 2"}), f"c{len(asked)}")])

    host = NullHost()
    agent, _ = _agent(router, host=host)
    result = agent.run_turn("set replicas to 2")
    assert asked == [True, True, True, True, False]  # the 4th identical call is the last; then no tools
    assert result.text == "I could not find that text in deploy.yaml."
    assert "Stop using tools" in agent.messages[-2]["content"]
    assert [e["text"] for e in host.events if e["type"] == "notice" and e.get("level") == "warning"] == \
        ["Stopped: the model kept repeating the same step"]
    assert (isolated / "deploy.yaml").read_text() == "spec:\n  replicas: 1\n"
    # Every tool call still has its answer, so the history stays valid for the next message.
    calls = [c["id"] for m in agent.messages if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    assert calls == [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
    agent.run_turn("try again")  # a new task starts with a clean count
    assert asked[5] is True


def test_repeat_guard_breaks_loops(isolated):
    (isolated / "a.txt").write_text("x")
    same = AssistantTurn(tool_calls=[call("read_file", '{"path": "a.txt"}')])
    agent, _ = _agent(scripted(same, same, same, AssistantTurn(content="stop")))
    agent.run_turn("loop")
    tool_msgs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "identical arguments" in tool_msgs[-1]


def test_step_limit_forces_wrap_up(isolated):
    def router(messages, tools):
        if tools is None:
            return AssistantTurn(content="Progress report.")
        return AssistantTurn(tool_calls=[call("list_files", json.dumps({"path": ".", "max_depth": len(messages)}))])

    agent, _ = _agent(router)
    result = agent.run_turn("go", max_steps=3)
    assert result.text == "Progress report."
    assert result.steps == 3


def test_empty_reply_gets_one_nudge():
    agent, llm = _agent(scripted(AssistantTurn(content=""), AssistantTurn(content="final")))
    assert agent.run_turn("hi").text == "final"
    assert len(llm.calls) == 2


def test_memory_recall_lands_in_system_prompt(isolated):
    store = MemoryStore(isolated)
    store.save("deploy", "Deploy with ./scripts/ship.sh on the bastion host.")
    seen = {}

    def router(messages, tools):
        seen["system"] = messages[0]["content"]
        return AssistantTurn(content="ok")

    agent, _ = _agent(router, memory=store)
    agent.run_turn("how do I deploy?")
    assert "ship.sh" in seen["system"]
    assert "memory_save" in agent.tool_names()


def test_memory_save_tool(isolated):
    store = MemoryStore(isolated)
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[call("memory_save", '{"name": "style", "content": "Use black"}')]),
        AssistantTurn(content="saved"),
    ), memory=store)
    agent.run_turn("remember we use black")
    assert store.get("style").body == "Use black"


def test_project_instructions_file_is_loaded(isolated):
    (isolated / "AGENTS.md").write_text("Always run pytest -q.")
    agent, _ = _agent(scripted())
    assert "Always run pytest -q." in agent.system_prompt()


def test_reused_tool_call_ids_are_made_unique(isolated):
    (isolated / "a.txt").write_text("x")
    (isolated / "b.txt").write_text("y")
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[call("read_file", '{"path": "a.txt"}', "call_0")]),
        AssistantTurn(tool_calls=[call("read_file", '{"path": "b.txt"}', "call_0")]),
        AssistantTurn(content="done"),
    ))
    agent.run_turn("read both")
    ids = [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
    assert len(ids) == len(set(ids)) == 2


def test_silent_finish_gets_a_model_written_report(isolated):
    (isolated / "a.txt").write_text("x")
    agent, llm = _agent(scripted(
        AssistantTurn(tool_calls=[call("edit_file", '{"path": "a.txt", "old_text": "x", "new_text": "y"}')]),
        AssistantTurn(content=""),            # silent stop
        AssistantTurn(content=""),            # still silent after the nudge
        AssistantTurn(content="Changed a.txt from x to y."),  # answers the report request
    ))
    result = agent.run_turn("change x to y")
    assert result.text == "Changed a.txt from x to y."
    usage = [e for e in agent.host.events if e["type"] == "usage"][-1]
    assert usage["files_changed"] == ["a.txt"]


def test_fallback_report_when_model_stays_silent(isolated):
    (isolated / "a.txt").write_text("x")
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[call("edit_file", '{"path": "a.txt", "old_text": "x", "new_text": "y"}')]),
        AssistantTurn(content=""), AssistantTurn(content=""), AssistantTurn(content=""),
    ))
    result = agent.run_turn("change x to y")
    assert "Report" in result.text and "`a.txt`" in result.text
    # History stays valid: the unanswered report request was removed, the report recorded.
    assert agent.messages[-1] == {"role": "assistant", "content": result.text}


def test_activity_counts_commands_and_failures(isolated):
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "true"}', "c1")]),
        AssistantTurn(tool_calls=[call("run_command", '{"command": "false"}', "c2")]),
        AssistantTurn(content="done"),
    ), experimental={"auto_approve_commands": True, "auto_commit": False, "multi_file_edit": False, "streaming_tools": False})
    agent.run_turn("run things")
    usage = [e for e in agent.host.events if e["type"] == "usage"][-1]
    assert usage["commands"] == 2 and usage["commands_failed"] == 1


def test_a_command_failing_the_same_way_again_and_again_is_stopped(isolated):
    """Re-running a command is normal; the same failure three times in a row means the model is stuck."""
    ran: list[str] = []

    def router(messages, tools):
        if not tools:
            return AssistantTurn(content="The build keeps failing on a missing header.")
        ran.append("x")
        return AssistantTurn(tool_calls=[call("run_command", '{"command": "echo missing.h; exit 2"}', f"r{len(ran)}")])

    host = NullHost(approve=True)
    agent, _ = _agent(router, host=host)
    result = agent.run_turn("build it")
    outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert outputs[0] == outputs[1] == "missing.h\n(return code: 2)"
    assert "failed the same way" in outputs[2]
    assert result.text == "The build keeps failing on a missing header."


def test_a_command_rerun_after_a_change_is_not_blocked(isolated):
    outputs = iter(["fail", "fail", "ok"])

    def router(messages, tools):
        if messages[-1]["role"] == "tool" and messages[-1]["content"].strip() == "ok":
            return AssistantTurn(content="Tests pass.")
        return AssistantTurn(tool_calls=[call("run_command", json.dumps({"command": f"echo {next(outputs)}"}))])

    agent, _ = _agent(router, host=NullHost(approve=True))
    assert agent.run_turn("run the tests").text == "Tests pass."


def test_a_reply_that_was_all_thinking_gets_told_to_act(isolated):
    agent, llm = _agent(scripted(
        AssistantTurn(content="", finish_reason="length", reasoning="hmm " * 50),
        AssistantTurn(content="Done: nothing to change."),
    ))
    assert agent.run_turn("check the config").text == "Done: nothing to change."
    assert "whole reply on thinking" in llm.calls[1][-1]["content"]


def test_the_clients_notes_reach_the_screen_and_calls_arrive_cleaned(isolated):
    (isolated / "a.txt").write_text("hello")
    host = NullHost()
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[call("read_file", '{"file_path": "a.txt"}')],
                      notes=["The model thought in circles; asking again with no thinking"]),
        AssistantTurn(content="It says hello."),
    ), host=host)
    agent.run_turn("read a.txt")
    assert "The model thought in circles; asking again with no thinking" in \
        [e["text"] for e in host.events if e["type"] == "notice"]
    assert [e["args"] for e in host.events if e["type"] == "tool_call"] == [{"path": "a.txt"}]
    assert agent.messages[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'


def test_write_file_will_not_replace_an_unread_file_at_first(isolated):
    """Gemma 4 on OpenRouter rewrote data.txt and calc.py from memory when only asked to commit them."""
    (isolated / "data.txt").write_text("3\n42\n7\n")
    (isolated / "seen.txt").write_text("old\n")
    blind = call("write_file", json.dumps({"path": "data.txt", "content": "1\n2\n"}))
    agent, _ = _agent(scripted(
        AssistantTurn(tool_calls=[blind]),
        AssistantTurn(tool_calls=[call("read_file", '{"path": "seen.txt"}')]),
        AssistantTurn(tool_calls=[call("write_file", json.dumps({"path": "seen.txt", "content": "new\n"})),
                                  call("write_file", json.dumps({"path": "fresh.txt", "content": "x\n"}))]),
        AssistantTurn(tool_calls=[blind]),  # asked again on purpose: it goes through
        AssistantTurn(content="done")))
    agent.run_turn("tidy up")
    outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert outputs[0].startswith("Not written: data.txt already exists and you haven't read it")
    assert (isolated / "seen.txt").read_text() == "new\n" and (isolated / "fresh.txt").exists()
    assert (isolated / "data.txt").read_text() == "1\n2\n"


def test_a_new_turn_stops_resending_the_last_turns_big_outputs(isolated):
    """Reported: "which containers run here?" then "save this on memory" went past a 32K/day
    limit: the 10K-char kubectl/docker outputs of the answered turn were re-sent on every call."""
    (isolated / "big.txt").write_text("pod line\n" * 600)
    (isolated / "small.txt").write_text("tiny")
    agent, llm = _agent(scripted(
        AssistantTurn(tool_calls=[call("read_file", '{"path": "big.txt"}', "c1"),
                                  call("read_file", '{"path": "small.txt"}', "c2")]),
        AssistantTurn(content="600 pods."),
        AssistantTurn(tool_calls=[call("read_file", '{"path": "big.txt"}', "c3")]),
        AssistantTurn(content="Still 600."),
    ))
    agent.run_turn("list the pods")
    within = [m["content"] for m in llm.calls[1] if m["role"] == "tool"]
    assert len(within[0]) > 4000  # the turn that ran the tool still sees all of it
    agent.run_turn("save this")
    earlier = [m["content"] for m in llm.calls[2] if m["role"] == "tool"]
    assert earlier[0].startswith("[elided") and "re-run the tool" in earlier[0] and len(earlier[0]) < 500
    assert earlier[1] == "1| tiny"  # small outputs stay
    assert len([m for m in llm.calls[3] if m["role"] == "tool"][-1]["content"]) > 4000  # a fresh read is whole


def test_retrying_a_failed_request_does_not_repeat_the_message(isolated):
    """Reported: "save this on memory" was in the history twice after a 429 and a retry."""
    def router(messages, tools):
        if sum(m["role"] == "user" for m in messages) == 1 and not router.failed:
            router.failed = True
            raise RuntimeError("429 token_limit")
        return AssistantTurn(content="saved")
    router.failed = False
    agent, llm = _agent(router)
    try:
        agent.run_turn("save this on memory")
    except RuntimeError:
        pass
    agent.run_turn("save this on memory")
    assert [m["content"] for m in agent.messages if m["role"] == "user"] == ["save this on memory"]
