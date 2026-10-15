"""Tier 3 conveniences: your own shell commands, the model's reasoning, the editor, notifications."""

from __future__ import annotations

import stat
import time

from conftest import FakeLLM, make_config, scripted

from dcode import ui
from dcode.config import DEFAULT_UI
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn, LLMClient, thinking_text
from dcode.session import SHELL_NOTE, Session, transcript_from_messages


def _wait(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _session(router=None):
    config = make_config()
    return Session(config, llm=FakeLLM(router or scripted(), config))


# --- !command ---------------------------------------------------------------------------

def test_own_shell_command_runs_without_asking_and_reaches_the_model(isolated):
    seen = {}

    def router(messages, tools):
        seen["messages"] = [dict(m) for m in messages]
        return AssistantTurn(content="I see it.")

    session = _session(router)
    try:
        session.set_mode("manual")  # even here: it is the user's own command, nothing to approve
        session.run_shell("echo from-me && touch made.txt")
        assert _wait(lambda: session.idle and len(session.agent.messages) > 1)
        assert (isolated / "made.txt").exists()
        assert not session.pending_approvals()
        roles = [m["role"] for m in session.agent.messages[1:]]
        assert roles == ["user", "assistant", "tool", "assistant"]  # a valid history
        assert session.agent.messages[1]["content"] == f"{SHELL_NOTE}echo from-me && touch made.txt]"
        assert "from-me" in session.agent.messages[3]["content"]
        events = session.bus.since(0)
        call = next(e for e in events if e["type"] == "tool_call")
        result = next(e for e in events if e["type"] == "tool_result")
        assert call["by"] == result["by"] == "user" and "from-me" in result["output"]
        assert next(e for e in events if e["type"] == "user_message")["text"] == "!echo from-me && touch made.txt"
        assert not any(e["type"] == "usage" for e in events)  # no model call was spent

        session.submit("what did I run?")
        assert _wait(lambda: session.idle and "messages" in seen)
        assert any("from-me" in str(m.get("content")) for m in seen["messages"])
    finally:
        session.close()


def test_own_shell_command_reports_failure_and_can_be_stopped():
    session = _session()
    try:
        session.run_shell("exit 3")
        assert _wait(lambda: session.idle and len(session.agent.messages) > 1)
        assert "(return code: 3)" in session.agent.messages[3]["content"]

        started = time.time()
        session.run_shell("sleep 30")
        assert _wait(lambda: session.busy)
        session.interrupt()
        assert _wait(lambda: session.idle)
        assert time.time() - started < 10
        assert "(stopped by the user)" in session.agent.messages[-2]["content"]
    finally:
        session.close()


def test_saved_chat_shows_the_command_as_typed():
    items = transcript_from_messages([
        {"role": "user", "content": f"{SHELL_NOTE}git status]"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "s1", "type": "function", "function": {"name": "run_command", "arguments": '{"command": "git status"}'}}]},
        {"role": "tool", "tool_call_id": "s1", "name": "run_command", "content": "clean"},
        {"role": "assistant", "content": "Noted."},
    ])
    assert items[0] == {"type": "user_message", "text": "!git status", "source": "history"}
    assert [i["type"] for i in items] == ["user_message", "tool_call", "tool_result", "assistant_message"]


# --- reasoning -----------------------------------------------------------------------------

def test_reasoning_is_kept_for_display_and_out_of_the_context():
    assert thinking_text("<think>first\nplan</think>Answer<think>more</think>") == "first\nplan\n\nmore"
    assert thinking_text("<think>never closed") == "never closed"
    assert thinking_text("plain") == ""
    turn = LLMClient._finish(AssistantTurn(content="<think>step one</think>Done.", reasoning="from the server"), None)
    assert turn.content == "Done." and turn.reasoning == "from the server\n\nstep one"
    assert "reasoning" not in turn.as_message()


def test_engine_emits_thinking_but_never_stores_it():
    config = make_config()
    host = NullHost()
    agent = Agent(config, FakeLLM(scripted(AssistantTurn(content="42", reasoning="6 times 7")), config), host)
    agent.run_turn("what is 6*7?")
    thoughts = [e for e in host.events if e["type"] == "thinking"]
    assert thoughts and thoughts[0]["text"] == "6 times 7" and thoughts[0]["agent"] == "main"
    assert not any("6 times 7" in str(m.get("content")) for m in agent.messages)


