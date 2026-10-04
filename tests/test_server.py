from __future__ import annotations

import http.client
import json
import threading
import time

import pytest
from conftest import FakeLLM, make_config, scripted

from dcode import server
from dcode.config import load_config, save_config, save_thread
from dcode.llm import AssistantTurn
from dcode.session import Session


@pytest.fixture
def stack(monkeypatch):
    """A live session + its API server + a hub, all in-process on ephemeral ports."""
    monkeypatch.setattr(server, "default_port", lambda configured=0: 0)
    config = make_config()
    save_config(config)
    session = Session(config, llm=FakeLLM(scripted(AssistantTurn(content="hello from dcode")), config))
    token = server.get_token()
    api = server.SessionServer(session, token).start()
    server.register_session({"id": session.id, "pid": session.pid, "port": api.port, "cwd": session.cwd,
                             "model": config.model, "started_at": session.started_at, "thread_name": session.thread_name})
    hub = server.Hub()
    thread = threading.Thread(target=hub.serve, daemon=True)
    thread.start()
    yield {"session": session, "api": api, "hub": hub, "token": token}
    hub.httpd.shutdown()
    api.stop()
    session.close()
    server.unregister_session(session.id)


def req(stack, method, path, body=None, cookie=True, csrf=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", stack["hub"].port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"{server.COOKIE}={stack['token']}"
    if csrf:
        headers["X-Dcode"] = "1"
    if host:
        headers["Host"] = host
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data), resp
    except ValueError:
        return resp.status, data, resp


def test_auth_flow(stack):
    status, _, _ = req(stack, "GET", "/", cookie=False)
    assert status == 401
    status, _, resp = req(stack, "GET", f"/?token={stack['token']}", cookie=False)
    assert status == 302 and server.COOKIE in resp.getheader("Set-Cookie")
    status, body, _ = req(stack, "GET", "/")
    assert status == 200 and b"Dcode" in body
    assert req(stack, "GET", "/api/sessions", host="evil.example:80")[0] == 401  # DNS rebinding
    assert req(stack, "GET", "/api/health", cookie=False)[0] == 200


def test_static_and_theme(stack):
    status, body, _ = req(stack, "GET", "/theme.css")
    assert status == 200 and b"--primary: #2563EB" in body and b'[data-theme="dark"]' in body
    assert req(stack, "GET", "/static/app.js")[0] == 200
    assert req(stack, "GET", "/static/../server.py")[0] == 404


def test_sessions_and_proxy_round_trip(stack):
    status, sessions, _ = req(stack, "GET", "/api/sessions")
    assert status == 200 and sessions[0]["id"] == stack["session"].id and sessions[0]["live"]
    sid = stack["session"].id
    assert req(stack, "POST", f"/api/s/{sid}/message", {"text": "hi"}, csrf=False)[0] == 401  # CSRF guard
    assert req(stack, "POST", f"/api/s/{sid}/message", {"text": "hi"})[0] == 200

    # Live event stream through the hub proxy.
    conn = http.client.HTTPConnection("127.0.0.1", stack["hub"].port, timeout=10)
    conn.request("GET", f"/api/s/{sid}/events?since=0", headers={"Cookie": f"{server.COOKIE}={stack['token']}"})
    resp = conn.getresponse()
    assert resp.getheader("Content-Type") == "text/event-stream"
    seen = []
    deadline = time.time() + 5
    while time.time() < deadline and "assistant_message" not in seen:
        line = resp.fp.readline().decode()
        if line.startswith("data: "):
            seen.append(json.loads(line[6:])["type"])
    conn.close()
    assert "user_message" in seen and "assistant_message" in seen
    assert req(stack, "GET", "/api/s/nope/state")[0] == 404


