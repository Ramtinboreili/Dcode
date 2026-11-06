"""The Dcode Manager: sign-in, the gateway (limits, access, guards, streaming, usage), admin API, monitoring.

A fake model server stands in for vLLM/OpenRouter; the manager runs on a random local port.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from dcode.manager import auth, discovery, guards, limits, monitoring
from dcode.manager.cli import bootstrap_admin
from dcode.manager.server import Manager, serve
from dcode.manager.store import Store

HEADERS = {"X-Dcode-Manager": "1", "Content-Type": "application/json"}


class Upstream:
    """A model server: /v1/models and /v1/chat/completions, streamed or not."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reject_stream_options = False
        self.reject_fields: list[str] = []
        self.must_think = False
        self.reply = "Hello from the model, all good."
        self.usage = True
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, payload: Any) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._send(200, {"data": [{"id": "up-model", "max_model_len": 8192}]})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                upstream.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                if upstream.reject_stream_options and "stream_options" in body:
                    self._send(400, {"error": {"message": "Unrecognized request argument supplied: stream_options"}})
                    return
                for name in upstream.reject_fields:
                    if name in body:
                        self._send(400, {"error": {"message": f"Extra inputs are not permitted: {name}"}})
                        return
                if upstream.must_think and (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False:
                    self._send(400, {"error": {"message": "Reasoning is mandatory for this endpoint and cannot be "
                                                          "disabled."}})
                    return
                text = upstream.reply
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b": PROCESSING\n\n")
                    for i in range(0, len(text), 4):
                        chunk = {"id": "c1", "model": body["model"], "choices": [
                            {"index": 0, "delta": {"content": text[i:i + 4]}, "finish_reason": None}]}
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    if (body.get("stream_options") or {}).get("include_usage") and upstream.usage:
                        self.wfile.write(b"data: " + json.dumps({"id": "c1", "choices": [], "usage": {
                            "prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59}}).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                payload = {"id": "c1", "model": body["model"], "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
                if upstream.usage:
                    payload["usage"] = {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48}
                self._send(200, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"


class Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie = ""

    def call(self, method: str, path: str, body: Any = None, key: str = "", headers: dict | None = None,
             raw: bool = False) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(HEADERS) | (headers or {})
        if key:
            sent["Authorization"] = f"Bearer {key}"
        if self.cookie:
            sent["Cookie"] = self.cookie
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=sent)
        response = conn.getresponse()
        data = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        conn.close()
        if raw:
            return response.status, data
        try:
            return response.status, json.loads(data) if data else None
        except ValueError:
            return response.status, data

    def login(self, username: str, password: str) -> None:
        status, body = self.call("POST", "/api/login", {"username": username, "password": password})
        assert status == 200, body


@pytest.fixture
def stack(tmp_path):
    store = Store(tmp_path / "manager.db")
    auth.create_user(store, "admin", "admin-pass-1", role="admin")
    auth.create_user(store, "sara", "sara-pass-1")
    upstream = Upstream()
    app = Manager(store)
    server = serve(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    admin = Client(server.server_address[1])
    admin.login("admin", "admin-pass-1")
    status, _ = admin.call("POST", "/api/admin/models", {
        "id": "coder", "upstream_url": upstream.url, "upstream_model": "up-model", "upstream_key": "up-secret",
        "context_length": 4000, "max_output_tokens": 500})
    assert status == 200
    status, body = Client(server.server_address[1]).call("POST", "/api/device/login", {
        "username": "sara", "password": "sara-pass-1", "device": "laptop", "client": "Dcode 0.6.0"})
    assert status == 200, body
    yield {"store": store, "upstream": upstream, "admin": admin, "port": server.server_address[1],
           "key": body["api_key"], "login": body, "user": Client(server.server_address[1])}
    server.shutdown()
    upstream.server.shutdown()


def chat(stack, text="hi", key=None, stream=False, **extra) -> tuple[int, Any]:
    body = {"model": "coder", "messages": [{"role": "user", "content": text}], "stream": stream, **extra}
    return stack["user"].call("POST", "/v1/chat/completions", body, key=key or stack["key"], raw=stream)


def usage_rows(stack) -> list[dict[str, Any]]:
    return stack["store"].all("SELECT * FROM usage ORDER BY id")


# --- sign-in and keys ------------------------------------------------------------------------
def test_device_login_gives_a_key_the_gateway_url_and_the_models(stack):
    login = stack["login"]
    assert login["api_key"].startswith("dcm-") and login["base_url"].endswith("/v1")
    assert [m["id"] for m in login["models"]] == ["coder"] and login["models"][0]["context_length"] == 4000
    status, models = stack["user"].call("GET", "/v1/models", key=stack["key"])
    assert status == 200 and models["data"][0]["max_model_len"] == 4000
    # only a hash is stored, and the upstream key never comes back from the admin API
    assert stack["key"] not in json.dumps(stack["store"].all("SELECT * FROM api_keys"))
    assert "up-secret" not in json.dumps(stack["admin"].call("GET", "/api/admin/models")[1])


def test_wrong_passwords_and_bad_keys(stack):
    client = stack["user"]
    status, body = client.call("POST", "/api/device/login", {"username": "sara", "password": "nope"})
    assert status == 401 and "Wrong username or password" in body["error"]
    status, body = client.call("GET", "/v1/models", key="dcm-not-a-real-key-at-all-xxxxxxxxxxxxxxxx")
    assert status == 401 and body["error"]["code"] == "invalid_api_key"
    for _ in range(8):
        client.call("POST", "/api/login", {"username": "sara", "password": "wrong"})
    status, body = client.call("POST", "/api/login", {"username": "sara", "password": "sara-pass-1"})
    assert status == 429  # paused after too many failures, even with the right password


def test_cookie_changes_need_the_manager_header(stack):
    admin = stack["admin"]
    status, body = admin.call("POST", "/api/admin/users", {"username": "x", "password": "long-enough"},
                              headers={"X-Dcode-Manager": ""})
    assert status == 403
    status, _ = admin.call("POST", "/api/admin/users", {"username": "x", "password": "long-enough"})
    assert status == 200


def test_users_cant_reach_admin_routes_and_keys_only_reach_their_own(stack):
    user = stack["user"]
    user.login("sara", "sara-pass-1")
    assert user.call("GET", "/api/admin/users")[0] == 403
    assert user.call("GET", "/api/me")[1]["user"]["username"] == "sara"
    keyed = type(user)(stack["port"])
    assert keyed.call("GET", "/api/me/usage", key=stack["key"])[0] == 200
    assert keyed.call("GET", "/api/admin/users", key=stack["key"])[0] == 401
    assert keyed.call("POST", "/api/me/keys", {"name": "x"}, key=stack["key"])[0] == 401


def test_keys_can_be_made_and_revoked(stack):
    user = stack["user"]
    user.login("sara", "sara-pass-1")
    status, made = user.call("POST", "/api/me/keys", {"name": "ci"})
    assert status == 200 and made["api_key"].startswith("dcm-")
    assert chat(stack, key=made["api_key"])[0] == 200
    names = [k["name"] for k in user.call("GET", "/api/me/keys")[1]["keys"]]
    assert "ci" in names and "Dcode on laptop" in names
    assert user.call("DELETE", f"/api/me/keys/{made['id']}")[0] == 200
    status, body = chat(stack, key=made["api_key"])
    assert status == 401 and "revoked" in body["error"]["message"]


def test_the_last_admin_stays(stack):
    admin = stack["admin"]
    admin_id = stack["store"].user_by_name("admin")["id"]
    status, body = admin.call("PATCH", f"/api/admin/users/{admin_id}", {"role": "user"})
    assert status == 400 and "last admin" in body["error"]
    assert admin.call("DELETE", f"/api/admin/users/{admin_id}")[0] == 400


# --- the gateway ---------------------------------------------------------------------------------
def test_a_chat_goes_to_the_model_server_with_its_own_name_and_key(stack):
    status, body = chat(stack, "write a test", max_tokens=9000)
    assert status == 200 and body["model"] == "coder"  # the public name, not the server's
    sent = stack["upstream"].requests[-1]
    assert sent["body"]["model"] == "up-model" and sent["auth"] == "Bearer up-secret"
    assert sent["body"]["max_tokens"] == 500  # capped to the model's max output
    row = usage_rows(stack)[-1]
    assert (row["prompt_tokens"], row["completion_tokens"], row["status"], row["estimated"]) == (40, 8, 200, 0)
    assert row["client"] == "Dcode 0.6.0"  # what made the key, when the request doesn't say


def test_streaming_is_relayed_and_counted(stack):
    status, raw = chat(stack, "stream it", stream=True)
    text = raw.decode()
    assert status == 200 and "Hell" in text and text.rstrip().endswith("data: [DONE]")
    assert '"usage"' not in text  # the client didn't ask for the usage chunk the gateway added
    assert ": PROCESSING" in text
    row = usage_rows(stack)[-1]
    assert (row["prompt_tokens"], row["completion_tokens"], row["stream"], row["estimated"]) == (50, 9, 1, 0)
    assert row["ttft_ms"] is not None and row["ttft_ms"] <= row["latency_ms"]


def test_a_server_that_refuses_stream_options_is_learned_and_estimated(stack):
    stack["upstream"].reject_stream_options = True
    for _ in range(2):
        status, raw = chat(stack, "stream it", stream=True)
        assert status == 200 and b"Hell" in raw
    sent = [r["body"] for r in stack["upstream"].requests]
    assert "stream_options" in sent[0] and "stream_options" not in sent[1] and "stream_options" not in sent[2]
    row = usage_rows(stack)[-1]
    assert row["estimated"] == 1 and row["completion_tokens"] > 0


def test_unknown_and_turned_off_models(stack):
    status, body = stack["user"].call("POST", "/v1/chat/completions", {"model": "nope", "messages": []},
                                      key=stack["key"])
    assert status == 404 and "does not exist" in body["error"]["message"]
    stack["admin"].call("PATCH", "/api/admin/models/coder", {"enabled": False})
    status, body = chat(stack)
    assert status == 403 and body["error"]["code"] == "model_disabled"
    assert stack["user"].call("GET", "/v1/models", key=stack["key"])[1]["data"] == []


def test_context_overflow_reads_like_vllm(stack):
    status, body = chat(stack, "x" * 20000)
    assert status == 400 and "maximum context length is 4000 tokens" in body["error"]["message"]
    chat(stack, "y" * 13000, max_tokens=400)
    assert stack["upstream"].requests[-1]["body"]["max_tokens"] < 400  # trimmed to fit


# --- access and limits -------------------------------------------------------------------------------------
def test_selected_access_and_an_explicit_deny(stack):
    admin, sara = stack["admin"], stack["store"].user_by_name("sara")
    admin.call("PATCH", "/api/admin/models/coder", {"access": "selected"})
    status, body = chat(stack)
    assert status == 403 and "don't have access" in body["error"]["message"]
    admin.call("PUT", f"/api/admin/users/{sara['id']}/access", {"coder": {"allowed": True}})
    assert chat(stack)[0] == 200
    admin.call("PATCH", "/api/admin/models/coder", {"access": "all"})
    admin.call("PUT", f"/api/admin/users/{sara['id']}/access", {"coder": {"allowed": False}})
    assert chat(stack)[0] == 403


def test_token_limits_reset_by_the_admin(stack):
    admin, sara = stack["admin"], stack["store"].user_by_name("sara")
    admin.call("PUT", f"/api/admin/users/{sara['id']}/access", {"coder": {"token_limit": 60, "period": "day"}})
    assert chat(stack)[0] == 200  # 48 tokens
    assert chat(stack)[0] == 200  # 96: over now, but checked before the request
    status, body = chat(stack)
    assert status == 429 and body["error"]["code"] == "token_limit"
    assert "96 of 60 this day" in body["error"]["message"] and "resets on" in body["error"]["message"]
    usage = stack["user"].call("GET", "/api/me/usage", key=stack["key"])[1]
    assert usage["models"][0]["used"] == 96 and usage["models"][0]["left"] == 0 and usage["models"][0]["resets_at"]
    admin.call("POST", f"/api/admin/users/{sara['id']}/reset-usage")
    assert chat(stack)[0] == 200


def test_a_total_limit_and_resetting_everyone(stack):
    admin, sara = stack["admin"], stack["store"].user_by_name("sara")
    admin.call("PATCH", f"/api/admin/users/{sara['id']}", {"token_limit": 40, "period": "none"})
    chat(stack)
    status, body = chat(stack)
    assert status == 429 and "across all models" in body["error"]["message"] and "An admin can reset it" in \
        body["error"]["message"]
    admin.call("POST", "/api/admin/reset-usage")
    assert chat(stack)[0] == 200


def test_period_bounds_follow_the_time_zone():
    tz = limits.zone("Asia/Tehran")
    now = datetime(2026, 10, 16, 1, 30, tzinfo=tz).timestamp()  # a Friday
    start, end = limits.period_bounds("day", now, tz)
    assert datetime.fromtimestamp(start, tz).hour == 0 and end - start == 86400
    start, end = limits.period_bounds("week", now, tz)
    assert datetime.fromtimestamp(start, tz).strftime("%a %d") == "Mon 12"
    start, end = limits.period_bounds("month", now, tz)
    assert datetime.fromtimestamp(start, tz).day == 1 and datetime.fromtimestamp(end, tz).month == 11
    assert limits.period_bounds("none", now, tz) == (0.0, None)


# --- guards ----------------------------------------------------------------------------------------------------
def set_guard(stack, **guard) -> None:
    status, body = stack["admin"].call("PATCH", "/api/admin/models/coder", {"guard": guard})
    assert status == 200, body


def test_block_patterns_refuse_and_are_logged(stack):
    set_guard(stack, block=[{"name": "prod database", "pattern": r"drop\s+table"}])
    status, body = chat(stack, "please DROP  TABLE users")
    assert status == 400 and "prod database" in body["error"]["message"]
    assert usage_rows(stack)[-1]["guard"] == "block:prod database"
    assert chat(stack, "select 1")[0] == 200


def test_secrets_are_masked_before_the_model_sees_them(stack):
    set_guard(stack, redact_secrets=True, system_prompt="Follow company policy.")
    chat(stack, "my key is sk-or-v1-abcdefghijklmnopqrstuvwxyz123456 and password: hunter2go")
    sent = stack["upstream"].requests[-1]["body"]["messages"]
    assert sent[0] == {"role": "system", "content": "Follow company policy."}
    assert "sk-or-v1" not in sent[1]["content"] and "hunter2go" not in sent[1]["content"]
    assert sent[1]["content"].count(guards.MASK) == 2


def test_output_masking_catches_a_match_split_across_chunks(stack):
    stack["upstream"].reply = "The internal host is db-prod-7.corp.local, use it carefully. " * 3
    set_guard(stack, mask_output=[r"db-prod-\d+\.corp\.local"])
    status, raw = chat(stack, "where is the db?", stream=True)
    text = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                   for line in raw.decode().split("\n\n") if line.startswith("data: {"))
    assert "db-prod" not in text and text.count(guards.MASK) == 3 and text.endswith("carefully. ")
    status, body = chat(stack, "again")
    assert "db-prod" not in body["choices"][0]["message"]["content"]


def test_requests_per_minute(stack):
    set_guard(stack, rpm=2)
    assert chat(stack)[0] == 200 and chat(stack)[0] == 200
    status, body = chat(stack)
    assert status == 429 and body["error"]["code"] == "rate_limit"


def test_a_guard_model_can_refuse(stack):
    upstream = stack["upstream"]
    stack["admin"].call("POST", "/api/admin/models", {"id": "guard", "upstream_url": upstream.url})
    set_guard(stack, guard_model="guard")
    upstream.reply = "unsafe\nS2"
    status, body = chat(stack, "something bad")
    assert status == 400 and "guard model" in body["error"]["message"]
    upstream.reply = "safe"
    assert chat(stack, "something fine")[0] == 200
    endpoints = [r["endpoint"] for r in usage_rows(stack)]
    assert endpoints.count("guard") == 2


def test_bad_guard_patterns_are_refused_when_saved(stack):
    status, body = stack["admin"].call("PATCH", "/api/admin/models/coder", {"guard": {"block": ["(unclosed"]}})
    assert status == 400 and "not a valid pattern" in body["error"]


# --- monitoring ---------------------------------------------------------------------------------------------------
def test_monitoring_overview(stack):
    chat(stack)
    chat(stack, stream=True)
    stack["admin"].call("PATCH", "/api/admin/models/coder", {"guard": {"block": ["forbidden"]}})
    chat(stack, "forbidden words")
    status, data = stack["admin"].call("GET", "/api/admin/monitoring?range=24h")
    assert status == 200
    assert data["total"]["requests"] == 3 and data["total"]["refused"] == 1 and data["total"]["users"] == 1
    model = data["models"][0]
    assert model["model"] == "coder" and model["p50_ms"] is not None and model["ttft_ms"] is not None
    assert sum(data["requests_series"]) == 3 and data["users"][0]["user"] == "sara"
    assert data["problems"][0]["guard"] == "block:forbidden"
    live = stack["admin"].call("GET", "/api/admin/monitoring/live")[1]["models"][0]
    assert live["up"] is True and live["serves"] == ["up-model"]
    clients = stack["admin"].call("GET", "/api/admin/clients")[1]["clients"]
    assert clients[0]["username"] == "sara" and clients[0]["name"] == "Dcode on laptop"


def test_vllm_metrics_are_read():
    text = ('# HELP x\nvllm:num_requests_running{model_name="g"} 2.0\nvllm:num_requests_waiting{model_name="g"} 1\n'
            'vllm:kv_cache_usage_perc{model_name="g"} 0.42\nvllm:generation_tokens_total{model_name="g"} 1234\n'
            'other_metric 5\n')
    assert monitoring.parse_metrics(text) == {"running": 2.0, "waiting": 1.0, "kv_cache": 0.42,
                                               "generation_tokens_total": 1234.0}


def test_model_test_reports_the_server(stack):
    status, out = stack["admin"].call("POST", "/api/admin/models/coder/test")
    assert status == 200 and out["status"] == 200 and out["listed"][0]["id"] == "up-model" and out["answer"]


# --- discovery ----------------------------------------------------------------------------------------------------
VLLM_DEPLOYMENT = {
    "metadata": {"name": "vllm-gemma4"},
    "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "vllm-gemma4"}}, "template": {
        "metadata": {"labels": {"app": "vllm-gemma4"}},
        "spec": {"containers": [{"name": "vllm", "image": "vllm/vllm-openai:gemma4-cu130", "args": [
            "--model", "/models/gemma4", "--served-model-name", "gemma4:31b", "--max-model-len", "16384",
            "--port", "8010"]}], "volumes": [{"name": "w", "hostPath": {"path": "/home/admin1/gemma4"}}]}}},
    "status": {"readyReplicas": 1},
}


class FakeKube:
    def __init__(self) -> None:
        self.patches: list[tuple[str, Any]] = []
        self.deployment = json.loads(json.dumps(VLLM_DEPLOYMENT))

    def get(self, path: str) -> dict[str, Any]:
        if path.endswith("/deployments"):
            return {"items": [self.deployment, {"metadata": {"name": "web"}, "spec": {"template": {
                "metadata": {"labels": {"app": "web"}}, "spec": {"containers": [{"name": "nginx", "image": "nginx"}]}}}}]}
        if path.endswith("/services"):
            return {"items": [
                {"metadata": {"name": "vllm-gemma4-nodeport"}, "spec": {"type": "NodePort", "selector": {
                    "app": "vllm-gemma4"}, "ports": [{"port": 8010}]}},
                {"metadata": {"name": "vllm-gemma4"}, "spec": {"selector": {"app": "vllm-gemma4"},
                                                               "ports": [{"port": 8010}]}}]}
        return self.deployment

    def request(self, method: str, path: str, body: Any = None, content_type: str = "") -> dict[str, Any]:
        self.patches.append((path, body))
        return {}


def test_discovery_finds_vllm_deployments(monkeypatch):
    monkeypatch.setattr(discovery, "probe", lambda url, key="": ([{"id": "gemma4:31b", "max_model_len": 16384}], ""))
    found = discovery.discover(FakeKube(), ["vllm"], [])
    assert len(found) == 1  # nginx isn't a model server
    item = found[0]
    assert item["url"] == "http://vllm-gemma4.vllm.svc:8010/v1"  # the ClusterIP service, not the NodePort
    assert item["args"]["served_name"] == "gemma4:31b" and item["args"]["max_model_len"] == 16384
    assert item["served"][0]["id"] == "gemma4:31b" and item["host_paths"] == ["/home/admin1/gemma4"]


def test_server_args_from_a_shell_command():
    args = discovery.server_args({"command": ["sh", "-c", "vllm serve /m/qwen --max-model-len 32768 --port 8000"]})
    assert args["model_path"] == "/m/qwen" and args["max_model_len"] == 32768


def test_max_model_len_is_replaced_or_added():
    assert discovery.with_max_model_len(["--model", "m", "--max-model-len", "8192"], 16384) == [
        "--model", "m", "--max-model-len", "16384"]
    assert discovery.with_max_model_len(["--max-model-len=8192"], 4096) == ["--max-model-len=4096"]
    assert discovery.with_max_model_len(["--model", "m"], 4096) == ["--model", "m", "--max-model-len", "4096"]
    assert discovery.with_max_model_len(["sh", "-c", "vllm serve m --max-model-len 8192"], 4096) == [
        "sh", "-c", "vllm serve m --max-model-len 4096"]


def test_scaling_and_resizing_patch_the_deployment():
    kube = FakeKube()
    discovery.scale(kube, "vllm", "vllm-gemma4", 0)
    assert kube.patches[-1] == ("/apis/apps/v1/namespaces/vllm/deployments/vllm-gemma4/scale", {"spec": {"replicas": 0}})
    words = discovery.set_max_model_len(kube, "vllm", "vllm-gemma4", "vllm", 8192)
    assert words[words.index("--max-model-len") + 1] == "8192"
    patch = kube.patches[-1][1]["spec"]["template"]["spec"]["containers"][0]
    assert patch["name"] == "vllm" and "8192" in patch["args"]


def test_weights_on_disk(tmp_path):
    folder = tmp_path / "gemma4"
    folder.mkdir()
    (folder / "config.json").write_text(json.dumps({"architectures": ["Gemma4ForCausalLM"],
                                                    "text_config": {"max_position_embeddings": 131072}}))
    (folder / "model-00001.safetensors").write_bytes(b"0" * 100)
    (tmp_path / "qwen.gguf").write_bytes(b"0" * 10)
    found = {w["name"]: w for w in discovery.scan_weights(str(tmp_path), [
        {"args": {"model_path": "/models/gemma4"}, "host_paths": []}])}
    assert found["gemma4"]["architecture"] == "Gemma4ForCausalLM" and found["gemma4"]["context"] == 131072
    assert found["gemma4"]["served"] and found["gemma4"]["size"] == 100
    assert found["qwen"]["format"] == "gguf" and not found["qwen"]["served"]


# --- the rest ------------------------------------------------------------------------------------------------------
def test_the_first_admin_from_the_environment(tmp_path, monkeypatch):
    store = Store(tmp_path / "m.db")
    monkeypatch.setenv("DCODE_MANAGER_ADMIN_PASSWORD", "from-a-secret")
    assert bootstrap_admin(store) == "admin"
    assert bootstrap_admin(store) == ""  # only once
    assert auth.authenticate(store, "admin", "from-a-secret")["role"] == "admin"


def test_the_ui_is_served_with_security_headers(stack):
    conn = http.client.HTTPConnection("127.0.0.1", stack["port"], timeout=5)
    conn.request("GET", "/")
    response = conn.getresponse()
    assert response.status == 200 and b"<html" in response.read()
    assert "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")
    for path, kind in (("/app.js", "javascript"), ("/admin.js", "javascript"), ("/app.css", "css"), ("/theme.css", "css"),
                       ("/models", "html")):  # the UI's own routes fall back to the page
        conn.request("GET", path)
        response = conn.getresponse()
        assert response.status == 200 and kind in response.getheader("Content-Type") and response.read(), path


def test_the_manager_imports_no_terminal_or_client_libraries():
    import subprocess
    import sys

    code = ("import sys, dcode.manager.server, dcode.manager.cli; "
            "bad = [m for m in ('openai', 'httpx', 'rich', 'click', 'prompt_toolkit') if m in sys.modules]; "
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == ""


def test_usage_times_are_recent(stack):
    chat(stack)
    assert abs(usage_rows(stack)[-1]["ts"] - time.time()) < 30


# --- reasoning, translated per model ---------------------------------------------------------------------------
def test_reasoning_effort_reaches_each_server_in_its_own_form(stack):
    admin, upstream = stack["admin"], stack["upstream"]
    admin.call("PATCH", "/api/admin/models/coder", {"family": "qwen"})  # a public name that says nothing
    assert stack["user"].call("GET", "/v1/models", key=stack["key"])[1]["data"][0]["family"] == "qwen"
    chat(stack, reasoning_effort="none")
    sent = upstream.requests[-1]["body"]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False} and "reasoning_effort" not in sent
    admin.call("PATCH", "/api/admin/models/coder", {"family": "openai-reasoning"})
    chat(stack, reasoning_effort="high")
    assert upstream.requests[-1]["body"]["reasoning_effort"] == "high"
    chat(stack)  # nothing asked: nothing added
    assert "reasoning_effort" not in upstream.requests[-1]["body"]


def test_a_refused_reasoning_field_is_learned_per_model(stack):
    admin, upstream = stack["admin"], stack["upstream"]
    admin.call("PATCH", "/api/admin/models/coder", {"family": "qwen"})
    upstream.reject_fields = ["chat_template_kwargs"]
    assert chat(stack, reasoning_effort="low")[0] == 200
    assert chat(stack, reasoning_effort="low")[0] == 200
    bodies = [r["body"] for r in upstream.requests]
    assert "chat_template_kwargs" in bodies[0] and "chat_template_kwargs" not in bodies[1]
    assert "chat_template_kwargs" not in bodies[2]  # remembered: one try per request, not two
    assert stack["store"].model("coder")["learned"]["rejected"] == ["chat_template_kwargs"]


def test_a_model_that_must_think_is_left_thinking(stack):
    admin, upstream = stack["admin"], stack["upstream"]
    admin.call("PATCH", "/api/admin/models/coder", {"family": "qwen"})
    upstream.must_think = True
    assert chat(stack, reasoning_effort="none")[0] == 200
    assert chat(stack, reasoning_effort="none")[0] == 200
    assert "chat_template_kwargs" not in upstream.requests[-1]["body"]
    assert len(upstream.requests) == 3 and stack["store"].model("coder")["learned"]["must_think"] is True
    chat(stack, reasoning_effort="high")  # the effort itself still goes
    assert upstream.requests[-1]["body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_one_pod_with_two_servers_gives_two_entries_and_plain_web_servers_are_left_out(monkeypatch):
    """jina-stack on the real cluster: one container serving embeddings on 8011 and a reranker on 8010."""
    kube = FakeKube()
    jina = {"metadata": {"name": "jina-stack"}, "spec": {"replicas": 1, "template": {
        "metadata": {"labels": {"app": "jina-stack"}},
        "spec": {"containers": [{"name": "jina", "image": "vllm/vllm-openai:gemma4-cu130", "command": ["bash", "/app/launch.sh"]}]}}},
        "status": {"readyReplicas": 1}}
    web = {"metadata": {"name": "web"}, "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": "web"}},
           "spec": {"containers": [{"name": "nginx", "image": "nginx:1.27"}]}}}, "status": {"readyReplicas": 1}}
    services = [{"metadata": {"name": n}, "spec": {"selector": {"app": a}, "ports": [{"port": 8000}]}}
                for n, a in (("jina-embeddings", "jina-stack"), ("vllm-jina-reranker", "jina-stack"), ("web", "web"))]
    kube.get = lambda path: {"items": [jina, web]} if path.endswith("/deployments") else {"items": services}
    answers = {"jina-embeddings": [{"id": "jina-embeddings-v3"}], "vllm-jina-reranker": [{"id": "jina-reranker"}]}
    monkeypatch.setattr(discovery, "probe", lambda url, key="": (next((v for k, v in answers.items() if k in url), []),
                                                                  "" if "jina" in url else "404"))
    found = discovery.discover(kube, ["vllm"], [])
    assert [(f["service"], f["served"][0]["id"]) for f in found] == [
        ("jina-embeddings", "jina-embeddings-v3"), ("vllm-jina-reranker", "jina-reranker")]



