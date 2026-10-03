from __future__ import annotations

from dcode.context import SUMMARY_PREFIX, ContextBudget, TokenCounter, compact, elide_tool_results


def _history(turns: int, tool_chars: int = 4000):
    messages = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        messages.append({"role": "user", "content": f"task {i}"})
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": "x" * tool_chars})
        messages.append({"role": "assistant", "content": f"answer {i}"})
    return messages


def _tool_pairs_valid(messages):
    """Every tool message must answer a tool_call from an earlier assistant message."""
    seen = set()
    for m in messages:
        for c in m.get("tool_calls") or []:
            seen.add(c["id"])
        if m.get("role") == "tool" and m["tool_call_id"] not in seen:
            return False
    return True


def test_counter_calibrates_toward_reported_usage():
    counter = TokenCounter()
    messages = [{"role": "user", "content": "x" * 3500}]
    estimate = counter.messages(messages)
    counter.calibrate(messages, None, estimate * 2)
    assert counter.messages(messages) > estimate


def test_elide_keeps_recent_tool_results():
    messages = _history(5)
    assert elide_tool_results(messages, keep_recent=2) == 3
    tool_contents = [m["content"] for m in messages if m["role"] == "tool"]
    assert all(c.startswith("[elided") for c in tool_contents[:3])
    assert all(not c.startswith("[elided") for c in tool_contents[3:])


def test_under_budget_is_untouched():
    messages = _history(1, tool_chars=100)
    assert compact(messages, ContextBudget(32768, 4096), lambda t: "s") is None


def test_compaction_summarizes_and_keeps_history_valid():
    budget = ContextBudget(context_window=4096, max_output_tokens=512)
    messages = _history(12)
    seen_transcripts = []
    report = compact(messages, budget, lambda t: seen_transcripts.append(t) or "- did things")
    assert report is not None
    assert report["after"] < report["before"]
    assert budget.usage(messages) <= budget.input_budget
    assert messages[0]["role"] == "system"
    assert any(isinstance(m.get("content"), str) and m["content"].startswith(SUMMARY_PREFIX) for m in messages)
    assert _tool_pairs_valid(messages)
    assert seen_transcripts and "task 0" in seen_transcripts[0]


def test_compaction_survives_summarizer_failure():
    budget = ContextBudget(context_window=4096, max_output_tokens=512)
    messages = _history(12)

    def boom(_):
        raise RuntimeError("server down")

    report = compact(messages, budget, boom)
    assert report is not None and report["summary"]  # mechanical fallback digest
    assert _tool_pairs_valid(messages)


def test_output_budget_capped_for_tiny_windows():
    assert ContextBudget(4096, 4096).max_output_tokens == 1024
