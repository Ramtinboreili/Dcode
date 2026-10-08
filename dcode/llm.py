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

import json
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig


class Interrupted(Exception):
    """Raised when the user stops a running task."""


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0})
    recovered_tool_calls: bool = False
    finish_reason: str = ""  # "stop", "tool_calls", "length" (hit the output limit), …
    reasoning: str = ""      # the model's thinking: shown on request, never sent back

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_BODY_RE = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
_TOOL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json|tool_call|tool)?\s*(\{.*?\})\s*```", re.DOTALL)


_TOOL_NAME_RE = re.compile(r"[A-Za-z_][\w.-]*")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def clean_tool_name(name: str, known: Iterable[str]) -> str:
    """Strip markup some models leak into tool names (e.g. ``git_diff</｜DSML｜ parameter>``)."""
    known = list(known)
    raw = (name or "").strip()
    if raw in known:
        return raw
    match = _TOOL_NAME_RE.match(raw)
    candidate = match.group(0) if match else raw
    if candidate in known:
        return candidate
    for tool in sorted(known, key=len, reverse=True):
        if raw.startswith(tool):
            return tool
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


def thinking_text(text: str) -> str:
    """What the model wrote inside ``<think>`` blocks (an unclosed block counts to the end)."""
    parts = [m.strip() for m in _THINK_BODY_RE.findall(text or "")]
    return "\n\n".join(p for p in parts if p)


def strip_thinking(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    # An unclosed <think> at the start means the whole thing was reasoning.
    if text.lstrip().lower().startswith("<think>"):
        return ""
    return text.replace("</think>", "").strip()


def _make_call(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {})
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _call_from_obj(obj: Any, known: Iterable[str]) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # {"function": {"name":..., "arguments":...}}
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if not isinstance(name, str) or name not in set(known):
        return None
    args = next((obj[k] for k in ("arguments", "parameters", "args", "input") if k in obj), {})
    return _make_call(name, args)


def parse_text_tool_calls(content: str, known: Iterable[str]) -> tuple[str, list[dict[str, Any]]]:
    """Extract tool calls embedded in assistant text. Returns (remaining_text, calls)."""
    known = list(known)
    if not content or not known:
        return content, []
    calls: list[dict[str, Any]] = []

    def _try(raw: str) -> bool:
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        items = obj if isinstance(obj, list) else [obj]
        parsed = [_call_from_obj(item, known) for item in items]
        if parsed and all(parsed):
            calls.extend(parsed)  # type: ignore[arg-type]
            return True
        return False

    remaining = content
    for match in _TOOL_TAG_RE.finditer(content):
        if _try(match.group(1)):
            remaining = remaining.replace(match.group(0), "")
    if not calls:
        for match in _FENCE_RE.finditer(content):
            if _try(match.group(1)):
                remaining = remaining.replace(match.group(0), "")
    if not calls:
        stripped = content.strip()
        if stripped.startswith(("{", "[")) and _try(stripped):
            remaining = ""
    return remaining.strip(), calls


class LLMClient:
    def __init__(self, config: AppConfig) -> None:
        from openai import OpenAI  # imported lazily: keeps tests / hub startup fast

        self.config = config
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key, timeout=600.0, max_retries=1)
        # None = unknown yet, True/False = learned from the server.
        self._effort_ok: bool | None = None
        self._usage_opt_ok: bool | None = None
        self._stream_tools_ok: bool | None = None

    @property
    def model(self) -> str:
        return self.config.model

    def with_model(self, model: str) -> LLMClient:
        """Same server and connection, another model (helpers can run on a smaller one)."""
        import copy
        import dataclasses

        clone = copy.copy(self)
        clone.config = dataclasses.replace(self.config, model=model)
        return clone

    # --- low level --------------------------------------------------------------
    def _create(self, **kwargs: Any) -> Any:
        effort = self.config.reasoning_effort
        if effort in {"low", "medium", "high"} and self._effort_ok is not False:
            try:
                result = self.client.chat.completions.create(extra_body={"reasoning_effort": effort}, **kwargs)
                self._effort_ok = True
                return result
            except Exception as exc:
                if not _looks_like_param_error(exc):
                    raise
                self._effort_ok = False
        return self.client.chat.completions.create(**kwargs)

    # --- public API ---------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        max_tokens: int | None = None,
    ) -> AssistantTurn:
        kwargs: dict[str, Any] = {"model": self.config.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        use_stream = self.config.stream and (not tools or self._stream_tools_ok is not False)
        if use_stream:
            try:
                turn = self._chat_stream(kwargs, on_delta, should_stop)
                if tools:
                    self._stream_tools_ok = True
                return self._finish(turn, tools)
            except Interrupted:
                raise
            except Exception as exc:
                rejected = _looks_like_param_error(exc) or "stream" in str(exc).lower()
                if not tools or self._stream_tools_ok or not rejected:
                    raise
                # Server can't stream tool calls — fall back permanently.
                self._stream_tools_ok = False
        if should_stop and should_stop():
            raise Interrupted()
        response = self._create(**kwargs)
        message = response.choices[0].message
        turn = AssistantTurn(content=getattr(message, "content", None) or "",
                             finish_reason=str(getattr(response.choices[0], "finish_reason", "") or ""),
                             reasoning=_reasoning_of(message))
        for call in getattr(message, "tool_calls", None) or []:
            data = call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else dict(call)
            function = data.get("function") or {}
            turn.tool_calls.append(
                {
                    "id": data.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": function.get("name", ""), "arguments": function.get("arguments") or "{}"},
                }
            )
        usage = getattr(response, "usage", None)
        if usage is not None:
            turn.usage = _usage_dict(usage)
        if on_delta and turn.content:
            on_delta(turn.content)
        return self._finish(turn, tools)

    def complete_text(self, messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
        """One-shot, tool-less completion (used for summaries)."""
        response = self._create(model=self.config.model, messages=messages, max_tokens=max_tokens)
        return strip_thinking(response.choices[0].message.content or "")

    def list_models(self) -> list[str]:
        response = self.client.models.list()
        return sorted({getattr(item, "id", None) or str(item) for item in getattr(response, "data", [])})

    # --- helpers ----------------------------------------------------------------------
    def _chat_stream(
        self,
        kwargs: dict[str, Any],
        on_delta: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> AssistantTurn:
        stream_kwargs = dict(kwargs, stream=True)
        if self._usage_opt_ok is not False:
            stream_kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = self._create(**stream_kwargs)
            self._usage_opt_ok = self._usage_opt_ok if "stream_options" not in stream_kwargs else True
        except Exception as exc:
            if "stream_options" not in stream_kwargs or not _looks_like_param_error(exc):
                raise
            self._usage_opt_ok = False
            stream_kwargs.pop("stream_options", None)
            stream = self._create(**stream_kwargs)

        turn = AssistantTurn()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        try:
            for chunk in stream:
                if should_stop and should_stop():
                    raise Interrupted()
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    turn.usage = _usage_dict(usage)
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
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    if on_delta:
                        on_delta(text)
                for tc in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tc, "index", None)
                    if index is None:
                        index = len(calls)
                    entry = calls.setdefault(
                        index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    if getattr(tc, "id", None):
                        entry["id"] = tc.id
                    function = getattr(tc, "function", None)
                    if function is not None:
                        name = getattr(function, "name", None)
                        if name and name != entry["function"]["name"]:
                            entry["function"]["name"] += name
                        arguments = getattr(function, "arguments", None)
                        if arguments:
                            entry["function"]["arguments"] += arguments
        finally:
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
    def _finish(turn: AssistantTurn, tools: list[dict[str, Any]] | None) -> AssistantTurn:
        inline = thinking_text(turn.content)
        turn.reasoning = "\n\n".join(p for p in (turn.reasoning.strip(), inline) if p)
        turn.content = strip_thinking(turn.content)
        if tools:
            names = [t["function"]["name"] for t in tools]
            for call in turn.tool_calls:
                function = call.get("function") or {}
                function["name"] = clean_tool_name(function.get("name", ""), names)
                function["arguments"] = clean_arguments(function.get("arguments", ""))
        if tools and not turn.tool_calls and turn.content:
            names = [t["function"]["name"] for t in tools]
            remaining, recovered = parse_text_tool_calls(turn.content, names)
            if recovered:
                turn.content = remaining
                turn.tool_calls = recovered
                turn.recovered_tool_calls = True
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
