"""Model families: each one reasons and calls tools its own way.

``detect()`` reads the model name (and the server URL) and returns a ``Family``: how to turn its
reasoning up or down, which request fields it needs, and what it expects back in the history.
``request_extras()`` turns Dcode's one setting (``reasoning_effort``) into that family's fields.

Only request *shaping* lives here. Tool calls a model writes as text are parsed for every family
(``toolcalls.py``): a wrong guess about the family must never lose a call. Whatever a server
still rejects is learned from its error by ``LLMClient`` and not sent again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

EFFORTS = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class Family:
    key: str
    label: str
    # How reasoning is turned up or down:
    #   effort    - reasoning_effort (OpenAI reasoning models, gpt-oss, Gemini, most gateways)
    #   template  - a chat-template switch (Qwen3, GLM, DeepSeek V3.1+ on vLLM / SGLang / llama.cpp)
    #   anthropic - a thinking budget (Claude)
    #   fixed     - the model decides; nothing to send (DeepSeek R1, MiniMax M2, QwQ)
    thinking: str = "effort"
    template_key: str = "enable_thinking"  # the chat_template_kwargs switch a template reads
    least_effort: str = ""      # the lowest reasoning_effort this family accepts ("" = send nothing)
    completion_tokens: bool = False  # wants max_completion_tokens instead of max_tokens
    echo_reasoning: bool = False     # the API wants reasoning_content back during a tool loop
    short_call_ids: bool = False     # tool call ids must be 9 letters/digits (Mistral)
    system_role: bool = True         # the chat template accepts a system message
    note: str = ""                   # what Dcode does differently, for /models


GENERIC = Family("generic", "Other", note="standard OpenAI-style requests")

FAMILIES: dict[str, Family] = {f.key: f for f in (
    GENERIC,
    Family("openai", "GPT", note="standard OpenAI requests"),
    Family("openai-reasoning", "GPT reasoning (o-series, GPT-5)", least_effort="low", completion_tokens=True,
           note="reasoning_effort, max_completion_tokens"),
    Family("gpt-oss", "gpt-oss", least_effort="low", note="reasoning_effort low/medium/high; its tool names are cleaned"),
    Family("claude", "Claude", thinking="anthropic", note="a thinking budget on high effort"),
    Family("gemini", "Gemini", least_effort="low", note="reasoning_effort; keeps thought signatures on tool calls"),
    Family("gemini-flash", "Gemini Flash", least_effort="none",
           note="reasoning_effort (none switches thinking off); keeps thought signatures"),
    Family("gemma", "Gemma", thinking="fixed", note="reads tool_code blocks as tool calls"),
    Family("gemma-legacy", "Gemma 2", thinking="fixed", system_role=False,
           note="no system role: the instructions go into the first message"),
    Family("qwen", "Qwen3", thinking="template", note="thinking on/off through the chat template"),
    Family("qwen-coder", "Qwen3-Coder", thinking="fixed", note="reads its XML tool calls"),
    Family("qwq", "QwQ", thinking="fixed", note="always thinks: long thoughts are cut short"),
    Family("deepseek", "DeepSeek", thinking="template", template_key="thinking",
           note="thinking on/off through the chat template; reads its tool-call tokens"),
    Family("deepseek-reasoner", "DeepSeek reasoner", thinking="fixed", echo_reasoning=True,
           note="always thinks; reasoning goes back during a tool loop"),
    Family("glm", "GLM", thinking="template", note="thinking on/off; reads its arg_key/arg_value tool calls"),
    Family("kimi", "Kimi", thinking="fixed", note="cleans 'functions.name:0' tool names"),
    Family("kimi-thinking", "Kimi thinking", thinking="fixed", echo_reasoning=True,
           note="reasoning goes back during a tool loop"),
    Family("minimax", "MiniMax", thinking="fixed", note="reads its XML tool calls"),
    Family("mistral", "Mistral", thinking="fixed", short_call_ids=True, note="9-character tool call ids"),
    Family("magistral", "Magistral", thinking="fixed", short_call_ids=True, note="9-character tool call ids"),
    Family("llama", "Llama", thinking="fixed", note="reads <|python_tag|> and <function=…> tool calls"),
    Family("phi", "Phi", thinking="fixed"),
    Family("granite", "Granite", thinking="fixed"),
    Family("command", "Command (Cohere)", thinking="fixed"),
)}

# First match wins, so the specific names come before the general ones.
_RULES: list[tuple[str, str]] = [
    (r"gpt-?oss", "gpt-oss"),
    (r"(^|[^a-z])(o[134](-mini|-pro)?|gpt-5[\w.-]*)($|[^a-z])", "openai-reasoning"),
    (r"gpt-|chatgpt|davinci|turbo", "openai"),
    (r"claude|anthropic", "claude"),
    (r"gemini[\w.-]*flash", "gemini-flash"),
    (r"gemini", "gemini"),
    (r"(code)?gemma-?(1|2)([^\d.]|$)|gemma2|codegemma", "gemma-legacy"),
    (r"gemma", "gemma"),
    (r"deepseek[\w.-]*(r1|reasoner)|r1-distill|deepseek-?(v3\.2|v3-2)[\w.-]*(think|speciale)", "deepseek-reasoner"),
    (r"qwq", "qwq"),
    (r"qwen[\w.-]*coder", "qwen-coder"),
    (r"qwen", "qwen"),
    (r"deepseek", "deepseek"),
    (r"glm|chatglm|zhipu|codegeex", "glm"),
    (r"kimi[\w.-]*think", "kimi-thinking"),
    (r"kimi|moonshot", "kimi"),
    (r"minimax|abab", "minimax"),
    (r"magistral", "magistral"),
    (r"mistral|mixtral|devstral|codestral|ministral|pixtral", "mistral"),
    (r"llama|codellama", "llama"),
    (r"(^|[^a-z])phi-?\d", "phi"),
    (r"granite", "granite"),
    (r"command-?(r|a)", "command"),
]

# Hosted APIs, by host name: they tell the family when the name alone doesn't, and how to switch reasoning.
_SERVERS: list[tuple[str, str]] = [
    ("api.openai.com", "openai"), ("openai.azure.com", "openai"), ("api.anthropic.com", "anthropic"),
    ("generativelanguage.googleapis.com", "google"), ("aiplatform.googleapis.com", "google"),
    ("api.deepseek.com", "deepseek"), ("bigmodel.cn", "zai"), ("api.z.ai", "zai"),
    ("moonshot", "moonshot"), ("dashscope", "dashscope"), ("api.mistral.ai", "mistral"),
    ("openrouter.ai", "openrouter"), ("api.minimax", "minimax"),
]
_SERVER_FAMILY = {"openai": "openai", "anthropic": "claude", "google": "gemini", "deepseek": "deepseek",
                  "zai": "glm", "moonshot": "kimi", "dashscope": "qwen", "mistral": "mistral", "minimax": "minimax"}


def server_kind(base_url: str) -> str:
    """'ollama', 'local' (vLLM, SGLang, llama.cpp, LM Studio…) or a hosted API's short name."""
    url = (base_url or "").lower()
    host = urlparse(url).hostname or ""
    for needle, kind in _SERVERS:
        if needle in host:
            return kind
    if "ollama" in url or urlparse(url).port == 11434:
        return "ollama"
    return "local"


