"""The API document matches the server, and `dcode attach` drives a session through it."""

from __future__ import annotations

import sqlite3
import time

import pytest
from conftest import req

from dcode import attach, openapi, schedules, server
from dcode import db as databases
from dcode.artifacts import ArtifactStore
from dcode.config import save_thread
from dcode.skills import save_skill


def _wait(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- the document ---------------------------------------------------------------------------

def test_spec_shape():
    spec = openapi.spec()
    assert spec["openapi"] == "3.1.0" and spec["info"]["title"] == "Dcode hub API"
    operations = [op for item in spec["paths"].values() for op in item.values()]
    assert len(operations) == len(openapi.ROUTES) >= 70
    ids = [op["operationId"] for op in operations]
    assert len(set(ids)) == len(ids)
    assert spec["paths"]["/api/health"]["get"]["security"] == []
    message = spec["paths"]["/api/s/{session}/message"]["post"]
    assert message["requestBody"]["content"]["application/json"]["schema"]["required"] == ["text"]
    assert message["parameters"] == [{"name": "session", "in": "path", "required": True, "schema": {"type": "string"}}]
    assert "text/event-stream" in spec["paths"]["/api/s/{session}/events"]["get"]["responses"]["200"]["content"]


def test_spec_is_served_without_a_login_but_only_to_this_host(stack):
    status, body, _ = req(stack, "GET", "/api/openapi.json", cookie=False)
    assert status == 200 and "/api/sessions" in body["paths"]
    assert req(stack, "GET", "/api/openapi.json", cookie=False, host="evil.example")[0] == 401


def test_every_documented_route_exists(stack, isolated):
    """Call each route with real ids. An unknown route answers 404 "not found" or 409 "unknown command"."""
    session = stack["session"]
    sqlite3.connect(isolated / "shop.db").executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);")
    databases.save_connection({"name": "shop", "engine": "sqlite", "database": str(isolated / "shop.db")})
    export = databases.exports_dir() / "old.csv"
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_text("a\n1\n")
    artifact = ArtifactStore().save("Report", "<h1>hi</h1>", kind="html")
    save_skill("deploy", "How to deploy", "Run ./ship.sh")
    job = schedules.save_job({"schedule": "0 0 1 1 *", "command": "echo hi", "name": "yearly"})
    for event in ({"type": "agent_start", "agent": "explore-1", "role": "explore", "task": "look"},
                  {"type": "agent_end", "agent": "explore-1", "ok": True, "report": "done"}):
        session.bus.emit(**event)
    save_thread("Old chat", [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
                extra={"helpers": session.helpers.dump()})
    req(stack, "POST", "/api/memory", {"name": "style", "content": "use tabs", "scope_key": "global"})
    req(stack, "POST", "/api/agents", {"name": "linter", "description": "Runs the linter"})

    ids = {"session": session.id, "thread": "Old-chat", "helper": "explore-1", "artifact": artifact["id"],
           "connection": "shop", "file": "old.csv", "job": job["id"], "scope": "global", "name": "style"}
    bodies = {
        ("post", "/api/threads/import"): {"name": "Imported", "messages": [{"role": "user", "content": "hi"}]},
        ("post", "/api/memory"): {"name": "other", "content": "x"},
        ("post", "/api/skills"): {"name": "lint", "description": "d", "content": "c"},
        ("post", "/api/agents"): {"name": "tester", "description": "Runs tests"},
        ("post", "/api/artifacts"): {"title": "New", "content": "x"},
        ("patch", "/api/artifacts/{artifact}"): {"title": "Renamed"},
        ("put", "/api/artifacts/{artifact}/content"): {"content": "<h1>v2</h1>"},
        ("post", "/api/artifacts/{artifact}/restore"): {"version": 1},
        ("post", "/api/db"): {"name": "shop2", "engine": "sqlite", "database": str(isolated / "shop.db")},
        ("post", "/api/db/test"): {"engine": "sqlite", "database": str(isolated / "shop.db")},
        ("post", "/api/db/{connection}/query"): {"sql": "SELECT count(*) FROM items"},
        ("post", "/api/db/{connection}/export"): {"what": "schema"},
        ("post", "/api/cron"): {"schedule": "@daily", "command": "true", "name": "daily"},
        ("post", "/api/cron/{job}/toggle"): {"enabled": True},
        ("post", "/api/s/{session}/message"): {"text": "hello"},
        ("post", "/api/s/{session}/approve"): {"id": "none", "approved": False},
        ("post", "/api/s/{session}/mode"): {"mode": "auto"},
        ("post", "/api/s/{session}/plan"): {"decision": "keep"},
        ("post", "/api/s/{session}/rename"): {"name": "Renamed chat"},
        ("post", "/api/s/{session}/resume"): {"thread_id": "Old-chat"},
        ("post", "/api/s/{session}/command"): None,  # no custom command exists: checked separately below
    }
    names = {"/api/skills/{name}": "deploy", "/api/agents/{name}": "linter"}
    queries = {"/api/threads/{thread}/export": "?format=md", "/api/cron/preview": "?schedule=@daily",
               "/api/cron/{job}/log": "?run=none", "/api/s/{session}/files": "?q=a"}
    order = {"get": 0, "post": 1, "put": 1, "patch": 1, "delete": 2}
    missing = []
    for method, path, _summary, _tag, body, _query in sorted(openapi.ROUTES, key=lambda r: order[r[0]]):
        if path.endswith("/events") or (method, path) == ("post", "/api/s/{session}/command"):
            continue  # a stream that never ends; a command that needs a file in .dcode/commands
        if method != "get" and path.startswith("/api/s/"):
            _wait(lambda: session.idle)  # let the previous command finish: "busy" is 409 too
        url = path
        for key, value in {**ids, "name": names.get(path, ids["name"])}.items():
            url = url.replace("{" + key + "}", str(value))
        payload = bodies.get((method, path), {} if body is not None else None)
        status, data, _ = req(stack, method.upper(), url + queries.get(path, ""), payload)
        text = str(data.get("error") if isinstance(data, dict) else "")
        if status in (405, 501) or (status == 404 and text in ("not found", "")) or "unknown command" in text:
            missing.append(f"{method.upper()} {path} -> {status} {text}")
        assert status != 401, f"{method} {path}"
    assert not missing, missing
    # ...and the two left out above are real as well.
    assert req(stack, "POST", f"/api/s/{session.id}/command", {"name": "nope"})[1]["error"] == "Unknown command /nope"
    stream = server.http.client.HTTPConnection("127.0.0.1", stack["hub"].port, timeout=5)
    stream.request("GET", f"/api/s/{session.id}/events?since=0", headers={"Cookie": f"{server.COOKIE}={stack['token']}"})
    reply = stream.getresponse()
    assert reply.status == 200 and reply.getheader("Content-Type") == "text/event-stream"
    stream.close()


# --- attach ---------------------------------------------------------------------------------

def test_parse_target_and_errors():
    assert attach.parse_target("http://127.0.0.1:7300/?token=abc#s=1a2b") == ("http://127.0.0.1:7300", "abc", "1a2b")
    assert attach.parse_target("http://localhost:7412/") == ("http://localhost:7412", "", "")
    with pytest.raises(attach.AttachError, match="not a web UI link"):
        attach.parse_target("ftp://x")
    with pytest.raises(attach.AttachError, match="no access token"):
        attach.resolve("http://127.0.0.1:1/")
    with pytest.raises(attach.AttachError, match="Can't reach Dcode"):
        attach.Remote("http://127.0.0.1:1", "t").sessions()


def _remote(stack):
    return attach.Remote(f"http://127.0.0.1:{stack['hub'].port}", stack["token"])


def test_resolve_finds_the_session(stack, monkeypatch):
    session = stack["session"]
    link = f"http://127.0.0.1:{stack['hub'].port}/?token={stack['token']}"
    assert attach.resolve(link)[1] == session.id                      # the only session there
    assert attach.resolve(f"{link}#s={session.id[:4]}")[1] == session.id  # a prefix is enough
    with pytest.raises(attach.AttachError, match="No session “zzzz” there"):
        attach.resolve(f"{link}#s=zzzz")
    with pytest.raises(attach.AttachError, match="token was refused"):
        attach.resolve(f"http://127.0.0.1:{stack['hub'].port}/?token=wrong")
    monkeypatch.setattr(attach, "local_hub", lambda: (f"http://127.0.0.1:{stack['hub'].port}", stack["token"]))
    assert attach.resolve("")[1] == session.id and attach.resolve(session.id)[1] == session.id


def test_attached_terminal_follows_and_drives_the_session(stack):
    session = stack["session"]
    remote = attach.RemoteSession(_remote(stack), session.id)
    seen: list[dict] = []
    woken: list[int] = []
    remote.bus.subscribe(seen.append)
    remote.on_attention(lambda: woken.append(1))
    try:
        assert remote.cwd == session.cwd and remote.config.model == "qwen2.5-coder" and remote.idle

        remote.submit("hello from the second terminal")
        assert not remote.idle  # expecting the task to start: the input box must not flash back
        assert _wait(lambda: any(e["type"] == "assistant_message" for e in seen))
        assert _wait(lambda: remote.idle)
        said = next(e for e in seen if e["type"] == "user_message")
        assert said["source"] == "attach" and said["text"] == "hello from the second terminal"
        assert next(e for e in seen if e["type"] == "assistant_message")["text"] == "hello from dcode"
        assert woken  # the status change closes whatever prompt is open

        remote.cycle_mode()
        assert _wait(lambda: session.mode == "auto") and remote.mode == "auto"
        assert [e for e in session.bus.since(0) if e["type"] == "mode"][-1]["by"] == "attach"
        session.set_mode("plan", source="cli")  # changed in the main terminal: the attached one follows
        assert _wait(lambda: remote.mode == "plan")

        remote.run_shell("echo from-attach")
        assert _wait(lambda: any(e["type"] == "tool_result" and "from-attach" in e["output"] for e in seen))
        assert remote.context()[1] == 32768
    finally:
        remote.ended = True


def test_attached_terminal_answers_an_approval_and_sees_the_session_end(stack):
    from conftest import call

    from dcode.llm import AssistantTurn

    session = stack["session"]
    turns = [AssistantTurn(tool_calls=[call("run_command", '{"command": "echo approved-remotely"}')]), AssistantTurn(content="ran it")]
    session.llm.router = lambda messages, tools: turns.pop(0) if turns else AssistantTurn(content="done")
    session.set_mode("manual")
    remote = attach.RemoteSession(_remote(stack), session.id)
    app = attach.AttachApp(remote, "http://x/?token=t#s=1")
    try:
        session.submit("run it", source="cli")
        assert _wait(lambda: remote.pending_approvals())
        approval = remote.pending_approvals()[0]
        assert approval["kind"] == "run_command" and "approved-remotely" in approval["detail"]
        assert remote.resolve_approval(approval["id"], True) is True
        assert _wait(lambda: session.idle and not session.pending_approvals()) and not remote.pending_approvals()
        resolved = [e for e in session.bus.since(0) if e["type"] == "approval_resolved"][-1]
        assert resolved["by"] == "attach" and resolved["approved"] is True
        assert any("approved-remotely" in m.get("content", "") for m in session.agent.messages if m["role"] == "tool")
        # Its own actions are not echoed back to it; the main terminal's are.
        assert app._mine({"type": "approval_resolved", "by": "attach"}, "by") is True
        assert app._mine({"type": "user_message", "source": "cli", "text": "run it"}, "source") is False
        remote.sent.append("typed here")
        assert app._mine({"type": "user_message", "source": "attach", "text": "typed here"}, "source") is True
        assert app._mine({"type": "user_message", "source": "attach", "text": "typed in a third terminal"}, "source") is False

        server.unregister_session(session.id)  # the session's process is gone
        stack["api"].stop()
        assert _wait(lambda: remote.ended, timeout=12)
    finally:
        remote.ended = True
        stack["api"] = type("Stopped", (), {"stop": lambda self: None})()
