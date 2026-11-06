"""Thin, forgiving wrapper over OpenAI-compatible chat endpoints.

Local servers (Ollama, llama.cpp, vLLM, LM Studio, TGI) differ in what they
accept, and small open models are sloppy with tool calls. This module absorbs
those differences so the engine sees one clean interface:

* remembers which optional params (reasoning_effort, stream_options, streaming
  with tools) the server rejected, so we don't pay for a failed request each turn
* strips ``<think>…</think>`` blocks so reasoning text never re-enters context
* recovers tool calls a model wrote as text (``<tool_call>{…}</tool_call>``,
  fenced JSON, or a bare JSON object) when the server didn't parse them
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from . import families, manager_client, toolcalls, trace
from .config import AppConfig
from .context import CHARS_PER_TOKEN, TokenCounter


class Interrupted(Exception):
    """Raised when the user stops a running task."""


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0})
    recovered_tool_calls: bool = False
    finish_reason: str = ""  # "stop", "tool_calls", "length" (hit the output limit), …
    reasoning: str = ""      # the model's thinking: shown on request, never stored in the history
    notes: list[str] = field(default_factory=list)  # what the client did about the model's quirks, for the UI
    # Set by LLMClient.chat. ``usage`` is then the sum of every attempt behind this reply.
    calls: int = 1                  # HTTP attempts
    cached: int | None = None       # prompt tokens the server says it reused; None = it doesn't say
    usage_estimated: bool = False   # some attempt reported nothing, so its share is an estimate
    calibration_prompt: int | None = None  # the last attempt's reported prompt: only a real figure may tune the estimator

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_BODY_RE = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
# gpt-oss on a server without its parser: "<|channel|>analysis<|message|>…<|end|>…<|channel|>final<|message|>answer"
_HARMONY_THOUGHT_RE = re.compile(r"<\|channel\|>\s*analysis\s*<\|message\|>(.*?)(?:<\|end\|>|\Z)", re.DOTALL)
_HARMONY_FINAL = "<|channel|>final<|message|>"
_HARMONY_TOKEN_RE = re.compile(r"<\|(start|end|return|channel|message|constrain)\|>")

_TOOL_NAME_RE = re.compile(r"[A-Za-z_][\w.:-]*")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Names models trained on other agents use for Dcode's tools. Used only when the name isn't a real tool.
TOOL_ALIASES = {
    "run_command": ("bash", "shell", "sh", "terminal", "exec", "execute", "execute_command", "execute_bash",
                    "run_shell_command", "run_shell", "shell_command", "run_terminal_cmd", "run_terminal_command",
                    "exec_command", "container.exec", "run", "cmd", "run_bash", "bash_command", "command"),
    "read_file": ("read", "cat", "open_file", "view_file", "view", "file_read", "read_text_file", "get_file_contents"),
    "write_file": ("write", "create_file", "file_write", "save_file", "write_to_file"),
    "edit_file": ("edit", "str_replace", "replace_in_file", "apply_edit", "file_edit", "search_replace",
                  "modify_file", "edit_existing_file"),
    "list_files": ("ls", "list_dir", "list_directory", "dir", "find_files", "list_files_in_directory"),
    "search_in_files": ("grep", "search", "grep_search", "find_in_files", "search_files", "ripgrep", "rg",
                        "search_code", "codebase_search"),
    "delete_file": ("rm", "remove_file", "delete"),
    "create_directory": ("mkdir", "make_directory", "create_dir", "create_folder"),
}
_ALIAS = {alias: tool for tool, aliases in TOOL_ALIASES.items() for alias in aliases}


def clean_tool_name(name: str, known: Iterable[str]) -> str:
    """The real tool behind a messy name: leaked markup (``git_diff</｜DSML｜ parameter>``), a namespace
    (``functions.read_file:0``, ``default_api.read_file``) or another agent's name (``bash``, ``str_replace``)."""
    known = list(known)
    raw = (name or "").strip()
    if raw in known:
        return raw
    match = _TOOL_NAME_RE.match(raw)
    candidate = toolcalls.short_name(match.group(0) if match else raw, known)
    if candidate in known:
        return candidate
    for tool in sorted(known, key=len, reverse=True):
        if raw.startswith(tool):
            return tool
    for key in (candidate.lower(), candidate.lower().rsplit(".", 1)[-1].split(":", 1)[0]):
        if _ALIAS.get(key) in known:
            return _ALIAS[key]
    return candidate


def clean_arguments(arguments: str) -> str:
    """Keep valid JSON arguments; salvage a JSON object wrapped in junk; else ``{}``."""
    text = (arguments or "").strip() or "{}"
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, TypeError):
        pass
    match = _JSON_OBJECT_RE.search(text)
    if match:
        try:
            json.loads(match.group(0))
            return match.group(0)
        except json.JSONDecodeError:
            pass
    return text


_TRUE, _FALSE = {"true", "yes", "1", "on"}, {"false", "no", "0", "off", ""}


