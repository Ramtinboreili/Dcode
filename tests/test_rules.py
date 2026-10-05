from __future__ import annotations

import json

from conftest import FakeLLM, call, make_config, scripted

from dcode import mentions, rules
from dcode.config import DEFAULT_RULES, config_from_dict
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn


class RecordingHost(NullHost):
    def __init__(self, approve=False, mode="accept_edits"):
        super().__init__(approve)
        self.asked = []
        self.mode = mode

    def request_approval(self, kind, detail, agent):
        self.asked.append((kind, detail))
        return self._approve


def _run(tool_calls, host, **cfg):
    config = make_config(**cfg)
    agent = Agent(config, FakeLLM(scripted(AssistantTurn(tool_calls=tool_calls), AssistantTurn(content="ok")), config), host)
    agent.run_turn("go")
    return [m["content"] for m in agent.messages if m["role"] == "tool"]


def _cmd(command):
    return call("run_command", json.dumps({"command": command}))


def test_command_patterns_last_match_and_chains():
    rs = rules.load({"commands": {"git *": "allow", "git push*": "ask", "rm -rf *": "deny"}})
    assert rs.command("git status") == ("allow", "git *")
    assert rs.command("git push origin dev") == ("ask", "git push*")
    assert rs.command("git status && rm -rf /") == ("deny", "rm -rf *")
    assert rs.command("git status && ls")[0] is None  # not every part is allowed → the mode decides
    assert rs.command("ls")[0] is None


def test_project_rules_can_only_tighten(isolated):
    (isolated / ".dcode").mkdir()
    (isolated / ".dcode" / "rules.json").write_text(json.dumps(
        {"commands": {"curl *": "allow", "make deploy*": "deny"}, "files": {"secrets/*": "ask"}}))
    rs = rules.load({"commands": {"make *": "allow"}}, isolated)
    assert rs.command("curl http://x")[0] is None  # a repo can't allow itself anything
    assert rs.command("make deploy")[0] == "deny"
    assert rs.command("make test")[0] == "allow"
    assert rs.file("secrets/key.txt")[0] == "ask"


def test_env_files_are_denied_by_default(isolated):
    (isolated / ".env").write_text("TOKEN=hunter2")
    (isolated / ".env.example").write_text("TOKEN=")
    (isolated / "app.py").write_text("import os  # TOKEN")
    host = RecordingHost()
    out = _run([call("read_file", '{"path": ".env"}'), call("read_file", '{"path": ".env.example"}', "c2"),
                call("search_in_files", '{"pattern": "TOKEN"}', "c3")], host)
    assert "protected by the rule" in out[0] and "hunter2" not in out[0]
    assert out[1] == "1| TOKEN="
    assert "hunter2" not in out[2] and "app.py" in out[2]
    assert mentions.find("look at @.env", isolated) == [".env"]  # found, but reading it is blocked:
    config = make_config()
    agent = Agent(config, FakeLLM(scripted(AssistantTurn(content="ok")), config), NullHost())
    agent.run_turn("what is in @.env")
    assert not any("hunter2" in str(m.get("content")) for m in agent.messages)


def test_allow_rule_skips_the_prompt_and_ask_rule_asks_even_in_auto(isolated):
    host = RecordingHost(approve=False)
    out = _run([_cmd("echo hi")], host, rules={"commands": {"echo *": "allow"}, "files": {}, "outside_workspace": "ask"})
    assert out[0] == "hi" and host.asked == []
    host = RecordingHost(approve=False, mode="auto")
    out = _run([_cmd("git push")], host, rules={"commands": {"git push*": "ask"}, "files": {}, "outside_workspace": "ask"})
    assert out[0] == "Command cancelled by user." and host.asked[0][0] == "rule"


def test_deny_rule_blocks_even_in_auto(isolated):
    host = RecordingHost(mode="auto")
    out = _run([_cmd("touch x && rm -rf x")], host,
               rules={"commands": {"rm -rf *": "deny"}, "files": {}, "outside_workspace": "ask"})
    assert out[0].startswith("Blocked: the rule 'rm -rf *'") and not (isolated / "x").exists()


def test_outside_workspace_asks(isolated, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("far away")
    host = RecordingHost(approve=False)
    out = _run([call("read_file", json.dumps({"path": str(outside)}))], host)
    assert "cancelled" in out[0] and host.asked[0][0] == "outside_workspace"
    host = RecordingHost(approve=True)
    out = _run([call("read_file", json.dumps({"path": str(outside)}))], host)
    assert out[0] == "1| far away"
    host = RecordingHost(approve=True)
    out = _run([call("read_file", json.dumps({"path": str(outside)}))], host,
               rules={"commands": {}, "files": {}, "outside_workspace": "deny"})
    assert "outside the workspace" in out[0] and host.asked == []


def test_config_round_trip_does_not_alias_defaults():
    cfg = config_from_dict({"base_url": "u", "api_key": "k", "model": "m"})
    cfg.rules["commands"]["x"] = "deny"
    cfg.rules["files"]["y"] = "deny"
    assert "y" not in DEFAULT_RULES["files"] and DEFAULT_RULES["commands"] == {}
