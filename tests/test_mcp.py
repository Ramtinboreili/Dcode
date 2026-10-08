from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from conftest import FakeLLM, call, make_config, scripted

from dcode import mcp
from dcode.config import config_from_dict
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn
from dcode.session import Session

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def spec(*extra, **fields):
    return {"notes": {"command": [sys.executable, FAKE, *extra], **fields}}


@pytest.fixture
def manager(isolated):
    made = []

    def make(servers=None):
        made.append(mcp.Manager(mcp.clean_servers(servers or spec()), str(isolated)).start(wait=10))
        return made[-1]

    yield make
    for item in made:
        item.close()


def _wait(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- config ---------------------------------------------------------------------------------

def test_server_config_is_cleaned():
    servers = mcp.clean_servers({
        "My Files!": {"command": "python3 server.py '/srv/my data'", "env": {"TOKEN": 5}, "approve": "always", "timeout": "9"},
        "other": {"command": "npx", "args": ["-y", "pkg"], "enabled": False, "approve": "allow"},
        "bad1": {"command": []}, "bad2": {"command": "unclosed 'quote"}, "bad3": "x", "": {"command": ["x"]},
    })
    assert list(servers) == ["my-files", "other"]
    assert servers["my-files"] == {"command": ["python3", "server.py", "/srv/my data"], "env": {"TOKEN": "5"},
                                   "enabled": True, "approve": "ask", "timeout": 9.0}
    assert servers["other"]["command"] == ["npx", "-y", "pkg"] and servers["other"]["approve"] == "allow"
    cfg = config_from_dict({"base_url": "u", "api_key": "k", "model": "m", "mcp": {"servers": {"a": {"command": "x"}}}})
    assert cfg.mcp["servers"]["a"]["command"] == ["x"] and cfg.as_dict()["mcp"]["servers"]["a"]["approve"] == "ask"
    assert make_config().mcp == {"servers": {}}


# --- the client -----------------------------------------------------------------------------

def test_handshake_tools_and_calls(manager, isolated):
    servers = manager(spec(env={"TOKEN": "s3cret"}))
    (status,) = servers.status()
    assert status["state"] == "ready" and status["title"] == "Fake notebook" and len(status["tools"]) == 6
    assert status["tools"][0] == {"name": "add", "description": "Add two numbers and return the sum.", "read_only": True}
    server = servers.servers["notes"]
    assert server.call("add", {"a": 2, "b": 40}) == ("42", False)
    assert server.call("fail", {}) == ("disk is full", True)
    assert server.call("whoami", {})[0] == f"token=s3cret cwd={isolated}"  # its env and the project folder
    text, _ = server.call("picture", {})
    assert "image left out" in text
    with pytest.raises(mcp.MCPError, match="Unknown tool"):
        server.call("nope", {})


def test_index_is_names_only_and_details_come_on_demand(manager):
    servers = manager()
    index = servers.index_text()
    assert index == "- notes: add, write_note, fail, slow, whoami, picture"
    assert "Add two numbers" not in index and "inputSchema" not in index
    listing = servers.describe("notes")
    assert "notes: 6 tools" in listing and "- add: Add two numbers and return the sum." in listing
    detail = servers.describe("notes", "add")
    assert "notes · add (read-only)" in detail and '"required":["a","b"]' in detail and "mcp_call" in detail
    assert servers.describe("", "write_note").startswith("notes · write_note\n")  # one server: its name may be left out
    with pytest.raises(mcp.MCPError, match="Did you mean: write_note"):
        servers.describe("notes", "note")
    with pytest.raises(mcp.MCPError, match="No MCP server named 'ghost'"):
        servers.describe("ghost")


def test_many_tools_are_paged_and_the_index_stays_small(manager):
    servers = manager(spec("--many"))
    server = servers.servers["notes"]
    assert len(server.tools) == 46
    index = servers.index_text(max_chars=4000)
    assert "(+21 more)" in index and len(index) < 400  # 46 schemas would be thousands of tokens every turn
    assert len(servers.describe("notes", max_chars=3000)) <= 3000


def test_a_server_that_fails_or_hangs_is_reported_not_fatal(manager, isolated):
    servers = manager({
        "crashes": {"command": [sys.executable, FAKE, "--crash"]},
        "missing": {"command": ["/no/such/program"]},
        "off": {"command": ["x"], "enabled": False},
        "notes": {"command": [sys.executable, FAKE], "timeout": 1},
    })
    rows = {row["name"]: row for row in servers.status()}
    assert rows["crashes"]["state"] == "failed" and "cannot open the notebook" in rows["crashes"]["error"]
    assert rows["missing"]["state"] == "failed" and "couldn't start" in rows["missing"]["error"]
    assert rows["off"]["state"] == "off" and rows["notes"]["state"] == "ready"
    assert servers.index_text() == "- notes: add, write_note, fail, slow, whoami, picture"
    with pytest.raises(mcp.MCPError, match="not available"):
        servers.describe("crashes")
    started = time.time()
    with pytest.raises(mcp.MCPError, match="did not answer tools/call within 1s"):
        servers.servers["notes"].call("slow", {"seconds": 20})
    assert time.time() - started < 5

    servers.restart("notes")
    servers.start(wait=10)
    assert servers.servers["notes"].call("add", {"a": 1, "b": 1}) == ("2", False)
    process = servers.servers["notes"].process
    servers.close()
    assert process.poll() is not None  # stopped with the session


# --- the model's two tools --------------------------------------------------------------------

class Host(NullHost):
    def __init__(self, servers, approve=True, mode="accept_edits"):
        super().__init__(approve=approve)
        self.mcp = servers
        self.mode = mode
        self.asked = []

    def request_approval(self, kind, detail, agent):
        self.asked.append((kind, detail))
        return self._approve


def _run(host, *calls):
    config = make_config()
    turns = [AssistantTurn(tool_calls=[c]) for c in calls] + [AssistantTurn(content="done")]
    agent = Agent(config, FakeLLM(scripted(*turns), config), host)
    agent.run_turn("use the notebook")
    return agent, [m["content"] for m in agent.messages if m["role"] == "tool"]


def test_model_reads_a_tool_then_calls_it_and_is_asked_first(manager):
    host = Host(manager())
    agent, outputs = _run(
        host,
        call("mcp_tools", '{"server": "notes", "tool": "write_note"}', "c1"),
        call("mcp_call", json.dumps({"server": "notes", "tool": "write_note", "arguments": {"text": "hi"}}), "c2"),
        call("mcp_call", json.dumps({"server": "notes", "tool": "add", "arguments": '{"a": 1, "b": 2}'}), "c3"),
        call("mcp_call", '{"server": "notes", "tool": "fail"}', "c4"),
        call("mcp_call", '{"server": "notes", "tool": "nope"}', "c5"),
    )
    assert "notes · write_note" in outputs[0] and "Arguments (JSON Schema)" in outputs[0]
    assert outputs[1] == "saved note 1" and outputs[2] == "3"  # arguments sent as a JSON string still work
    assert outputs[3] == "The tool reported an error:\ndisk is full"
    assert outputs[4].startswith("MCP error: 'notes' has no tool 'nope'")
    assert [kind for kind, _ in host.asked] == ["mcp_tool"] * 3  # every call that ran, even the read-only one
    assert host.asked[0][1] == 'notes · write_note  {"text": "hi"}'
    names = agent.tool_names()
    assert "mcp_tools" in names and "mcp_call" in names
    prompt = agent.system_prompt()
    assert "- notes: add, write_note" in prompt and "Add two numbers" not in prompt
    schemas = json.dumps(agent.tools())
    assert "write_note" not in schemas  # the server's tools are never in the tool list itself


def test_denied_auto_allow_and_plan_mode(manager):
    servers = manager()
    _, outputs = _run(Host(servers, approve=False), call("mcp_call", '{"server": "notes", "tool": "write_note", "arguments": {"text": "x"}}'))
    assert "was cancelled by the user" in outputs[0]

    auto = Host(servers, approve=False, mode="auto")
    _, outputs = _run(auto, call("mcp_call", '{"server": "notes", "tool": "write_note", "arguments": {"text": "x"}}'))
    assert outputs[0].startswith("saved note") and auto.asked == []

    trusted = Host(manager(spec(approve="allow")), approve=False)
    _, outputs = _run(trusted, call("mcp_call", '{"server": "notes", "tool": "write_note", "arguments": {"text": "x"}}'))
    assert outputs[0] == "saved note 1" and trusted.asked == []

    plan = Host(servers, mode="plan")
    _, outputs = _run(plan, call("mcp_call", '{"server": "notes", "tool": "write_note", "arguments": {"text": "x"}}', "p1"),
                      call("mcp_call", '{"server": "notes", "tool": "add", "arguments": {"a": 1, "b": 1}}', "p2"))
    assert outputs[0].startswith("Plan mode: notes · write_note may change something") and outputs[1] == "2"


def test_no_servers_means_no_tools_and_no_prompt_cost():
    config = make_config()
    agent = Agent(config, FakeLLM(scripted(), config), NullHost())
    assert "mcp_call" not in agent.tool_names() and "MCP" not in agent.system_prompt()


def test_session_starts_and_stops_its_servers(isolated):
    config = make_config(mcp={"servers": mcp.clean_servers(spec())})
    session = Session(config, llm=FakeLLM(scripted(), config))
    try:
        assert _wait(lambda: session.state()["mcp"] == [{"name": "notes", "state": "ready", "tools": 6}])
        process = session.mcp.servers["notes"].process
        session.agent.refresh_system_message()
        assert "- notes: add" in session.agent.messages[0]["content"]

        config.mcp = {"servers": {}}  # removed in settings: the server goes away with it
        session.apply_config(config)
        assert session.mcp is None and process.poll() is not None and session.state()["mcp"] == []
        assert "mcp_call" not in session.agent.tool_names()
    finally:
        session.close()
