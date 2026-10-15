"""LLMClient against a scripted OpenAI-style API: what each family gets, and what it learns from errors."""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import make_config

from dcode.llm import (
    AssistantTurn,
    LLMClient,
    clean_tool_name,
    coerce_arguments,
    strip_thinking,
    thinking_text,
)

TOOLS = [{"type": "function", "function": {
    "name": name, "description": f"{name} tool",
    "parameters": {"type": "object", "properties": props, "required": list(props)[:1]}}}
    for name, props in (("read_file", {"path": {"type": "string"}, "start_line": {"type": "integer"}}),
                        ("run_command", {"command": {"type": "string"}, "background": {"type": "boolean"}}))]


class Rejected(Exception):
    status_code = 400


def reply(content="", tool_calls=None, finish="stop", reasoning=None, extra=None):
    calls = [SimpleNamespace(model_dump=lambda exclude_none=True, c=c: c) for c in (tool_calls or [])]
    message = SimpleNamespace(content=content, tool_calls=calls or None, model_extra=dict(extra or {}))
    if reasoning:
        message.reasoning_content = reasoning
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=None)


def chunk(content=None, reasoning=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=None, model_extra={})
    if reasoning:
        delta.reasoning_content = reasoning
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=finish, delta=delta)])


class FakeAPI:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        body = kwargs.pop("extra_body", None) or {}
        self.requests.append({**kwargs, **body})
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item) if isinstance(item, list) else item


def client(*replies, **config) -> tuple[LLMClient, FakeAPI]:
    llm = LLMClient(make_config(**config))
    llm.client = FakeAPI(*replies)
    return llm, llm.client


def test_each_family_gets_its_own_request_fields():
    llm, api = client(reply("hi"), model="Qwen/Qwen3-32B", base_url="http://10.0.0.5:8000/v1",
                      reasoning_effort="none")
    llm.chat([{"role": "user", "content": "hi"}], max_tokens=1000)
    assert api.requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert api.requests[0]["max_tokens"] == 1000 and "reasoning_effort" not in api.requests[0]

    llm, api = client(reply("hi"), model="gpt-5-mini", base_url="https://api.openai.com/v1", reasoning_effort="high")
    llm.chat([{"role": "user", "content": "hi"}], max_tokens=1000)
    assert api.requests[0]["max_completion_tokens"] == 1000 and "max_tokens" not in api.requests[0]
    assert api.requests[0]["reasoning_effort"] == "high"


def test_side_jobs_ask_for_no_thinking():
    llm, api = client(reply("<think>hmm</think>Fix the parser"), model="qwen3:8b", reasoning_effort="high")
    assert llm.complete_text([{"role": "user", "content": "title?"}], max_tokens=40) == "Fix the parser"
    assert api.requests[0]["think"] is False  # Ollama


def test_a_rejected_field_is_dropped_and_remembered():
    llm, api = client(Rejected("Unrecognized request argument supplied: chat_template_kwargs"), reply("a"), reply("b"),
                      model="glm-4.6", base_url="http://gw.local/v1")
    llm.chat([{"role": "user", "content": "hi"}])
    llm.chat([{"role": "user", "content": "again"}])
    assert "chat_template_kwargs" in api.requests[0]
    assert all("chat_template_kwargs" not in r for r in api.requests[1:])
    assert len(api.requests) == 3


def test_a_context_overflow_teaches_nothing():
    llm, api = client(Rejected("This model's maximum context length is 16384 tokens"), model="gpt-4o",
                      base_url="http://gw.local/v1")
    try:
        llm.chat([{"role": "user", "content": "hi"}])
    except Rejected:
        pass
    assert llm._learned["rejected"] == set()


def test_max_completion_tokens_is_learned_from_the_error():
    llm, api = client(Rejected("Unsupported parameter: 'max_tokens' is not supported with this model. Use "
                               "'max_completion_tokens' instead."), reply("ok"), model="my-model", base_url="http://gw/v1")
    llm.chat([{"role": "user", "content": "hi"}], max_tokens=500)
    assert api.requests[1]["max_completion_tokens"] == 500 and "max_tokens" not in api.requests[1]


