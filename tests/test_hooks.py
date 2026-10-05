from __future__ import annotations

import json
import time

from conftest import FakeLLM, call, make_config, scripted

from dcode import hooks
from dcode.config import config_from_dict
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn
from dcode.session import Session


def test_clean_drops_bad_hooks():
    raw = [{"on": "before_tool", "command": "x", "match": "run_*", "timeout": "5"}, {"on": "nope", "command": "x"},
           {"on": "task_done"}, "junk", {"on": "after_tool", "command": "y", "timeout": "z"}]
    assert hooks.clean(raw) == [{"on": "before_tool", "command": "x", "match": "run_*", "timeout": 5.0},
                                {"on": "after_tool", "command": "y"}]
    cfg = config_from_dict({"base_url": "u", "api_key": "k", "model": "m", "hooks": raw})
    assert len(cfg.hooks) == 2


def test_before_tool_hook_blocks_and_after_tool_sees_output(isolated):
    log = isolated / "audit.log"
    config = make_config(hooks=[
        {"on": "before_tool", "match": "run_command", "command": 'case "$DCODE_COMMAND" in *curl*) echo "no network"; exit 1;; esac'},
        {"on": "after_tool", "match": "*", "command": f'echo "$DCODE_TOOL:$DCODE_OUTPUT" >> {log}'},
    ], experimental={"auto_approve_commands": True})
    agent = Agent(config, FakeLLM(scripted(
        AssistantTurn(tool_calls=[call("run_command", json.dumps({"command": "curl http://x"})),
                                  call("run_command", json.dumps({"command": "echo fine"}), "c2")]),
        AssistantTurn(content="ok"),
    ), config), NullHost())
    agent.run_turn("go")
    outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert outputs[0] == "Blocked by the user's hook: no network"
    assert outputs[1] == "fine"
    assert "run_command:fine" in log.read_text()


def test_stdin_gets_json(isolated):
    out = isolated / "payload.json"
    results = hooks.run([{"on": "task_done", "command": f"cat > {out}"}], "task_done", isolated, text="all done")
    assert results[0][1] == 0
    assert json.loads(out.read_text())["text"] == "all done"


def test_task_done_and_approval_hooks_fire_from_the_session(isolated):
    marks = isolated / "marks.txt"
    config = make_config(hooks=[{"on": "task_done", "command": f'echo "done:$DCODE_TEXT" >> {marks}'},
                                {"on": "approval_needed", "command": f'echo "ask:$DCODE_TOOL" >> {marks}'}])
    llm = FakeLLM(scripted(AssistantTurn(tool_calls=[call("run_command", '{"command": "echo hi"}')]),
                           AssistantTurn(content="all good")), config)
    session = Session(config, llm=llm)
    try:
        session.submit("run it")
        deadline = time.time() + 5
        while not session.pending_approvals() and time.time() < deadline:
            time.sleep(0.02)
        session.resolve_approval(session.pending_approvals()[0]["id"], True)
        while (not marks.exists() or "done:" not in marks.read_text()) and time.time() < deadline:
            time.sleep(0.05)
        text = marks.read_text()
        assert "ask:run_command" in text and "done:all good" in text
    finally:
        session.close()
