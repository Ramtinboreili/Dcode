from __future__ import annotations

import json
import os
import stat

import pytest

from code_one import config


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", home / ".code-one")
    monkeypatch.setattr(config, "CONFIG_FILE", home / ".code-one" / "config.json")
    monkeypatch.setattr(config, "THREADS_DIR", home / ".code-one" / "threads")
    monkeypatch.setattr(config, "SKILLS_DIR", home / ".code-one" / "skills")
    for var in ("CODE_ONE_BASE_URL", "CODE_ONE_API_KEY", "CODE_ONE_MODEL", "CODE_ONE_REASONING_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    return home


def _cfg():
    return config.AppConfig(base_url="http://localhost:11434/v1", api_key="secret", model="qwen2.5")


def test_save_and_load_roundtrip(isolated_home):
    cfg = _cfg()
    cfg.enabled_skills = ["code_review.md"]
    config.save_config(cfg)
    loaded = config.load_config()
    assert loaded is not None
    assert loaded.model == "qwen2.5"
    assert loaded.enabled_skills == ["code_review.md"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_config_file_is_private(isolated_home):
    config.save_config(_cfg())
    mode = stat.S_IMODE(config.CONFIG_FILE.stat().st_mode)
    assert mode == 0o600


def test_corrupt_config_does_not_crash(isolated_home):
    config.ensure_app_dirs()
    config.CONFIG_FILE.write_text("{ this is not json")
    assert config.load_config() is None


def test_env_overrides_win(isolated_home, monkeypatch):
    config.save_config(_cfg())
    monkeypatch.setenv("CODE_ONE_MODEL", "llama3")
    monkeypatch.setenv("CODE_ONE_BASE_URL", "http://gpu.local/v1")
    loaded = config.load_config()
    assert loaded.model == "llama3"
    assert loaded.base_url == "http://gpu.local/v1"


def test_env_only_config_no_file(isolated_home, monkeypatch):
    monkeypatch.setenv("CODE_ONE_BASE_URL", "http://gpu.local/v1")
    monkeypatch.setenv("CODE_ONE_API_KEY", "x")
    monkeypatch.setenv("CODE_ONE_MODEL", "llama3")
    loaded = config.load_config()
    assert loaded is not None
    assert loaded.model == "llama3"
