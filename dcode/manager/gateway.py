"""The model gateway: OpenAI-compatible ``/v1`` in front of every model server.

Each request: find the model → is it on? → may this user use it, within their limits and the model's
requests per minute? → guards → fit it to the model's context → send it to the model's server (its
own name and key) → relay the answer (streamed or not), masking what the guard says to mask → write
one usage row with tokens, latency and time to first token.

Errors use OpenAI's shape (``{"error": {"message", "type", "code"}}``) and the wording clients
already understand: a context overflow reads like vLLM's, so Dcode compacts instead of failing.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .. import families
from . import guards, limits, tls
from .store import Store

CHARS_PER_TOKEN = 3.5
# An upstream that refuses turning reasoning off (GLM 5.3 on OpenRouter: "Reasoning is mandatory…").
_MUST_THINK_RE = re.compile(
    r"(reasoning|thinking)\b.{0,60}\b(is mandatory|cannot be (disabled|turned off)|can'?t be (disabled|turned off)"
    r"|must be enabled|is required)|\b(mandatory|required)\b.{0,30}\b(reasoning|thinking)\b", re.IGNORECASE)
UPSTREAM_TIMEOUT = 600.0
GUARD_TIMEOUT = 30.0


class GatewayError(Exception):
    def __init__(self, status: int, message: str, code: str = "", kind: str = "invalid_request_error",
                 retry_after: int | None = None, guard: str = "") -> None:
        super().__init__(message)
        self.status, self.message, self.code, self.kind, self.retry_after = status, message, code, kind, retry_after
        self.guard = guard

    def body(self) -> bytes:
        return json.dumps({"error": {"message": self.message, "type": self.kind, "code": self.code or None}}).encode()


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return int(len(text) / CHARS_PER_TOKEN) + 1


def join_url(base: str, suffix: str) -> str:
    """http://svc:8010/v1 + /chat/completions (the part after /v1 in the client's path)."""
    return base.rstrip("/") + "/" + suffix.lstrip("/")


def open_upstream(url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise GatewayError(500, f"The model's server address is not a URL: {url}", "bad_upstream", "server_error")
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=timeout, context=tls.context())
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    path = parts.path or "/"
    return conn, path + (f"?{parts.query}" if parts.query else "")


def upstream_headers(model: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "User-Agent": "dcode-manager"}
    if model.get("upstream_key"):
        headers["Authorization"] = f"Bearer {model['upstream_key']}"
    return headers


def clean_task_id(value: str | None) -> str | None:
    """A client's header, so not to be trusted: letters, digits and . _ : - only, 64 at most; None when empty."""
    cleaned = re.sub(r"[^A-Za-z0-9._:-]", "_", (value or "").strip())[:64]
    return cleaned or None


@dataclass
class Call:
    """One request through the gateway, filled in as it goes; becomes one usage row."""
    user: dict[str, Any]
    key: dict[str, Any] | None
    model_id: str
    endpoint: str
    client: str = ""
    started: float = field(default_factory=time.perf_counter)
    status: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ttft_ms: float | None = None
    stream: bool = False
    estimated: bool = False
    error: str = ""
    guard: str = ""
    recorded: bool = False
    added: list[str] = field(default_factory=list)  # request fields the gateway added (reasoning, in the model's form)
    task_id: str | None = None                      # the client's X-Dcode-Task: joins this row to the client's own trace


