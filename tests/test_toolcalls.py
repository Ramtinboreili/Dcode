"""Tool calls written as text, in each family's own format (the server ran no parser for them)."""

from __future__ import annotations

import pytest

from dcode.toolcalls import parse

KNOWN = ["read_file", "run_command", "edit_file", "git_status", "list_files"]


@pytest.mark.parametrize("family, text, expected", [
    ("hermes / Qwen", 'Reading it.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>',
     [("read_file", {"path": "a.py"})]),
    ("Qwen3-Coder", "<tool_call>\n<function=run_command>\n<parameter=command>\nls -la\n</parameter>\n</function>\n"
     "</tool_call>", [("run_command", {"command": "ls -la"})]),
    ("Qwen3-Coder unwrapped", "<function=read_file>\n<parameter=path>\nsrc/a.py\n</parameter>\n"
     "<parameter=start_line>\n10\n</parameter>\n</function>",
     [("read_file", {"path": "src/a.py", "start_line": "10"})]),
    ("Llama 3.1 function tag", '<function=read_file>{"path": "a.py"}</function>', [("read_file", {"path": "a.py"})]),
    ("Llama python_tag", '<|python_tag|>{"name": "run_command", "parameters": {"command": "pwd"}}',
     [("run_command", {"command": "pwd"})]),
    ("GLM-4.5", "Let me check.\n<tool_call>edit_file\n<arg_key>path</arg_key>\n<arg_value>a.py</arg_value>\n"
     "<arg_key>old_text</arg_key>\n<arg_value>x = 1</arg_value>\n<arg_key>new_text</arg_key>\n"
     "<arg_value>x = 2</arg_value>\n</tool_call>",
     [("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})]),
    ("GLM, no arguments", "<tool_call>git_status</tool_call>", [("git_status", {})]),
    ("DeepSeek V3 / R1", "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>read_file\n```json\n"
     '{"path": "a.py"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>', [("read_file", {"path": "a.py"})]),
    ("DeepSeek V3.1", '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>run_command<｜tool▁sep｜>{"command": "ls"}'
     "<｜tool▁call▁end｜><｜tool▁calls▁end｜>", [("run_command", {"command": "ls"})]),
    ("DeepSeek V3.2 DSML", '<｜DSML｜function_calls>\n<｜DSML｜invoke name="read_file">\n'
     '<｜DSML｜parameter name="path" string="true">a.py</｜DSML｜parameter>\n'
     '<｜DSML｜parameter name="start_line" string="false">5</｜DSML｜parameter>\n'
     "</｜DSML｜invoke>\n</｜DSML｜function_calls>", [("read_file", {"path": "a.py", "start_line": 5})]),
    ("MiniMax M2", '<minimax:tool_call>\n<invoke name="run_command">\n<parameter name="command">git log -3'
     "</parameter>\n</invoke>\n</minimax:tool_call>", [("run_command", {"command": "git log -3"})]),
    ("Kimi K2", '<|tool_calls_section_begin|><|tool_call_begin|>functions.read_file:0<|tool_call_argument_begin|>'
     '{"path": "a.py"}<|tool_call_end|><|tool_calls_section_end|>', [("read_file", {"path": "a.py"})]),
    ("Mistral v0.3", '[TOOL_CALLS][{"name": "read_file", "arguments": {"path": "a.py"}}]',
     [("read_file", {"path": "a.py"})]),
    ("Mistral / Devstral new", '[TOOL_CALLS]run_command[ARGS]{"command": "make test"}',
     [("run_command", {"command": "make test"})]),
    ("Gemma tool_code", '```tool_code\nread_file(path="a.py", start_line=3)\n```',
     [("read_file", {"path": "a.py", "start_line": 3})]),
    ("Gemini-style print", '```tool_code\nprint(default_api.run_command(command="ls"))\n```',
     [("run_command", {"command": "ls"})]),
    ("gpt-oss harmony leak", '<|channel|>commentary to=functions.run_command <|constrain|>json<|message|>'
     '{"command": "pytest -q"}<|call|>', [("run_command", {"command": "pytest -q"})]),
    ("fenced JSON", '```json\n{"name": "run_command", "parameters": {"command": "ls"}}\n```',
     [("run_command", {"command": "ls"})]),
    ("bare JSON list", '[{"name": "read_file", "arguments": {"path": "a"}}, {"name": "git_status", "arguments": {}}]',
     [("read_file", {"path": "a"}), ("git_status", {})]),
    ("OpenAI-shaped object", '{"type": "function", "function": {"name": "read_file", "arguments": "{\\"path\\": \\"b\\"}"}}',
     [("read_file", '{"path": "b"}')]),
])
def test_every_family_format_is_read(family, text, expected):
    remaining, calls = parse(text, KNOWN)
    assert calls == expected, family
    assert "<" not in remaining.replace("<think>", "") or "Let me" in remaining  # the markup is gone


def test_the_words_around_a_call_stay():
    remaining, calls = parse("Let me check.\n<tool_call>git_status</tool_call>", KNOWN)
    assert remaining == "Let me check." and calls == [("git_status", {})]


@pytest.mark.parametrize("text", [
    "just words about <function=calls> in prose",
    'Here is JSON: {"name": "delete_everything", "arguments": {}}',
    "<tool_call>rm_rf\n<arg_key>path</arg_key><arg_value>/</arg_value></tool_call>",
    '<|tool_call_begin|>functions.format_disk:0<|tool_call_argument_begin|>{}<|tool_call_end|>',
    "```tool_code\nos.system('rm -rf /')\n```",
    '{"name": "read_file", "arguments": {"path": "a"}} and then some words',
])
def test_unknown_tools_and_prose_are_left_alone(text):
    assert parse(text, KNOWN) == (text, [])
