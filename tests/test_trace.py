"""The research trace, against a real HTTP server and the real client: its schema, prefix measure and accounting."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import FakeLLM, call, make_config, scripted

from dcode import stats, trace
from dcode.config import AppConfig
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn, LLMClient
from dcode.session import Session

REPO = Path(__file__).parent.parent
num = (int, float)
maybe = lambda *kinds: (*kinds, type(None))  # noqa: E731

# The whole schema: docs/research/tracing.md explains each of these, and a test below keeps the two together.
COMMON = {"ts": num, "task_id": str, "agent_id": maybe(str), "type": str}
SCHEMA: dict[str, dict] = {
    "run_header": {
        "dcode_version": str, "dcode_git": dict, "python": str, "platform": str, "experiment": dict, "experiment_hash": str,
        "experiment_overrides": dict, "profiles": list, "stream": bool, "router": maybe(str), "workspace": str, "snapshots": bool},
    "llm_call": {
        "purpose": str, "step": maybe(int), "attempt": int, "retry_reason": str, "model": str, "server_kind": str,
        "effort": maybe(str), "max_tokens": maybe(int), "message_count": int, "tools_count": int,
        "estimated_prompt_tokens": int, "estimated_completion_tokens": int, "reported_prompt_tokens": maybe(int),
        "reported_completion_tokens": maybe(int), "reasoning_tokens": maybe(int), "cached_tokens": maybe(int),
        "usage_estimated": bool, "counted_prompt_tokens": int, "counted_completion_tokens": int, "request_ts": num,
        "ttft_ms": maybe(*num), "last_delta_ms": maybe(*num), "decode_ms": maybe(*num), "total_ms": num,
        "finish_reason": maybe(str), "streamed": bool, "snapshot": maybe(str), "rejected_fields": maybe(list),
        "http_sends": maybe(int), "http_statuses": maybe(list), "error_class": maybe(str), "error_message": maybe(str),
        "message_hashes": list, "tools_hash": maybe(str), "prev_messages": maybe(int), "lcp_messages": int, "lcp_tokens": int,
        "first_changed_index": maybe(int), "first_changed_role": maybe(str), "prefix_intact": maybe(bool),
        "tools_changed": maybe(bool)},
    "tool_call": {
        "step": int, "name": str, "arg_chars": int, "raw_output_chars": maybe(int), "kept_chars": int, "truncated": bool,
        "duration_ms": num, "approval_wait_ms": num, "ok": bool, "error": maybe(str), "was_refused": bool, "refusal": maybe(str)},
    "compaction": {
        "stage": int, "trigger": str, "before_tokens": int, "after_tokens": int, "elided": int, "summarized": int,
        "summary_tokens": int, "args_shrunk": int, "intra_task": bool},
    "elision": {"count": int, "chars_removed": int},
    "elision_regret": {"name": str, "how": str, "signature": str},
    "route": {"source": str, "reason": str, "hard": bool, "model": str, "utility_answer": maybe(str), "input_chars": int},
    "delegate": {
        "role": str, "helper_id": str, "model": str, "ok": bool, "steps": int, "report_chars": int, "report_kept_chars": int,
        "calls": int, "prompt_tokens": int, "completion_tokens": int, "cached_tokens": maybe(int)},
    "task_end": {
        "reason": str, "steps": int, "calls": int, "estimated_calls": int, "tool_calls": int, "model_ms": num, "tool_ms": num,
        "approval_wait_ms": num, "wall_ms": num, "prompt_tokens": int, "completion_tokens": int, "cached_tokens": maybe(int)},
}
PURPOSES = trace.PURPOSES
RETRY_REASONS = {"none", "busy", "learned_param", "think_cutoff", "context_overflow"}
END_REASONS = {"final_answer", "step_limit", "stuck_guard", "error", "interrupted", "context_overflow"}


def _check_schema(record: dict) -> None:
    fields = {**COMMON, **SCHEMA[record["type"]]}
    assert set(record) == set(fields), f"{record['type']}: {set(record) ^ set(fields)}"
    for name, kinds in fields.items():
        assert isinstance(record[name], kinds), f"{record['type']}.{name} = {record[name]!r}"
        assert not (isinstance(record[name], bool) and bool not in (kinds if isinstance(kinds, tuple) else (kinds,))), name


# --- a model server that does exactly what the test says --------------------------------------------------------
def text(content="ok", **extra):
    return {"content": content, **extra}


def tools_(*calls, **extra):
    return {"tool_calls": [{"id": f"call_{n}_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                           for n, (name, args) in enumerate(calls)], **extra}


class Model:
    """Replies with tools go through ``script`` in order; a request without tools is a side job and gets a line."""

    def __init__(self, *script, side=lambda body: "Title of the chat"):
        self.script = list(script)
        self.side = side
        self.seen: list[dict] = []
        self.headers: list[dict] = []
        self.lock = threading.Lock()
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._json(200, {"object": "list", "data": []})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with model.lock:
                    model.seen.append(body)
                    model.headers.append({key.lower(): value for key, value in self.headers.items()})
                    if not body.get("tools"):
                        reply = text(model.side(body))
                    else:
                        reply = model.script.pop(0) if model.script else text("done")
                if "status" in reply:
                    self._json(reply["status"], {"error": {"message": reply.get("message", "refused")}})
                    return
                prompt = sum(len(json.dumps(m)) for m in body["messages"]) // 4
                usage = reply.get("usage", {"prompt_tokens": prompt, "completion_tokens": 7, "total_tokens": prompt + 7})
                message = {"role": "assistant", "content": reply.get("content", "")}
                if reply.get("tool_calls"):
                    message["tool_calls"] = reply["tool_calls"]
                if reply.get("reasoning"):
                    message["reasoning_content"] = reply["reasoning"]
                finish = reply.get("finish") or ("tool_calls" if reply.get("tool_calls") else "stop")
                if body.get("stream"):
                    self._stream(message, finish, usage, reply.get("cut", False))
                else:
                    self._json(200, {"id": "x", "object": "chat.completion", "model": body["model"], "created": 1,
                                     "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                     **({"usage": usage} if usage else {})})

            def _json(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _stream(self, message, finish, usage, cut):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                def send(chunk):
                    self.wfile.write(f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'm', **chunk})}\n\n".encode())
                    self.wfile.flush()

                delta = {"role": "assistant", "content": message["content"]}
                if message.get("reasoning_content"):
                    delta["reasoning_content"] = message["reasoning_content"]
                if message.get("tool_calls"):
                    delta["tool_calls"] = [{"index": n, **c} for n, c in enumerate(message["tool_calls"])]
                send({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                if cut:
                    return  # the connection drops: no finish reason, no usage
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
                if usage:
                    send({"choices": [], "usage": usage})
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def model():
    made: list[Model] = []

    def make(*script, **options):
        made.append(Model(*script, **options))
        return made[-1]

    yield make
    for item in made:
        item.close()


def _config(tmp_path, url, snapshots=False, **fields):
    config = AppConfig(**{"base_url": url, "api_key": "sk-never-in-a-trace", "model": "mock-model", "stream": True, **fields})
    config.trace = {"path": str(tmp_path / "trace.jsonl"), "snapshots": snapshots}
    return config


def _rows(tmp_path):
    return [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]


def _wait(session, timeout=30.0):
    time.sleep(0.05)
    deadline = time.time() + timeout
    while time.time() < deadline and not session.idle:
        time.sleep(0.01)
    assert session.idle


def _session(config, *tasks, compact=False):
    session = Session(config)
    session.set_mode("auto", source="run")
    events: list[dict] = []
    session.bus.subscribe(events.append)
    try:
        for task in tasks:
            session.submit(task, source="run")
            _wait(session)
        if compact:
            session.compact(source="run")
            _wait(session)
    finally:
        session.close()
    return events


def _busy_model(model):
    """One session that writes every kind of record."""
    big = "y" * 3000
    Path("big.txt").write_text(big)
    return model(
        tools_(("read_file", {"path": "big.txt"})), text("read it"),                # first task
        tools_(("delegate", {"role": "explore", "task": "list the files"})),        # second task: elides, then delegates...
        tools_(("list_files", {"path": "."})), text("the helper's report"),
        tools_(("read_file", {"path": "big.txt"})),                                 # ...and asks again for what left its context
        text("read it again"),
    )


# --- the schema ------------------------------------------------------------------------------------------------
def test_every_record_type_has_exactly_its_documented_fields(model, isolated, tmp_path):
    server = _busy_model(model)
    events = _session(_config(tmp_path, server.url), "first task", "second task", compact=True)
    assert not [e for e in events if e["type"] == "error"]
    rows = _rows(tmp_path)
    assert {r["type"] for r in rows} == set(SCHEMA)
    assert rows[0]["type"] == "run_header" and sum(r["type"] == "run_header" for r in rows) == 1
    for row in rows:
        _check_schema(row)
    for row in rows:
        if row["type"] == "llm_call":
            assert row["purpose"] in PURPOSES and row["retry_reason"] in RETRY_REASONS
            assert len(row["message_hashes"]) == row["message_count"]
        elif row["type"] == "task_end":
            assert row["reason"] in END_REASONS
    purposes = {r["purpose"] for r in rows if r["type"] == "llm_call"}
    assert {"main_step", "helper_step", "title", "compaction_summary"} <= purposes
    agents = {r["agent_id"] for r in rows if r["type"] == "llm_call"}
    assert "main" in agents and any(a.startswith("explore") for a in agents)  # a helper's calls say whose they are


def test_the_docs_explain_every_field():
    doc = (REPO / "docs" / "research" / "tracing.md").read_text()
    for kind, fields in SCHEMA.items():
        assert f"`{kind}`" in doc, kind
        for name in fields:
            assert f"`{name}`" in doc, f"{kind}.{name}"
    for word in (*PURPOSES, *RETRY_REASONS, *END_REASONS):
        assert f"`{word}`" in doc, word


def test_the_header_describes_the_run_and_never_a_key(model, isolated, tmp_path):
    server = model(text("hi"))
    url = server.url.replace("http://", "http://someone:hunter2@")
    config = _config(tmp_path, url, snapshots=True, models={
        "profiles": {"big": {"model": "big-32b", "base_url": "http://gpu.example:8010/v1", "api_key": "sk-profile-secret", "context_window": 8192}}})
    config.experiment["seed"] = 11
    _session(config, "hello")
    head = _rows(tmp_path)[0]
    assert head["experiment_overrides"] == {"seed": 11} and head["experiment"]["seed"] == 11
    assert len(head["experiment_hash"]) == 64
    assert head["workspace"] == os.getcwd() and head["python"] == sys.version.split()[0]
    assert head["profiles"][0]["base_url_host"] == "127.0.0.1"
    big = next(p for p in head["profiles"] if p["name"] == "big")
    assert big == {"name": "big", "model": "big-32b", "base_url_host": "gpu.example", "context_window": 8192,
                   "max_output_tokens": config.max_output_tokens, "family": "auto", "reasoning_effort": config.reasoning_effort}
    written = [tmp_path / "trace.jsonl", *(tmp_path / "blobs").iterdir()]
    assert len(written) > 1
    for path in written:
        content = path.read_text()
        for secret in ("sk-never-in-a-trace", "sk-profile-secret", "hunter2", "someone"):
            assert secret not in content, path


def test_what_a_server_doesnt_report_stays_null(model, isolated, tmp_path):
    server = model(text("plain", usage=None))
    _session(_config(tmp_path, server.url, stream=False), "hello")
    row = next(r for r in _rows(tmp_path) if r["type"] == "llm_call" and r["purpose"] == "main_step")
    assert row["reported_prompt_tokens"] is None and row["reported_completion_tokens"] is None
    assert row["cached_tokens"] is None and row["reasoning_tokens"] is None
    assert row["usage_estimated"] is True and row["counted_prompt_tokens"] > 0
    assert row["ttft_ms"] is None  # not streamed: there is no first delta to time
    end = next(r for r in _rows(tmp_path) if r["type"] == "task_end")
    assert end["cached_tokens"] is None  # unknown is not zero


def test_what_a_server_does_report_is_kept(model, isolated, tmp_path):
    usage = {"prompt_tokens": 500, "completion_tokens": 60, "total_tokens": 560, "prompt_tokens_details": {"cached_tokens": 320},
             "completion_tokens_details": {"reasoning_tokens": 40}}
    server = model(text("hello", usage=usage))
    _session(_config(tmp_path, server.url), "hello")
    rows = _rows(tmp_path)
    row = next(r for r in rows if r["type"] == "llm_call" and r["purpose"] == "main_step")
    assert (row["reported_prompt_tokens"], row["reported_completion_tokens"], row["cached_tokens"], row["reasoning_tokens"]) == (500, 60, 320, 40)
    assert row["usage_estimated"] is False and row["streamed"] is True
    assert row["ttft_ms"] is not None and row["total_ms"] >= row["ttft_ms"] and row["finish_reason"] == "stop"
    assert next(r for r in rows if r["type"] == "task_end")["cached_tokens"] == 320


def _agent(tmp_path, router, **exp):
    config = make_config()
    config.experiment.update(exp)
    config.trace = {"path": str(tmp_path / "trace.jsonl"), "snapshots": False}
    return Agent(config, FakeLLM(router, config), NullHost(approve=True))


def _same_call(n):
    return AssistantTurn(tool_calls=[call("list_files", "{}", f"c{n}")])


@pytest.mark.parametrize("reason,exp,turns", [
    ("final_answer", {}, [AssistantTurn(content="done")]),
    ("step_limit", {"main_max_steps": 1}, [_same_call(0), AssistantTurn(content="wrap-up")]),
    ("stuck_guard", {"stuck_limit": 1, "repeat_limit": 2}, [_same_call(n) for n in range(9)]),
])
def test_task_end_says_why_it_ended(isolated, tmp_path, reason, exp, turns):
    agent = _agent(tmp_path, scripted(*turns, AssistantTurn(content="done")), **exp)
    agent.run_turn("go")
    assert [r["reason"] for r in _rows(tmp_path) if r["type"] == "task_end"] == [reason]


def test_task_end_after_a_failure_and_an_interrupt(isolated, tmp_path):
    from dcode.engine import Interrupted

    def fail(messages, tools):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _agent(tmp_path, fail).run_turn("go")

    def stop(messages, tools):
        raise Interrupted()

    with pytest.raises(Interrupted):
        _agent(tmp_path, stop).run_turn("go")
    assert [r["reason"] for r in _rows(tmp_path) if r["type"] == "task_end"] == ["error", "interrupted"]


def test_tool_records_tell_a_refusal_a_failure_and_a_cut_apart(isolated, tmp_path):
    Path("big.txt").write_text("line\n" * 200_000)
    agent = _agent(
        tmp_path,
        scripted(AssistantTurn(tool_calls=[call("read_file", json.dumps({"path": "nothing.txt"}), "a")]),
                 AssistantTurn(tool_calls=[call("write_file", json.dumps({"path": "big.txt", "content": "new"}), "b")]),
                 AssistantTurn(tool_calls=[call("run_command", json.dumps({"command": "seq 1 200000"}), "c")]),
                 AssistantTurn(content="done")))
    agent.run_turn("go")
    missing, blind, long_output = [r for r in _rows(tmp_path) if r["type"] == "tool_call"]
    assert not missing["ok"] and missing["error"] == "tool_error" and not missing["was_refused"]
    assert blind["was_refused"] and blind["refusal"] and not blind["ok"]  # the blind-write guard
    assert long_output["ok"] and long_output["truncated"] and long_output["kept_chars"] < long_output["raw_output_chars"]


@pytest.mark.parametrize("router,answer,task,source,hard,model_used", [
    ("off", "HARD", "redesign the module", "off", False, "mock-model"),
    ("always", "EASY", "rename a variable", "always", True, "big-32b"),
    ("auto", "HARD", "rename a variable", "utility", True, "big-32b"),
    ("auto", "EASY", "redesign the architecture", "utility", False, "mock-model"),
    ("auto", "no idea", "design the architecture and migrate the database", "heuristic", True, "big-32b"),
])
def test_a_route_is_recorded_for_every_decision_not_only_when_routed(model, isolated, tmp_path, router, answer, task, source, hard, model_used):
    server = model(text("a"), side=lambda body: answer)
    config = _config(tmp_path, server.url, models={
        "profiles": {"big": {"model": "big-32b", "context_window": 8192}, "small": {"model": "small-7b"}},
        "reasoning": "big", "utility": "small", "router": router})
    _session(config, task)
    route = next(r for r in _rows(tmp_path) if r["type"] == "route")
    assert (route["source"], route["hard"], route["model"], route["input_chars"]) == (source, hard, model_used, len(task))
    assert route["utility_answer"] == (answer if source in ("utility", "heuristic") else None)


def test_a_follow_up_stays_on_the_model_that_started_the_work(model, isolated, tmp_path):
    server = model(text("a"), text("b"), side=lambda body: "HARD")
    config = _config(tmp_path, server.url, models={
        "profiles": {"big": {"model": "big-32b", "context_window": 8192}, "small": {"model": "small-7b"}},
        "reasoning": "big", "utility": "small", "router": "auto"})
    _session(config, "redesign the module", "continue")
    assert [r["source"] for r in _rows(tmp_path) if r["type"] == "route"] == ["utility", "follow_up"]


def test_calling_something_again_after_it_was_elided_is_regret(model, isolated, tmp_path):
    server = _busy_model(model)
    _session(_config(tmp_path, server.url), "first task", "second task")
    rows = _rows(tmp_path)
    elision = [r for r in rows if r["type"] == "elision"]
    assert elision[0]["count"] == 0 and elision[1]["count"] == 1 and elision[1]["chars_removed"] > 2000
    regret = [r for r in rows if r["type"] == "elision_regret"]
    assert [(r["name"], r["how"]) for r in regret] == [("read_file", "elided")]
    assert isinstance(regret[0]["signature"], str) and len(regret[0]["signature"]) == trace.HASH_CHARS


# --- how much of the prefix survives -----------------------------------------------------------------------------
SYSTEM = {"role": "system", "content": "s" * 350}
USER = {"role": "user", "content": "u" * 35}
TOOL_LIST = [{"type": "function", "function": {"name": "f", "parameters": {}}}]


def _state(messages, tools=None):
    return trace.prefix_fields(None, messages, tools)["_state"]


def test_common_prefix():
    assert trace.common_prefix([], []) == 0
    assert trace.common_prefix(["a", "b"], ["a", "b", "c"]) == 2
    assert trace.common_prefix(["a", "b", "c"], ["a", "b"]) == 2
    assert trace.common_prefix(["a", "b"], ["x", "b"]) == 0
    assert trace.common_prefix(["a", "x", "c"], ["a", "y", "c"]) == 1  # only the leading run counts


def test_the_first_call_has_nothing_to_compare_with():
    fields = trace.prefix_fields(None, [SYSTEM, USER], TOOL_LIST)
    assert fields["prev_messages"] is None and fields["prefix_intact"] is None and fields["tools_changed"] is None
    assert fields["lcp_messages"] == 0 and fields["first_changed_index"] is None
    assert len(fields["message_hashes"]) == 2 and fields["tools_hash"] and len(fields["tools_hash"]) == trace.HASH_CHARS


def test_appending_keeps_the_prefix_intact_and_tokens_include_the_unchanged_tools():
    longer = [SYSTEM, USER, {"role": "assistant", "content": "a"}]
    plain = trace.prefix_fields(_state([SYSTEM, USER]), longer, None)
    assert plain["prefix_intact"] is True and plain["lcp_messages"] == 2 and plain["prev_messages"] == 2
    assert plain["first_changed_index"] is None and plain["first_changed_role"] is None
    assert 100 < plain["lcp_tokens"] < 125  # 385 chars / 3.5, and a little for each message's framing
    with_tools = trace.prefix_fields(_state([SYSTEM, USER], TOOL_LIST), longer, TOOL_LIST)
    assert with_tools["lcp_tokens"] > plain["lcp_tokens"] and with_tools["tools_changed"] is False


def test_a_changed_system_prompt_is_named_and_the_prefix_is_gone():
    changed = [{"role": "system", "content": "s" * 350 + " memories"}, USER]
    fields = trace.prefix_fields(_state([SYSTEM, USER], TOOL_LIST), changed, TOOL_LIST)
    assert fields["prefix_intact"] is False and fields["lcp_messages"] == 0
    assert fields["first_changed_index"] == 0 and fields["first_changed_role"] == "system"


def test_a_change_in_the_middle_points_at_that_message():
    tool = {"role": "tool", "tool_call_id": "c", "content": "o" * 4000}
    stubbed = {**tool, "content": "[elided]"}
    last = {"role": "user", "content": "next"}
    fields = trace.prefix_fields(_state([SYSTEM, USER, tool, last]), [SYSTEM, USER, stubbed, last], None)
    assert fields["lcp_messages"] == 2 and fields["first_changed_index"] == 2 and fields["first_changed_role"] == "tool"


def test_a_shorter_request_breaks_the_prefix_without_a_message_to_blame():
    fields = trace.prefix_fields(_state([SYSTEM, USER, USER]), [SYSTEM, USER], None)
    assert fields["prefix_intact"] is False and fields["lcp_messages"] == 2
    assert fields["first_changed_index"] == 2 and fields["first_changed_role"] is None


def test_changed_tools_come_first_on_the_wire_so_no_tokens_are_shared():
    other = [{"type": "function", "function": {"name": "g", "parameters": {}}}]
    fields = trace.prefix_fields(_state([SYSTEM, USER], TOOL_LIST), [SYSTEM, USER, USER], other)
    assert fields["tools_changed"] is True and fields["lcp_tokens"] == 0
    assert fields["lcp_messages"] == 2 and fields["prefix_intact"] is True  # the messages themselves did carry over


def test_a_hash_covers_role_content_and_tool_calls_and_ignores_key_order():
    base = {"role": "assistant", "content": "x", "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]}
    same = {"tool_calls": [{"function": {"arguments": "{}", "name": "f"}, "id": "1"}], "content": "x", "role": "assistant"}
    assert trace.message_hash(base) == trace.message_hash(same)
    assert trace.message_hash(base) != trace.message_hash({**base, "role": "user"})
    assert trace.message_hash(base) != trace.message_hash({**base, "content": "y"})
    assert trace.message_hash(base) != trace.message_hash({**base, "tool_calls": []})


def test_a_side_job_or_a_helper_doesnt_count_as_breaking_the_main_chain(tmp_path):
    tracer = trace.Tracer(str(tmp_path / "t.jsonl"), make_config())
    first, second = [SYSTEM, USER], [SYSTEM, USER, {"role": "assistant", "content": "a"}]
    assert tracer.prefix("main", "main_step", first, None)["prev_messages"] is None
    assert tracer.prefix("main", "title", [{"role": "user", "content": "name this"}], None)["prev_messages"] is None
    assert tracer.prefix("explore-1", "helper_step", [{"role": "user", "content": "sub"}], None)["prev_messages"] is None
    following = tracer.prefix("main", "main_step", second, None)
    assert following["prefix_intact"] is True and following["prev_messages"] == 2
    assert tracer.prefix("main", "final_report", [*second, USER], None)["prefix_intact"] is True  # the same conversation
    assert tracer.prefix("main", "title", [{"role": "user", "content": "name this"}], None)["prefix_intact"] is True  # its own chain


def test_memory_recall_in_the_system_prompt_shows_as_a_broken_prefix_and_in_the_user_message_doesnt(model, isolated, tmp_path, monkeypatch):
    from dcode.memory import MemoryStore

    MemoryStore().save("zebra service", "The zebra service listens on port 9417.", "project", "zebra port")

    def second_call(placement, folder):
        server = model(text("one"), text("two"))
        config = _config(folder, server.url)
        config.experiment["recall_placement"] = placement
        _session(config, "say hello", "what is the zebra service port")
        return [r for r in _rows(folder) if r["type"] == "llm_call" and r["purpose"] == "main_step"][1]

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    broken = second_call("system", tmp_path / "a")
    stable = second_call("user", tmp_path / "b")
    assert broken["lcp_messages"] == 0 and broken["first_changed_role"] == "system"
    assert stable["prev_messages"] == stable["lcp_messages"] == 2 and stable["prefix_intact"] is True


# --- accounting -------------------------------------------------------------------------------------------------
THINKING = {"content": "", "reasoning": "hmm " * 100, "finish": "length", "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
ANSWER = {"content": "four", "usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}}
TOOL_LIST_REQUEST = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}]


def _client(tmp_path, url, **fields):
    defaults = {"model": "Qwen/Qwen3-32B", "reasoning_effort": "medium", "stream": False}
    return LLMClient(_config(tmp_path, url, **{**defaults, **fields}))


def test_a_cut_off_attempt_and_its_retry_are_both_counted(model, isolated, tmp_path):
    server = model(THINKING, ANSWER)
    turn = _client(tmp_path, server.url).chat([{"role": "user", "content": "2+2?"}], tools=TOOL_LIST_REQUEST, max_tokens=600)
    assert turn.content == "four"
    assert turn.usage == {"prompt": 190, "completion": 60, "total": 250} and turn.calls == 2
    assert turn.calibration_prompt == 90  # only the request that was answered can tune the estimator
    rows = [r for r in _rows(tmp_path) if r["type"] == "llm_call"]
    assert [(r["attempt"], r["retry_reason"], r["reported_prompt_tokens"]) for r in rows] == [(0, "none", 100), (1, "think_cutoff", 90)]


def test_a_refused_field_is_a_learned_retry_that_costs_nothing(model, isolated, tmp_path):
    refused = {"status": 400, "message": "Unrecognized request argument supplied: chat_template_kwargs"}
    server = model(refused, ANSWER)
    turn = _client(tmp_path, server.url, model="glm-4.6").chat([{"role": "user", "content": "hi"}], tools=TOOL_LIST_REQUEST)
    assert turn.usage["prompt"] == 90 and turn.calls == 2
    first, second = [r for r in _rows(tmp_path) if r["type"] == "llm_call"]
    assert first["error_class"] == "BadRequestError" and "chat_template_kwargs" in first["error_message"]
    assert first["counted_prompt_tokens"] == 0 and first["usage_estimated"] is False
    assert second["retry_reason"] == "learned_param" and second["rejected_fields"] == ["chat_template_kwargs"]


def test_a_busy_server_is_a_retry_with_its_reason(model, isolated, tmp_path, monkeypatch):
    from dcode import llm as llm_module

    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    client = _client(tmp_path, model({"status": 429, "message": "slow down"}, ANSWER).url)
    client.client = client.client.with_options(max_retries=0)  # the SDK's own retries are the next test
    assert client.chat([{"role": "user", "content": "hi"}], tools=TOOL_LIST_REQUEST).content == "four"
    rows = [r for r in _rows(tmp_path) if r["type"] == "llm_call"]
    assert [r["retry_reason"] for r in rows] == ["none", "busy"] and rows[0]["counted_prompt_tokens"] == 0


def test_the_sdks_silent_retry_is_visible(model, isolated, tmp_path):
    server = model({"status": 503, "message": "overloaded"}, ANSWER)
    turn = _client(tmp_path, server.url).chat([{"role": "user", "content": "hi"}], tools=TOOL_LIST_REQUEST)
    assert turn.content == "four"
    row = next(r for r in _rows(tmp_path) if r["type"] == "llm_call" and not r["error_class"])
    assert row["http_sends"] == 2 and row["http_statuses"] == [503, 200]


def test_a_stream_cut_off_before_its_usage_is_estimated_from_what_was_sent(model, isolated, tmp_path):
    long_prompt = "p" * 3500
    server = model({"content": "c" * 700, "cut": True})
    turn = _client(tmp_path, server.url, stream=True).chat([{"role": "user", "content": long_prompt}], tools=TOOL_LIST_REQUEST)
    assert turn.usage_estimated is True
    assert 1000 < turn.usage["prompt"] < 1100 and 190 < turn.usage["completion"] < 210  # chars / 3.5
    row = next(r for r in _rows(tmp_path) if r["type"] == "llm_call")
    assert row["usage_estimated"] is True and row["reported_prompt_tokens"] is None and row["finish_reason"] is None


def test_a_task_total_holds_steps_helpers_side_jobs_and_summaries(model, isolated, tmp_path):
    server = _busy_model(model)
    events = _session(_config(tmp_path, server.url), "first task", "second task")
    rows = _rows(tmp_path)
    usage = [e for e in events if e["type"] == "usage" and e["agent"] == "main"]
    ends = [r for r in rows if r["type"] == "task_end"]
    assert len(usage) == len(ends) == 2
    calls = [r for r in rows if r["type"] == "llm_call"]
    assert {r["purpose"] for r in calls} >= {"main_step", "helper_step", "title"}
    for event, end in zip(usage, ends, strict=True):
        mine = [r for r in calls if r["task_id"] == end["task_id"]]
        assert event["prompt"] == end["prompt_tokens"] == sum(r["counted_prompt_tokens"] for r in mine) > 0
        assert event["completion"] == end["completion_tokens"] == sum(r["counted_completion_tokens"] for r in mine) > 0
        assert event["calls"] == end["calls"] == len(mine)
        assert event["total"] == event["prompt"] + event["completion"]
    second = [r for r in calls if r["task_id"] == ends[1]["task_id"]]
    assert any(r["purpose"] == "helper_step" for r in second)
    assert any(r["purpose"] == "title" for r in calls if r["task_id"] == ends[0]["task_id"])
    delegate = next(r for r in rows if r["type"] == "delegate")
    helper = [r for r in second if r["purpose"] == "helper_step"]
    assert delegate["calls"] == len(helper) and delegate["prompt_tokens"] == sum(r["counted_prompt_tokens"] for r in helper)
    saved = stats.load()
    assert [(row["prompt"], row["completion"], row["calls"], row["steps"]) for row in saved] == \
        [(e["prompt"], e["completion"], e["calls"], e["steps"]) for e in usage]
    assert all("cached" in row and "usage_estimated" in row for row in saved)
    assert saved[0]["steps"] == 1  # one round of tool calls; the closing reply isn't a step


def test_a_compaction_summary_counts_toward_the_task_that_caused_it(model, isolated, tmp_path):
    server = model(*[text(f"answer {n} " + "z" * 2500) for n in range(8)])
    config = _config(tmp_path, server.url, context_window=4000, max_output_tokens=256)
    events = _session(config, *[f"round {n}" for n in range(8)])
    rows = _rows(tmp_path)
    summaries = [r for r in rows if r["type"] == "llm_call" and r["purpose"] == "compaction_summary"]
    assert summaries
    usage = [e for e in events if e["type"] == "usage" and e["agent"] == "main"]
    assert sum(e["prompt"] for e in usage) == sum(r["counted_prompt_tokens"] for r in rows if r["type"] == "llm_call")


def test_what_a_failed_task_spent_rolls_into_the_next_total(model, isolated, tmp_path):
    server = model(THINKING, {"status": 401, "message": "the key was revoked"}, ANSWER)
    config = _config(tmp_path, server.url, model="Qwen/Qwen3-32B", reasoning_effort="medium")
    events = _session(config, "will fail", "works")
    usage = [e for e in events if e["type"] == "usage" and e["agent"] == "main"]
    assert len(usage) == 1  # no usage event for the failed task...
    calls = [r for r in _rows(tmp_path) if r["type"] == "llm_call"]
    assert usage[0]["prompt"] == sum(r["counted_prompt_tokens"] for r in calls)  # ...and nothing lost: the next one holds it
    assert usage[0]["prompt"] >= 190


def test_counting_needs_no_tracing(model, isolated, tmp_path):
    server = model(THINKING, ANSWER)
    config = AppConfig(base_url=server.url, api_key="x", model="Qwen/Qwen3-32B", reasoning_effort="medium", stream=False)
    events = _session(config, "hello")
    assert [e["prompt"] for e in events if e["type"] == "usage"][0] >= 190
    assert not (tmp_path / "trace.jsonl").exists()


# --- a trace must never get in the way -----------------------------------------------------------------------------
def test_the_file_is_private_even_if_it_existed(model, isolated, tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text("")
    path.chmod(0o644)
    _session(_config(tmp_path, model(text("hi")).url, snapshots=True), "hello")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    blobs = list((tmp_path / "blobs").iterdir())
    assert blobs and all(stat.S_IMODE(blob.stat().st_mode) == 0o600 for blob in blobs)
    assert stat.S_IMODE((tmp_path / "blobs").stat().st_mode) == 0o700


def test_a_path_that_cant_be_written_doesnt_stop_the_task(model, isolated, tmp_path):
    (tmp_path / "a-file").write_text("not a folder")
    config = _config(tmp_path, model(tools_(("list_files", {"path": "."})), text("finished")).url, snapshots=True)
    config.trace["path"] = str(tmp_path / "a-file" / "trace.jsonl")
    events = _session(config, "hello")
    assert not [e for e in events if e["type"] == "error"]
    assert any(e["type"] == "assistant_message" and e["text"] == "finished" for e in events)


def test_a_broken_snapshot_folder_loses_the_blob_and_nothing_else(model, isolated, tmp_path):
    (tmp_path / "blobs").write_text("in the way")
    _session(_config(tmp_path, model(text("hi")).url, snapshots=True), "hello")
    row = next(r for r in _rows(tmp_path) if r["type"] == "llm_call")
    assert row["snapshot"] is None


def test_parallel_writers_never_mix_lines(isolated, tmp_path):
    tracer = trace.Tracer(str(tmp_path / "trace.jsonl"), make_config())
    padding = "x" * 5000

    def write(n):
        for index in range(200):
            tracer.emit("elision", task_id=f"t{n}", agent_id=f"a{n}", count=index, chars_removed=0, padding=padding)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = _rows(tmp_path)
    assert len(rows) == 8 * 200 + 1 and rows[0]["type"] == "run_header"
    assert sum(r["type"] == "run_header" for r in rows) == 1
    for n in range(8):
        mine = [r["count"] for r in rows if r["task_id"] == f"t{n}" and r["type"] == "elision"]
        assert mine == list(range(200))  # each writer's order holds


def test_a_snapshot_is_the_exact_request_named_by_its_hash(model, isolated, tmp_path):
    server = model(tools_(("list_files", {"path": "."})), text("done"))
    _session(_config(tmp_path, server.url, snapshots=True), "hello")
    rows = [r for r in _rows(tmp_path) if r["type"] == "llm_call" and r["purpose"] == "main_step"]
    assert len(rows) == 2
    for index, row in enumerate(rows):
        blob = tmp_path / "blobs" / f"{row['snapshot']}.json"
        raw = blob.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["snapshot"]
        body = json.loads(raw)
        assert len(body["messages"]) == row["message_count"]  # as sent: the history grew in place afterwards
        assert row["step"] == index and body["messages"][-1]["role"] == ("user", "tool")[index]
    sent = [b for b in server.seen if b.get("tools")]
    for row, wire in zip(rows, sent, strict=True):
        assert json.loads((tmp_path / "blobs" / f"{row['snapshot']}.json").read_text())["messages"] == wire["messages"]


def test_snapshots_are_off_by_default_and_tracing_off_writes_nothing(model, isolated, tmp_path):
    _session(_config(tmp_path, model(text("hi")).url), "hello")
    assert not (tmp_path / "blobs").exists()
    assert all(r.get("snapshot") is None for r in _rows(tmp_path))
    assert trace.for_config(AppConfig(base_url="http://x/v1", api_key="k", model="m")) is None


def test_a_second_process_on_the_same_file_appends_with_its_own_header(isolated, tmp_path):
    for _ in range(2):
        trace.Tracer(str(tmp_path / "trace.jsonl"), make_config()).emit("elision", task_id="t", count=0, chars_removed=0)
    assert [r["type"] for r in _rows(tmp_path)] == ["run_header", "elision", "run_header", "elision"]


# --- the real command --------------------------------------------------------------------------------------------
def _run(args, folder, env):
    full = {key: value for key, value in os.environ.items() if not key.startswith(("DCODE_", "CODE_ONE_"))}
    full.update({"PYTHONPATH": str(REPO), "DCODE_HOME": str(folder / "home"), "BROWSER": "true", **env})
    return subprocess.run([sys.executable, "-m", "dcode", *args], cwd=folder, env=full, capture_output=True, text=True, timeout=120)


def test_dcode_run_end_to_end_with_trace_task_id_and_experiment(model, isolated, tmp_path):
    (tmp_path / "exp.json").write_text(json.dumps({"seed": 7, "temperature": 0.25, "shrink_old_args": False}))
    server = model(tools_(("write_file", {"path": "out.txt", "content": "hello"})), text("all done"))
    run = _run(["run", "--mode", "auto", "write the file"], tmp_path, {
        "DCODE_BASE_URL": server.url, "DCODE_API_KEY": "sk-never-in-a-trace", "DCODE_MODEL": "mock-model",
        "DCODE_TRACE": str(tmp_path / "trace.jsonl"), "DCODE_TRACE_SNAPSHOTS": "1", "DCODE_TASK_ID": "study-41",
        "DCODE_EXPERIMENT": str(tmp_path / "exp.json")})
    assert run.returncode == 0, run.stdout + run.stderr
    assert (tmp_path / "out.txt").read_text() == "hello"
    rows = _rows(tmp_path)
    assert rows[0]["type"] == "run_header" and rows[-1]["type"] == "task_end" and rows[-1]["reason"] == "final_answer"
    assert rows[0]["experiment_overrides"] == {"seed": 7, "temperature": 0.25, "shrink_old_args": False}
    assert {r["task_id"] for r in rows} == {"study-41"}
    for row in rows:
        _check_schema(row)
    steps = [b for b in server.seen if b.get("tools")]
    assert steps and all(b["seed"] == 7 and b["temperature"] == 0.25 and "top_p" not in b for b in steps)
    assert server.headers and all(h.get("x-dcode-task") == "study-41" for h in server.headers)
    assert "sk-never-in-a-trace" not in (tmp_path / "trace.jsonl").read_text()
    # An environment override describes one run, it doesn't change the user's saved settings.
    saved = tmp_path / "home" / "config.json"
    assert not saved.exists() or "experiment" not in json.loads(saved.read_text())


@pytest.mark.parametrize("content,says", [('{"compact_triger": 0.5}', "compact_triger"), ("{not json", "can't read")])
def test_a_wrong_experiment_file_stops_the_run_with_a_clear_line(tmp_path, content, says):
    (tmp_path / "exp.json").write_text(content)
    run = _run(["run", "hello"], tmp_path, {"DCODE_BASE_URL": "http://127.0.0.1:9/v1", "DCODE_API_KEY": "x", "DCODE_MODEL": "m",
                                            "DCODE_EXPERIMENT": str(tmp_path / "exp.json")})
    assert run.returncode == 2
    assert says in run.stdout + run.stderr and "Traceback" not in run.stdout + run.stderr
