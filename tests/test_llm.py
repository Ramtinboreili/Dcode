from __future__ import annotations

import json

from dcode.llm import AssistantTurn, LLMClient, parse_text_tool_calls, strip_thinking

KNOWN = ["read_file", "run_command"]


def test_strip_thinking():
    assert strip_thinking("<think>plan...</think>Answer") == "Answer"
    assert strip_thinking("<think>never closed") == ""
    assert strip_thinking("plain") == "plain"


def test_parse_hermes_tool_call_tags():
    text = 'Let me look.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'
    rest, calls = parse_text_tool_calls(text, KNOWN)
    assert rest == "Let me look."
    assert calls[0]["function"]["name"] == "read_file"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}


def test_parse_fenced_and_bare_json():
    _, calls = parse_text_tool_calls('```json\n{"name": "run_command", "parameters": {"command": "ls"}}\n```', KNOWN)
    assert calls[0]["function"]["name"] == "run_command"
    _, calls = parse_text_tool_calls('{"name": "read_file", "arguments": "{\\"path\\": \\"b\\"}"}', KNOWN)
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "b"}


def test_unknown_tools_and_prose_are_left_alone():
    text = 'Here is JSON: {"name": "delete_everything", "arguments": {}}'
    assert parse_text_tool_calls(text, KNOWN) == (text, [])
    assert parse_text_tool_calls("just words", KNOWN) == ("just words", [])


def test_finish_recovers_calls_only_when_tools_offered():
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    turn = LLMClient._finish(AssistantTurn(content='<tool_call>{"name":"read_file","arguments":{"path":"x"}}</tool_call>'), tools)
    assert turn.recovered_tool_calls and turn.tool_calls
    turn = LLMClient._finish(AssistantTurn(content='{"name":"read_file","arguments":{}}'), None)
    assert not turn.tool_calls
