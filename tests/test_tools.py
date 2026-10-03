from __future__ import annotations

import pytest

from dcode import tools


def test_write_then_read_numbered():
    assert "Wrote" in tools.execute_tool("write_file", {"path": "a.txt", "content": "hello\nworld"})
    assert tools.execute_tool("read_file", {"path": "a.txt"}) == "1| hello\n2| world"


def test_read_line_range():
    tools.execute_tool("write_file", {"path": "f.txt", "content": "\n".join(f"l{i}" for i in range(1, 51))})
    out = tools.execute_tool("read_file", {"path": "f.txt", "start_line": 10, "end_line": 12})
    assert out.splitlines()[:3] == ["10| l10", "11| l11", "12| l12"]
    assert "lines 10-12 of 50" in out


def test_read_missing_file():
    assert "does not exist" in tools.execute_tool("read_file", {"path": "nope.txt"})


def test_edit_file_unique_match(isolated):
    (isolated / "c.py").write_text("x = 1\ny = 2\n")
    assert "Edited" in tools.execute_tool("edit_file", {"path": "c.py", "old_text": "y = 2", "new_text": "y = 3"})
    assert (isolated / "c.py").read_text() == "x = 1\ny = 3\n"


def test_edit_file_ambiguous_and_missing(isolated):
    (isolated / "c.py").write_text("a\na\n")
    assert "matches 2" in tools.execute_tool("edit_file", {"path": "c.py", "old_text": "a", "new_text": "b"})
    assert "not found" in tools.execute_tool("edit_file", {"path": "c.py", "old_text": "zzz", "new_text": "b"})
    assert "Edited" in tools.execute_tool("edit_file", {"path": "c.py", "old_text": "a", "new_text": "b", "replace_all": True})
    assert (isolated / "c.py").read_text() == "b\nb\n"


def test_edit_file_tolerates_pasted_line_numbers(isolated):
    (isolated / "c.py").write_text("def f():\n    return 1\n")
    out = tools.execute_tool("edit_file", {"path": "c.py", "old_text": "2|     return 1", "new_text": "2|     return 2"})
    assert "Edited" in out
    assert (isolated / "c.py").read_text() == "def f():\n    return 2\n"


def test_sandbox_blocks_absolute_escape():
    assert tools.execute_tool("read_file", {"path": "/etc/hosts"}).startswith("Blocked:")


def test_sandbox_blocks_parent_escape(isolated):
    out = tools.execute_tool("write_file", {"path": "../evil.txt", "content": "x"})
    assert out.startswith("Blocked:")
    assert not (isolated.parent / "evil.txt").exists()


def test_sandbox_override(monkeypatch):
    monkeypatch.setenv("DCODE_ALLOW_OUTSIDE_WORKSPACE", "1")
    assert not tools.execute_tool("read_file", {"path": "/etc/hostname"}).startswith("Blocked:")


def test_read_size_limit(monkeypatch):
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 4)
    tools.execute_tool("write_file", {"path": "big.txt", "content": "0123456789"})
    assert "too large" in tools.execute_tool("read_file", {"path": "big.txt"})


def test_list_ignores_noise_and_respects_depth(isolated):
    (isolated / "node_modules").mkdir()
    (isolated / "node_modules" / "junk.js").write_text("x")
    (isolated / "keep.py").write_text("x")
    (isolated / "a" / "b" / "c").mkdir(parents=True)
    (isolated / "a" / "b" / "c" / "deep.py").write_text("x")
    out = tools.execute_tool("list_files", {"path": "."})
    assert "keep.py" in out
    assert "node_modules" not in out
    assert "deep.py" not in out  # beyond default depth 2
    assert "deep.py" in tools.execute_tool("list_files", {"path": ".", "max_depth": 5})


def test_search_regex_and_glob(isolated):
    (isolated / "f.txt").write_text("needle here\nother line\n")
    (isolated / "g.py").write_text("def needle_fn():\n    pass\n")
    assert "f.txt:1" in tools.execute_tool("search_in_files", {"pattern": "needle", "path": "."})
    out = tools.execute_tool("search_in_files", {"pattern": r"def \w+", "glob": "*.py"})
    assert "g.py:1" in out and "f.txt" not in out
    # Invalid regex falls back to a literal search instead of erroring.
    assert "(no matches)" == tools.execute_tool("search_in_files", {"pattern": "(unclosed"})


def test_parse_arguments_invalid():
    assert tools.parse_arguments("not json") == {}
    assert tools.parse_arguments('{"a": 1}') == {"a": 1}
    assert tools.parse_arguments("[1]") == {}


def test_run_command_truncation_keeps_tail(monkeypatch):
    monkeypatch.setattr(tools, "MAX_OUTPUT_CHARS", 10)
    out = tools.execute_tool("run_command", {"command": "printf '%s' 0123456789ABCDEFGHIJ"})
    assert "truncated" in out
    assert out.endswith("FGHIJ")


def test_truncate_honors_runtime_limit(monkeypatch):
    monkeypatch.setattr(tools, "MAX_OUTPUT_CHARS", 5)
    assert "truncated" in tools._truncate("0123456789")


@pytest.mark.parametrize("window", [4096, 32768, 200000])
def test_configure_limits_scales(window, monkeypatch):
    for name in ("MAX_OUTPUT_CHARS", "MAX_READ_LINES", "MAX_LIST_ENTRIES", "MAX_SEARCH_MATCHES"):
        monkeypatch.setattr(tools, name, getattr(tools, name))
    tools.configure_limits(window)
    assert 3_000 <= tools.MAX_OUTPUT_CHARS <= 60_000
    assert tools.MAX_OUTPUT_CHARS <= max(3_000, window * 3.5 * 0.12)


def test_permissions_hide_tools():
    names = tools.allowed_tool_names({"write_file": False, "run_command": False})
    assert "write_file" not in names and "edit_file" not in names and "run_command" not in names
    assert "list_files" in names
