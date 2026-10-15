"""Every slash command, run the way the terminal runs it: TerminalApp._command with a scripted model.

Menus and prompts are answered from a queue (or keep their current value), so each command runs
to the end. A separate pty check (docs/dev/terminal_e2e.py) drives the real terminal.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import pytest
from conftest import FakeLLM, call, make_config

from dcode import __version__, ui
from dcode.cli import TerminalApp
from dcode.config import load_config, save_config
from dcode.llm import AssistantTurn
from dcode.session import Session

UI_URL = "http://127.0.0.1:9/?token=t"


def model_router(messages: list[dict[str, Any]], tools: Any) -> AssistantTurn:
    """A stand-in model: writes notes.txt when asked, otherwise answers with what it was asked."""
    last = messages[-1]
    if last["role"] == "tool":
        return AssistantTurn(content="Done.")
    text = str(last.get("content") or "")
    if "write notes" in text and tools:
        return AssistantTurn(tool_calls=[call("write_file", json.dumps({"path": "notes.txt", "content": "hi\n"}))])
    return AssistantTurn(content=f"Answer to: {text[:80]}")


class Terminal:
    def __init__(self, monkeypatch, work):
        self.work = work
        self.answers: list[Any] = []
        self.opened: list[str] = []
        config = make_config()
        save_config(config)
        self.llm = FakeLLM(model_router, config)
        self.session = Session(config, llm=self.llm)
        self.app = TerminalApp(self.session, ui_url=UI_URL)

        def answer(default: Any = None, options: Any = None) -> Any:
            if not self.answers:
                return default
            given = self.answers.pop(0)
            if isinstance(given, str) and options:  # a menu: the first entry that starts with the answer
                return next((o for o in options if str(o).startswith(given)), given)
            return given

        monkeypatch.setattr(ui, "select_one", lambda title, options, current=None, *a, **k: answer(current, options))
        monkeypatch.setattr(ui, "select_model", lambda models, current: answer(current))
        monkeypatch.setattr(ui, "_selector", lambda title, labels, *a, **k: answer(k.get("cancel_result")))
        monkeypatch.setattr(ui, "checklist_prompt", lambda title, items: answer({label: on for label, on in items}))
        monkeypatch.setattr(ui, "prompt_input", lambda label, default="", **k: answer(default))
        monkeypatch.setattr(ui, "edit_in_editor", lambda text="": answer(""))
        monkeypatch.setattr(ui, "open_in_browser", lambda url: self.opened.append(url) or False)
        monkeypatch.setattr(ui, "ring", lambda *a, **k: None)

    def run(self, command: str, *answers: Any) -> str:
        """Run one command; wait for any task it started; return what the terminal printed."""
        self.answers = list(answers)
        with ui.console.capture() as capture:
            result = self.app._command(command)
            self.wait()
        self.result = result
        return capture.get()

    def wait(self, timeout: float = 10) -> None:
        deadline, quiet = time.time() + timeout, 0
        while time.time() < deadline and quiet < 5:
            quiet = quiet + 1 if self.session.idle else 0
            time.sleep(0.02)

    def asked(self) -> str:
        """The last user message the model saw."""
        return next(m["content"] for m in reversed(self.llm.calls[-1]) if m["role"] == "user")


@pytest.fixture
def term(monkeypatch, isolated):
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_NAME", "t"),
                       ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(key, value)
    (isolated / "app.py").write_text("print('hi')\n")
    subprocess.run("git init -q . && git add -A && git commit -qm init", shell=True, cwd=isolated, check=True)
    terminal = Terminal(monkeypatch, isolated)
    yield terminal
    terminal.session.close()


def test_every_listed_command_is_handled(term, monkeypatch):
    """No command in /help falls through to "Unknown command" (or crashes)."""
    from dcode import main

    monkeypatch.setattr(main, "run_setup", lambda model=None: make_config())
    for command in ui.COMMANDS:
        shown = term.run(command)
        assert "Unknown command" not in shown, command
        assert "Traceback" not in shown, command
    assert term.run("/exit") == "" and term.result == "exit"
    assert "Unknown command /nope" in term.run("/nope")


def test_help_lists_every_command(term):
    shown = term.run("/help")
    for command in ui.COMMANDS:
        assert command in shown


def test_version_thinking_details_notify(term):
    assert __version__ in term.run("/version") and "qwen2.5-coder" in term.run("/version")
    assert "reasoning is shown" in term.run("/thinking")
    assert load_config().ui["show_thinking"] is True
    assert "reasoning is hidden" in term.run("/thinking off")
    assert "Tool calls and results are hidden" in term.run("/details off")
    assert load_config().ui["show_details"] is False
    assert "terminal bell" in term.run("/notify bell")
    assert load_config().ui["notify"] == "bell"
    assert "desktop notification" in term.run("/notify", "desktop")
    assert "Usage: /notify" in term.run("/notify loud")


def test_mode(term):
    assert "Mode: plan — read-only" in term.run("/mode plan")
    assert term.session.mode == "plan"
    assert "Mode: accept edits" in term.run("/mode accept")
    assert "Mode: auto — never ask" in term.run("/mode", "auto")
    assert term.session.mode == "auto"


def test_model_and_models(term):
    term.run("/model", "big-model", "high")
    assert term.session.config.model == "big-model" and term.session.config.reasoning_effort == "high"
    assert load_config().model == "big-model"
    shown = term.run("/models")
    assert "big-model (main)" in shown and "family" in shown


def test_context(term):
    assert "Context: ~" in term.run("/context", "custom", "16384")
    assert load_config().context_window == 16384


def test_memory_and_remember(term):
    assert "No memories yet" in term.run("/memory")
    assert "Saved memory" in term.run("/remember the staging db is read-only")
    assert "the-staging-db-is-read-only" in term.run("/memory")


def test_permissions_experimental_skills_agents(term, isolated):
    from dcode.config import skills_dir

    term.run("/permissions", {"run shell commands": False})
    assert load_config().permissions["run_command"] is False
    term.run("/experimental", {"auto_commit": True})
    assert load_config().experimental["auto_commit"] is True
    (skills_dir() / "k8s.md").write_text("---\nname: k8s\ndescription: kubectl tips\n---\nUse -n.\n")
    term.run("/skills", {"k8s.md": True})  # the built-in skills are listed too, all off
    assert load_config().enabled_skills == ["k8s.md"]
    shown = term.run("/agents")
    assert "@explore" in shown and "/agents settings" in shown
    term.run("/agents settings", {"enabled": True}, "3", "24", "small-model")
    agents = load_config().agents
    assert (agents["max_parallel"], agents["max_steps"], agents["helper_model"]) == (3, 24, "small-model")


def test_hooks_and_rules(term):
    assert "No hooks" in term.run("/hooks")
    config = load_config()
    config.hooks = [{"on": "after_tool", "command": "ruff format {path}", "match": "edit_file"}]
    save_config(config)
    term.session.reload_config()
    assert "ruff format {path}" in term.run("/hooks")
    assert "Rule saved: deny command “rm -rf *”" in term.run('/rules deny "rm -rf *"')
    assert "Rule saved: ask file “*.pem”" in term.run('/rules file ask "*.pem"')
    shown = term.run("/rules")
    assert "rm -rf *" in shown and "*.pem" in shown
    assert "Removed" in term.run('/rules remove "rm -rf *"')
    assert "Usage: /rules" in term.run("/rules maybe x y")


def test_chats_new_rename_clear_reset_sessions_export(term, isolated):
    term.run("/rename deploy notes")
    assert term.session.thread_name == "deploy notes"
    term.session.submit("hello there", source="cli")
    term.wait()
    term.session.save()
    assert "Exported this chat" in term.run("/export md chat.md")
    assert "hello there" in (isolated / "chat.md").read_text()
    assert "Nothing to export" not in term.run("/export json")
    first = term.session.thread_name
    term.run("/new")
    assert term.session.thread_name != first and len(term.session.agent.messages) == 1
    term.run("/clear")
    term.run("/reset")
    assert len(term.session.agent.messages) == 1
    shown = term.run("/sessions", 0)
    assert term.session.thread_name == first and "Continuing" in shown
    assert "No other saved chats match" in term.run("/sessions zzzz")


def test_tasks_from_commands_reach_the_model(term, isolated):
    term.run("/init")
    assert "AGENTS.md" in json.dumps(term.llm.calls[-1])
    (isolated / "app.py").write_text("print('changed')\n")
    term.run("/review")
    assert "changed" in json.dumps(term.llm.calls[-1])  # the reviewer saw the diff
    term.run("/compact keep the deploy steps")
    assert "Nothing written" in term.run("/editor")
    term.run("/editor", "explain app.py")
    assert term.asked() == "explain app.py"
    (isolated / ".dcode" / "commands").mkdir(parents=True)
    (isolated / ".dcode" / "commands" / "hello.md").write_text("Say hello to $ARGUMENTS.")
    term.run("/hello the team")
    assert term.asked() == "Say hello to the team."


def test_undo_redo_diff_commit(term, isolated):
    assert "Nothing to undo" in term.run("/undo")
    term.session.set_mode("accept_edits")
    term.session.submit("write notes", source="cli")
    term.wait()
    assert (isolated / "notes.txt").read_text() == "hi\n"
    term.run("/undo")
    assert not (isolated / "notes.txt").exists()
    term.run("/redo")
    assert (isolated / "notes.txt").exists()
    assert "Nothing to redo" in term.run("/redo")
    (isolated / "app.py").write_text("print('bye')\n")
    assert "bye" in term.run("/diff")
    term.run("/commit Say bye", "Commit")
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=isolated, capture_output=True, text=True).stdout
    assert "Say bye" in log
    assert "No uncommitted changes" in term.run("/diff")
    (isolated / "app.py").write_text("print('again')\n")
    assert "Commit cancelled" in term.run("/commit", "Cancel")
    assert "Commit message:" in term.run("/commit", "Cancel")  # a message was written for it


def test_jobs_and_artifacts(term):
    from dcode.artifacts import ArtifactStore

    assert "No background jobs" in term.run("/jobs")
    job = term.session.jobs.start("sleep 30", str(term.work))
    shown = term.run("/jobs", f"#{job.id}  running", "Stop it")
    assert "Stopped job #1" in shown and "sleep 30" in shown
    term.wait()
    assert not job.running
    assert "No artifacts yet" in term.run("/artifacts")
    meta = ArtifactStore().save("Deploy plan", "# Plan", kind="markdown")
    shown = term.run("/artifacts", f"Deploy plan  ·  doc · v{meta['versions']}")
    assert term.opened and f"/a/{meta['id']}" in term.opened[-1] and "Deploy plan" in shown


def test_guide_ui_stats_setup(term, monkeypatch):
    from dcode import main

    term.run("/guide")
    term.run("/ui")
    assert term.opened == ["http://127.0.0.1:9/guide", UI_URL]
    assert "No tasks recorded yet" in term.run("/stats 7")
    term.session.submit("hello", source="cli")
    term.wait()
    assert "usage" in term.run("/stats 7").lower()
    monkeypatch.setattr(main, "run_setup", lambda model=None: make_config(model="from-setup"))
    term.run("/setup")
    assert term.session.config.model == "from-setup" and load_config().model == "from-setup"


def test_db_cron_mcp(term, isolated):
    assert "No databases" in term.run("/db") or "/db add" in term.run("/db")
    import sqlite3

    sqlite3.connect(isolated / "x.db").execute("CREATE TABLE t (id INTEGER)").connection.commit()
    assert "Saved local and connected" in term.run(f"/db add local sqlite:///{isolated / 'x.db'}")
    assert "local" in term.run("/db")
    shown = term.run('/cron add "0 6 * * 1-5" df -h')
    assert "Scheduled" in shown
    assert "df -h" in term.run("/cron")
    assert "/mcp add" in term.run("/mcp")