def test_dcode_login_takes_the_key_and_models_and_keeps_other_settings(stack, monkeypatch):
    from conftest import make_config

    from dcode import main, manager_client, ui
    from dcode.config import load_config, save_config

    before = make_config(model="old-model")
    before.permissions["run_command"] = False
    save_config(before)
    stack["admin"].call("POST", "/api/admin/models", {"id": "other", "upstream_url": stack["upstream"].url,
                                                      "context_length": 16384, "family": "glm"})
    answers = iter(["sara-pass-1"])
    monkeypatch.setattr(ui, "prompt_input", lambda *a, **k: next(answers))
    monkeypatch.setattr(ui, "select_model", lambda models, selected=None: "other")
    monkeypatch.setattr(ui, "notice", lambda *a, **k: None)
    url = f"http://127.0.0.1:{stack['port']}"
    config = main.manager_setup(url, "sara")
    saved = load_config()
    assert saved.base_url == url + "/v1" and saved.api_key.startswith("dcm-") and saved.model == "other"
    assert saved.context_window == 16384 and saved.manager["models"]["other"]["family"] == "glm"
    assert saved.permissions["run_command"] is False  # the rest of the settings stayed
    assert manager_client.is_manager(config)
    lines = manager_client.usage_lines(manager_client.usage(config))
    assert lines[0].startswith("All models: 0 tokens") and any(line.startswith("other:") for line in lines)


