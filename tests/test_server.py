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


def test_hub_child_env_does_not_reuse_parent_unpack_dir(monkeypatch):
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/_MEI123")
    monkeypatch.setenv("_MEIPASS2", "/tmp/_MEI123")
    monkeypatch.setenv("KEEP_ME", "1")
    env = server._clean_child_env()
    assert "_PYI_APPLICATION_HOME_DIR" not in env and "_MEIPASS2" not in env
    assert env["KEEP_ME"] == "1" and env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_health_reports_version_and_assets(stack):
    status, health, _ = req(stack, "GET", "/api/health", cookie=False)
    assert status == 200 and health["assets"] is True and health["version"] == server.__version__
    info = {"pid": __import__("os").getpid(), "port": stack["hub"].port}
    assert server.hub_usable(info)
    real = server.hub_health
    try:  # a hub from another Dcode version, or one whose files vanished, is not reused
        server.hub_health = lambda i: {"ok": True, "version": "0.0.0-other", "assets": True}
        assert not server.hub_usable(info)
        server.hub_health = lambda i: {"ok": True, "version": server.__version__, "assets": False}
        assert not server.hub_usable(info)
    finally:
        server.hub_health = real


def test_file_completion_through_the_hub(stack, isolated):
    from dcode import mentions

    mentions._index.clear()
    (isolated / "deploy.yaml").write_text("x: 1")
    sid = stack["session"].id
    status, body, _ = req(stack, "GET", f"/api/s/{sid}/files?q=depl")
    assert status == 200 and body["files"][0] == "deploy.yaml"


def test_rules_are_replaced_whole_from_the_web(stack):
    status, cfg, _ = req(stack, "GET", "/api/config")
    assert cfg["rules"]["files"][".env"] == "deny"
    rules = {"commands": {"git *": "allow", "bad": "maybe"}, "files": {}, "outside_workspace": "deny"}
    assert req(stack, "PUT", "/api/config", {"rules": rules})[0] == 200
    saved = load_config().rules
    assert saved["commands"] == {"git *": "allow"} and saved["files"] == {} and saved["outside_workspace"] == "deny"


