"""The experiment section: defaults, validation, and every key really changing what Dcode does."""

from __future__ import annotations

import json

import pytest
from conftest import FakeLLM, call, make_config, scripted

from dcode import config as config_module
from dcode import context, engine, experiment, llm, tools
from dcode.config import config_from_dict, load_config, load_raw_config, save_config
from dcode.engine import Agent, NullHost
from dcode.experiment import ExperimentError
from dcode.llm import AssistantTurn, LLMClient


def _agent(router, approve=True, **exp):
    config = make_config()
    config.experiment.update(exp)
    fake = FakeLLM(router, config)
    return Agent(config, fake, NullHost(approve=approve)), fake


def test_every_key_is_documented_tested_and_read_by_the_code():
    from pathlib import Path

    root = Path(__file__).parent.parent
    doc = (root / "docs" / "research" / "experiment-config.md").read_text()
    tests = (root / "tests" / "test_experiment.py").read_text() + (root / "tests" / "test_long_tasks.py").read_text()
    code = "".join(path.read_text() for path in (root / "dcode").glob("*.py") if path.name != "experiment.py")
    for key, default in experiment.DEFAULTS.items():
        assert f"`{key}`" in doc, f"{key} isn't in the docs"
        assert key in tests, f"{key} has no test"
        assert f'"{key}"' in code, f"nothing reads {key}"
        assert f"`{json.dumps(default)}`" in doc, f"the docs have the wrong default for {key}"


# --- the section itself ------------------------------------------------------------------------------------

def test_defaults_equal_the_constants_they_replace():
    d = experiment.DEFAULTS
    assert d["earlier_turn_min_chars"] == context.EARLIER_TURN_MIN_CHARS
    assert d["compact_trigger"] == context.COMPACT_TRIGGER and d["compact_target"] == context.COMPACT_TARGET
    assert d["keep_recent_tool_results"] == context.KEEP_RECENT_TOOL_RESULTS
    assert d["elide_min_chars"] == context.ELIDE_MIN_CHARS
    assert set(d["payload_stub_tools"]) == engine.COMPACT_ARG_TOOLS
    assert (d["think_share"], d["think_retries"], d["loop_check_every"]) == (llm.THINK_SHARE, llm.THINK_RETRIES, llm.LOOP_CHECK_EVERY)
    assert (d["repeat_limit"], d["stuck_limit"]) == (engine.REPEAT_LIMIT, engine.STUCK_LIMIT)
    assert (d["main_max_steps"], d["max_follow_ups"]) == (engine.MAIN_MAX_STEPS, engine.MAX_FOLLOW_UPS)
    tools.configure_limits(32768)
    capped = tools.MAX_OUTPUT_CHARS
    tools.configure_limits(32768, d["output_cap_share"], d["output_cap_min"], d["output_cap_max"])
    assert tools.MAX_OUTPUT_CHARS == capped


def test_a_config_without_the_section_gets_the_defaults_and_saving_adds_nothing(isolated):
    data = {"base_url": "http://x/v1", "api_key": "k", "model": "m"}
    config = config_from_dict(data)
    assert config.experiment == experiment.DEFAULTS
    save_config(config)
    assert "experiment" not in load_raw_config()


@pytest.mark.parametrize("section, word", [
    ({"earlier_turn_elison": False}, "unknown"),            # a typo must not quietly run the baseline
    ({"compact_trigger": "high"}, "compact_trigger"),
    ({"compact_trigger": 2}, "compact_trigger"),
    ({"summarization": 1}, "summarization"),
    ({"recall_placement": "assistant"}, "recall_placement"),
    ({"payload_stub_tools": "write_file"}, "payload_stub_tools"),
    ({"seed": 1.5}, "seed"),
    ({"repeat_limit": 1}, "repeat_limit"),
    ({"compact_target": 0.9, "compact_trigger": 0.5}, "compact_target"),
])
def test_anything_it_cant_honour_is_an_error(section, word):
    with pytest.raises(ExperimentError, match=word):
        experiment.clean(section)