def test_tls_trusts_the_hosts_ca_bundle_even_when_openssl_looks_elsewhere(monkeypatch):
    """The AlmaLinux-built binary's OpenSSL found no CA store on Ubuntu: every HTTPS model server failed."""
    import os

    from dcode.manager import tls

    bundle = next((p for p in tls.SYSTEM_BUNDLES if os.path.isfile(p)), None)
    if bundle is None:
        pytest.skip("no system CA bundle on this machine")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")  # what the frozen OpenSSL effectively sees
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/cert.pem")
    monkeypatch.setattr(tls, "SYSTEM_BUNDLES", ())
    monkeypatch.setenv("DCODE_MANAGER_CA_BUNDLE", bundle)
    assert tls._build().cert_store_stats()["x509_ca"] > 10


# --- joining a usage row to the client's task --------------------------------------------------------------

def test_a_request_keeps_its_task_id_and_the_admin_log_can_filter_by_it(stack):
    body = {"model": "coder", "messages": [{"role": "user", "content": "hi"}]}
    send = lambda task: stack["user"].call("POST", "/v1/chat/completions", body, key=stack["key"], headers=task)  # noqa: E731
    assert send({"X-Dcode-Task": "t-abc123"})[0] == 200
    assert send({})[0] == 200
    rows = stack["admin"].call("GET", "/api/admin/requests")[1]["requests"]
    assert [r["task_id"] for r in rows] == [None, "t-abc123"]  # nullable: other clients send nothing
    only = stack["admin"].call("GET", "/api/admin/requests?task=t-abc123")[1]["requests"]
    assert len(only) == 1 and only[0]["task_id"] == "t-abc123"