def test_hub_process_skips_the_terminal_libraries():
    """The hub outlives sessions, so it must not load the terminal app's stack (memory)."""
    import subprocess
    import sys
    from pathlib import Path

    code = ("import sys; sys.argv = ['dcode', 'hub', '--port', '7411']\n"
            "import dcode.server as s\n"
            "s.run_hub = lambda port=0: print('port', port)\n"
            "import dcode.__main__ as m; m.main()\n"
            "print('heavy', [n for n in ('click', 'rich', 'prompt_toolkit', 'openai', 'httpx') if n in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         cwd=Path(server.__file__).resolve().parents[1]).stdout
    assert "port 7411" in out and "heavy []" in out


def test_model_list_is_fetched_without_an_http_client_library(stack):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Models(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
            body = json.dumps({"data": [{"id": "qwen2.5-coder:7b"}, {"id": "deepseek-coder"}, "junk"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    upstream = HTTPServer(("127.0.0.1", 0), Models)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        save_config(make_config(base_url=f"http://127.0.0.1:{upstream.server_port}/v1/"))
        status, data, _ = req(stack, "GET", "/api/models")
        assert status == 200 and data["models"] == ["deepseek-coder", "qwen2.5-coder:7b"]
        assert seen == {"path": "/v1/models", "auth": "Bearer secret"}
    finally:
        upstream.shutdown()
    save_config(make_config(base_url="http://127.0.0.1:9/v1"))  # nothing listens: an error, not a crash
    status, data, _ = req(stack, "GET", "/api/models")
    assert status == 200 and data["models"] == [] and data["error"]


def test_database_api(stack, isolated):
    import sqlite3

    path = isolated.parent / "api.db"
    handle = sqlite3.connect(path)
    handle.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);"
                         "INSERT INTO items VALUES (1, 'bolt'), (2, 'nut'), (3, 'gear');")
    handle.commit()
    handle.close()
    form = {"name": "Stock", "engine": "sqlite", "database": str(path), "password": "unused-secret"}
    status, tested, _ = req(stack, "POST", "/api/db/test", form)
    assert status == 200 and tested["ok"] and tested["version"]
    status, saved, _ = req(stack, "POST", "/api/db", form)
    assert status == 200 and saved["name"] == "stock" and "password" not in saved
    status, listing, _ = req(stack, "GET", "/api/db")
    assert [c["name"] for c in listing["connections"]] == ["stock"] and "unused-secret" not in json.dumps(listing)
    assert listing["drivers"]["sqlite"] is True and listing["limits"]["row_limit"] == 200
    status, schema, _ = req(stack, "GET", "/api/db/stock/schema")
    assert status == 200 and schema["tables"][0]["label"] == "items" and schema["tables"][0]["rows"] == 3
    status, summary, _ = req(stack, "GET", "/api/db/stock/summary")
    assert summary["tables"] == 1 and summary["rows_estimate"] == 3

    status, rows, _ = req(stack, "POST", "/api/db/stock/query", {"sql": "SELECT name FROM items ORDER BY id", "limit": 2})
    assert rows["rows"] == [["bolt"], ["nut"]] and rows["truncated"]
    # A change is never run on the first request: the page must show it and confirm.
    status, ask, _ = req(stack, "POST", "/api/db/stock/query", {"sql": "DELETE FROM items WHERE id = 3"})
    assert status == 200 and ask["needs_confirmation"] and ask["statement"] == "DELETE FROM items WHERE id = 3"
    assert req(stack, "POST", "/api/db/stock/query", {"sql": "SELECT count(*) FROM items"})[1]["rows"] == [[3]]
    status, done, _ = req(stack, "POST", "/api/db/stock/query", {"sql": "DELETE FROM items WHERE id = 3", "confirmed": True})
    assert done["kind"] == "write" and done["rowcount"] == 1
    status, schema, _ = req(stack, "GET", "/api/db/stock/schema")
    assert schema["tables"][0]["rows"] == 2  # the cached schema was dropped after the change
    status, bad, _ = req(stack, "POST", "/api/db/stock/query", {"sql": "SELECT nope FROM items"})
    assert status == 400 and "nope" in bad["error"]
    assert req(stack, "POST", "/api/db/stock/query", {"sql": "SELECT 1"}, csrf=False)[0] == 401

    status, out, _ = req(stack, "POST", "/api/db/stock/export", {"what": "table", "table": "items"})
    assert status == 200 and out["rows"] == 2 and out["url"].startswith("/api/db/exports/stock-items-")
    status, data, resp = req(stack, "GET", out["url"])
    assert status == 200 and data.decode().splitlines() == ["id,name", "1,bolt", "2,nut"]
    assert resp.getheader("Content-Disposition").startswith("attachment")
    save_config(make_config(db={"export_max_rows": 1, "export_max_mb": 1}))
    req(stack, "GET", "/api/db/stock/schema?refresh=1")
    status, heavy, _ = req(stack, "POST", "/api/db/stock/export", {"what": "table", "table": "items"})
    assert heavy["too_heavy"] and "items is heavy" in heavy["message"]
    status, cut, _ = req(stack, "POST", "/api/db/stock/export", {"what": "table", "table": "items", "force": True})
    assert cut["rows"] == 1 and cut["truncated"]
    assert len(req(stack, "GET", "/api/db/exports")[1]) == 2
    assert req(stack, "GET", "/api/db/exports/..%2F..%2Fconfig.json")[0] == 404  # only files in the exports folder
    assert req(stack, "DELETE", "/api/db/stock")[1]["ok"]
    assert req(stack, "GET", "/api/db/stock/schema")[0] == 400


def test_bang_message_runs_the_users_own_command(stack):
    session = stack["session"]
    status, _, _ = req(stack, "POST", f"/api/s/{session.id}/message", {"text": "!echo web-shell"})
    assert status == 200
    deadline = time.time() + 8
    while time.time() < deadline and not any(e["type"] == "tool_result" for e in session.bus.since(0)):
        time.sleep(0.02)
    result = next(e for e in session.bus.since(0) if e["type"] == "tool_result")
    assert result["by"] == "user" and "web-shell" in result["output"]
    assert not any(e["type"] == "assistant_message" for e in session.bus.since(0))  # the model was not called


def test_redo_endpoint_and_state(stack):
    session = stack["session"]
    sid = session.id
    status, body, _ = req(stack, "POST", f"/api/s/{sid}/redo", {})
    assert status == 200 and body == {"ok": False, "files": []}
    target = session.agent.checkpoints
    path = __import__("pathlib").Path(session.cwd) / "note.txt"
    target.begin("write a note")
    target.record(path)
    path.write_text("hello")
    assert req(stack, "POST", f"/api/s/{sid}/undo", {})[1]["ok"] and not path.exists()
    assert req(stack, "GET", f"/api/s/{sid}/state")[1]["can_redo"] is True
    status, body, _ = req(stack, "POST", f"/api/s/{sid}/redo", {})
    assert body == {"ok": True, "files": ["note.txt"]} and path.read_text() == "hello"
    assert any(e["type"] == "redo" for e in session.bus.since(0))
    assert "brought back your changes" in session.agent.messages[-2]["content"]