def coerce_arguments(arguments: str, schema: dict[str, Any] | None) -> str:
    """Give each argument its schema type: models send ``"true"``, ``"20"`` or a JSON string for an object,
    and ``bool("false")`` is True. Arguments that don't parse are left for the tool to report."""
    props = (schema or {}).get("properties") or {}
    try:
        args = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments
    if not props or not isinstance(args, dict):
        return arguments
    changed = False
    for key, value in list(args.items()):
        kind = (props.get(key) or {}).get("type")
        new = value
        if kind == "boolean" and not isinstance(value, bool):
            text = str(value).strip().lower()
            new = True if text in _TRUE else False if text in _FALSE else value
        elif kind in ("integer", "number") and isinstance(value, str):
            try:
                new = int(value.strip()) if kind == "integer" else float(value.strip())
            except ValueError:
                pass
        elif kind == "integer" and isinstance(value, float) and value.is_integer():
            new = int(value)
        elif kind == "string" and isinstance(value, (bool, int, float)):
            new = json.dumps(value) if isinstance(value, bool) else str(value)
        elif kind in ("object", "array") and isinstance(value, str) and value.strip()[:1] in "{[":
            try:
                new = json.loads(value)
            except json.JSONDecodeError:
                pass
        if new is not value:
            args[key], changed = new, True
    return json.dumps(args, ensure_ascii=False) if changed else arguments


def thinking_text(text: str) -> str:
    """The reasoning written into the answer: ``<think>`` blocks (an unclosed one runs to the end), the text
    before a lone ``</think>`` (templates that open the block in the prompt), gpt-oss analysis."""
    text = text or ""
    parts = [m.strip() for m in _THINK_BODY_RE.findall(text)]
    rest = _THINK_RE.sub("", text)
    if "</think>" in rest and "<think>" not in rest.lower():
        parts.insert(0, rest.rsplit("</think>", 1)[0].replace("</think>", "").strip())
    parts += [m.strip() for m in _HARMONY_THOUGHT_RE.findall(text)]
    return "\n\n".join(p for p in parts if p)


def strip_thinking(text: str) -> str:
    text = text or ""
    if _HARMONY_FINAL in text:
        text = text.rsplit(_HARMONY_FINAL, 1)[1]
    elif "<|channel|>" in text:
        text = _HARMONY_THOUGHT_RE.sub("", text)
    text = _HARMONY_TOKEN_RE.sub("", text) if "<|" in text else text
    text = _THINK_RE.sub("", text)
    # An unclosed <think> at the start means the whole thing was reasoning.
    if text.lstrip().lower().startswith("<think>"):
        return ""
    if "</think>" in text:  # the opening tag was in the prompt: everything before the close is reasoning
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


class _VisibleText:
    """Streams only the answer: text inside ``<think>`` (even half-received) is held back."""

    def __init__(self) -> None:
        self.full = ""
        self.sent = 0

    def feed(self, delta: str) -> str:
        self.full += delta
        if "<" not in self.full:  # the usual case: nothing to hold back, no rescan
            self.sent = len(self.full)
            return delta
        visible = _THINK_RE.sub("", self.full)
        cut = visible.lower().find("<think>")
        if cut >= 0:
            visible = visible[:cut]
        for size in range(min(len("<think>") - 1, len(visible)), 0, -1):  # a tag still arriving
            if "<think>".startswith(visible[-size:].lower()):
                visible = visible[:-size]
                break
        new = visible[self.sent:] if len(visible) > self.sent else ""
        self.sent = max(self.sent, len(visible))
        return new


