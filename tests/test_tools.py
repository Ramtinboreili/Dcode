from __future__ import annotations

import os

import pytest

from code_one import tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODE_ONE_ALLOW_OUTSIDE_WORKSPACE", raising=False)
    monkeypatch.delenv("CODE_ONE_WORKSPACE", raising=False)
    return tmp_path


def test_write_then_read(workspace):
    assert "Wrote" in tools.execute_tool("write_file", {"path": "a.txt", "content": "hello"})
    assert tools.execute_tool("read_file", {"path": "a.txt"}) == "hello"


def test_read_missing_file(workspace):
    assert "does not exist" in tools.execute_tool("read_file", {"path": "nope.txt"})


def test_sandbox_blocks_absolute_escape(workspace):
    out = tools.execute_tool("read_file", {"path": "/etc/hosts"})
    assert out.startswith("Blocked:")


def test_sandbox_blocks_parent_escape(workspace):
    out = tools.execute_tool("write_file", {"path": "../evil.txt", "content": "x"})
    assert out.startswith("Blocked:")
    assert not (workspace.parent / "evil.txt").exists()


def test_sandbox_override(workspace, monkeypatch):
    monkeypatch.setenv("CODE_ONE_ALLOW_OUTSIDE_WORKSPACE", "1")
    out = tools.execute_tool("read_file", {"path": "/etc/hostname"})
    # Either reads it or reports missing, but is NOT blocked.
    assert not out.startswith("Blocked:")


def test_read_size_limit(workspace, monkeypatch):
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 4)
    tools.execute_tool("write_file", {"path": "big.txt", "content": "0123456789"})
    assert "too large" in tools.execute_tool("read_file", {"path": "big.txt"})


def test_list_ignores_noise_dirs(workspace):
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "junk.js").write_text("x")
    (workspace / "keep.py").write_text("x")
    out = tools.execute_tool("list_files", {"path": "."})
    assert "keep.py" in out
    assert "node_modules" not in out


def test_search(workspace):
    (workspace / "f.txt").write_text("needle here\nother line\n")
    out = tools.execute_tool("search_in_files", {"pattern": "needle", "path": "."})
    assert "f.txt:1" in out


def test_parse_arguments_invalid():
    assert tools.parse_arguments("not json") == {}
    assert tools.parse_arguments('{"a": 1}') == {"a": 1}


def test_run_command_truncation(workspace, monkeypatch):
    monkeypatch.setattr(tools, "MAX_OUTPUT_CHARS", 10)
    out = tools.execute_tool("run_command", {"command": "printf '%s' 0123456789ABCDEF"})
    assert "truncated" in out
