"""The three bugs that stopped long tasks: elided "!command" output, overflow ending a task, compaction inside one task."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import pytest
from conftest import FakeLLM, call, make_config, scripted

from dcode import context
from dcode.context import ContextBudget, compact, elide_earlier_turns, shrink_tool_args, step_cut, unread_start
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn, ContextOverflow, LLMClient, is_context_overflow
from dcode.session import Session

sys.path.insert(0, str(Path(__file__).parent / "golden"))
import scenario  # noqa: E402


def _wait(condition, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _pairs_ok(messages: list[dict]) -> None:
    """What a server insists on: every result follows its call, every call has its result."""
    open_calls: list[str] = []
    for message in messages:
        if message.get("role") == "tool":
            assert message["tool_call_id"] in open_calls, f"result without its call: {message['tool_call_id']}"
            open_calls.remove(message["tool_call_id"])
            continue
        assert not open_calls, f"calls without results: {open_calls}"
        open_calls = [c["id"] for c in message.get("tool_calls") or []]
    assert not open_calls, f"calls without results: {open_calls}"


# --- "!command" ------------------------------------------------------------------------------------------------

def test_a_user_commands_output_reaches_the_model_whole_then_is_stubbed_once_answered(isolated):
    """Regression (0.7.1): a 5,000-char output reached the model as 404 chars."""
    fake = FakeLLM(scripted(AssistantTurn(content="I saw it"), AssistantTurn(content="next")))
    session = Session(make_config(), llm=fake)
    try:
        session.run_shell("python3 -c \"print('o' * 5000)\"")
        assert _wait(lambda: session.idle and any(m.get("role") == "tool" for m in session.agent.messages))
        session.submit("what did that print?")
        assert _wait(lambda: session.idle and fake.calls)
        seen = next(m for m in fake.calls[0] if m.get("role") == "tool")
        assert len(seen["content"]) >= 2900 and not seen["content"].startswith("[elided")
        session.submit("and now something else")
        assert _wait(lambda: session.idle and len(fake.calls) == 2)
        later = next(m for m in fake.calls[1] if m.get("role") == "tool")
        assert later["content"].startswith("[elided")  # the model answered it, so it may go now
    finally:
        session.close()


def test_two_commands_in_a_row_are_both_unread():
    def shell(n):
        return [{"role": "user", "content": f"[I ran this shell command myself: c{n}]"},
                {"role": "assistant", "content": "", "tool_calls": [call("run_command", "{}", f"shell_{n}")]},
                {"role": "tool", "tool_call_id": f"shell_{n}", "name": "run_command", "content": "o" * 3000},
                {"role": "assistant", "content": "Noted."}]

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    messages += shell(1) + shell(2)
    assert unread_start(messages) == 3
    assert elide_earlier_turns(messages) == 0


def test_a_real_turns_output_is_still_stubbed_and_a_turn_never_answered_is_not():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [call("read_file", "{}", "c1")]},
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "a" * 3000}]
    assert elide_earlier_turns(list(messages)) == 0  # interrupted before the model read it
    answered = [*messages, {"role": "assistant", "content": "done"}]
    assert elide_earlier_turns(answered) == 1


def test_compaction_leaves_an_unread_user_output_alone():
    messages = [{"role": "system", "content": "s"}]
    for n in range(6):
        messages += [{"role": "user", "content": f"turn {n} " + "w" * 1200}, {"role": "assistant", "content": "ok " + "w" * 1200}]
    messages += [{"role": "user", "content": "[I ran this shell command myself: c]"},
                 {"role": "assistant", "content": "", "tool_calls": [call("run_command", "{}", "shell_1")]},
                 {"role": "tool", "tool_call_id": "shell_1", "name": "run_command", "content": "o" * 2500},
                 {"role": "assistant", "content": "Noted."},
                 {"role": "user", "content": "what did it print?"}]
    report = compact(messages, ContextBudget(3000, 256), lambda text: "- summary")
    assert report and report["stage"] >= 2
    kept = next(m for m in messages if m.get("tool_call_id") == "shell_1")
    assert kept["content"] == "o" * 2500
    _pairs_ok(messages)


# --- overflow ----------------------------------------------------------------------------------------------------
class Refused(Exception):
    status_code = 400


TOO_LONG = "This model's maximum context length is 16384 tokens. However, your request has 20000 input tokens."


def _agent(router, approve=True, trace_path=None, **exp):
    config = make_config()
    config.experiment.update(exp)
    if trace_path:
        config.trace = {"path": str(trace_path), "snapshots": False}
    fake = FakeLLM(router, config)
    return Agent(config, fake, NullHost(approve=approve)), fake


def _big_history(agent):
    for n in range(5):
        agent.messages.append({"role": "user", "content": f"earlier {n} " + "w" * 4000})
        agent.messages.append({"role": "assistant", "content": "ok " + "w" * 4000})


def _records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_overflow_compacts_and_retries_once_and_the_task_goes_on(isolated, tmp_path):
    sizes = []

    def router(messages, tools):
        sizes.append(sum(len(str(m.get("content"))) for m in messages))
        if len(sizes) == 1:
            raise Refused(TOO_LONG)
        return AssistantTurn(content="finished")

    trace_file = tmp_path / "t.jsonl"
    agent, _ = _agent(router, trace_path=trace_file)
    _big_history(agent)
    assert agent.run_turn("carry on").text == "finished"
    assert len(sizes) == 2 and sizes[1] < sizes[0] / 2
    assert any(e["type"] == "notice" and "compacting and trying again" in e["text"] for e in agent.host.events)
    rows = _records(trace_file)
    assert next(r for r in rows if r["type"] == "compaction")["trigger"] == "forced"
    assert rows[-1]["type"] == "task_end" and rows[-1]["reason"] == "final_answer"
    _pairs_ok(agent.messages)


def test_overflow_twice_ends_with_its_own_reason_not_a_generic_failure(isolated, tmp_path):
    attempts = []

    def router(messages, tools):
        attempts.append(1)
        raise Refused(TOO_LONG)

    trace_file = tmp_path / "t.jsonl"
    agent, _ = _agent(router, trace_path=trace_file)
    _big_history(agent)
    with pytest.raises(ContextOverflow, match="no longer fits"):
        agent.run_turn("carry on")
    assert len(attempts) == 2  # retried once, not in a loop
    assert _records(trace_file)[-1]["reason"] == "context_overflow"


def test_overflow_with_nothing_left_to_compact_doesnt_send_the_same_request_again(isolated):
    attempts = []

    def router(messages, tools):
        attempts.append(1)
        raise Refused(TOO_LONG)

    agent, _ = _agent(router)
    with pytest.raises(ContextOverflow):
        agent.run_turn("hi")
    assert len(attempts) == 1


def test_a_session_reports_an_overflow_as_what_it_is(isolated):
    def router(messages, tools):
        raise Refused(TOO_LONG)

    config = make_config()
    session = Session(config, llm=FakeLLM(router, config))
    events = []
    session.bus.subscribe(events.append)
    try:
        session.submit("hello")
        assert _wait(lambda: any(e["type"] == "error" for e in events))
    finally:
        session.close()
    assert "no longer fits the model's context window" in next(e for e in events if e["type"] == "error")["text"]


def test_recovery_off_is_todays_failure(isolated):
    def router(messages, tools):
        raise Refused(TOO_LONG)

    agent, _ = _agent(router, overflow_recovery=False)
    with pytest.raises(Refused):
        agent.run_turn("hi")


@pytest.mark.parametrize("status, text, expected", [
    (400, TOO_LONG, True),
    (400, "Please reduce the length of the input messages.", True),
    (400, "Unrecognized request argument supplied: seed", False),
    (429, "Too many tokens per minute", False),      # a rate limit: waiting helps, compacting doesn't
    (500, "internal error", False),
])
def test_what_counts_as_an_overflow(status, text, expected):
    error = Refused(text)
    error.status_code = status
    assert is_context_overflow(error) is expected


def test_an_overflow_doesnt_teach_the_client_that_streaming_with_tools_is_refused():
    from types import SimpleNamespace

    config = make_config(stream=True)
    client = LLMClient(config)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(Refused(TOO_LONG)))))
    tool = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}]
    with pytest.raises(Refused):
        client.chat([{"role": "user", "content": "hi"}], tools=tool)
    assert client._stream_tools_ok is not False


# --- compaction inside one task ---------------------------------------------------------------------------------
def _long_task(steps=12, chars=3000):
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "THE ORIGINAL TASK"}]
    for n in range(steps):
        args = json.dumps({"path": f"f{n}.py", "content": "x" * chars})
        messages.append({"role": "assistant", "content": "", "tool_calls": [call("write_file", args, f"c{n}")]})
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": "write_file", "content": f"Wrote {chars} chars"})
    return messages


def test_stage_one_shrinks_old_arguments_and_keeps_the_recent_ones():
    messages = _long_task(steps=6)
    assert shrink_tool_args(messages, 3, ["write_file", "edit_file"], 240) == 3
    args = [json.loads(m["tool_calls"][0]["function"]["arguments"]) for m in messages if m.get("tool_calls")]
    assert all("omitted from history" in a["content"] for a in args[:3])
    assert all(a["content"] == "x" * 3000 for a in args[3:])
    assert args[0]["path"] == "f0.py"  # what it did stays readable
    assert shrink_tool_args(messages, 3, ["write_file", "edit_file"], 240) == 0  # not twice


def test_shrinking_covers_edit_file_only_when_asked_and_leaves_other_tools():
    edit = call("edit_file", json.dumps({"path": "a", "old_text": "o" * 500, "new_text": "n" * 500}), "c1")
    other = call("run_command", json.dumps({"command": "c" * 500}), "c2")
    messages = [{"role": "assistant", "content": "", "tool_calls": [edit, other]}]
    assert shrink_tool_args(messages, 0, ["write_file", "edit_file"], 240) == 1
    calls = messages[0]["tool_calls"]
    edited = json.loads(calls[0]["function"]["arguments"])
    assert "omitted" in edited["old_text"] and "omitted" in edited["new_text"]
    assert calls[1] == other
    # Never in place: the same dict may be what an earlier request recorded.
    assert "o" * 500 in edit["function"]["arguments"]


def test_a_long_task_with_big_writes_stays_inside_the_budget():
    """Spec section 6.5, scenario C: this reached 3.3 times the input budget."""
    budget = ContextBudget(6000, 512)
    messages = _long_task(steps=1)
    peak = 0
    for n in range(1, 40):
        args = json.dumps({"path": f"f{n}.py", "content": "x" * 3000})
        messages.append({"role": "assistant", "content": "", "tool_calls": [call("write_file", args, f"c{n}")]})
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": "write_file", "content": "Wrote 3000 chars"})
        compact(messages, budget, lambda text: "- summary", None, {}, task="THE ORIGINAL TASK")
        peak = max(peak, budget.usage(messages))
        _pairs_ok(messages)
    assert peak <= budget.input_budget
    assert "THE ORIGINAL TASK" in json.dumps(messages)


def test_the_summarizer_folds_steps_when_shrinking_arguments_is_not_enough_and_pins_the_task():
    budget = ContextBudget(4000, 256)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "THE ORIGINAL TASK"}]
    for n in range(12):
        messages.append({"role": "assistant", "content": "thinking " * 200, "tool_calls": [call("read_file", "{}", f"c{n}")]})
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": "r" * 500})
    report = compact(messages, budget, lambda text: "- what happened so far", None, {}, task="THE ORIGINAL TASK")
    assert report["stage"] >= 2 and report["intra_task"] and report["folded"]
    assert budget.usage(messages) <= budget.input_budget
    assert messages[1]["role"] == "user" and "THE ORIGINAL TASK" in messages[1]["content"]
    assert "what happened so far" in messages[1]["content"]
    assert messages[2]["role"] == "assistant"   # no two assistants in a row: strict templates refuse that
    _pairs_ok(messages)


def test_a_cut_is_never_between_a_call_and_its_results():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [call("a", "{}", "c1"), call("b", "{}", "c2")]},
                {"role": "tool", "tool_call_id": "c1", "name": "a", "content": "x" * 400},
                {"role": "tool", "tool_call_id": "c2", "name": "b", "content": "x" * 400},
                {"role": "assistant", "content": "", "tool_calls": [call("a", "{}", "c3")]},
                {"role": "tool", "tool_call_id": "c3", "name": "a", "content": "x"}]
    cut = step_cut(messages, 1, context.TokenCounter(), len(messages))
    assert cut == 5 and messages[cut]["role"] == "assistant"
    assert step_cut(messages[:3], 1, context.TokenCounter(), 3) == 0  # one unanswered step: nothing to fold


@pytest.mark.parametrize("seed", range(40))
def test_compaction_never_breaks_the_pairing_whatever_the_history(seed):
    """Random long tasks: parallel calls, big arguments and outputs, reminders and new turns, summaries that fail."""
    rng = random.Random(seed)
    budget = ContextBudget(rng.choice([2048, 3000, 4096, 6000]), 256)
    options = {"intra_task_trigger": rng.choice([0.5, 0.8, 1.0]), "keep_recent_tool_results": rng.choice([0, 1, 3]),
               "shrink_old_args": rng.random() > 0.2, "shrink_args_min_chars": rng.choice([10, 240])}
    messages = [{"role": "system", "content": "s" * rng.randint(0, 800)}, {"role": "user", "content": "the task"}]
    number = 0

    def summarize(text):
        if rng.random() < 0.2:
            raise RuntimeError("the summarizer failed")
        return "- summary"

    for _ in range(rng.randint(5, 45)):
        kind = rng.random()
        if kind < 0.1:
            messages.append({"role": "user", "content": "Continue. " + "u" * rng.randint(0, 800)})
            messages.append({"role": "assistant", "content": "ok"})
        calls = []
        for _ in range(rng.randint(1, 3)):
            number += 1
            name = rng.choice(["write_file", "edit_file", "read_file", "run_command"])
            calls.append(call(name, json.dumps({"path": "p", "content": "c" * rng.randint(0, 3000)}), f"c{number}"))
        messages.append({"role": "assistant", "content": rng.choice(["", "note " * 40]), "tool_calls": calls})
        for item in calls:
            messages.append({"role": "tool", "tool_call_id": item["id"], "name": item["function"]["name"],
                             "content": "o" * rng.randint(0, 2500)})
        compact(messages, budget, summarize, None, options, force=rng.random() < 0.1, task="the task")
        _pairs_ok(messages)
        assert messages[0]["role"] == "system"


def test_intra_task_keys(isolated):
    def fold(**options):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
        for n in range(20):
            messages.append({"role": "assistant", "content": "w" * 800, "tool_calls": [call("read_file", "{}", f"c{n}")]})
            messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": "r"})
        return compact(messages, ContextBudget(4000, 256), lambda text: "- s", None, options)

    assert fold()["intra_task"]
    assert not fold(intra_task_summarize=False)["intra_task"]
    assert not fold(summarization=False)["intra_task"]
    # By default it waits for the budget; a study can move it down to the compaction threshold.
    just_over = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    for n in range(5):
        just_over.append({"role": "assistant", "content": "w" * 800, "tool_calls": [call("read_file", "{}", f"c{n}")]})
        just_over.append({"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": "r"})
    budget = ContextBudget(1500, 256)
    assert budget.compact_threshold < budget.usage(just_over) <= budget.input_budget
    assert not compact([dict(m) for m in just_over], budget, lambda text: "- s", None, {})["intra_task"]
    assert compact([dict(m) for m in just_over], budget, lambda text: "- s", None, {"intra_task_trigger": 0.5})["intra_task"]


def test_a_shrink_off_key_keeps_arguments(isolated):
    messages = _long_task(steps=8)
    compact(messages, ContextBudget(16000, 512), lambda text: "- s", None, {"shrink_old_args": False, "intra_task_summarize": False})
    assert all(json.loads(m["tool_calls"][0]["function"]["arguments"])["content"] == "x" * 3000 for m in messages if m.get("tool_calls"))


# --- against the recording of the old code ------------------------------------------------------------------------
def test_with_the_new_compaction_off_a_long_task_sends_what_it_always_did():
    golden = scenario.load_golden()["long-writes"]
    assert scenario.record("long-writes", experiment={"shrink_old_args": False, "intra_task_summarize": False}) == golden


def test_with_the_defaults_the_same_long_task_stays_small():
    golden = scenario.load_golden()["long-writes"]
    fresh = scenario.record("long-writes")
    assert max(len(r["body"]) for r in golden) > 3 * max(len(r["body"]) for r in fresh)