def test_config_api_masks_key_and_reloads_sessions(stack):
    status, cfg, _ = req(stack, "GET", "/api/config")
    assert status == 200 and "secret" not in cfg["api_key"] and cfg["has_api_key"]
    status, result, _ = req(stack, "PUT", "/api/config", {"model": "llama3", "api_key": cfg["api_key"],
                                                         "agents": {"max_parallel": 3}})
    assert status == 200 and result["sessions_reloaded"] == 1
    saved = load_config()
    assert saved.model == "llama3" and saved.api_key == "secret" and saved.agents["max_parallel"] == 3
    assert stack["session"].config.model == "llama3"


def test_memory_api(stack):
    assert req(stack, "POST", "/api/memory", {"scope_key": "global", "name": "Tabs", "content": "Use tabs"})[0] == 200
    status, items, _ = req(stack, "GET", "/api/memory")
    assert status == 200 and items[0]["name"] == "tabs" and items[0]["scope_key"] == "global"
    assert req(stack, "DELETE", "/api/memory/global/tabs")[0] == 200
    assert req(stack, "GET", "/api/memory")[1] == []
    assert req(stack, "POST", "/api/memory", {"scope_key": "projects/../../x", "name": "a"})[0] == 400


def test_threads_api(stack):
    save_thread("old work", [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
                             {"role": "assistant", "content": "a"}], extra={"cwd": "/w"})
    status, threads, _ = req(stack, "GET", "/api/threads")
    assert status == 200 and threads[0]["id"] == "old-work" and threads[0]["count"] == 3
    status, thread, _ = req(stack, "GET", "/api/threads/old-work")
    assert [i["type"] for i in thread["items"]] == ["user_message", "assistant_message"]
    assert req(stack, "GET", "/api/threads/..%2F..%2Fconfig")[0] == 404


def test_live_sessions_prunes_dead_entries():
    server.register_session({"id": "dead", "pid": 999999, "port": 1})
    assert all(e["id"] != "dead" for e in server.live_sessions())


def test_hub_exits_when_last_session_leaves(monkeypatch):
    monkeypatch.setattr(server, "default_port", lambda configured=0: 0)
    monkeypatch.setattr(server, "HUB_IDLE_GRACE", 0.5)
    server.register_session({"id": "me", "pid": __import__("os").getpid(), "port": 1})
    hub = server.Hub()
    thread = threading.Thread(target=hub.serve, daemon=True)
    thread.start()
    time.sleep(1.5)
    assert thread.is_alive() and server.hub_file().exists()
    server.unregister_session("me")
    thread.join(timeout=6)
    assert not thread.is_alive()
    assert not server.hub_file().exists()


def test_config_api_with_env_only_endpoint(stack, monkeypatch):
    """Air-gapped installs often configure the endpoint purely via env vars."""
    from dcode.config import config_file, write_private_json

    write_private_json(config_file(), {"agents": {"enabled": True}})  # no endpoint in the file
    monkeypatch.setenv("DCODE_BASE_URL", "http://gpu.local/v1")
    monkeypatch.setenv("DCODE_API_KEY", "envkey")
    monkeypatch.setenv("DCODE_MODEL", "envmodel")
    status, cfg, _ = req(stack, "GET", "/api/config")
    assert status == 200 and cfg["model"] == "envmodel"
    assert set(cfg["env_locked"]) >= {"base_url", "api_key", "model"}
    status, result, _ = req(stack, "PUT", "/api/config", {"agents": {"max_parallel": 4}})
    assert status == 200, result
    assert load_config().agents["max_parallel"] == 4


