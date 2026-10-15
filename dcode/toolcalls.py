"""Tool calls a model wrote as text, in each family's own format.

A server only turns a model's tool-call text into real ``tool_calls`` when it runs the matching
parser (vLLM ``--tool-call-parser``, Ollama's template). When it doesn't, the call arrives as
plain text. Every known format is tried, whatever model is running: they don't overlap, and a
wrong guess about the family must never lose a call.

Formats: Hermes/Qwen ``<tool_call>{json}</tool_call>``, Qwen3-Coder ``<function=x><parameter=k>``,
Llama ``<function=x>{json}`` and ``<|python_tag|>``, GLM ``<arg_key>/<arg_value>``, DeepSeek
``<｜tool▁call▁begin｜>`` tokens, DeepSeek V3.2 DSML and MiniMax ``<invoke name=…>``, Kimi
``<|tool_call_begin|>functions.x:0``, Mistral ``[TOOL_CALLS]``, Gemma ``tool_code`` blocks,
gpt-oss ``to=functions.x <|message|>{json}``, and fenced or bare JSON.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from typing import Any

Call = tuple[str, Any]  # (tool name, arguments as a dict or a JSON string)

_DECODER = json.JSONDecoder()

_HERMES_RE = re.compile(r"<(tool_call|function_call|tools)>\s*(.*?)\s*(?:</\1>|\Z)", re.DOTALL | re.IGNORECASE)
_FUNCTION_RE = re.compile(r"<function=([\w.:-]+)>(.*?)(?:</function>|\Z)", re.DOTALL)
_XML_PARAM_RE = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
_GLM_PAIR_RE = re.compile(r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>(.*?)</arg_value>", re.DOTALL)
_BAR = r"[|｜]"
_SEP = r"[▁_ ]"
_DS_CALL_RE = re.compile(rf"<{_BAR}tool{_SEP}call{_SEP}begin{_BAR}>(.*?)(?:<{_BAR}tool{_SEP}call{_SEP}end{_BAR}>|\Z)",
                         re.DOTALL)
_DS_SEP_RE = re.compile(rf"<{_BAR}tool{_SEP}sep{_BAR}>")
_DS_TOKENS_RE = re.compile(rf"<{_BAR}tool{_SEP}(calls{_SEP}begin|calls{_SEP}end|outputs?{_SEP}\w+){_BAR}>")
_DSML = rf"(?:{_BAR}DSML{_BAR}\s*)?"
_INVOKE_RE = re.compile(rf"<{_DSML}invoke\s+name=\"([^\"]+)\"\s*>(.*?)(?:</{_DSML}invoke>|\Z)", re.DOTALL)
_INVOKE_PARAM_RE = re.compile(
    rf"<{_DSML}parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>(.*?)</{_DSML}parameter>", re.DOTALL)
_INVOKE_WRAP_RE = re.compile(rf"</?(?:{_DSML}function_calls|minimax:tool_call)>")
_KIMI_RE = re.compile(r"<\|tool_call_begin\|>\s*([\w.:-]+)\s*<\|tool_call_argument_begin\|>(.*?)"
                      r"(?:<\|tool_call_end\|>|\Z)", re.DOTALL)
_KIMI_WRAP_RE = re.compile(r"<\|tool_calls_section_(begin|end)\|>")
_MISTRAL_RE = re.compile(r"\[TOOL_CALLS\]\s*")
_MISTRAL_ARGS_RE = re.compile(r"([\w.-]+)\s*(?:\[CALL_ID\]\s*\w+\s*)?\[ARGS\]\s*")
_HARMONY_RE = re.compile(r"to=(?:functions\.)?([\w.-]+)[^<{]*(?:<\|constrain\|>\s*\w+\s*)?(?:<\|message\|>)?\s*(?=\{)")
_HARMONY_TOKENS_RE = re.compile(r"<\|(start|end|call|channel|message|constrain|return)\|>|\bassistant\b(?=<\|)")
_TOOL_CODE_RE = re.compile(r"```tool_code\s*\n(.*?)```", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json|tool_call|tool|tools)?\s*\n?(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_PYTHON_TAG = "<|python_tag|>"


def _json_at(text: str, start: int = 0) -> tuple[Any, int] | None:
    """A JSON value starting at ``start`` (after spaces) and where it ends; None if there isn't one."""
    index = start
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    try:
        return _DECODER.raw_decode(text, index)
    except (json.JSONDecodeError, ValueError):
        return None