def test_a_server_without_tool_support_gets_the_tools_as_text():
    """vLLM started without --enable-auto-tool-choice rejects every request that carries tools."""
    error = Rejected('"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set')
    llm, api = client(error, reply('<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'),
                      reply("Done."), model="my-model", base_url="http://10.0.0.5:8000/v1")
    history = [{"role": "system", "content": "You are Dcode."}, {"role": "user", "content": "read a.py"}]
    turn = llm.chat(history, tools=TOOLS)
    assert [c["function"]["name"] for c in turn.tool_calls] == ["read_file"]
    assert not turn.recovered_tool_calls  # the normal way now, not worth a notice each time
    assert turn.notes == ["This server takes no tool definitions; Dcode describes the tools in the prompt"]
    sent = api.requests[1]
    assert "tools" not in sent and "tool_choice" not in sent
    assert "read_file(path, start_line?)" in sent["messages"][0]["content"]
    # The next step: the call and its result go back as text too.
    history += [turn.as_message(), {"role": "tool", "tool_call_id": turn.tool_calls[0]["id"], "name": "read_file",
                                    "content": "1| x = 1"}]
    llm.chat(history, tools=TOOLS)
    sent = api.requests[2]["messages"]
    assert sent[2] == {"role": "assistant", "content":
                       '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'}
    assert sent[3] == {"role": "user", "content": "[result of read_file]\n1| x = 1"}
    assert history[2].get("tool_calls")  # the stored history keeps its real shape


def test_a_template_without_a_system_role_gets_the_instructions_in_the_first_message():
    llm, api = client(Rejected("System role not supported"), reply("ok"), model="my-model", base_url="http://x/v1")
    llm.chat([{"role": "system", "content": "Rules."}, {"role": "user", "content": "hi"}])
    assert api.requests[1]["messages"] == [{"role": "user", "content": "Rules.\n\n---\n\nhi"}]


def test_mistral_gets_nine_character_call_ids_and_strict_turns():
    llm, api = client(Rejected("Conversation roles must alternate user/assistant/user/assistant/..."), reply("ok"),
                      model="mistralai/Devstral-Small-2507", base_url="http://x/v1")
    history = [{"role": "user", "content": "go"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": "call_0123456789ab", "type": "function",
                                                                    "function": {"name": "git_status", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "call_0123456789ab", "name": "git_status", "content": "clean"},
               {"role": "user", "content": "Continue."}, {"role": "user", "content": "And commit."}]
    llm.chat(history)
    sent = api.requests[1]["messages"]
    call_id = sent[1]["tool_calls"][0]["id"]
    assert len(call_id) == 9 and call_id.isalnum() and sent[2]["tool_call_id"] == call_id
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "assistant", "user"]
    assert sent[4]["content"] == "Continue.\n\nAnd commit."
    assert history[1]["tool_calls"][0]["id"] == "call_0123456789ab"


def test_reasoning_goes_back_only_to_the_apis_that_want_it():
    step = {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    history = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "", "tool_calls": [step]},
               {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "x"}]
    llm, api = client(reply("done"), model="kimi-k2-thinking", base_url="https://api.moonshot.ai/v1")
    llm.keep_reasoning([step], "I should read a first.")
    llm.chat(history)
    assert api.requests[0]["messages"][1]["reasoning_content"] == "I should read a first."
    assert "reasoning_content" not in history[1]  # never stored

    llm, api = client(reply("done"), model="qwen3:8b")
    llm.keep_reasoning([step], "I should read a first.")
    llm.chat(history)
    assert "reasoning_content" not in api.requests[0]["messages"][1]

    llm, api = client(Rejected("Missing `reasoning_content` field in the assistant message"), reply("done"),
                      model="deepseek-chat", base_url="https://api.deepseek.com/v1")
    llm.keep_reasoning([step], "Plan: read a.")
    llm.chat(history)
    assert api.requests[1]["messages"][1]["reasoning_content"] == "Plan: read a."


