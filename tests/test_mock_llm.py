"""The scripted model: usage from the request, a simulated prefix cache, long tasks, and a server's real limit."""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import pytest

from dcode.config import AppConfig
from dcode.llm import LLMClient
from dcode.session import Session

PATH = Path(__file__).parent.parent / "docs" / "dev" / "mock_llm.py"


@pytest.fixture
def mock():
    spec = importlib.util.spec_from_file_location("mock_llm_under_test", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.serve(0)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield module, f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def _config(url, **fields):
    return AppConfig(**{"base_url": url, "api_key": "x", "model": "mock", "stream": True, **fields})


def _client(url, **fields):
    return LLMClient(_config(url, **fields))


def test_usage_comes_from_the_request_not_a_fixed_number(mock):
    _, url = mock
    client = _client(url)
    short = client.chat([{"role": "user", "content": "hi"}])
    long = client.chat([{"role": "user", "content": "x" * 7000}])
    assert short.usage["prompt"] < 50
    assert 1900 < long.usage["prompt"] < 2100  # 7000 / 3.5
    assert long.usage["total"] == long.usage["prompt"] + long.usage["completion"]


def test_without_the_flag_there_is_no_cached_figure_and_with_it_the_prefix_is_counted(mock):
    module, url = mock
    client = _client(url)
    first = [{"role": "user", "content": "a" * 3500}]
    assert client.chat(first).cached is None  # unknown stays unknown

    module.Options.simulate_cache = True
    client.chat(first)
    second = client.chat(first + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}])
    assert 990 < second.cached < 1010
    third = client.chat([{"role": "user", "content": "b" * 3500}])
    assert third.cached < 10  # nothing shared but the JSON's first characters


def test_a_server_limit_is_refused_in_vllms_words(mock):
    module, url = mock
    module.Options.max_context = 100
    client = _client(url)
    with pytest.raises(Exception, match="maximum context length is 100 tokens"):
        client.chat([{"role": "user", "content": "x" * 3500}])


def test_a_long_task_survives_on_a_server_that_enforces_its_window(mock, isolated, tmp_path):
    """End to end, nothing faked but the model: 30 big writes on a 6K window with a real limit behind it."""
    module, url = mock
    module.Options.max_context = 6000 - 512
    module.Options.simulate_cache = True
    trace_file = tmp_path / "trace.jsonl"
    config = _config(url, context_window=6000, max_output_tokens=512)
    config.trace = {"path": str(trace_file), "snapshots": False}
    session = Session(config)
    session.set_mode("auto", source="run")
    events = []
    session.bus.subscribe(events.append)
    try:
        session.submit("long task 30", source="run")
        assert _wait_idle(session)
    finally:
        session.close()
    assert not [e for e in events if e["type"] == "error"], [e for e in events if e["type"] == "error"]
    assert len(list(isolated.glob("part_*.py"))) == 30
    assert any(e["type"] == "assistant_message" and "wrote 30 files" in e["text"] for e in events)
    rows = [json.loads(line) for line in trace_file.read_text().splitlines()]
    end = rows[-1]
    assert end["type"] == "task_end" and end["reason"] == "final_answer" and end["steps"] == 30
    calls = [r for r in rows if r["type"] == "llm_call"]
    # Our own measure of prefix reuse and the (simulated) server's agree on what happened between steps.
    follows = [(a, b) for a, b in zip(calls, calls[1:], strict=False)
               if a["purpose"] == b["purpose"] == "main_step" and b["prefix_intact"] and not a["error_class"] and not b["error_class"]]
    assert follows
    for _, call in follows:  # the same prefix, measured from two sides (the mock also counts JSON punctuation)
        assert abs(call["cached_tokens"] - call["lcp_tokens"]) < 0.15 * call["cached_tokens"]
    assert sum(1 for r in rows if r["type"] == "compaction") > 0
    assert end["prompt_tokens"] == sum(r["counted_prompt_tokens"] for r in calls)
    assert end["cached_tokens"] == sum(r["cached_tokens"] or 0 for r in calls)


def _wait_idle(session, timeout=60.0):
    import time

    deadline = time.time() + timeout
    time.sleep(0.05)
    while time.time() < deadline:
        if session.idle:
            return True
        time.sleep(0.02)
    return False


def test_the_old_positional_command_line_still_works(mock):
    module, _ = mock
    served = []
    module.serve = lambda port=0: type("S", (), {"serve_forever": lambda self: served.append(port)})()
    module.main(["18099", "thinker", "--simulate-cache", "--max-context", "500"])
    assert served == [18099] and module.Options.name == "thinker"
    assert module.Options.simulate_cache and module.Options.max_context == 500
