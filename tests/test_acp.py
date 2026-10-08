"""dcode acp: an editor drives a session over the Agent Client Protocol (JSON-RPC on stdio)."""

from __future__ import annotations

import json
import os
import queue
import threading

import pytest
from conftest import FakeLLM, call, make_config, scripted

from dcode import acp
from dcode.llm import AssistantTurn
from dcode.session import Session


class Editor:
    """The client side: writes requests to the agent, collects what comes back."""

    def __init__(self, router, answer=None):
        to_agent_r, to_agent_w = os.pipe()
        from_agent_r, from_agent_w = os.pipe()
        self.out = os.fdopen(to_agent_w, "w")
        self.inbox: queue.Queue = queue.Queue()
        self.updates: list[dict] = []
        self.asked: list[dict] = []
        self.answer = answer or (lambda params: {"outcome": {"outcome": "selected", "optionId": "allow"}})
        self.sessions: list[Session] = []
        self._id = 0

        def factory(cwd, servers):
            config = make_config()
            self.servers = servers
            self.sessions.append(Session(config, llm=FakeLLM(router, config)))
            return self.sessions[-1]

        self.agent = acp.Agent(os.fdopen(to_agent_r), os.fdopen(from_agent_w, "w"), session_factory=factory)
        threading.Thread(target=self.agent.serve, daemon=True).start()
        threading.Thread(target=self._read, args=(os.fdopen(from_agent_r),), daemon=True).start()

    def _read(self, stream):
        for line in stream:
            message = json.loads(line)
            assert message["jsonrpc"] == "2.0"
            if message.get("method") == "session/update":
                self.updates.append(message["params"]["update"])
            elif message.get("method") == "session/request_permission":
                self.asked.append(message["params"])
                self._write({"jsonrpc": "2.0", "id": message["id"], "result": self.answer(message["params"])})
            else:
                self.inbox.put(message)

    def _write(self, message):
        self.out.write(json.dumps(message) + "\n")
        self.out.flush()

    def call(self, method, params=None, timeout=15):
        self._id += 1
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        reply = self.inbox.get(timeout=timeout)
        assert reply["id"] == self._id
        return reply

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def kinds(self):
        return [u["sessionUpdate"] for u in self.updates]

    def close(self):
        self.out.close()
        for session in self.sessions:
            session.close()


@pytest.fixture
def editor():
    made = []

    def make(router, answer=None):
        made.append(Editor(router, answer))
        return made[-1]

    yield make
    for item in made:
        item.close()


def _start(client, isolated):
    init = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})["result"]
    assert init["protocolVersion"] == 1 and init["agentInfo"]["name"] == "dcode" and init["authMethods"] == []
    assert init["agentCapabilities"]["promptCapabilities"]["embeddedContext"] is True
    new = client.call("session/new", {"cwd": str(isolated), "mcpServers": []})["result"]
    assert new["modes"]["currentModeId"] == "accept_edits"
    assert [m["id"] for m in new["modes"]["availableModes"]] == ["accept_edits", "auto", "plan", "manual"]
    return new["sessionId"]


