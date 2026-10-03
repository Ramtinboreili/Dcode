from __future__ import annotations

import json
import os
import stat

import pytest

from dcode import config


def _cfg():
    return config.AppConfig(base_url="http://localhost:11434/v1", api_key="secret", model="qwen2.5")


def test_save_and_load_roundtrip():
    cfg = _cfg()
    cfg.enabled_skills = ["code_review.md"]
    cfg.agents["max_parallel"] = 3
    config.save_config(cfg)
    loaded = config.load_config()
    assert loaded is not None
    assert loaded.model == "qwen2.5"
    assert loaded.enabled_skills == ["code_review.md"]
    assert loaded.agents["max_parallel"] == 3
    assert loaded.context_window == config.DEFAULT_CONTEXT_WINDOW


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_config_file_is_private():
    config.save_config(_cfg())
    assert stat.S_IMODE(config.config_file().stat().st_mode) == 0o600


def test_corrupt_config_does_not_crash():
    config.ensure_app_dirs()
    config.config_file().write_text("{ this is not json")
    assert config.load_config() is None


def test_env_overrides_win(monkeypatch):
    config.save_config(_cfg())
    monkeypatch.setenv("DCODE_MODEL", "llama3")
    monkeypatch.setenv("DCODE_CONTEXT_WINDOW", "8192")
    loaded = config.load_config()
    assert loaded.model == "llama3"
    assert loaded.context_window == 8192


def test_legacy_env_names_still_work(monkeypatch):
    monkeypatch.setenv("CODE_ONE_BASE_URL", "http://gpu.local/v1")
    monkeypatch.setenv("CODE_ONE_API_KEY", "x")
    monkeypatch.setenv("CODE_ONE_MODEL", "llama3")
    loaded = config.load_config()
    assert loaded is not None and loaded.base_url == "http://gpu.local/v1"


def test_unknown_and_bad_values_are_ignored():
    config.ensure_app_dirs()
    config.config_file().write_text(json.dumps({
        "base_url": "u", "api_key": "k", "model": "m", "context_window": "lots",
        "agents": {"max_parallel": "x", "bogus": 1}, "reasoning_effort": "extreme",
    }))
    loaded = config.load_config()
    assert loaded.context_window == config.DEFAULT_CONTEXT_WINDOW
    assert loaded.agents["max_parallel"] == config.DEFAULT_AGENTS["max_parallel"]
    assert "bogus" not in loaded.agents
    assert loaded.reasoning_effort == config.DEFAULT_REASONING_EFFORT


def test_threads_roundtrip():
    config.save_thread("my thread", [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                       extra={"cwd": "/w"})
    threads = config.list_threads()
    assert threads[0]["name"] == "my thread"
    assert threads[0]["id"] == "my-thread"
    assert threads[0]["cwd"] == "/w"