def _value(raw: str, as_string: bool | None = None) -> Any:
    """A parameter's text: kept as text, unless it is plainly JSON (a list, an object, a number…)."""
    text = raw.strip("\n")
    if as_string:
        return text
    stripped = text.strip()
    if as_string is False or (stripped[:1] in "[{" and stripped[-1:] in "]}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text


def _from_obj(obj: Any, known: set[str]) -> Call | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # {"type": "function", "function": {"name": …, "arguments": …}}
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or obj.get("function")
    if not isinstance(name, str):
        return None
    name = short_name(name, known)
    if name not in known:
        return None
    for key in ("arguments", "parameters", "args", "input", "tool_input", "params"):
        if key in obj:
            return name, obj[key]
    return name, {}


def _from_json(raw: str, known: set[str]) -> list[Call]:
    found = _json_at(raw)
    if found is None:
        return []
    items = found[0] if isinstance(found[0], list) else [found[0]]
    calls = [_from_obj(item, known) for item in items]
    return calls if calls and all(calls) else []  # type: ignore[return-value]


def short_name(name: str, known: Iterable[str]) -> str:
    """'functions.read_file:0', 'default_api.read_file', 'repo_browser.read_file' → 'read_file'."""
    known = set(known)
    raw = (name or "").strip()
    if raw in known:
        return raw
    bare = re.sub(r":\d+$", "", raw)
    if bare in known:
        return bare
    last = bare.rsplit(".", 1)[-1]
    return last if last in known else raw


def _xml_function(name: str, body: str, known: set[str]) -> Call | None:
    """Qwen3-Coder ``<parameter=k>v</parameter>`` or Llama ``{json}`` inside ``<function=name>``."""
    name = short_name(name, known)
    if name not in known:
        return None
    if body.strip().startswith("{"):
        found = _json_at(body)
        return (name, found[0]) if found and isinstance(found[0], dict) else None
    return name, {key: _value(value) for key, value in _XML_PARAM_RE.findall(body)}


def _glm(body: str, known: set[str]) -> Call | None:
    """GLM: ``name`` then ``<arg_key>k</arg_key><arg_value>v</arg_value>`` pairs (or just the name)."""
    head = body.split("<arg_key>", 1)[0].strip()
    name = short_name(head, known)
    if name not in known:
        return None
    return name, {key: _value(value) for key, value in _GLM_PAIR_RE.findall(body)}


def _deepseek(body: str, known: set[str]) -> Call | None:
    """``function<sep>name\\n```json{…}```'' (V3, R1) or ``name<sep>{…}`` (V3.1)."""
    parts = _DS_SEP_RE.split(body, maxsplit=1)
    if len(parts) != 2:
        return None
    head, rest = parts[0].strip(), parts[1]
    if head in ("function", ""):
        name, _, rest = rest.partition("\n")
    else:
        name = head
    name = short_name(name.strip(), known)
    if name not in known:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", rest, re.DOTALL)
    found = _json_at(fence.group(1) if fence else rest)
    return name, (found[0] if found and isinstance(found[0], dict) else {})


def _python_call(source: str, known: set[str]) -> Call | None:
    """Gemma ``tool_code``: ``read_file(path="a.py")`` or ``print(default_api.read_file(…))``."""
    try:
        node = ast.parse(source.strip(), mode="eval").body
    except SyntaxError:
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print" and node.args:
        node = node.args[0]
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    if name not in known or node.args:
        return None
    try:
        return name, {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
    except (ValueError, SyntaxError):
        return None


def _mistral(text: str, known: set[str]) -> list[Call]:
    """``[TOOL_CALLS][{…}]`` (v0.3) or ``[TOOL_CALLS]name[ARGS]{…}`` (newer), one or more."""
    calls: list[Call] = []
    for match in _MISTRAL_RE.finditer(text):
        tail = text[match.end():]
        if tail.startswith("["):
            calls += _from_json(tail, known)
            continue
        named = _MISTRAL_ARGS_RE.match(tail)
        found = _json_at(tail, named.end()) if named else None
        name = short_name(named.group(1), known) if named else ""
        if found and name in known:
            calls.append((name, found[0]))
    return calls


def parse(content: str, known: Iterable[str]) -> tuple[str, list[Call]]:
    """(the text without the calls, the calls). Unknown tool names are left alone, as prose."""
    known = set(known)
    if not content or not known:
        return content, []
    text = content

    # Token formats first: their markers never appear in prose.
    if _DS_CALL_RE.search(text):
        calls = [c for c in (_deepseek(m.group(1), known) for m in _DS_CALL_RE.finditer(text)) if c]
        if calls:
            return _DS_TOKENS_RE.sub("", _DS_CALL_RE.sub("", text)).strip(), calls
    if _INVOKE_RE.search(text):
        calls = []
        for match in _INVOKE_RE.finditer(text):
            name = short_name(match.group(1), known)
            if name in known:
                args = {key: _value(value, None if flag == "" else flag == "true")
                        for key, flag, value in _INVOKE_PARAM_RE.findall(match.group(2))}
                calls.append((name, args))
        if calls:
            return _INVOKE_WRAP_RE.sub("", _INVOKE_RE.sub("", text)).strip(), calls
    if "<|tool_call_begin|>" in text:
        calls = []
        for match in _KIMI_RE.finditer(text):
            name, found = short_name(match.group(1), known), _json_at(match.group(2))
            if name in known:
                calls.append((name, found[0] if found else {}))
        if calls:
            return _KIMI_WRAP_RE.sub("", _KIMI_RE.sub("", text)).strip(), calls
    if "[TOOL_CALLS]" in text:
        calls = _mistral(text, known)
        if calls:
            return text.split("[TOOL_CALLS]", 1)[0].strip(), calls
    if "to=" in text and "<|" in text:
        calls, cut = [], len(text)
        for match in _HARMONY_RE.finditer(text):
            name, found = short_name(match.group(1), known), _json_at(text, match.end())
            if name in known and found and isinstance(found[0], dict):
                calls.append((name, found[0]))
                cut = min(cut, match.start())
        if calls:
            return _HARMONY_TOKENS_RE.sub("", text[:cut]).strip(), calls

    # Tag formats.
    calls, remaining = [], text
    for match in _HERMES_RE.finditer(text):
        body = match.group(2)
        found: list[Call] = []
        if body.startswith("{") or body.startswith("["):
            found = _from_json(body, known)
        elif "<function=" in body:
            found = [c for c in (_xml_function(m.group(1), m.group(2), known) for m in _FUNCTION_RE.finditer(body)) if c]
        else:
            call = _glm(body, known)
            found = [call] if call else []
        if found:
            calls += found
            remaining = remaining.replace(match.group(0), "")
    if not calls and "<function=" in text:
        for match in _FUNCTION_RE.finditer(text):
            call = _xml_function(match.group(1), match.group(2), known)
            if call:
                calls.append(call)
                remaining = remaining.replace(match.group(0), "")
    if not calls and _PYTHON_TAG in text:
        before, _, after = text.partition(_PYTHON_TAG)
        for part in after.split(";"):
            calls += _from_json(part, known)
        remaining = before if calls else remaining
    if not calls:
        for match in _TOOL_CODE_RE.finditer(text):
            call = _python_call(match.group(1), known)
            if call:
                calls.append(call)
                remaining = remaining.replace(match.group(0), "")
    if not calls:
        for match in _FENCE_RE.finditer(text):
            found = _from_json(match.group(1), known)
            if found:
                calls += found
                remaining = remaining.replace(match.group(0), "")
    if not calls:
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            found_value = _json_at(stripped)
            if found_value and not stripped[found_value[1]:].strip():
                calls = _from_json(stripped, known)
                remaining = "" if calls else remaining
    return (remaining.strip(), calls) if calls else (content, [])