def _make_call(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def parse_text_tool_calls(content: str, known: Iterable[str]) -> tuple[str, list[dict[str, Any]]]:
    """Extract tool calls embedded in assistant text, in any family's format. Returns (remaining_text, calls)."""
    remaining, found = toolcalls.parse(content, known)
    return remaining, [_make_call(name, args) for name, args in found]


THINK_SHARE = 0.75        # a reply whose reasoning passes this share of the output cap is cut and asked again
THINK_RETRIES = 2         # at most this many asks again with less thinking
LOOP_CHECK_EVERY = 2000   # chars of reasoning between checks for a thought going in circles
SIDE_JOB_ROOM = 2048      # max_tokens for a side job asked again because its thinking used the whole cap
BUSY_WAITS = (3, 8, 15)   # seconds to wait on a "rate limited / busy" reply (429, 503) before giving up
ECHO_CHARS = 8000         # of reasoning sent back to the families whose API asks for it
_EFFORT_ORDER = ["none", "low", "medium", "high"]
SAMPLING_FIELDS = ("temperature", "top_p", "seed")

_CONTEXT_ERROR_RE = re.compile(r"context (length|window)|maximum context|too many tokens|prompt is too long|"
                               r"reduce the length|input is too long|exceeds the (max|context)", re.IGNORECASE)
_MISSING_MODEL_RE = re.compile(r"model\b.*\b(not found|does not exist|not exist)|no such model|unknown model",
                               re.IGNORECASE)
_NO_TOOLS_RE = re.compile(r"does not support (tools|function)|tools? (is|are) not supported|"
                          r"(tool|function)[ _]?(use|calling|calls?|choice)\W+(is |are )?not (supported|enabled)|"
                          r"enable-auto-tool-choice|tool-call-parser|function calling is not", re.IGNORECASE)
_SYSTEM_ROLE_RE = re.compile(r"system (role|message|prompt)s? (is |are )?not (supported|allowed)|"
                             r"only (user|assistant) and (user|assistant) roles", re.IGNORECASE)
_ROLE_ORDER_RE = re.compile(r"must alternate|unexpected role|roles must|role .* after role|"
                            r"expected (an? )?(user|assistant) (message|role)", re.IGNORECASE)
_FIELD_WORDS = ("reasoning_effort", "chat_template_kwargs", "enable_thinking", "thinking", "think", "reasoning",
                "stream_options")
_SKIP_SIGNATURE = {"google": {"thought_signature": "skip_thought_signature_validator"}}

TEXT_TOOLS_PROMPT = (
    "\n\n# Tools\nTo use a tool, reply with one block per call and nothing after it:\n"
    '<tool_call>{"name": "TOOL", "arguments": {...}}</tool_call>\n'
    "Results come back in the next message. Tools (? = optional):\n"
)


class _Overthinking(Exception):
    """The reasoning ran too long, or in circles, before any answer."""

    def __init__(self, reason: str, turn: AssistantTurn) -> None:
        super().__init__(reason)
        self.reason, self.turn = reason, turn


def _looping(thought: str) -> bool:
    """The same passage, three times over: the model is thinking in circles."""
    tail = thought[-160:].strip()
    return len(tail) >= 80 and thought.count(tail) >= 3


def _short_id(call_id: str) -> str:
    """Mistral wants tool call ids of exactly 9 letters or digits; map ours the same way every time."""
    if re.fullmatch(r"[A-Za-z0-9]{9}", call_id or ""):
        return call_id
    return hashlib.sha1((call_id or "").encode()).hexdigest()[:9]


def _tool_line(tool: dict[str, Any]) -> str:
    function = tool.get("function") or {}
    params = (function.get("parameters") or {}).get("properties") or {}
    required = set((function.get("parameters") or {}).get("required") or [])
    args = ", ".join(name + ("" if name in required else "?") for name in params)
    return f"- {function.get('name', '')}({args}): {function.get('description', '')}"


def _as_text_tools(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For a server that takes no tool definitions: tools described in the prompt, calls and results as text."""
    listing = TEXT_TOOLS_PROMPT + "\n".join(_tool_line(t) for t in tools)
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            blocks = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append("<tool_call>" + json.dumps({"name": function.get("name", ""), "arguments": args},
                                                         ensure_ascii=False) + "</tool_call>")
            text = "\n".join(p for p in (str(message.get("content") or "").strip(), *blocks) if p)
            out.append({"role": "assistant", "content": text})
        elif role == "tool":
            out.append({"role": "user", "content": f"[result of {message.get('name') or 'the tool'}]\n"
                                                   f"{message.get('content') or ''}"})
        else:
            out.append(dict(message))
    if out and out[0].get("role") == "system":
        out[0]["content"] = str(out[0].get("content") or "") + listing
    else:
        out.insert(0, {"role": "system", "content": listing.strip()})
    return out


def _merge_system(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For a template without a system role: the instructions open the first user message."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    rest = [dict(m) for m in messages if m.get("role") != "system"]
    if not system:
        return rest
    for message in rest:
        if message.get("role") == "user":
            message["content"] = f"{system}\n\n---\n\n{message.get('content') or ''}"
            return rest
    return [{"role": "user", "content": system}, *rest]


def _alternate(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For a strict template: user and assistant take turns, so join neighbours and bridge tool → user."""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        last = out[-1] if out else None
        if last and role == last.get("role") == "user":
            last["content"] = f"{last.get('content') or ''}\n\n{message.get('content') or ''}"
            continue
        if last and role == "user" and last.get("role") == "tool":
            out.append({"role": "assistant", "content": "OK."})
        out.append(dict(message))
    return out


_MUST_THINK_RE = re.compile(
    r"(reasoning|thinking)\b.{0,60}\b(is mandatory|cannot be (disabled|turned off)|can'?t be (disabled|turned off)"
    r"|must be enabled|is required)|\b(mandatory|required)\b.{0,30}\b(reasoning|thinking)\b", re.IGNORECASE)


def _nested(usage: Any, section: str, key: str) -> int | None:
    """``usage.<section>.<key>``, an object or a dict depending on the SDK and the server; None when absent."""
    part = getattr(usage, section, None)
    if part is None:
        part = (getattr(usage, "model_extra", None) or {}).get(section)
    value = part.get(key) if isinstance(part, dict) else getattr(part, key, None)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class _Attempt:
    """One HTTP request and what is known about it so far."""

    def __init__(self, number: int, retry_reason: str, streamed: bool, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None, effort: str, max_tokens: int | None) -> None:
        self.number, self.retry_reason, self.streamed = number, retry_reason, streamed
        self.messages, self.tools, self.effort, self.max_tokens = messages, tools, effort, max_tokens
        self.request_ts = time.time()
        self.started = time.perf_counter()
        self.first_delta: float | None = None
        self.last_delta: float | None = None
        self.out_chars = 0
        self.answered = False      # the server took the request (a stream opened, or a reply came back)
        self.http_sends = 0        # more than 1: the SDK retried by itself, which nothing else would show
        self.http_statuses: list[int] = []
        self.prefix: dict[str, Any] = {}
        self.snapshot: str | None = None
        self.done = False


class _CallLog:
    """One chat() or complete_text(): every HTTP attempt under it, written and added up."""

    def __init__(self, client: LLMClient, purpose: str) -> None:
        self.client = client
        self.purpose = purpose
        scope = trace.current()
        self.next_reason = scope.retry_reason if scope else "none"
        self.tracer = trace.for_config(client.config)
        self.attempts = 0
        self.current: _Attempt | None = None
        self.totals: dict[str, Any] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "cached": None}
        self.estimated = False
        self.last_reported_prompt = 0   # the last attempt's own figure: what calibration compares with

    def begin(self, request: dict[str, Any], body: dict[str, Any], effort: str, max_tokens: int | None) -> None:
        self.finish()  # an attempt nobody closed
        attempt = _Attempt(self.attempts, self.next_reason, bool(request.get("stream")), request.get("messages") or [],
                           request.get("tools"), effort, max_tokens)
        self.attempts += 1
        self.next_reason = "none"
        self.current = attempt
        if self.tracer is not None:
            _watch_http(self.client)
            scope = trace.current()
            attempt.prefix = self.tracer.prefix(scope.agent_id if scope else "main", self.purpose, attempt.messages, attempt.tools)
            attempt.snapshot = self.tracer.snapshot({**request, **body})
        attempt.started = time.perf_counter()  # the hashing above is not the model's time

    def answered(self) -> None:
        if self.current is not None:
            self.current.answered = True

    def delta(self, chars: int) -> None:
        attempt = self.current
        if attempt is not None and chars:
            now = time.perf_counter()
            attempt.first_delta = attempt.first_delta or now
            attempt.last_delta = now
            attempt.out_chars += chars

    def add_output(self, chars: int) -> None:
        if self.current is not None:
            self.current.out_chars += chars

    def _estimate_prompt(self, attempt: _Attempt) -> int:
        return TokenCounter().messages(attempt.messages, attempt.tools)

    def finish(self, usage: Any = None, finish_reason: str = "", error: BaseException | None = None) -> None:
        """Close the current attempt: count it, and write its record when tracing."""
        attempt, self.current = self.current, None
        if attempt is None or attempt.done:
            return
        attempt.done = True
        try:
            self._close(attempt, usage, finish_reason, error)
        except Exception:
            pass  # bookkeeping must never fail a request

    def _close(self, attempt: _Attempt, usage: Any, finish_reason: str, error: BaseException | None) -> None:
        now = time.perf_counter()
        numbers = _usage_dict(usage) if usage is not None else {"prompt": 0, "completion": 0, "total": 0}
        reported = bool(numbers["prompt"] or numbers["completion"])
        cached = _nested(usage, "prompt_tokens_details", "cached_tokens") if usage is not None else None
        reasoning = _nested(usage, "completion_tokens_details", "reasoning_tokens") if usage is not None else None
        estimated_completion = int(attempt.out_chars / CHARS_PER_TOKEN)
        estimated_prompt: int | None = None
        if self.tracer is not None or (attempt.answered and not reported):
            estimated_prompt = self._estimate_prompt(attempt)
        if reported:
            counted_prompt, counted_completion, estimated = numbers["prompt"], numbers["completion"], False
        elif attempt.answered:  # cut off before the usage chunk, or a server that never sends one
            counted_prompt, counted_completion, estimated = int(estimated_prompt or 0), estimated_completion, True
        else:                   # refused: nothing was generated
            counted_prompt, counted_completion, estimated = 0, 0, False
        self.last_reported_prompt = numbers["prompt"]
        self.estimated = self.estimated or estimated
        total = numbers["total"] if reported and numbers["total"] else counted_prompt + counted_completion
        trace.add_usage(self.totals, {"prompt": counted_prompt, "completion": counted_completion,
                                      "total": total, "calls": 1, "cached": cached})
        if self.tracer is None:
            return
        scope = trace.current()
        client = self.client
        self.tracer.emit(
            "llm_call", purpose=self.purpose, step=scope.step if scope else None, attempt=attempt.number,
            retry_reason=attempt.retry_reason, model=client.config.model, server_kind=client.server,
            effort=attempt.effort, max_tokens=attempt.max_tokens, message_count=len(attempt.messages),
            tools_count=len(attempt.tools or []), estimated_prompt_tokens=estimated_prompt,
            estimated_completion_tokens=estimated_completion,
            reported_prompt_tokens=numbers["prompt"] if reported else None,
            reported_completion_tokens=numbers["completion"] if reported else None,
            reasoning_tokens=reasoning, cached_tokens=cached, usage_estimated=estimated,
            counted_prompt_tokens=counted_prompt, counted_completion_tokens=counted_completion,
            request_ts=round(attempt.request_ts, 3),
            ttft_ms=_ms(attempt.started, attempt.first_delta), last_delta_ms=_ms(attempt.started, attempt.last_delta),
            decode_ms=_ms(attempt.first_delta, attempt.last_delta), total_ms=_ms(attempt.started, now),
            finish_reason=finish_reason or None, streamed=attempt.streamed, snapshot=attempt.snapshot,
            rejected_fields=sorted(client._learned.get("rejected") or []) or None,
            http_sends=attempt.http_sends or None, http_statuses=attempt.http_statuses or None,
            **trace.error_fields(error), **attempt.prefix)


def _ms(start: float | None, end: float | None) -> float | None:
    return round((end - start) * 1000, 1) if start is not None and end is not None else None


# The log of the call running on this thread: lets the layers below reach it without a new parameter each.
_call_log: contextvars.ContextVar[_CallLog | None] = contextvars.ContextVar("dcode_call_log", default=None)


def _on_http_request(request: Any) -> None:
    log = _call_log.get()
    if log is not None and log.current is not None:
        log.current.http_sends += 1


def _on_http_response(response: Any) -> None:
    log = _call_log.get()
    if log is not None and log.current is not None:
        log.current.http_statuses.append(response.status_code)


def _watch_http(client: LLMClient) -> None:
    """The OpenAI SDK retries once by itself (max_retries=1) and says nothing. Only when tracing, watch what it sends."""
    http = getattr(client.client, "_client", None)  # the SDK's httpx client; a test's stand-in has none
    try:
        hooks = http.event_hooks
        if _on_http_request not in hooks["request"]:
            hooks["request"].append(_on_http_request)
            hooks["response"].append(_on_http_response)
    except (AttributeError, KeyError, TypeError):
        pass


class ModelBusy(RuntimeError):
    """The server kept answering "rate limited" / "overloaded" after Dcode waited."""


def _busy(exc: Exception) -> bool:
    """Worth waiting for: rate limited or overloaded, but not a spent quota (a Dcode Manager token limit)."""
    if getattr(exc, "status_code", None) not in (429, 503):
        return False
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    code = (error.get("code") or error.get("type")) if isinstance(error, dict) else ""
    return code not in ("token_limit", "insufficient_quota")


def _retry_after(exc: Exception) -> float | None:
    """The server's own Retry-After (seconds), capped so a long ban doesn't hang the task."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = float(headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None
    return min(max(value, 1.0), 30.0)


def error_text(exc: BaseException) -> str:
    """A server error in one readable line: its own message, not the whole JSON body."""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        error = body.get("error", body) if isinstance(body.get("error", body), dict) else body
        raw = (error.get("metadata") or {}).get("raw") if isinstance(error.get("metadata"), dict) else None
        message = str(raw or error.get("message") or "")
    message = message or str(getattr(exc, "message", "") or exc)
    code = error.get("code") if isinstance(body, dict) and isinstance(error, dict) else None
    if code in ("token_limit", "insufficient_quota"):
        return f"{message} See /usage."  # a Dcode Manager limit: the message already says when it resets
    if status == 429:
        return f"The model server is rate-limiting requests (429): {message}"
    if status == 503:
        return f"The model server is overloaded (503): {message}"
    return f"{status}: {message}" if status else message


class LLMClient:
    def __init__(self, config: AppConfig) -> None:
        from openai import OpenAI  # imported lazily: keeps tests / hub startup fast

        self.config = config
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key, timeout=600.0, max_retries=1,
                             default_headers={"X-Dcode-Client": manager_client.client_name()})
        self._stream_tools_ok: bool | None = None  # None = unknown yet, True/False = learned from the server
        # What the server taught us by rejecting a request: fields it refuses, the max-tokens name, and
        # what it needs in the history (reasoning back, short ids, no system role, tools as text…).
        self._learned: dict[str, Any] = {"rejected": set()}
        self._thoughts: dict[str, str] = {}  # first tool call id -> the reasoning that led to it
        self._notes: list[str] = []
        self._learned_for = config.model

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def family(self) -> families.Family:
        chosen = getattr(self.config, "family", "auto")
        if chosen in ("", "auto") and self.server == "manager":  # a gateway name ("coder") says nothing
            chosen = (self.config.manager.get("models", {}).get(self.config.model) or {}).get("family") or "auto"
        return families.detect(self.config.model, self.config.base_url, chosen)

    @property
    def server(self) -> str:
        if manager_client.is_manager(self.config):
            return "manager"
        return families.server_kind(self.config.base_url)

    def with_model(self, model: str) -> LLMClient:
        """Same server and connection, another model (helpers can run on a smaller one)."""
        import copy
        import dataclasses

        clone = copy.copy(self)
        changes: dict[str, Any] = {"model": model}
        if model != self.config.model and hasattr(self.config, "family"):
            changes["family"] = "auto"  # another model: the main model's family doesn't carry over
        clone.config = dataclasses.replace(self.config, **changes)
        clone._learned, clone._thoughts, clone._notes = self._server_lessons(), {}, []
        clone._learned_for = model
        return clone

    def _server_lessons(self) -> dict[str, Any]:
        """What a server refuses holds for every model on it; what a model needs is its own."""
        return {"rejected": set(self._learned["rejected"]), "max_key": self._learned.get("max_key"),
                "text_tools": self._learned.get("text_tools")}

    def keep_reasoning(self, tool_calls: list[dict[str, Any]], reasoning: str) -> None:
        """Remember the reasoning behind a step (by its final call id), for the families that want it back."""
        if tool_calls and reasoning:
            self._thoughts[str(tool_calls[0].get("id") or "")] = reasoning[-ECHO_CHARS:]
            while len(self._thoughts) > 32:
                self._thoughts.pop(next(iter(self._thoughts)))

    # --- low level --------------------------------------------------------------
    def _prepare(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """The history as this model's API wants it. Works on copies: the stored history never changes."""
        family, learned = self.family, self._learned
        echo = learned.get("echo") or family.echo_reasoning
        short = learned.get("short_ids") or family.short_call_ids
        signatures = learned.get("signatures")
        text_tools = bool(learned.get("text_tools") and tools)
        no_system = learned.get("no_system") or not family.system_role
        if not ((echo and self._thoughts) or short or signatures or text_tools or no_system or learned.get("roles")):
            return messages, tools
        out = [dict(m) for m in messages]
        last_user = max((i for i, m in enumerate(out) if m.get("role") == "user"), default=-1)
        for index, message in enumerate(out):
            calls = message.get("tool_calls")
            if message.get("role") == "assistant" and calls:
                thought = self._thoughts.get(str(calls[0].get("id") or "")) if echo else None
                if thought and (index > last_user or echo == "all"):
                    message["reasoning_content"] = thought
                if short or signatures:
                    fixed = []
                    for call in calls:
                        call = dict(call)
                        if short:
                            call["id"] = _short_id(str(call.get("id") or ""))
                        if signatures and not call.get("extra_content"):
                            call["extra_content"] = _SKIP_SIGNATURE
                        fixed.append(call)
                    message["tool_calls"] = fixed
            elif message.get("role") == "tool" and short:
                message["tool_call_id"] = _short_id(str(message.get("tool_call_id") or ""))
        if text_tools:
            out, tools = _as_text_tools(out, tools or []), None
        if no_system:
            out = _merge_system(out)
        if learned.get("roles"):
            out = _alternate(out)
        return out, tools

    def _shape(self, kwargs: dict[str, Any], effort: str, max_tokens: int | None
               ) -> tuple[dict[str, Any], dict[str, Any]]:
        """(extra body fields, the request) for this family and server."""
        learned, rejected = self._learned, self._learned["rejected"]
        request = dict(kwargs)
        for key in SAMPLING_FIELDS:  # only when a study sets them; a server that refuses one loses it (see _learn)
            value = self.config.experiment.get(key)
            if value is not None and key not in rejected:
                request[key] = value
        if max_tokens:
            default = "max_completion_tokens" if self.family.completion_tokens or self.server == "openai" else "max_tokens"
            request[learned.get("max_key") or default] = max_tokens
        if "stream_options" in rejected:
            request.pop("stream_options", None)
        request["messages"], tools = self._prepare(request["messages"], request.get("tools"))
        if not tools:
            request.pop("tools", None)
            request.pop("tool_choice", None)
        if effort == "none" and learned.get("must_think"):
            return {}, request  # it can't stop thinking: leave it at its default rather than be refused
        extras = families.request_extras(self.family, self.server, effort, max_tokens)
        return {k: v for k, v in extras.items() if k not in rejected}, request

    def _learn(self, exc: Exception, body: dict[str, Any], request: dict[str, Any]) -> bool:
        """Adjust to what the server just rejected. True = something changed, so the request is worth sending again."""
        text = str(exc)
        lowered = text.lower()
        if not _looks_like_param_error(exc) or _CONTEXT_ERROR_RE.search(text) or _MISSING_MODEL_RE.search(text):
            return False
        learned = self._learned

        def flag(key: str, value: Any = True) -> bool:
            if learned.get(key) == value:
                return False
            learned[key] = value
            return True

        if body and _MUST_THINK_RE.search(text) and not learned.get("must_think"):
            # "Reasoning is mandatory" (GLM 5.3 on OpenRouter) refuses only turning it off; dropping the
            # whole field would also lose the effort level, and for every model on that server.
            return flag("must_think")
        if "max_completion_tokens" in lowered and "max_tokens" in request:
            return flag("max_key", "max_completion_tokens")
        if "max_completion_tokens" in lowered and "max_completion_tokens" in request \
                and not re.search(r"use\W+max_completion_tokens", lowered):
            return flag("max_key", "max_tokens")
        if "reasoning_content" in lowered:
            return flag("echo") or flag("echo", "all")
        if "thought_signature" in lowered or "thought signature" in lowered:
            return flag("signatures")
        if re.search(r"tool[ _]?call[ _]?id", lowered) and re.search(r"\b9\b|a-z", lowered):
            return flag("short_ids")
        if request.get("tools") and _NO_TOOLS_RE.search(text):
            if flag("text_tools"):
                self._notes.append("This server takes no tool definitions; Dcode describes the tools in the prompt")
                return True
            return False
        if _SYSTEM_ROLE_RE.search(text) or ("system" in lowered and "role" in lowered and "not" in lowered):
            return flag("no_system")
        if _ROLE_ORDER_RE.search(text):
            return flag("roles")
        offered = list(body) + (["stream_options"] if "stream_options" in request else [])
        offered += [key for key in SAMPLING_FIELDS if key in request]
        named = [key for key in offered if re.search(rf"\b{re.escape(key)}\b", lowered)]
        named += [key for key in body if isinstance(body[key], dict)
                  and any(re.search(rf"\b{re.escape(sub)}\b", lowered) for sub in body[key])]
        if not named and request.get("stream") and request.get("tools") and "stream" in lowered:
            return False  # streaming with tools refused: chat() falls back to one plain request
        guilty = named or list(body) or [key for key in offered if key == "stream_options"]
        if not guilty:
            return False
        learned["rejected"].update(guilty)
        return True

    def _create(self, effort: str | None = None, max_tokens: int | None = None, **kwargs: Any) -> Any:
        """Send one request; when the server rejects a field or a message shape, adjust and send again."""
        effort = effort or self.config.reasoning_effort
        if self.config.model != self._learned_for:  # /model switched models on this client
            self._learned, self._thoughts, self._learned_for = self._server_lessons(), {}, self.config.model
        waits = list(BUSY_WAITS)
        log = _call_log.get()
        for _ in range(6 + len(waits)):
            body, request = self._shape(kwargs, effort, max_tokens)
            if log is not None:
                log.begin(request, body, effort, max_tokens)
            try:
                if body:
                    response = self.client.chat.completions.create(extra_body=body, **request)
                else:
                    response = self.client.chat.completions.create(**request)
                if log is not None:
                    log.answered()
                return response
            except Exception as exc:
                if log is not None:
                    log.finish(error=exc)
                if _busy(exc):
                    if not waits:
                        raise ModelBusy(error_text(exc)) from exc
                    wait = _retry_after(exc) or waits[0]
                    waits.pop(0)
                    self._notes.append(f"The model server is busy; trying again in {wait:g}s")
                    if log is not None:
                        log.next_reason = "busy"
                    time.sleep(wait)
                elif not self._learn(exc, body, request):
                    raise
                elif log is not None:
                    log.next_reason = "learned_param"
        request = self._shape(kwargs, effort, max_tokens)[1]
        if log is not None:
            log.begin(request, {}, effort, max_tokens)
        try:
            response = self.client.chat.completions.create(**request)
        except Exception as exc:
            if log is not None:
                log.finish(error=exc)
            raise
        if log is not None:
            log.answered()
        return response

    # --- public API ---------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        max_tokens: int | None = None,
    ) -> AssistantTurn:
        """One reply. When the model thinks itself out of room (or in circles), ask again with less thinking."""
        effort = self.config.reasoning_effort
        notes: list[str] = []
        retries = int(self.config.experiment.get("think_retries", THINK_RETRIES))
        scope = trace.current()
        log = _CallLog(self, scope.purpose if scope else "other")
        token = _call_log.set(log)
        try:
            for attempt in range(retries + 1):
                try:
                    turn = self._chat_once(messages, tools, on_delta, should_stop, max_tokens, effort)
                    if not (turn.finish_reason == "length" and not turn.content.strip() and not turn.tool_calls
                            and turn.reasoning):
                        break
                    reason = "used its whole reply on thinking"
                except _Overthinking as over:
                    turn, reason = over.turn, over.reason
                lower = self._less_effort(effort, reason)
                if lower is None or attempt == retries:
                    turn.finish_reason = "length"
                    break
                notes.append(f"The model {reason}; asking again with {'no' if lower == 'none' else lower} thinking")
                effort = lower
                log.next_reason = "think_cutoff"
        except BaseException:
            log.finish()
            trace.charge(log.totals)  # the attempts before the failure were still spent
            raise
        finally:
            _call_log.reset(token)
        log.finish()
        turn.usage = {key: log.totals[key] for key in ("prompt", "completion", "total")}
        turn.calls, turn.cached = log.totals["calls"], log.totals["cached"]
        turn.usage_estimated, turn.calibration_prompt = log.estimated, log.last_reported_prompt
        turn.notes = self._notes + notes + turn.notes
        self._notes = []
        return turn

    def _less_effort(self, effort: str, reason: str) -> str | None:
        """The next effort down, or None when lowering it would change nothing for this model."""
        family = self.family
        if family.thinking == "fixed" or (family is families.GENERIC and self.server in ("local", "ollama")):
            return None
        if effort not in _EFFORT_ORDER or effort == "none":
            return None
        if "circles" in reason:
            return "none"
        return _EFFORT_ORDER[_EFFORT_ORDER.index(effort) - 1]

    def _chat_once(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                   on_delta: Callable[[str], None] | None, should_stop: Callable[[], bool] | None,
                   max_tokens: int | None, effort: str) -> AssistantTurn:
        kwargs: dict[str, Any] = {"model": self.config.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        use_stream = self.config.stream and (not tools or self._stream_tools_ok is not False)
        if use_stream:
            try:
                turn = self._chat_stream(kwargs, on_delta, should_stop, effort, max_tokens)
                if tools:
                    self._stream_tools_ok = True
                return self._finish(turn, tools, bool(tools and self._learned.get("text_tools")),
                                    self.config.experiment["text_tool_recovery"])
            except (Interrupted, _Overthinking):
                raise
            except Exception as exc:
                rejected = _looks_like_param_error(exc) or "stream" in str(exc).lower()
                if not tools or self._stream_tools_ok or not rejected:
                    raise
                # Server can't stream tool calls — fall back permanently.
                self._stream_tools_ok = False
                log = _call_log.get()
                if log is not None:
                    log.next_reason = "learned_param"
        if should_stop and should_stop():
            raise Interrupted()
        response = self._create(effort=effort, max_tokens=max_tokens, **kwargs)
        message = response.choices[0].message
        turn = AssistantTurn(content=getattr(message, "content", None) or "",
                             finish_reason=str(getattr(response.choices[0], "finish_reason", "") or ""),
                             reasoning=_reasoning_of(message))
        for call in getattr(message, "tool_calls", None) or []:
            data = call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else dict(call)
            function = data.get("function") or {}
            entry = {
                "id": data.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": function.get("name", ""), "arguments": function.get("arguments") or "{}"},
            }
            if data.get("extra_content"):  # Gemini's thought signature: it must come back with the call
                entry["extra_content"] = data["extra_content"]
            turn.tool_calls.append(entry)
        usage = getattr(response, "usage", None)
        if usage is not None:
            turn.usage = _usage_dict(usage)
        log = _call_log.get()
        if log is not None:
            log.add_output(len(turn.content) + len(turn.reasoning)
                           + sum(len(c["function"]["arguments"]) for c in turn.tool_calls))
            log.finish(usage, turn.finish_reason)
        if on_delta and turn.content:
            visible = strip_thinking(turn.content)
            if visible:
                on_delta(visible)
        return self._finish(turn, tools, bool(tools and self._learned.get("text_tools")),
                            self.config.experiment["text_tool_recovery"])

    def complete_text(self, messages: list[dict[str, Any]], max_tokens: int = 800, purpose: str = "other") -> str:
        """One-shot, tool-less completion for side jobs (summaries, titles, routing): no thinking needed."""
        log = _CallLog(self, purpose)
        token = _call_log.set(log)

        def ask(limit: int) -> Any:
            response = self._create(effort="none", max_tokens=limit, model=self.config.model, messages=messages)
            choice = response.choices[0]
            log.add_output(len(choice.message.content or ""))
            log.finish(getattr(response, "usage", None), str(getattr(choice, "finish_reason", "") or ""))
            return choice

        try:
            choice = ask(max_tokens)
            text = strip_thinking(choice.message.content or "")
            if not text.strip() and getattr(choice, "finish_reason", None) == "length":
                # A model that thinks anyway (GLM 5.3: "reasoning is mandatory") spent the whole cap on it:
                # once more with room for the thinking, so a title isn't lost to a 60-token cap.
                log.next_reason = "think_cutoff"
                text = strip_thinking(ask(max(max_tokens * 8, SIDE_JOB_ROOM)).message.content or "")
            return text
        finally:
            log.finish()
            trace.charge(log.totals)  # no reply carries these back: the task's total picks them up
            _call_log.reset(token)

    def list_models(self) -> list[str]:
        response = self.client.models.list()
        return sorted({getattr(item, "id", None) or str(item) for item in getattr(response, "data", [])})

    # --- helpers ----------------------------------------------------------------------
    def _chat_stream(
        self,
        kwargs: dict[str, Any],
        on_delta: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
        effort: str,
        max_tokens: int | None,
    ) -> AssistantTurn:
        stream = self._create(effort=effort, max_tokens=max_tokens,
                              **dict(kwargs, stream=True, stream_options={"include_usage": True}))
        experiment = self.config.experiment
        budget = int(max_tokens * experiment["think_share"] * 3.5) if max_tokens else 0  # ~3.5 chars per token
        check_every = int(experiment["loop_check_every"])
        turn = AssistantTurn()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_size, next_check = 0, check_every
        visible = _VisibleText()
        calls: dict[int, dict[str, Any]] = {}
        log = _call_log.get()
        raw_usage: Any = None
        ended_by: BaseException | None = None
        try:
            for chunk in stream:
                if should_stop and should_stop():
                    raise Interrupted()
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    turn.usage = _usage_dict(usage)
                    raw_usage = usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                reason = getattr(choices[0], "finish_reason", None)
                if reason:
                    turn.finish_reason = str(reason)
                delta = getattr(choices[0], "delta", None)
                if delta is None:
                    continue
                thought = _reasoning_of(delta)
                if thought:
                    reasoning_parts.append(thought)
                    reasoning_size += len(thought)
                    if log is not None:
                        log.delta(len(thought))
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    if log is not None:
                        log.delta(len(text))
                    shown = visible.feed(text)
                    if on_delta and shown:
                        on_delta(shown)
                    if not visible.sent:  # still inside an inline <think>
                        reasoning_size = max(reasoning_size, len(visible.full))
                for tc in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tc, "index", None)
                    if index is None:
                        index = len(calls)
                    entry = calls.setdefault(
                        index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    if getattr(tc, "id", None):
                        entry["id"] = tc.id
                    extra = (getattr(tc, "model_extra", None) or {}).get("extra_content")
                    if extra:
                        entry["extra_content"] = extra
                    function = getattr(tc, "function", None)
                    if function is not None:
                        name = getattr(function, "name", None)
                        if name and name != entry["function"]["name"]:
                            entry["function"]["name"] += name
                        arguments = getattr(function, "arguments", None)
                        if arguments:
                            entry["function"]["arguments"] += arguments
                            if log is not None:
                                log.delta(len(arguments))
                if check_every and reasoning_size >= next_check and not visible.sent and not calls:
                    next_check += check_every
                    so_far = "".join(reasoning_parts) or thinking_text(visible.full)
                    cut = ("thought in circles" if _looping(so_far)
                           else "thought too long" if budget and reasoning_size > budget else "")
                    if cut:
                        raise _Overthinking(cut, AssistantTurn(reasoning=so_far, finish_reason="length",
                                                               usage=turn.usage))
        except BaseException as exc:
            ended_by = exc
            raise
        finally:
            if log is not None:
                # A reply cut off on purpose isn't a server error: its finish_reason says what happened.
                cut_off = isinstance(ended_by, _Overthinking)
                log.finish(raw_usage, str(ended_by) if cut_off else turn.finish_reason, None if cut_off else ended_by)
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        turn.content = "".join(content_parts)
        turn.reasoning = "".join(reasoning_parts)
        for index in sorted(calls):
            entry = calls[index]
            if not entry["function"]["name"]:
                continue
            entry["id"] = entry["id"] or f"call_{uuid.uuid4().hex[:12]}"
            entry["function"]["arguments"] = entry["function"]["arguments"] or "{}"
            turn.tool_calls.append(entry)
        return turn

    @staticmethod
    def _finish(turn: AssistantTurn, tools: list[dict[str, Any]] | None, expected_text: bool = False,
                recover_text: bool = True) -> AssistantTurn:
        inline = thinking_text(turn.content)
        turn.reasoning = "\n\n".join(p for p in (turn.reasoning.strip(), inline) if p)
        turn.content = strip_thinking(turn.content)
        if not tools:
            return turn
        names = [t["function"]["name"] for t in tools]
        schemas = {t["function"]["name"]: t["function"].get("parameters") for t in tools}
        if not turn.tool_calls and turn.content and (recover_text or expected_text):
            remaining, recovered = parse_text_tool_calls(turn.content, names)
            if recovered:
                turn.content = remaining
                turn.tool_calls = recovered
                turn.recovered_tool_calls = not expected_text  # in text-tools mode that's the normal way
        for call in turn.tool_calls:
            function = call.get("function") or {}
            function["name"] = clean_tool_name(function.get("name", ""), names)
            function["arguments"] = coerce_arguments(clean_arguments(function.get("arguments", "")),
                                                     schemas.get(function["name"]))
        return turn


def _reasoning_of(message: Any) -> str:
    """Reasoning a server sends beside the answer (vLLM and DeepSeek: reasoning_content; Ollama: reasoning)."""
    for key in ("reasoning_content", "reasoning"):
        value = getattr(message, key, None)
        if value is None:
            value = (getattr(message, "model_extra", None) or {}).get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _usage_dict(usage: Any) -> dict[str, int]:
    return {
        "prompt": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion": int(getattr(usage, "completion_tokens", 0) or 0),
        "total": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _looks_like_param_error(exc: Exception) -> bool:
    """True for 4xx 'bad request'-style rejections (unknown field, unsupported option)."""
    status = getattr(exc, "status_code", None)
    if status in (400, 404, 415, 422):
        return True
    text = str(exc).lower()
    return any(word in text for word in ("unrecognized", "unknown", "unsupported", "not supported", "extra", "invalid"))
