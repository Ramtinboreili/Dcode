from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest

from dcode.config import AppConfig
from dcode.llm import AssistantTurn


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own Dcode home and workspace; no real env leaks in."""
    home = tmp_path / "dcode-home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("DCODE_HOME", str(home))
    monkeypatch.chdir(work)
    for var in ("BASE_URL", "API_KEY", "MODEL", "REASONING_EFFORT", "CONTEXT_WINDOW", "MAX_OUTPUT_TOKENS",
                "WORKSPACE", "ALLOW_OUTSIDE_WORKSPACE", "NO_UI", "FAMILY", "EXPERIMENT", "TRACE", "TRACE_SNAPSHOTS",
                "TASK_ID"):
        monkeypatch.delenv(f"DCODE_{var}", raising=False)
        monkeypatch.delenv(f"CODE_ONE_{var}", raising=False)
    from dcode import tools

    tools.set_file_guard(None)  # rules from an earlier test must not leak
    return work


def make_config(**overrides: Any) -> AppConfig:
    cfg = AppConfig(base_url="http://localhost:11434/v1", api_key="secret", model="qwen2.5-coder", stream=False)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def call(name: str, args: str = "{}", call_id: str | None = None) -> dict[str, Any]:
    return {"id": call_id or f"call_{name}", "type": "function", "function": {"name": name, "arguments": args}}


class FakeLLM:
    """Scripted stand-in for LLMClient. ``router(messages, tools)`` returns the next turn."""

    def __init__(self, router: Callable[[list[dict[str, Any]], Any], AssistantTurn], config: AppConfig | None = None):
        self.router = router
        self.config = config or make_config()
        self.calls: list[list[dict[str, Any]]] = []

    @property
    def model(self) -> str:
        return self.config.model

    def chat(self, messages, tools=None, on_delta=None, should_stop=None, max_tokens=None) -> AssistantTurn:
        self.calls.append([dict(m) for m in messages])
        turn = self.router(messages, tools)
        if on_delta and turn.content:
            on_delta(turn.content)
        return turn

    def complete_text(self, messages, max_tokens=800, purpose="other") -> str:
        return "- summary of earlier work"

    def list_models(self) -> list[str]:
        return [self.config.model]


def scripted(*turns: AssistantTurn) -> Callable[..., AssistantTurn]:
    queue = list(turns)

    def router(messages, tools):
        return queue.pop(0) if queue else AssistantTurn(content="done")

    return router


# --- an in-process hub + session, for anything that talks HTTP ----------------------------------

@pytest.fixture
def stack(monkeypatch):
    """A live session + its API server + a hub, all in-process on ephemeral ports."""
    from dcode import server
    from dcode.config import save_config
    from dcode.session import Session

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
    from dcode import server

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
