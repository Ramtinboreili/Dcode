from __future__ import annotations

import json
import shutil

import pytest
from conftest import FakeLLM, call, make_config, scripted

from dcode import checks
from dcode.config import DEFAULT_CHECKS
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn


def _settings(**over):
    return dict(DEFAULT_CHECKS) | over


def test_python_syntax_error_is_reported(isolated):
    path = isolated / "bad.py"
    path.write_text("def f(:\n    pass\n")
    note, problems = checks.after_edit(path, _settings(), isolated)
    assert "SyntaxError" in problems and problems.startswith("bad.py:1")
    assert "fix them now" in note
    assert str(isolated) not in note  # relative paths only (tokens)


def test_clean_files_say_nothing(isolated):
    (isolated / "ok.py").write_text("x = 1\n")
    (isolated / "ok.json").write_text('{"a": 1}')
    for name in ("ok.py", "ok.json"):
        assert checks.after_edit(isolated / name, _settings(), isolated) == ("", "")


def test_invalid_json_but_not_jsonc(isolated):
    (isolated / "data.json").write_text('{"a": 1,}')
    (isolated / "tsconfig.json").write_text('{// comment\n"a": 1}')
    assert "invalid JSON" in checks.after_edit(isolated / "data.json", _settings(), isolated)[1]
    assert checks.after_edit(isolated / "tsconfig.json", _settings(), isolated) == ("", "")


def test_disabled_and_custom_commands(isolated):
    path = isolated / "bad.py"
    path.write_text("def f(:\n")
    assert checks.after_edit(path, _settings(enabled=False), isolated) == ("", "")
    custom = _settings(commands={".txt": "grep -q TODO $FILE && echo 'TODO left in $FILE' && exit 1 || exit 0"})
    (isolated / "n.txt").write_text("a TODO here")
    note, problems = checks.after_edit(isolated / "n.txt", custom, isolated)
    assert "TODO left" in problems and "grep" in note


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_external_checker_runs_when_installed(isolated):
    (isolated / "s.sh").write_text("if then fi\n")
    _, problems = checks.after_edit(isolated / "s.sh", _settings(), isolated)
    assert problems


def test_formatter_runs_only_when_enabled(isolated):
    path = isolated / "f.txt"
    path.write_text("messy")
    fmt = {".txt": "printf tidy > $FILE"}
    assert checks.after_edit(path, _settings(formatters=fmt), isolated) == ("", "")
    assert path.read_text() == "messy"
    note, _ = checks.after_edit(path, _settings(format=True, formatters=fmt), isolated)
    assert path.read_text() == "tidy" and "re-read" in note


def test_edit_result_carries_the_problem_back_to_the_model(isolated):
    (isolated / "m.py").write_text("x = 1\n")
    config = make_config()
    llm = FakeLLM(scripted(
        AssistantTurn(tool_calls=[call("edit_file", json.dumps({"path": "m.py", "old_text": "x = 1", "new_text": "x = (1"}))]),
        AssistantTurn(content="done"),
    ), config)
    host = NullHost()
    agent = Agent(config, llm, host)
    agent.run_turn("change x")
    result = next(m["content"] for m in agent.messages if m["role"] == "tool")
    assert result.startswith("Edited m.py") and "SyntaxError" in result
    event = next(e for e in host.events if e["type"] == "check")
    assert event["path"] == "m.py" and not event["ok"]