class Gateway:
    def __init__(self, store: Store) -> None:
        self.store = store

    # --- listing ----------------------------------------------------------------------
    def visible_models(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        rows = {r["model_id"]: r for r in self.store.access_rows(user_id=user["id"])}
        return [m for m in self.store.models() if m["enabled"] and limits.allowed(m, rows.get(m["id"]), user)]

    def list_models(self, user: dict[str, Any]) -> dict[str, Any]:
        data = []
        for model in self.visible_models(user):
            item = {"id": model["id"], "object": "model", "created": int(model["created"]), "owned_by": "dcode-manager",
                    "kind": model["kind"], "description": model["description"], "family": family_of(model).key}
            if model.get("context_length"):
                item["max_model_len"] = item["context_length"] = model["context_length"]
            if model.get("max_output_tokens"):
                item["max_output_tokens"] = model["max_output_tokens"]
            data.append(item)
        return {"object": "list", "data": data}

    # --- one request --------------------------------------------------------------------
    def prepare(self, user: dict[str, Any], body: dict[str, Any], endpoint: str, call: Call | None = None
                ) -> tuple[dict[str, Any], guards.Guard]:
        """Every check before the model is called; returns the model and its guard, or raises GatewayError.
        ``body`` is changed in place into what the model's server gets."""
        model_id = str(body.get("model") or "")
        model = self.store.model(model_id) if model_id else None
        if model is None:
            raise GatewayError(404, f"The model `{model_id}` does not exist on this manager.", "model_not_found")
        if not model["enabled"]:
            raise GatewayError(403, f"{model_id} is turned off by an admin right now.", "model_disabled")
        settings = self.store.settings()
        refusal = limits.check(self.store, user, model, settings)
        if refusal:
            if refusal.startswith("Token limit"):
                raise GatewayError(429, refusal, "token_limit", "insufficient_quota")
            raise GatewayError(403, refusal, "no_access", "permission_error")
        guard = guards.load(model.get("guard"))
        if guard.rpm:
            recent = self.store.one("SELECT COUNT(*) AS n FROM usage WHERE user_id = ? AND model_id = ? AND ts > ?",
                                    (user["id"], model_id, time.time() - 60))["n"]
            if recent >= guard.rpm:
                raise GatewayError(429, f"Too many requests to {model_id}: at most {guard.rpm} a minute.",
                                   "rate_limit", "rate_limit_error", retry_after=20)
        refusal, guard_name, _masked = guards.check_request(guard, body)
        if refusal:
            raise GatewayError(400, refusal, "guard_blocked", guard=guard_name)
        if endpoint == "chat":
            self._fit(model, body)
            added = self._reasoning(model, body)
            if call is not None:
                call.added = added
        body["model"] = model.get("upstream_model") or model_id
        return model, guard

    def _reasoning(self, model: dict[str, Any], body: dict[str, Any]) -> list[str]:
        """Clients send one ``reasoning_effort``; each model's server gets it its own way (see families.py)."""
        if "reasoning_effort" not in body:
            return []
        effort = str(body.pop("reasoning_effort") or "medium")
        learned = model.get("learned") or {}
        if effort == "none" and learned.get("must_think"):
            return []
        cap = body.get("max_tokens") or body.get("max_completion_tokens")
        extras = families.request_extras(family_of(model), families.server_kind(model["upstream_url"]), effort,
                                         cap if isinstance(cap, int) else None)
        rejected = set(learned.get("rejected") or [])
        added = []
        for key, value in extras.items():
            if key not in rejected:
                body[key] = value
                added.append(key)
        return added

    def _fit(self, model: dict[str, Any], body: dict[str, Any]) -> None:
        """Cap the reply length and refuse what can't fit the model's context, the way vLLM words it."""
        cap = model.get("max_output_tokens")
        key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
        asked = body.get(key)
        if cap and (not isinstance(asked, int) or asked > cap):
            body[key] = cap
        window = model.get("context_length")
        if not window:
            return
        prompt = estimate_tokens(body.get("messages") or "") + estimate_tokens(body.get("tools") or "")
        if prompt >= window:
            raise GatewayError(400, (
                f"This model's maximum context length is {window} tokens. However, your request has about "
                f"{prompt} input tokens. Please reduce the length of the input messages."), "context_length_exceeded")
        asked = body.get(key)
        if isinstance(asked, int) and prompt + asked > window:
            body[key] = max(16, window - prompt)

    def check_with_guard_model(self, guard: guards.Guard, body: dict[str, Any], user: dict[str, Any]) -> None:
        if not guard.guard_model:
            return
        text = guards.last_user_text(body.get("messages") or [])
        if not text.strip():
            return
        model = self.store.model(guard.guard_model)
        if model is None or not model["enabled"]:
            raise GatewayError(503, f"This model's guard ({guard.guard_model}) is not available.", "guard_unavailable",
                               "server_error")
        call = Call(user=user, key=None, model_id=model["id"], endpoint="guard")
        answer = ""
        try:
            request = {"model": model.get("upstream_model") or model["id"], "messages": guards.guard_messages(guard, text),
                       "max_tokens": 16, "temperature": 0}
            status, payload = self._post_json(model, "/chat/completions", request, GUARD_TIMEOUT)
            call.status = status
            if status == 200:
                answer = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                usage = payload.get("usage") or {}
                call.prompt_tokens, call.completion_tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            else:
                call.error = str(payload)[:300]
        except OSError as exc:
            call.status, call.error = 502, str(exc)[:300]
        finally:
            self.record(call)
        if call.status != 200:
            raise GatewayError(503, f"This model's guard ({guard.guard_model}) did not answer.", "guard_unavailable",
                               "server_error")
        if guards.verdict_unsafe(answer):
            raise GatewayError(400, "Refused by this model's guard model.", "guard_blocked",
                               guard=f"model:{guard.guard_model}")

    def _post_json(self, model: dict[str, Any], suffix: str, body: dict[str, Any], timeout: float
                   ) -> tuple[int, dict[str, Any]]:
        conn, path = open_upstream(join_url(model["upstream_url"], suffix), timeout)
        try:
            conn.request("POST", path, body=json.dumps(body).encode(), headers=upstream_headers(model))
            response = conn.getresponse()
            raw = response.read()
            try:
                return response.status, json.loads(raw or b"{}")
            except ValueError:
                return response.status, {"raw": raw[:500].decode(errors="replace")}
        finally:
            conn.close()

    # --- usage --------------------------------------------------------------------------------
    def record(self, call: Call) -> None:
        """Write the call's usage row (once). Done before the answer's last bytes go out, so a client's next
        request already sees these tokens against its limit."""
        if call.recorded:
            return
        call.recorded = True
        latency = (time.perf_counter() - call.started) * 1000
        self.store.run(
            "INSERT INTO usage (ts, user_id, model_id, key_id, endpoint, status, prompt_tokens, completion_tokens, "
            "latency_ms, ttft_ms, stream, estimated, error, guard, client, task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), call.user["id"] if call.user else None, call.model_id, call.key["id"] if call.key else None,
             call.endpoint, call.status, int(call.prompt_tokens or 0), int(call.completion_tokens or 0),
             round(latency, 1), None if call.ttft_ms is None else round(call.ttft_ms, 1), int(call.stream),
             int(call.estimated), call.error[:500], call.guard[:120], call.client[:120], call.task_id))

    def learn(self, model: dict[str, Any], **lessons: Any) -> None:
        learned = dict(model.get("learned") or {}) | lessons
        model["learned"] = learned
        self.store.run("UPDATE models SET learned = ? WHERE id = ?", (json.dumps(learned), model["id"]))

    # --- relaying ---------------------------------------------------------------------------
    def forward(self, handler: Any, call: Call, model: dict[str, Any], guard: guards.Guard, body: dict[str, Any],
                suffix: str) -> None:
        """Send ``body`` to the model's server and relay its answer to ``handler`` (a BaseHTTPRequestHandler)."""
        stream = bool(body.get("stream"))
        call.stream = stream
        added_usage = False
        if stream and call.endpoint == "chat" and not (model.get("learned") or {}).get("no_stream_usage"):
            options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
            if not options.get("include_usage"):
                body["stream_options"] = {**options, "include_usage": True}  # how usage is counted when streaming
                added_usage = True
        call.prompt_tokens = estimate_tokens(body.get("messages") or body.get("input") or body.get("prompt") or "")
        call.estimated = True
        for _ in range(2 + len(call.added)):
            conn, path = open_upstream(join_url(model["upstream_url"], suffix), UPSTREAM_TIMEOUT)
            try:
                conn.request("POST", path, body=json.dumps(body).encode(), headers=upstream_headers(model))
                response = conn.getresponse()
            except OSError as exc:
                conn.close()
                raise GatewayError(502, f"The server for {model['id']} isn't reachable: {exc}", "upstream_unreachable",
                                   "server_error") from exc
            if response.status < 400:
                break
            raw = response.read()
            conn.close()
            if added_usage and response.status == 400 and b"stream_options" in raw:
                self.learn(model, no_stream_usage=True)  # an older server: count streamed tokens by estimate
                body.pop("stream_options", None)
                added_usage = False
                continue
            if response.status == 400 and self._refused_field(model, call, body, raw.decode(errors="replace")):
                continue
            call.status, call.error = response.status, raw[:300].decode(errors="replace")
            self.record(call)
            _send(handler, response.status, raw, response.getheader("Content-Type") or "application/json")
            return
        call.status = response.status
        masker = guards.OutputMasker(guard.mask_output)
        try:
            if stream and "text/event-stream" in (response.getheader("Content-Type") or ""):
                self._relay_stream(handler, call, model, response, masker, added_usage)
            else:
                self._relay_json(handler, call, model, response, masker)
        finally:
            conn.close()
            if masker.masked:
                call.guard = (call.guard + f" masked:{masker.masked}").strip()

    def _relay_json(self, handler: Any, call: Call, model: dict[str, Any], response: Any,
                    masker: guards.OutputMasker) -> None:
        raw = response.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            _send(handler, response.status, raw, response.getheader("Content-Type") or "application/json")
            return
        if isinstance(payload, dict):
            usage = payload.get("usage") or {}
            if usage:
                call.prompt_tokens = usage.get("prompt_tokens") or usage.get("total_tokens") or call.prompt_tokens
                call.completion_tokens = usage.get("completion_tokens") or 0
                call.estimated = False
            for choice in payload.get("choices") or []:
                message = choice.get("message") or {}
                if isinstance(message.get("content"), str):
                    message["content"] = masker.whole(message["content"])
                if not usage:
                    call.completion_tokens += estimate_tokens(message.get("content") or "") + estimate_tokens(
                        message.get("tool_calls") or "") - 2
            if "model" in payload:
                payload["model"] = model["id"]
            raw = json.dumps(payload).encode()
        self.record(call)
        _send(handler, response.status, raw, "application/json")

    def _relay_stream(self, handler: Any, call: Call, model: dict[str, Any], response: Any,
                      masker: guards.OutputMasker, added_usage: bool) -> None:
        handler.send_response(response.status)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        out_chars = 0
        last: list[Any] = [None]

        def emit(chunk: dict[str, Any] | None, raw_line: bytes | None = None) -> None:
            data = raw_line if chunk is None else b"data: " + json.dumps(chunk).encode()
            handler.wfile.write(data + b"\n\n")
            handler.wfile.flush()

        try:
            while True:
                line = response.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                if not line.startswith(b"data:"):
                    if line.startswith(b":"):
                        emit(None, line)  # keep-alive comments (OpenRouter's "PROCESSING") keep idle clients connected
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    self._flush_masked(emit, masker, last[0], model)
                    if call.estimated:
                        call.completion_tokens = int(out_chars / CHARS_PER_TOKEN)
                    self.record(call)
                    emit(None, b"data: [DONE]")
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    emit(None, line)
                    continue
                usage = chunk.get("usage") if isinstance(chunk, dict) else None
                if usage:
                    call.prompt_tokens = usage.get("prompt_tokens") or call.prompt_tokens
                    call.completion_tokens = usage.get("completion_tokens") or 0
                    call.estimated = False
                choices = chunk.get("choices") if isinstance(chunk, dict) else None
                if not choices:
                    if usage and added_usage:
                        continue  # the client didn't ask for the usage chunk: don't hand it an empty choices list
                    emit(chunk)
                    continue
                delta = choices[0].get("delta") or {}
                produced = sum(len(json.dumps(delta.get(k))) for k in ("content", "reasoning_content", "reasoning",
                                                                       "tool_calls") if delta.get(k))
                if produced and call.ttft_ms is None:
                    call.ttft_ms = (time.perf_counter() - call.started) * 1000
                out_chars += produced
                if isinstance(delta.get("content"), str) and masker.patterns:
                    delta["content"] = masker.feed(delta["content"])
                chunk["model"] = model["id"]
                last[0] = chunk
                emit(chunk)
            if masker.pending:
                self._flush_masked(emit, masker, last[0], model)
        except (BrokenPipeError, ConnectionResetError):
            call.error = "the client disconnected"
        except OSError as exc:  # the model's server dropped the stream
            call.error = f"stream broke: {exc}"[:300]
        if call.estimated and not call.recorded:
            call.completion_tokens = int(out_chars / CHARS_PER_TOKEN)

    def _refused_field(self, model: dict[str, Any], call: Call, body: dict[str, Any], text: str) -> bool:
        """The server refused a reasoning field the gateway added: drop it, remember it for this model, retry."""
        if not call.added:
            return False
        if _MUST_THINK_RE.search(text):
            for key in call.added:
                body.pop(key, None)
            call.added = []
            self.learn(model, must_think=True)
            return True
        for key in list(call.added):
            value = body.get(key)
            names = [key] + (list(value) if isinstance(value, dict) else [])
            if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
                body.pop(key, None)
                call.added.remove(key)
                rejected = sorted(set((model.get("learned") or {}).get("rejected") or []) | {key})
                self.learn(model, rejected=rejected)
                return True
        return False

    @staticmethod
    def _flush_masked(emit: Any, masker: guards.OutputMasker, last: dict[str, Any] | None, model: dict[str, Any]) -> None:
        rest = masker.flush()
        if rest:
            emit({"id": (last or {}).get("id", "chatcmpl-flush"), "object": "chat.completion.chunk",
                  "created": int(time.time()), "model": model["id"],
                  "choices": [{"index": 0, "delta": {"content": rest}, "finish_reason": None}]})


def _send(handler: Any, status: int, raw: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def family_of(model: dict[str, Any]) -> families.Family:
    """The model's family: the admin's choice, else from the server's own model name (the public id may be "coder")."""
    return families.detect(model.get("upstream_model") or model["id"], model.get("upstream_url", ""),
                           model.get("family") or "auto")
