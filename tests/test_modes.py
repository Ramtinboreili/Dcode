from __future__ import annotations

import json

from conftest import FakeLLM, call, make_config, scripted
from test_session import _session, _wait

from dcode.checkpoints import CheckpointStore
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn


class ModeHost(NullHost):
    def __init__(self, mode: str, approve: bool = False) -> None:
        super().__init__(approve=approve)
        self.mode = mode
        self.checkpoints = CheckpointStore()
        self.asked: list[str] = []

    def request_approval(self, kind, detail, agent):
        self.asked.append(kind)
        return super().request_approval(kind, detail, agent)


def _agent(router, mode, approve=False):
    host = ModeHost(mode, approve)
    config = make_config()
    return Agent(config, FakeLLM(router, config), host), host


def test_plan_mode_is_read_only():
    agent, _ = _agent(scripted(), "plan")
    names = agent.tool_names()
    assert "read_file" in names and "delegate" in names
    assert not {"write_file", "edit_file", "run_command", "delete_file", "git_commit"} & set(names)
    assert "PLAN MODE" in agent.system_prompt()
    assert "only read-only helpers" in agent._delegate("code", "change things")


def test_manual_mode_asks_before_edits(isolated):
    (isolated / "a.txt").write_text("x")
    agent, host = _agent(scripted(
        AssistantTurn(tool_calls=[call("edit_file", '{"path": "a.txt", "old_text": "x", "new_text": "y"}')]),
        AssistantTurn(content="done"),
    ), "manual", approve=False)
    agent.run_turn("edit it")
    assert host.asked == ["edit_file"] and (isolated / "a.txt").read_text() == "x"


def test_accept_edits_mode_edits_freely_but_asks_for_commands(isolated):
    (isolated / "a.txt").write_text("x")
    agent, host = _agent(scripted(
        AssistantTurn(tool_calls=[call("edit_file", '{"path": "a.txt", "old_text": "x", "new_text": "y"}')]),
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo hi"}', "c2")]),
        AssistantTurn(content="done"),
    ), "accept_edits")
    agent.run_turn("edit and run")
    assert (isolated / "a.txt").read_text() == "y" and host.asked == ["run_command"]


def test_auto_mode_never_asks(isolated):
    agent, host = _agent(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo hi"}')]),
        AssistantTurn(content="done"),
    ), "auto")
    agent.run_turn("run")
    assert host.asked == []
    assert "hi" in [m for m in agent.messages if m["role"] == "tool"][0]["content"]


def test_checkpoints_undo_restores_edits_and_new_files(isolated):
    (isolated / "a.txt").write_text("original")
    agent, host = _agent(scripted(
        AssistantTurn(tool_calls=[
            call("edit_file", '{"path": "a.txt", "old_text": "original", "new_text": "changed"}', "c1"),
            call("write_file", json.dumps({"path": "new.txt", "content": "fresh"}), "c2"),
        ]),
        AssistantTurn(content="done"),
    ), "accept_edits")
    agent.run_turn("change files")
    assert (isolated / "a.txt").read_text() == "changed" and (isolated / "new.txt").exists()
    label, restored = host.checkpoints.undo()
    assert label == "change files" and len(restored) == 2
    assert (isolated / "a.txt").read_text() == "original" and not (isolated / "new.txt").exists()
    assert host.checkpoints.undo() is None


def test_redo_brings_the_undone_changes_back(isolated):
    (isolated / "a.txt").write_text("original")
    agent, host = _agent(scripted(
        AssistantTurn(tool_calls=[
            call("edit_file", '{"path": "a.txt", "old_text": "original", "new_text": "changed"}', "c1"),
            call("write_file", json.dumps({"path": "new.txt", "content": "fresh"}), "c2"),
        ]),
        AssistantTurn(content="done"),
        AssistantTurn(tool_calls=[call("write_file", json.dumps({"path": "b.txt", "content": "later"}), "c3")]),
        AssistantTurn(content="done"),
    ), "accept_edits")
    store = host.checkpoints
    assert store.redo() is None and not store.can_redo()
    agent.run_turn("change files")
    store.undo()
    assert store.can_redo()
    label, changed = store.redo()
    assert label == "change files" and len(changed) == 2
    assert (isolated / "a.txt").read_text() == "changed" and (isolated / "new.txt").read_text() == "fresh"
    assert not store.can_redo()
    store.undo()  # undo → redo → undo walks back and forth
    assert (isolated / "a.txt").read_text() == "original" and not (isolated / "new.txt").exists()
    assert store.can_redo()
    agent.run_turn("another change")  # a new change: the undone one can't be layered on top
    assert (isolated / "b.txt").exists() and not store.can_redo() and store.redo() is None


def test_force_compact_replaces_history_with_summary():
    agent, host = _agent(scripted(AssistantTurn(content="a1"), AssistantTurn(content="a2")), "accept_edits")
    agent.run_turn("first question")
    agent.run_turn("second question")
    report = agent.force_compact("deploy steps")
    assert report and len(agent.messages) == 3
    assert agent.messages[1]["content"].startswith("[Session summary")
    assert any(e["type"] == "summary" for e in host.events)


def test_session_plan_flow_and_undo():
    session = _session(scripted(AssistantTurn(content="1. do x\n2. do y"), AssistantTurn(content="implemented")))
    try:
        session.set_mode("plan")
        session.submit("plan the change")
        assert _wait(lambda: session.plan_pending and session.idle)
        assert any(e["type"] == "plan_ready" for e in session.bus.since(0))
        session.resolve_plan("accept_edits")
        assert _wait(lambda: session.mode == "accept_edits" and session.idle and not session.plan_pending)
        assert any(e.get("text", "").startswith("The plan is approved") for e in session.bus.since(0)
                   if e["type"] == "user_message")
        assert session.undo() is None  # nothing was changed
        assert session.cycle_mode() == "auto"
    finally:
        session.close()