def test_a_task_id_cant_smuggle_anything_into_the_log(stack):
    body = {"model": "coder", "messages": [{"role": "user", "content": "hi"}]}
    stack["user"].call("POST", "/v1/chat/completions", body, key=stack["key"], headers={"X-Dcode-Task": "x'; DROP TABLE usage;--" + "y" * 90})
    row = stack["admin"].call("GET", "/api/admin/requests")[1]["requests"][0]
    assert len(row["task_id"]) == 64 and set(row["task_id"]) <= set("x_DROPTABLEusage;-y") - {";"}


def test_the_task_id_column_is_added_to_an_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, ts REAL NOT NULL, user_id INTEGER, model_id TEXT NOT NULL, "
                "key_id INTEGER, endpoint TEXT NOT NULL DEFAULT 'chat', status INTEGER NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0, "
                "completion_tokens INTEGER NOT NULL DEFAULT 0, latency_ms REAL NOT NULL DEFAULT 0, ttft_ms REAL, stream INTEGER NOT NULL DEFAULT 0, "
                "estimated INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', guard TEXT NOT NULL DEFAULT '', client TEXT NOT NULL DEFAULT '')")
    old.execute("INSERT INTO usage (ts, model_id, status, prompt_tokens) VALUES (1.0, 'coder', 200, 42)")
    old.commit()
    old.close()
    store = Store(path)
    assert store.all("SELECT prompt_tokens, task_id FROM usage") == [{"prompt_tokens": 42, "task_id": None}]