def test_the_environment_file_overrides_the_config_and_never_reaches_the_saved_file(isolated, tmp_path, monkeypatch):
    save = {"base_url": "http://x/v1", "api_key": "k", "model": "m", "experiment": {"think_retries": 1, "seed": 7}}
    config_module.write_private_json(config_module.config_file(), save)
    path = tmp_path / "study.json"
    path.write_text(json.dumps({"seed": 42, "recall_placement": "user"}))
    monkeypatch.setenv("DCODE_EXPERIMENT", str(path))
    config = load_config()
    assert config.experiment["seed"] == 42 and config.experiment["think_retries"] == 1
    assert config.experiment["recall_placement"] == "user"
    save_config(config)  # e.g. /model: the file keeps its own section, not the override
    assert load_raw_config()["experiment"] == {"think_retries": 1, "seed": 7}


def test_a_missing_or_broken_environment_file_stops_the_run(isolated, tmp_path, monkeypatch):
    config_module.write_private_json(config_module.config_file(), {"base_url": "http://x/v1", "api_key": "k", "model": "m"})
    monkeypatch.setenv("DCODE_EXPERIMENT", str(tmp_path / "nope.json"))
    with pytest.raises(ExperimentError, match="nope.json"):
        load_config()
    (tmp_path / "bad.json").write_text("{not json")
    monkeypatch.setenv("DCODE_EXPERIMENT", str(tmp_path / "bad.json"))
    with pytest.raises(ExperimentError):
        load_config()


def test_the_hash_follows_the_effective_settings_only():
    base = experiment.config_hash(experiment.defaults())
    assert experiment.config_hash({}) == base
    assert experiment.config_hash({"seed": None, "think_retries": 2}) == base  # spelled-out defaults
    assert experiment.config_hash({"think_retries": 1}) != base
    a, b = {"seed": 1, "think_retries": 1}, {"think_retries": 1, "seed": 1}
    assert experiment.config_hash(a) == experiment.config_hash(b)
    assert experiment.overrides({"seed": 1, "think_retries": 2}) == {"seed": 1}


# --- every key changes behaviour -----------------------------------------------------------------------------

def _big_output_task(agent):
    agent.run_turn("first")
    return agent.run_turn("second")


def _reads_then_answers(path="big.txt"):
    return scripted(AssistantTurn(tool_calls=[call("read_file", json.dumps({"path": path}), "c1")]),
                    AssistantTurn(content="read it"), AssistantTurn(content="second answer"))


def test_earlier_turn_elision_off_and_its_size(isolated):
    (isolated / "big.txt").write_text("y" * 2500)
    stubbed = lambda agent: any(  # noqa: E731
        m.get("role") == "tool" and m["content"].startswith("[elided") for m in agent.messages)
    agent, _ = _agent(_reads_then_answers())
    _big_output_task(agent)
    assert stubbed(agent)
    agent, _ = _agent(_reads_then_answers(), earlier_turn_elision=False)
    _big_output_task(agent)
    assert not stubbed(agent)
    agent, _ = _agent(_reads_then_answers(), earlier_turn_min_chars=100_000)
    _big_output_task(agent)
    assert not stubbed(agent)


def test_compaction_thresholds_move():
    low = context.ContextBudget(10_000, 2_000, 0.5, 0.2)
    assert low.compact_threshold == 4_000 and low.target_after_compaction == 1_600


def _chatty(n=6, chars=900):
    return scripted(*[AssistantTurn(content=f"answer {i} " + "w" * chars) for i in range(n)])


def _compactions(agent):
    seen = []
    emit = agent.host.emit
    agent.host.emit = lambda kind, **data: (seen.append(data) if kind == "compaction" else None, emit(kind, **data))[1]
    for n in range(6):
        agent.run_turn(f"task {n}")
    return seen