def test_artifact_and_skill_api(stack):
    from dcode.artifacts import ArtifactStore

    meta = ArtifactStore().save("Diagram", "<svg viewBox='0 0 10 10'></svg>", kind="svg")
    status, items, _ = req(stack, "GET", "/api/artifacts")
    assert status == 200 and items[0]["id"] == meta["id"] and "content" not in items[0]
    status, full, _ = req(stack, "GET", f"/api/artifacts/{meta['id']}")
    assert full["content"].startswith("<svg")
    status, raw, resp = req(stack, "GET", f"/api/artifacts/{meta['id']}/raw")
    assert status == 200 and "attachment" in resp.getheader("Content-Disposition")
    assert resp.getheader("Content-Type") == "application/octet-stream"  # never rendered on the hub origin
    assert req(stack, "GET", "/api/artifacts/..%2F..%2Fconfig")[0] == 404
    assert req(stack, "DELETE", f"/api/artifacts/{meta['id']}")[0] == 200

    status, skills, _ = req(stack, "GET", "/api/skills")
    assert status == 200 and any(s["name"] == "devops" and s["builtin"] for s in skills)
    assert req(stack, "POST", "/api/skills", {"name": "My Rules", "description": "d", "content": "- a"})[0] == 200
    names = [s["name"] for s in req(stack, "GET", "/api/skills")[1]]
    assert "my-rules" in names
    assert req(stack, "DELETE", "/api/skills/my-rules")[0] == 200


def test_guide_is_public_and_bilingual(stack):
    status, body, _ = req(stack, "GET", "/guide", cookie=False)
    assert status == 200 and b'lang="fa"' in body and "راهنمای Dcode".encode() in body
    assert req(stack, "GET", "/guide", cookie=False, host="evil.example")[0] != 200


def test_mode_and_undo_endpoints(stack):
    sid = stack["session"].id
    status, body, _ = req(stack, "POST", f"/api/s/{sid}/mode", {"mode": "plan"})
    assert status == 200 and body["mode"] == "plan" and stack["session"].mode == "plan"
    assert req(stack, "POST", f"/api/s/{sid}/mode", {"mode": "bogus"})[0] == 409
    status, body, _ = req(stack, "POST", f"/api/s/{sid}/undo", {})
    assert status == 200 and body["ok"] is False
    state = req(stack, "GET", f"/api/s/{sid}/state")[1]
    assert state["mode"] == "plan" and state["can_undo"] is False


def test_artifact_management_api(stack):
    status, meta, _ = req(stack, "POST", "/api/artifacts", {"title": "Notes", "kind": "markdown", "content": "# v1"})
    assert status == 200 and meta["kind"] == "markdown"
    aid = meta["id"]
    assert req(stack, "PUT", f"/api/artifacts/{aid}/content", {"content": "# v2"})[1]["versions"] == 2
    full = req(stack, "GET", f"/api/artifacts/{aid}")[1]
    assert full["content"] == "# v2" and [v["version"] for v in full["history"]] == [1, 2]
    assert req(stack, "GET", f"/api/artifacts/{aid}?version=1")[1]["content"] == "# v1"
    assert req(stack, "POST", f"/api/artifacts/{aid}/restore", {"version": 1})[1]["versions"] == 3
    assert req(stack, "GET", f"/api/artifacts/{aid}")[1]["content"] == "# v1"
    assert req(stack, "PATCH", f"/api/artifacts/{aid}", {"title": "Renamed"})[1]["title"] == "Renamed"
    dup = req(stack, "POST", f"/api/artifacts/{aid}/duplicate", {})[1]
    assert dup["id"] != aid and dup["title"] == "Renamed (copy)"
    assert req(stack, "PATCH", "/api/artifacts/nope-000000", {"title": "x"})[0] == 404


def test_artifact_page_and_login_redirect(stack):
    from dcode.artifacts import ArtifactStore

    aid = ArtifactStore().save("Page", "<p>hi</p>", kind="html")["id"]
    status, body, _ = req(stack, "GET", f"/a/{aid}")
    assert status == 200 and b"artifact" in body.lower()
    assert req(stack, "GET", f"/a/{aid}", cookie=False)[0] == 401
    status, _, resp = req(stack, "GET", f"/?token={stack['token']}&next=/a/{aid}", cookie=False)
    assert status == 302 and resp.getheader("Location") == f"/a/{aid}"
    status, _, resp = req(stack, "GET", f"/?token={stack['token']}&next=//evil.example", cookie=False)
    assert resp.getheader("Location") == "/"  # no open redirect
