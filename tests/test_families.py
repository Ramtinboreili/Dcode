"""Model families: detection from the name and server, and the request fields each one gets."""

from __future__ import annotations

import pytest

from dcode.families import FAMILIES, describe, detect, request_extras, server_kind


@pytest.mark.parametrize("model, family", [
    ("gpt-4o-mini", "openai"),
    ("gpt-4.1", "openai"),
    ("o3-mini", "openai-reasoning"),
    ("o4-mini", "openai-reasoning"),
    ("gpt-5", "openai-reasoning"),
    ("gpt-5.1-codex", "openai-reasoning"),
    ("openai/gpt-oss-120b", "gpt-oss"),
    ("gpt-oss:20b", "gpt-oss"),
    ("claude-sonnet-4-5", "claude"),
    ("anthropic/claude-3.7-sonnet", "claude"),
    ("gemini-2.5-pro", "gemini"),
    ("gemini-2.5-flash-lite", "gemini-flash"),
    ("gemma4:31b", "gemma"),
    ("google/gemma-3-27b-it", "gemma"),
    ("gemma2:9b", "gemma-legacy"),
    ("codegemma:7b", "gemma-legacy"),
    ("diffusiongemma-26B-A4B-it", "gemma"),
    ("Qwen/Qwen3-32B", "qwen"),
    ("qwen3:30b-a3b", "qwen"),
    ("Qwen/Qwen3-Coder-30B-A3B-Instruct", "qwen-coder"),
    ("qwen2.5-coder:14b", "qwen-coder"),
    ("Qwen/Qwen2.5-72B-Instruct", "generic"),
    ("qwq:32b", "qwq"),
    ("deepseek-ai/DeepSeek-V3.1", "deepseek"),
    ("deepseek-chat", "deepseek"),
    ("deepseek-reasoner", "deepseek-reasoner"),
    ("deepseek-r1:14b", "deepseek-reasoner"),
    ("DeepSeek-R1-Distill-Qwen-32B", "deepseek-reasoner"),
    ("zai-org/GLM-4.6", "glm"),
    ("glm-4.5-air", "glm"),
    ("moonshotai/Kimi-K2-Instruct", "kimi"),
    ("kimi-k2-thinking", "kimi-thinking"),
    ("MiniMaxAI/MiniMax-M2", "minimax"),
    ("mistralai/Devstral-Small-2507", "mistral"),
    ("mistral-large-latest", "mistral"),
    ("magistral-medium", "magistral"),
    ("meta-llama/Llama-3.3-70B-Instruct", "llama"),
    ("llama3.1:8b", "llama"),
    ("phi-4", "phi"),
    ("granite-3.3-8b-instruct", "granite"),
    ("command-r-plus", "command"),
    ("my-finetune", "generic"),
])
def test_the_family_comes_from_the_model_name(model, family):
    assert detect(model, "http://172.20.23.64:31000/v1").key == family


def test_the_server_fills_in_when_the_name_says_nothing():
    assert server_kind("https://api.deepseek.com/v1") == "deepseek"
    assert server_kind("http://localhost:11434/v1") == "ollama"
    assert server_kind("http://172.20.23.64:31000/v1") == "local"
    assert server_kind("https://openrouter.ai/api/v1") == "openrouter"
    assert detect("my-deployment", "https://api.z.ai/api/paas/v4").key == "glm"
    assert detect("custom", "https://generativelanguage.googleapis.com/v1beta/openai").key == "gemini"


def test_the_user_can_pick_the_family():
    assert detect("my-finetune", "", "qwen").key == "qwen"
    assert detect("gpt-4o", "", "auto").key == "openai"
    assert detect("gpt-4o", "", "no-such-family").key == "openai"
    assert "set by you" in describe("my-finetune", "", "qwen")
    assert describe("Qwen/Qwen3-32B").startswith("Qwen3 · reasoning: chat-template switch")


def test_each_family_turns_its_reasoning_down_its_own_way():
    qwen, glm, gpt5 = FAMILIES["qwen"], FAMILIES["glm"], FAMILIES["openai-reasoning"]
    # Qwen3 / GLM on vLLM, SGLang or llama.cpp: the chat-template switch
    assert request_extras(qwen, "local", "none") == {"chat_template_kwargs": {"enable_thinking": False}}
    assert request_extras(qwen, "local", "high") == {"chat_template_kwargs": {"enable_thinking": True}}
    assert request_extras(FAMILIES["deepseek"], "local", "low") == {"chat_template_kwargs": {"thinking": True}}
    assert request_extras(qwen, "ollama", "none") == {"think": False}
    assert request_extras(qwen, "dashscope", "none") == {"enable_thinking": False}
    assert request_extras(glm, "zai", "none") == {"thinking": {"type": "disabled"}}
    # OpenAI reasoning models: reasoning_effort, never below what they accept
    assert request_extras(gpt5, "openai", "high") == {"reasoning_effort": "high"}
    assert request_extras(gpt5, "openai", "none") == {"reasoning_effort": "low"}
    assert request_extras(FAMILIES["gemini-flash"], "google", "none") == {"reasoning_effort": "none"}
    assert request_extras(FAMILIES["gemini"], "google", "none") == {"reasoning_effort": "low"}
    # Claude on Anthropic's API: a thinking budget on high only, inside the reply cap
    assert request_extras(FAMILIES["claude"], "anthropic", "medium") == {}
    assert request_extras(FAMILIES["claude"], "anthropic", "high", 8192) == \
        {"thinking": {"type": "enabled", "budget_tokens": 7680}}
    assert request_extras(FAMILIES["claude"], "local", "high") == {"reasoning_effort": "high"}  # a gateway maps it
    # Models that decide for themselves get nothing they could reject
    assert request_extras(FAMILIES["deepseek-reasoner"], "local", "high") == {}
    assert request_extras(FAMILIES["gemma"], "local", "medium") == {}
    # Unknown models keep the old behaviour
    assert request_extras(FAMILIES["generic"], "local", "medium") == {"reasoning_effort": "medium"}
    assert request_extras(FAMILIES["generic"], "local", "none") == {}
    assert request_extras(qwen, "openrouter", "low") == {"reasoning": {"effort": "low"}}
    # "exclude" would only hide it: the model still thinks (and the tokens are billed)
    assert request_extras(qwen, "openrouter", "none") == {"reasoning": {"enabled": False}}