def test_compaction_trigger_and_target_reach_the_running_agent(isolated):
    def run(**exp):
        agent, _ = _agent(_chatty(), **exp)
        agent.config.context_window = 8000
        agent.budget = Agent._new_budget(agent.config)
        return agent, _compactions(agent)

    agent, none = run()
    assert not none and agent.budget.compact_threshold == int(agent.budget.input_budget * 0.75)
    early, some = run(compact_trigger=0.2, compact_target=0.15)
    assert some and early.budget.compact_threshold == int(early.budget.input_budget * 0.2)
    assert early.budget.target_after_compaction == int(early.budget.input_budget * 0.15)

    def left_after(target):
        messages = [{"role": "system", "content": "s"}]
        for n in range(12):
            messages += [{"role": "user", "content": f"task {n} " + "w" * 900}, {"role": "assistant", "content": "ok " + "w" * 900}]
        return context.compact(messages, context.ContextBudget(8000, 256, 0.75, target), lambda text: "sum")["after"]

    assert left_after(0.1) < left_after(0.6)  # a lower target keeps fewer recent turns


def _history(results=6, chars=2000):
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    for n in range(results):
        messages.append({"role": "assistant", "content": "", "tool_calls": [call("read_file", "{}", f"c{n}")]})
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": "z" * chars})
    return messages


def test_keep_recent_and_min_chars_change_what_is_elided():
    budget = context.ContextBudget(3000, 256)
    report = context.compact(_history(), budget, lambda text: "sum", None, {"keep_recent_tool_results": 1})
    assert report["elided"] == 5
    report = context.compact(_history(), budget, lambda text: "sum", None, {"elide_min_chars": 5000})
    assert report["elided"] == 0


def test_summarization_off_skips_stage_two():
    def history():
        messages = _history(results=4, chars=400)
        for n in range(4):
            messages += [{"role": "user", "content": f"task {n} " + "w" * 900}, {"role": "assistant", "content": "ok " + "w" * 900}]
        return messages

    asked: list[str] = []
    budget = context.ContextBudget(2600, 256)
    assert context.compact(history(), budget, lambda text: asked.append(text) or "sum")["summary"] == "sum"
    asked.clear()
    assert context.compact(history(), budget, lambda text: asked.append(text) or "sum", None, {"summarization": False})["summary"] == ""
    assert not asked


def _write(path="a.txt", size=1000):
    return AssistantTurn(tool_calls=[call("write_file", json.dumps({"path": path, "content": "c" * size}), "c1")])


def test_payload_stubbing_off_and_the_variant_for_file_writes(isolated):
    saved = AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"title": "t", "kind": "html", "content": "x" * 900}), "c1")])
    args = lambda agent: json.loads(next(m for m in agent.messages if m.get("tool_calls"))["tool_calls"][0]["function"]["arguments"])  # noqa: E731
    agent, _ = _agent(scripted(saved, AssistantTurn(content="ok")))
    agent.run_turn("save")
    assert "delivered and stored" in args(agent)["content"]
    saved = AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"title": "t", "kind": "html", "content": "x" * 900}), "c1")])
    agent, _ = _agent(scripted(saved, AssistantTurn(content="ok")), payload_stubbing=False)
    agent.run_turn("save")
    assert args(agent)["content"] == "x" * 900
    # By default a file write keeps its body; the variant stubs it, and says the result decides.
    agent, _ = _agent(scripted(_write(), AssistantTurn(content="ok")))
    agent.run_turn("write")
    assert args(agent)["content"] == "c" * 1000
    agent, _ = _agent(scripted(_write("b.txt"), AssistantTurn(content="ok")),
                      payload_stub_tools=["artifact_save", "skill_save", "write_file", "edit_file"])
    agent.run_turn("write")
    assert "tool result says whether it worked" in args(agent)["content"]
    assert (isolated / "b.txt").read_text() == "c" * 1000  # stubbed in history, written in full


