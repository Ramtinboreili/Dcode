"""Guards an admin puts on a model, checked on every request through the gateway.

A model's ``guard`` (JSON) may hold:

* ``block``: regexes; a request whose user text matches one is refused (the pattern's name is logged);
* ``redact_secrets``: mask keys, tokens, passwords and private keys in what is sent to the model;
* ``mask_output``: regexes masked in the model's answer, streamed or not;
* ``rpm``: requests per minute per user;
* ``max_input_chars``: refuse very large requests before they reach the model;
* ``system_prompt``: a policy put in front of every conversation;
* ``guard_model``: another model on this manager that classifies the user's last message first
  (Llama Guard and similar answer "safe" / "unsafe"; any model asked the prompt below works too).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

MASK = "[masked]"
# Secrets people paste into prompts by accident. Kept specific: a guard that masks ordinary code is worse
# than none.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("AWS key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("API key", re.compile(r"\b(sk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{20,}|dcm-[A-Za-z0-9_-]{30,}"
                           r"|gh[pousr]_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_-]{20,}|xox[abpr]-[A-Za-z0-9-]{20,})")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("password", re.compile(r"(?i)\b(password|passwd|pwd|secret|token)\b(\s*[:=]\s*)(['\"]?)([^\s'\";,]{4,})\3")),
    ("connection string", re.compile(r"\b([a-z][a-z0-9+]*://[^\s:/@]+:)([^\s@/]{3,})(@)")),
]
GUARD_PROMPT = (
    "You are a safety classifier for a company's coding assistant. Read the user's message below and "
    "answer with exactly one word: SAFE if it is an acceptable request, UNSAFE if it asks for something "
    "harmful, illegal, or against company policy.{policy}\n\nUser message:\n{text}"
)
HOLD_BACK = 96  # characters of streamed output held back so a masked match can't straddle two chunks


@dataclass
class Guard:
    block: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    redact_secrets: bool = False
    mask_output: list[re.Pattern[str]] = field(default_factory=list)
    rpm: int | None = None
    max_input_chars: int | None = None
    system_prompt: str = ""
    guard_model: str = ""
    policy: str = ""

    @property
    def active(self) -> bool:
        return bool(self.block or self.redact_secrets or self.mask_output or self.rpm or self.max_input_chars
                    or self.system_prompt or self.guard_model)


def _patterns(values: Any) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for item in values or []:
        name, pattern = (item.get("name") or item.get("pattern"), item.get("pattern")) if isinstance(item, dict) \
            else (str(item), str(item))
        if pattern:
            out.append((str(name)[:60], re.compile(str(pattern), re.IGNORECASE)))
    return out


def problems(config: dict[str, Any]) -> list[str]:
    """What's wrong with a guard an admin is saving (empty = fine)."""
    found = []
    for key in ("block", "mask_output"):
        for item in config.get(key) or []:
            pattern = item.get("pattern") if isinstance(item, dict) else item
            try:
                re.compile(str(pattern))
            except re.error as exc:
                found.append(f"{key}: {pattern!r} is not a valid pattern ({exc})")
    for key in ("rpm", "max_input_chars"):
        value = config.get(key)
        if value not in (None, "") and (not str(value).isdigit() or int(value) <= 0):
            found.append(f"{key} must be a positive whole number")
    return found


def load(config: dict[str, Any] | None) -> Guard:
    config = config or {}
    number = lambda v: int(v) if str(v or "").isdigit() and int(v) > 0 else None  # noqa: E731
    return Guard(
        block=_patterns(config.get("block")),
        redact_secrets=bool(config.get("redact_secrets")),
        mask_output=[p for _, p in _patterns(config.get("mask_output"))],
        rpm=number(config.get("rpm")),
        max_input_chars=number(config.get("max_input_chars")),
        system_prompt=str(config.get("system_prompt") or "").strip(),
        guard_model=str(config.get("guard_model") or "").strip(),
        policy=str(config.get("policy") or "").strip(),
    )


def _texts(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
    return []


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return "\n".join(_texts(message))
    return ""


def redact(text: str) -> tuple[str, int]:
    count = 0
    for name, pattern in SECRET_PATTERNS:
        if name == "password":
            text, n = pattern.subn(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}{m.group(3)}", text)
        elif name == "connection string":
            text, n = pattern.subn(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", text)
        else:
            text, n = pattern.subn(MASK, text)
        count += n
    return text, count


def check_request(guard: Guard, body: dict[str, Any]) -> tuple[str, str, int]:
    """(why it's refused or "", the guard that refused it, secrets masked). Redaction and the system
    prompt change ``body`` in place: the gateway sends exactly what was checked."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        texts = [body.get("input") or body.get("prompt") or ""]
        joined = json.dumps(texts) if not isinstance(texts[0], str) else texts[0]
        for name, pattern in guard.block:
            if pattern.search(joined):
                return f"Refused by this model's guard ({name}).", f"block:{name}", 0
        return "", "", 0
    if guard.max_input_chars:
        size = sum(len(t) for m in messages for t in _texts(m))
        if size > guard.max_input_chars:
            return (f"This request is {size:,} characters; this model takes at most {guard.max_input_chars:,}.",
                    "max_input_chars", 0)
    user_text = "\n".join(t for m in messages if m.get("role") == "user" for t in _texts(m))
    for name, pattern in guard.block:
        if pattern.search(user_text):
            return f"Refused by this model's guard ({name}).", f"block:{name}", 0
    masked = 0
    if guard.redact_secrets:
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                message["content"], n = redact(content)
                masked += n
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"], n = redact(part["text"])
                        masked += n
    if guard.system_prompt:
        if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
            messages[0]["content"] = guard.system_prompt + "\n\n" + messages[0]["content"]
        else:
            messages.insert(0, {"role": "system", "content": guard.system_prompt})
    return "", "", masked


def guard_messages(guard: Guard, text: str) -> list[dict[str, Any]]:
    policy = f"\nCompany policy: {guard.policy}" if guard.policy else ""
    return [{"role": "user", "content": GUARD_PROMPT.format(policy=policy, text=text[-6000:])}]


def verdict_unsafe(answer: str) -> bool:
    """Llama Guard says "safe"/"unsafe\\nS1"; a general model asked GUARD_PROMPT says SAFE/UNSAFE."""
    words = re.findall(r"[a-z]+", (answer or "").lower())
    return "unsafe" in words[:3]


class OutputMasker:
    """Masks patterns in a streamed answer: holds back a little text so a match split across chunks is caught."""

    def __init__(self, patterns: list[re.Pattern[str]]) -> None:
        self.patterns = patterns
        self.pending = ""
        self.masked = 0

    def _mask(self, text: str) -> str:
        for pattern in self.patterns:
            text, n = pattern.subn(MASK, text)
            self.masked += n
        return text

    def feed(self, text: str) -> str:
        if not self.patterns:
            return text
        self.pending = self._mask(self.pending + text)
        if len(self.pending) <= HOLD_BACK:
            return ""
        out, self.pending = self.pending[:-HOLD_BACK], self.pending[-HOLD_BACK:]
        return out

    def flush(self) -> str:
        out, self.pending = self._mask(self.pending), ""
        return out

    def whole(self, text: str) -> str:
        return self._mask(text) if self.patterns else text