def test_a_prompt_turn_streams_messages_tools_diffs_and_the_plan(editor, isolated):
    (isolated / "a.txt").write_text("old line\n")
    client = editor(scripted(
        AssistantTurn(content="Let me fix it.", reasoning="it needs one edit", tool_calls=[
            call("todo_write", json.dumps({"items": [{"text": "Edit a.txt", "status": "in_progress"}]}), "t1"),
            call("edit_file", json.dumps({"path": "a.txt", "old_text": "old line", "new_text": "new line"}), "e1")]),
        AssistantTurn(content="Changed **a.txt**."),
    ))
    session_id = _start(client, isolated)
    reply = client.call("session/prompt", {"sessionId": session_id, "prompt": [
        {"type": "text", "text": "fix"}, {"type": "resource_link", "uri": f"file://{isolated}/a.txt", "name": "a.txt"}]})
    assert reply["result"] == {"stopReason": "end_turn"}
    assert (isolated / "a.txt").read_text() == "new line\n"
    assert client.sessions[0].agent.messages[1]["content"] == "fix @a.txt"  # the link became an @mention

    kinds = client.kinds()
    assert kinds.index("agent_thought_chunk") < kinds.index("tool_call") < kinds.index("tool_call_update")
    assert "available_commands_update" in kinds and "usage_update" in kinds
    plan = next(u for u in client.updates if u["sessionUpdate"] == "plan")
    assert plan["entries"] == [{"content": "Edit a.txt", "priority": "medium", "status": "in_progress"}]
    edit = next(u for u in client.updates if u["sessionUpdate"] == "tool_call" and u["title"].startswith("edit_file"))
    assert edit["kind"] == "edit" and edit["status"] == "in_progress" and edit["locations"] == [{"path": str(isolated / "a.txt")}]
    done = next(u for u in client.updates if u["sessionUpdate"] == "tool_call_update" and u["toolCallId"] == edit["toolCallId"])
    assert done["status"] == "completed"
    assert done["content"][0] == {"type": "diff", "path": str(isolated / "a.txt"), "oldText": "old line", "newText": "new line"}
    texts = [u["content"]["text"] for u in client.updates if u["sessionUpdate"] == "agent_message_chunk"]
    assert texts[0].startswith("Let me fix it.") and texts[-1] == "Changed **a.txt**."
    assert not any(u["sessionUpdate"] == "tool_call" and "todo_write" in u["title"] for u in client.updates)
    commands = next(u for u in client.updates if u["sessionUpdate"] == "available_commands_update")["availableCommands"]
    assert [c["name"] for c in commands][:3] == ["compact", "init", "review"]


def test_approvals_go_to_the_editor_and_a_denial_is_respected(editor, isolated):
    answers = iter(["allow", "deny"])
    client = editor(scripted(
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo allowed > out.txt"}', "c1")]),
        AssistantTurn(tool_calls=[call("run_command", '{"command": "echo denied > no.txt"}', "c2")]),
        AssistantTurn(content="done"),
    ), answer=lambda params: {"outcome": {"outcome": "selected", "optionId": next(answers)}})
    session_id = _start(client, isolated)
    assert client.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "run"}]})["result"]["stopReason"] == "end_turn"
    assert (isolated / "out.txt").exists() and not (isolated / "no.txt").exists()
    first = client.asked[0]
    assert first["sessionId"] == session_id and first["toolCall"]["title"].startswith("Run a command: echo allowed")
    assert [o["kind"] for o in first["options"]] == ["allow_once", "allow_always", "reject_once"]
    shown = next(u for u in client.updates if u["sessionUpdate"] == "tool_call" and "echo allowed" in u["title"])
    assert first["toolCall"]["toolCallId"] == shown["toolCallId"]  # the prompt sits on that tool call's card
    failed = [u for u in client.updates if u["sessionUpdate"] == "tool_call_update" and u["status"] == "failed"]
    assert len(failed) == 1 and "cancelled by user" in failed[0]["content"][0]["content"]["text"]


def test_questions_modes_cancel_and_errors(editor, isolated):
    release = threading.Event()

    def router(messages, tools):
        last = messages[-1]
        if last["role"] == "user" and "ask" in last["content"]:
            return AssistantTurn(tool_calls=[call("ask_user", '{"question": "Which db?", "options": ["Postgres", "SQLite"]}', "q1")])
        if last["role"] == "user" and "wait" in last["content"]:
            release.wait(10)
            return AssistantTurn(content="finished waiting")
        return AssistantTurn(content="ok: " + str(last.get("content"))[:60])

    client = editor(router, answer=lambda params: {"outcome": {"outcome": "selected", "optionId": "answer:1"}})
    session_id = _start(client, isolated)
    client.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "ask me"}]})
    assert [o["name"] for o in client.asked[0]["options"]] == ["Postgres", "SQLite", "Skip (Dcode decides)"]
    assert any("The user answered: SQLite" in str(m.get("content")) for m in client.sessions[0].agent.messages)

    assert client.call("session/set_mode", {"sessionId": session_id, "modeId": "auto"})["result"] == {}
    assert client.sessions[0].mode == "auto"
    assert {"sessionUpdate": "current_mode_update", "currentModeId": "auto"} in client.updates
    assert client.call("session/set_mode", {"sessionId": session_id, "modeId": "nope"})["error"]["code"] == -32603

    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(
        client.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "wait here"}]})))
    worker.start()
    while not client.sessions[0].busy:
        pass
    client.notify("session/cancel", {"sessionId": session_id})
    release.set()
    worker.join(15)
    assert result["result"] == {"stopReason": "cancelled"}

    assert client.call("session/prompt", {"sessionId": "zzz", "prompt": []})["error"]["code"] == -32602
    assert client.call("no/such/method")["error"]["code"] == -32601
    assert client.call("session/new", {"cwd": "/no/such/folder", "mcpServers": []})["error"]["message"].startswith("The folder")
    assert client.call("session/prompt", {"sessionId": session_id, "prompt": []})["result"] == {"stopReason": "end_turn"}