def test_payload_stub_min_chars_is_the_size_it_starts_at(isolated):
    args = lambda agent: json.loads(next(m for m in agent.messages if m.get("tool_calls"))["tool_calls"][0]["function"]["arguments"])  # noqa: E731
    saved = lambda: AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"title": "t", "kind": "html", "content": "x" * 400}), "c1")])  # noqa: E731
    agent, _ = _agent(scripted(saved(), AssistantTurn(content="ok")))
    agent.run_turn("save")
    assert "delivered and stored" in args(agent)["content"]
    agent, _ = _agent(scripted(saved(), AssistantTurn(content="ok")), payload_stub_min_chars=500)
    agent.run_turn("save")
    assert args(agent)["content"] == "x" * 400


def test_shrink_keys_through_compaction(isolated):
    def history():
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
        for n in range(8):
            spec = ("write_file", {"path": f"f{n}", "content": "x" * 3000}) if n % 2 else ("skill_save", {"name": f"s{n}", "content": "y" * 3000})
            messages.append({"role": "assistant", "content": "", "tool_calls": [call(spec[0], json.dumps(spec[1]), f"c{n}")]})
            messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": spec[0], "content": "ok"})
        return messages

    budget = context.ContextBudget(6000, 256)
    shrunk = lambda **options: context.compact(history(), budget, lambda text: "- s", None, {"intra_task_summarize": False, **options})["args_shrunk"]  # noqa: E731
    assert shrunk() > 0
    assert shrunk(shrink_old_args=False) == 0
    assert shrunk(shrink_args_tools=["edit_file"]) == 0                         # only the named tools
    assert shrunk(shrink_args_tools=["write_file", "skill_save"]) > shrunk()    # a study may name others
    assert shrunk(shrink_args_min_chars=5000) == 0                              # nothing that big


def test_output_cap_keys_change_the_cap(isolated):
    agent, _ = _agent(scripted(), output_cap_share=0.5, output_cap_min=100, output_cap_max=200)
    assert agent is not None and tools.MAX_OUTPUT_CHARS == 200
    _agent(scripted())
    assert tools.MAX_OUTPUT_CHARS != 200


def test_skills_inline_puts_every_skill_in_the_prompt(isolated):
    from dcode.config import skills_dir

    skills_dir().mkdir(parents=True, exist_ok=True)
    (skills_dir() / "zebra.md").write_text("---\nname: zebra\ndescription: stripes\n---\nThe very long zebra playbook.\n")
    indexed, _ = _agent(scripted())
    inline, _ = _agent(scripted(), skills_mode="inline")
    assert "The very long zebra playbook" not in indexed.messages[0]["content"]
    assert "- zebra: stripes" in indexed.messages[0]["content"]
    assert "The very long zebra playbook" in inline.messages[0]["content"]


