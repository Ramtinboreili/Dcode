from __future__ import annotations

import time

import pytest
from conftest import FakeLLM, make_config

from dcode.commands import commands_dir, expand, fill_arguments, load_commands
from dcode.llm import AssistantTurn
from dcode.session import Session


def _write(folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text)


def _wait(session, timeout=5.0):
    time.sleep(0.05)
    deadline = time.time() + timeout
    while not session.idle and time.time() < deadline:
        time.sleep(0.02)


def test_load_and_project_wins(isolated):
    _write(commands_dir(), "deploy", "---\ndescription: Deploy it\n---\nDeploy $1 to $2")
    _write(isolated / ".dcode" / "commands", "deploy", "Project deploy")
    _write(isolated / ".dcode" / "commands", "empty", "---\ndescription: x\n---\n")
    found = load_commands(isolated)
    assert found["deploy"].scope == "project" and found["deploy"].description == "Project deploy"
    assert "empty" not in found
    assert load_commands(None)["deploy"].description == "Deploy it"


def test_arguments():
    assert fill_arguments("Deploy $1 to $2. All: $ARGUMENTS", 'web "prod eu"') == "Deploy web to prod eu. All: web \"prod eu\""
    assert fill_arguments("Fix it", "the login bug") == "Fix it\n\nthe login bug"  # never dropped
    assert fill_arguments("Only $3", "a b") == "Only "


def test_shell_injection_is_gated(isolated):
    _write(commands_dir(), "st", "Status:\n!`echo hello-from-shell`\n!`echo nope`")
    command = load_commands(None)["st"]
    asked = []

    def may_run(cmd):
        asked.append(cmd)
        return None if "hello" in cmd else "(not run: the user said no)"

    text = expand(command, "", isolated, may_run)
    assert "$ echo hello-from-shell\nhello-from-shell" in text and "(not run: the user said no)" in text
    assert asked == ["echo hello-from-shell", "echo nope"]


@pytest.mark.parametrize("mode,expect_ran", [("auto", True), ("accept_edits", False)])
def test_session_runs_custom_command(isolated, mode, expect_ran):
    _write(isolated / ".dcode" / "commands", "check", "---\ndescription: Check things\n---\nCheck $ARGUMENTS\n!`echo ran-it`")
    prompts = []

    def router(messages, tools):
        prompts.append(messages[-1]["content"])
        return AssistantTurn(content="checked")

    config = make_config()
    session = Session(config, llm=FakeLLM(router, config))
    session.mode = mode
    try:
        assert any(c["name"] == "check" for c in session.state()["commands"])
        session.run_custom_command("/check", "the api")
        deadline = time.time() + 5
        while mode != "auto" and not session.pending_approvals() and time.time() < deadline:
            time.sleep(0.02)
        for approval in session.pending_approvals():
            assert "echo ran-it" in approval["detail"]
            session.resolve_approval(approval["id"], False)
        _wait(session)
        assert prompts and prompts[0].startswith("Check the api")
        assert ("ran-it\nran-it" in prompts[0].replace("$ echo ", "")) is expect_ran
        assert any(e["type"] == "user_message" and e["text"] == "/check the api" for e in session.bus.since(0))
        with pytest.raises(RuntimeError, match="Unknown command"):
            session.run_custom_command("nope")
    finally:
        session.close()