def test_plan_mode_asks_to_switch_and_implements_in_the_same_turn(editor, isolated):
    client = editor(scripted(AssistantTurn(content="Plan: write b.txt"),
                             AssistantTurn(tool_calls=[call("write_file", '{"path": "b.txt", "content": "x"}', "w1")]),
                             AssistantTurn(content="Implemented.")),
                    answer=lambda params: {"outcome": {"outcome": "selected", "optionId": "accept_edits"}})
    session_id = _start(client, isolated)
    client.call("session/set_mode", {"sessionId": session_id, "modeId": "plan"})
    reply = client.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "plan b"}]})
    assert reply["result"]["stopReason"] == "end_turn" and (isolated / "b.txt").read_text() == "x"
    switch = client.asked[0]["toolCall"]
    assert switch["kind"] == "switch_mode" and client.sessions[0].mode == "accept_edits"
    assert {"sessionUpdate": "current_mode_update", "currentModeId": "accept_edits"} in client.updates


def test_content_blocks_and_editor_mcp_servers(isolated):
    cwd = str(isolated)
    assert acp.prompt_text([{"type": "text", "text": "look at"}, {"type": "resource_link", "uri": f"file://{cwd}/src/a.py"},
                            {"type": "resource_link", "uri": "file:///etc/hosts"}, {"type": "image", "data": "…"}], cwd) == \
        "look at @src/a.py /etc/hosts [image left out: Dcode takes text]"
    embedded = acp.prompt_text([{"type": "resource", "resource": {"uri": f"file://{cwd}/a.py", "text": "print(1)"}}], cwd)
    assert embedded == "[a.py]\n```\nprint(1)\n```"
    assert acp.prompt_text(None, cwd) == ""
    assert acp._editor_servers([{"name": "files", "command": "/bin/server", "args": ["--stdio"], "env": [{"name": "K", "value": "v"}]},
                                {"name": "remote", "type": "http", "url": "https://x"}, "junk"]) == \
        {"files": {"command": ["/bin/server", "--stdio"], "env": {"K": "v"}}}


def test_vscode_extension_packs_into_a_valid_vsix(tmp_path):
    import subprocess
    import sys
    import zipfile
    from pathlib import Path
    from xml.dom import minidom

    from dcode import __version__

    folder = Path(__file__).resolve().parents[1] / "editors" / "vscode"
    manifest = json.loads((folder / "package.json").read_text())
    assert manifest["version"] == __version__ and manifest["main"] == "./extension.js"
    source = (folder / "extension.js").read_text()
    for command in manifest["contributes"]["commands"]:
        assert f'"{command["command"]}"' in source  # every contributed command is registered
    assert "https://" not in source and "require(\"https\")" not in source  # localhost only
    done = subprocess.run([sys.executable, str(folder / "build_vsix.py"), str(tmp_path)], capture_output=True, text=True, check=True)
    package = Path(done.stdout.strip())
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {"[Content_Types].xml", "extension.vsixmanifest", "extension/package.json",
                                           "extension/extension.js", "extension/README.md"}
        identity = minidom.parseString(archive.read("extension.vsixmanifest")).getElementsByTagName("Identity")[0]
        assert identity.getAttribute("Version") == __version__ and identity.getAttribute("Id") == "dcode"