# --- editor, notifications, defaults -------------------------------------------------------

def test_editor_returns_what_was_written(tmp_path, monkeypatch):
    script = tmp_path / "fake-editor"
    script.write_text('#!/bin/sh\nprintf "a long message\\nwith two lines\\n" > "$1"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    assert ui.edit_in_editor() == "a long message\nwith two lines"

    quiet = tmp_path / "does-nothing"
    quiet.write_text("#!/bin/sh\nexit 0\n")
    quiet.chmod(quiet.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("EDITOR", str(quiet))
    assert ui.edit_in_editor() == ""


def test_ring_writes_only_to_a_real_terminal(monkeypatch):
    import sys

    class Stream:
        def __init__(self, tty):
            self.tty, self.data = tty, ""

        def isatty(self):
            return self.tty

        def write(self, text):
            self.data += text

        def flush(self):
            pass

    for kind, tty, expected in (("off", True, ""), ("bell", False, ""), ("bell", True, "\a")):
        stream = Stream(tty)
        monkeypatch.setattr(sys, "__stdout__", stream)
        ui.ring(kind, "done")
        assert stream.data == expected
    stream = Stream(True)
    monkeypatch.setattr(sys, "__stdout__", stream)
    ui.ring("desktop", "Dcode finished: fix; the\x1b tests")
    assert stream.data.startswith("\x1b]9;Dcode finished: fix the tests\x07") and stream.data.endswith("\a")


def test_terminal_preferences_have_safe_defaults():
    assert DEFAULT_UI["show_thinking"] is False and DEFAULT_UI["show_details"] is True and DEFAULT_UI["notify"] == "off"
    assert make_config().ui["notify"] == "off"


# --- chats: /sessions, --continue, --resume -------------------------------------------------

def test_continue_and_resume_pick_the_right_saved_chat(isolated, tmp_path):
    from dcode.config import save_thread
    from dcode.main import _resume_chat

    def chat(name, cwd, text):
        save_thread(name, [{"role": "system", "content": "s"}, {"role": "user", "content": text},
                           {"role": "assistant", "content": f"answer to {text}"}], extra={"cwd": cwd})

    chat("Elsewhere chat", str(tmp_path / "other"), "other question")
    chat("Fix the login bug", str(isolated), "why does login fail?")
    time.sleep(0.01)
    chat("Elsewhere newer", str(tmp_path / "other"), "newest question")

    session = _session()
    try:
        assert _resume_chat(session, False, None) is False  # neither flag: a new chat
        assert _resume_chat(session, True, None) is True     # --continue: this folder's last chat, not the newest
        assert session.thread_name == "Fix the login bug"
        assert _resume_chat(session, False, "elsewhere chat") is True  # --resume by name, any folder
        assert session.thread_name == "Elsewhere chat"
        assert _resume_chat(session, False, "no such chat") is False
        assert session.thread_name == "Elsewhere chat"
    finally:
        session.close()


def test_chat_labels():
    from datetime import datetime, timedelta, timezone

    assert ui.pretty_thread("20261008-141500") == "Chat of 2026-10-08 14:15"
    assert ui.pretty_thread("Fix the login bug") == "Fix the login bug"
    now = datetime.now(timezone.utc)
    assert ui.ago(now.isoformat()) == "just now"
    assert ui.ago((now - timedelta(minutes=5)).isoformat()) == "5m ago"
    assert ui.ago((now - timedelta(days=3)).isoformat()) == "3d ago"
    assert ui.ago("not a date") == ""


def test_help_keeps_the_square_brackets():
    """Regression: "/compact [focus]" lost its "[focus]" (rich read it as a style tag)."""
    with ui.console.capture() as capture:
        ui.show_help()
    shown = capture.get()
    assert "/compact [focus]" in shown and "[md|html|json]" in shown and "/stats [days]" in shown


def test_esc_leaves_a_one_line_prompt(monkeypatch):
    """Found by the pty check: Esc did nothing in /setup, so the next message became the server URL."""
    import pytest
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from dcode import ui

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        monkeypatch.setattr(ui, "_PROMPT_SESSION", None)
        pipe.send_text("\x1b")
        with pytest.raises(ui.PromptCancelled):
            ui.prompt_input("Base URL")
        pipe.send_text("typed\r")
        assert ui.prompt_input("Name") == "typed"