class _Server:
    name, state = "notes", "ready"
    tools = [{"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {}}}}]
    spec = {"approve": "allow"}

    def call(self, tool, arguments):
        return f"called {tool} {arguments}", False


class _Servers:
    servers = {"notes": _Server()}

    def __bool__(self):
        return True

    def index_text(self, max_chars=1500):
        return "- notes: add"

    def find(self, server, tool):
        return _Server(), _Server.tools[0]


def test_mcp_full_schemas_are_real_functions_and_work(isolated):
    def agent_with(**exp):
        agent, _ = _agent(scripted(AssistantTurn(tool_calls=[call("mcp__notes__add", '{"a": 1}', "c1")]), AssistantTurn(content="ok")), **exp)
        agent.host.mcp = _Servers()
        agent.refresh_system_message()
        return agent

    names = agent_with()
    assert "mcp_call" in names.tool_names() and "mcp__notes__add" not in names.tool_names()
    assert "notes: add" in names.messages[0]["content"]
    full = agent_with(mcp_schemas="full")
    assert "mcp_call" not in full.tool_names() and "mcp__notes__add" in full.tool_names()
    assert "Add two numbers" in json.dumps(full.tools())
    assert "notes: add" not in full.messages[0]["content"]
    full.run_turn("add them")
    assert any(m.get("role") == "tool" and "called add {'a': 1}" in m["content"] for m in full.messages)


def _remembering(**exp):
    agent, llm_fake = _agent(scripted(AssistantTurn(content="one"), AssistantTurn(content="two")), **exp)
    from dcode.memory import MemoryStore

    agent.memory = MemoryStore(tools.workspace_root())
    agent.memory.save(name="port", body="The zebra service port is 8123.", scope="project")
    return agent, llm_fake


def test_recall_placement_user_keeps_the_system_prompt_stable(isolated):
    agent, fake = _remembering(recall_placement="user")
    agent.run_turn("what is the zebra service port")
    sent = fake.calls[0]
    assert "Memories relevant to the current task" not in sent[0]["content"]
    assert sent[-1]["role"] == "user" and sent[-1]["content"].startswith("Memories relevant to the current task:")
    assert "8123" in sent[-1]["content"] and sent[-1]["content"].endswith("what is the zebra service port")
    agent.run_turn("and the other zebra service port")
    second = fake.calls[1]
    assert second[0]["content"] == sent[0]["content"]        # messages[0] did not change between tasks
    assert second[1]["content"] == sent[1]["content"]        # nor did the history
    # By default it stays in the system prompt and moves with each task.
    agent, fake = _remembering()
    agent.run_turn("what is the zebra service port")
    assert "Memories relevant to the current task" in fake.calls[0][0]["content"]
    assert fake.calls[0][-1]["content"] == "what is the zebra service port"


def test_retrying_a_failed_task_doesnt_add_its_message_twice_with_recall_in_the_user_message(isolated):
    agent, fake = _remembering(recall_placement="user")
    original = fake.chat

    def fail_once(*args, **kwargs):
        fake.chat = original
        raise RuntimeError("server down")

    fake.chat = fail_once
    with pytest.raises(RuntimeError):
        agent.run_turn("what is the zebra service port")
    agent.run_turn("what is the zebra service port")
    assert sum(1 for m in agent.messages if m["role"] == "user") == 1


def test_guards_follow_their_keys(isolated):
    repeated = AssistantTurn(tool_calls=[call("list_files", '{"path": "."}', "c1")])

    def looping(messages, tools_list):
        return AssistantTurn(tool_calls=[dict(repeated.tool_calls[0])]) if tools_list else AssistantTurn(content="stopped")

    def count(agent, text):
        return sum(1 for m in agent.messages if m.get("role") == "tool" and text in m["content"])

    agent, _ = _agent(looping)
    agent.run_turn("loop")
    default_refusals = count(agent, "identical arguments 3 times")
    assert default_refusals == 2  # stuck_limit
    agent, _ = _agent(looping, repeat_limit=2)
    agent.run_turn("loop")
    assert count(agent, "identical arguments 2 times") == 2
    agent, _ = _agent(looping, stuck_limit=4)
    agent.run_turn("loop")
    assert count(agent, "identical arguments 3 times") == 4

    n = iter(range(1000))
    agent, fake = _agent(lambda m, t: AssistantTurn(tool_calls=[call("list_files", json.dumps({"path": ".", "max_depth": next(n)}), "c")]) if t else AssistantTurn(content="end"),
                         main_max_steps=3)
    agent.run_turn("go on forever")
    assert len(fake.calls) == 4  # three steps and the wrap-up


def test_follow_ups_follow_their_key(isolated):
    promise = lambda m, t: AssistantTurn(content="I'll create it.")  # noqa: E731
    agent, fake = _agent(promise)
    agent.run_turn("do it")
    assert len(fake.calls) == 3
    agent, fake = _agent(promise, max_follow_ups=0)
    agent.run_turn("do it")
    assert len(fake.calls) == 1


def test_delegation_off_removes_the_tool_and_the_prompt_block(isolated):
    on, _ = _agent(scripted())
    off, _ = _agent(scripted(), delegation=False)
    assert "delegate" in on.tool_names() and "delegate" not in off.tool_names()
    assert len(off.messages[0]["content"]) < len(on.messages[0]["content"])


# --- what the client sends ---------------------------------------------------------------------------------
class _Rejected(Exception):
    status_code = 400


class _API:
    def __init__(self, *replies):
        from types import SimpleNamespace

        self.replies, self.requests = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item) if isinstance(item, list) else item


def _reply(text="ok"):
    from types import SimpleNamespace

    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _client(*replies, **exp):
    config = make_config()
    config.experiment.update(exp)
    client = LLMClient(config)
    client.client = _API(*replies)
    return client, client.client


def test_sampling_is_sent_only_when_set():
    client, api = _client(_reply())
    client.chat([{"role": "user", "content": "hi"}])
    assert not {"temperature", "top_p", "seed"} & set(api.requests[0])
    client, api = _client(_reply(), _reply(), temperature=0.0, top_p=0.5, seed=7)
    client.chat([{"role": "user", "content": "hi"}])
    assert (api.requests[0]["temperature"], api.requests[0]["top_p"], api.requests[0]["seed"]) == (0.0, 0.5, 7)
    client.complete_text([{"role": "user", "content": "hi"}])
    assert api.requests[1]["seed"] == 7


def test_a_server_that_refuses_a_sampling_field_loses_only_that_field():
    client, api = _client(_Rejected("Unrecognized request argument supplied: seed"), _reply(), seed=7, temperature=0.2)
    client.chat([{"role": "user", "content": "hi"}])
    assert "seed" not in api.requests[1] and api.requests[1]["temperature"] == 0.2


def test_text_tool_recovery_off_leaves_the_text_alone():
    text = '<tool_call>{"name": "list_files", "arguments": {"path": "."}}</tool_call>'
    tool = [{"type": "function", "function": {"name": "list_files", "parameters": {"type": "object"}}}]
    client, _ = _client(_reply(text))
    assert client.chat([{"role": "user", "content": "hi"}], tools=tool).tool_calls
    client, _ = _client(_reply(text), text_tool_recovery=False)
    turn = client.chat([{"role": "user", "content": "hi"}], tools=tool)
    assert not turn.tool_calls and "tool_call" in turn.content


def _stream_chunks(reasoning_chars, text="answer"):
    from types import SimpleNamespace

    def chunk(**fields):
        delta = SimpleNamespace(content=fields.get("content"), reasoning_content=fields.get("reasoning"), tool_calls=None)
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=fields.get("finish"), delta=delta)])

    rows = [chunk(reasoning=f"r{n:04d} ") for n in range(reasoning_chars // 6)]
    return rows + [chunk(content=text), chunk(finish="stop")]


def _thinking_client(**exp):
    config = make_config(stream=True, model="gpt-5-mini", base_url="https://api.openai.com/v1", reasoning_effort="high")
    config.experiment.update(exp)
    client = LLMClient(config)
    client.client = _API(_stream_chunks(30_000), _stream_chunks(30_000), _stream_chunks(30_000), _stream_chunks(10))
    return client, client.client


def test_reasoning_cutoff_keys_change_when_it_cuts_and_how_often_it_retries():
    messages = [{"role": "user", "content": "hi"}]
    client, api = _thinking_client()
    client.chat(messages, max_tokens=1000)
    assert len(api.requests) == 3                                 # think_retries = 2
    client, api = _thinking_client(think_retries=0)
    client.chat(messages, max_tokens=1000)
    assert len(api.requests) == 1
    client, api = _thinking_client(think_share=100.0)              # 30,000 chars is within the allowance
    assert client.chat(messages, max_tokens=1000).content == "answer" and len(api.requests) == 1
    client, api = _thinking_client(loop_check_every=0)             # no checks while it streams: no cut
    assert client.chat(messages, max_tokens=1000).content == "answer" and len(api.requests) == 1