def _model_key(model: str) -> str:
    """'accounts/fireworks/models/Qwen3-32B', 'ollama/qwen3:32b' → 'qwen3-32b' (vendor prefix and tag dropped)."""
    name = (model or "").strip().lower().rsplit("/", 1)[-1]
    return name.split(":", 1)[0] if ":" in name and not name.startswith("ft:") else name


def detect(model: str, base_url: str = "", override: str = "") -> Family:
    """The family for a model name (``override`` = a family key the user chose, or 'auto')."""
    if override and override != "auto" and override in FAMILIES:
        return FAMILIES[override]
    full = (model or "").lower()
    name = _model_key(model)
    for pattern, key in _RULES:
        if re.search(pattern, name) or re.search(pattern, full):
            if key == "qwen" and re.search(r"qwen-?(1|2)(\.\d)?([^\d]|$)", name) and "qwen3" not in name:
                return FAMILIES["generic"]  # Qwen 1/2/2.5 have no thinking switch
            return FAMILIES[key]
    return FAMILIES.get(_SERVER_FAMILY.get(server_kind(base_url), ""), GENERIC)


def _thinking_budget(effort: str, max_tokens: int | None) -> int:
    budget = {"low": 1024, "medium": 4096, "high": 12000}.get(effort, 0)
    if max_tokens:
        budget = min(budget, max_tokens - 512)
    return budget if budget >= 1024 else 0


def request_extras(family: Family, server: str, effort: str, max_tokens: int | None = None) -> dict:
    """The fields this family/server pair needs for the chosen reasoning effort (sent in the request body)."""
    effort = effort if effort in EFFORTS else "medium"
    if server == "manager":  # a Dcode Manager translates for the model's own server
        return {"reasoning_effort": effort}
    if server == "openrouter":  # one shape for every model behind it ("exclude" only hides the thinking)
        return {"reasoning": {"effort": effort}} if effort != "none" else {"reasoning": {"enabled": False}}
    if family.thinking == "effort":
        if effort == "none":
            return {"reasoning_effort": family.least_effort} if family.least_effort else {}
        return {"reasoning_effort": effort}
    if family.thinking == "anthropic":
        if server != "anthropic":  # gateways (LiteLLM and the like) map reasoning_effort themselves
            return {"reasoning_effort": effort} if effort != "none" else {}
        budget = _thinking_budget(effort, max_tokens) if effort == "high" else 0
        return {"thinking": {"type": "enabled", "budget_tokens": budget}} if budget else {}
    if family.thinking == "template":
        on = effort != "none"
        if server == "zai":
            return {"thinking": {"type": "enabled" if on else "disabled"}}
        if server == "dashscope":
            return {"enable_thinking": on}
        if server == "ollama":
            return {"think": on}
        if server == "local":
            return {"chat_template_kwargs": {family.template_key: on}}
        return {}
    return {}  # fixed: the model decides


def describe(model: str, base_url: str = "", override: str = "") -> str:
    """One line for /models and the settings screen."""
    family = detect(model, base_url, override)
    how = {"effort": "reasoning_effort", "template": "chat-template switch", "anthropic": "thinking budget",
           "fixed": "the model decides"}[family.thinking]
    picked = "" if not override or override == "auto" else " (set by you)"
    return f"{family.label}{picked} · reasoning: {how}" + (f" · {family.note}" if family.note else "")