def test_gemini_thought_signatures_stay_on_the_call():
    signed = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'},
              "extra_content": {"google": {"thought_signature": "abc=="}}}
    llm, api = client(reply(tool_calls=[signed]), model="gemini-3-pro-preview",
                      base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    turn = llm.chat([{"role": "user", "content": "go"}], tools=TOOLS)
    assert turn.tool_calls[0]["extra_content"] == {"google": {"thought_signature": "abc=="}}


def test_a_reply_that_is_all_thinking_is_asked_again_with_less():
    llm, api = client(reply("", finish="length", reasoning="Let me think... " * 50), reply("The answer is 4."),
                      model="Qwen/Qwen3-32B", base_url="http://10.0.0.5:8000/v1", reasoning_effort="medium")
    turn = llm.chat([{"role": "user", "content": "2+2?"}], max_tokens=600)
    assert turn.content == "The answer is 4."
    assert api.requests[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert api.requests[1]["chat_template_kwargs"] == {"enable_thinking": True}  # low: still on, but less
    assert turn.notes == ["The model used its whole reply on thinking; asking again with low thinking"]


def test_thinking_in_circles_is_cut_off_mid_stream():
    loop = "Wait, maybe the bug is in the parser. Let me re-check the parser once more from the top. "
    stream = [chunk(reasoning=loop) for _ in range(40)]
    llm, api = client(stream, [chunk(content="Found it: line 3."), chunk(finish="stop")],
                      model="glm-4.6", base_url="http://10.0.0.5:8000/v1", stream=True, reasoning_effort="high")
    turn = llm.chat([{"role": "user", "content": "debug"}], max_tokens=4000)
    assert turn.content == "Found it: line 3."
    assert api.requests[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "circles" in turn.notes[0]


def test_a_model_that_cannot_think_less_is_not_asked_again():
    llm, api = client(reply("", finish="length", reasoning="hmm " * 300), model="deepseek-r1:14b")
    turn = llm.chat([{"role": "user", "content": "hi"}], max_tokens=600)
    assert len(api.requests) == 1 and turn.finish_reason == "length" and not turn.content


def test_inline_thinking_never_reaches_the_screen():
    parts = ["<thi", "nk>secret plan", " more</think>", "Hello", " there"]
    llm, _ = client([chunk(content=p) for p in parts] + [chunk(finish="stop")], model="glm-4.6", stream=True)
    shown: list[str] = []
    turn = llm.chat([{"role": "user", "content": "hi"}], on_delta=shown.append)
    assert "".join(shown) == "Hello there"
    assert turn.content == "Hello there" and turn.reasoning == "secret plan more"


def test_reasoning_written_before_a_lone_closing_tag():
    """Qwen3, DeepSeek-R1 and QwQ templates open <think> in the prompt; the reply only closes it."""
    assert strip_thinking("I should greet.\n</think>\n\nHello!") == "Hello!"
    assert thinking_text("I should greet.\n</think>\n\nHello!") == "I should greet."


def test_gpt_oss_channels_without_a_parser():
    text = "<|channel|>analysis<|message|>User wants hi.<|end|><|start|>assistant<|channel|>final<|message|>Hi!"
    assert strip_thinking(text) == "Hi!"
    assert thinking_text(text) == "User wants hi."


def test_tool_names_from_other_agents_and_namespaces():
    known = ["read_file", "run_command", "edit_file", "list_files"]
    assert clean_tool_name("functions.read_file:0", known) == "read_file"
    assert clean_tool_name("default_api.run_command", known) == "run_command"
    assert clean_tool_name("bash", known) == "run_command"
    assert clean_tool_name("container.exec", known) == "run_command"
    assert clean_tool_name("str_replace", known) == "edit_file"
    assert clean_tool_name("ls", known) == "list_files"
    assert clean_tool_name("bash", ["read_file"]) == "bash"  # no alias target: left for the engine to refuse


def test_arguments_get_their_schema_types():
    schema = TOOLS[1]["function"]["parameters"]
    assert json.loads(coerce_arguments('{"command": "ls", "background": "false"}', schema))["background"] is False
    schema = TOOLS[0]["function"]["parameters"]
    assert json.loads(coerce_arguments('{"path": 7, "start_line": "12"}', schema)) == {"path": "7", "start_line": 12}
    assert coerce_arguments("not json", schema) == "not json"
    turn = LLMClient._finish(AssistantTurn(content="<function=read_file><parameter=path>a</parameter>"
                                                   "<parameter=start_line>3</parameter></function>"), TOOLS)
    assert json.loads(turn.tool_calls[0]["function"]["arguments"]) == {"path": "a", "start_line": 3}


def test_helpers_on_another_model_do_not_inherit_the_main_family():
    llm = LLMClient(make_config(model="my-qwen-finetune", family="qwen"))
    assert llm.family.key == "qwen"
    helper = llm.with_model("llama3.1:8b")
    assert helper.family.key == "llama"
