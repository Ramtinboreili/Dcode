"""More than one model: profiles, the reasoning router, helpers on another server, the utility model."""

from __future__ import annotations

import json
import time

from conftest import FakeLLM, call, make_config, req, scripted

from dcode.config import config_from_dict, load_raw_config
from dcode.llm import AssistantTurn
from dcode.models import ModelPool, clean_models, decide, heuristic_route, is_follow_up
from dcode.session import PLAN_GO, Session

HARD = "Why does the worker deadlock under load? Investigate the root cause."
EASY = "list the files in src"

MODELS = {
    "profiles": {
        "thinker": {"model": "big-32b", "base_url": "http://gpu:31002/v1", "context_window": 16384,
                    "reasoning_effort": "high"},
        "tiny": {"model": "small-3b", "base_url": "http://gpu:31003/v1", "context_window": 8192},
    },
    "reasoning": "thinker", "utility": "", "router": "auto",
}


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Rig:
    """A session whose every model is a FakeLLM; ``seen`` lists the model that answered each call."""

    def __init__(self, models=None, answer=None, **overrides):
        self.seen: list[str] = []
        self.windows: list[int] = []
        self.clients: dict[str, FakeLLM] = {}
        self.answer = answer or (lambda messages, tools: AssistantTurn(content="done"))
        config = make_config(models=clean_models(models or MODELS), **overrides)
        self.session = Session(config, llm=self._client(config))
        self.session.models.factory = self._client

    def _client(self, config):
        def router(messages, tools, model=config.model):
            self.seen.append(model)
            self.windows.append(self.session.agent.budget.context_window)
            return self.answer(messages, tools)

        self.clients[config.model] = FakeLLM(router, config)
        return self.clients[config.model]

    def run(self, text):
        before = len([e for e in self.events() if e["type"] == "usage" and e.get("agent") == "main"])
        self.session.submit(text)
        assert _wait(lambda: len([e for e in self.events() if e["type"] == "usage" and e.get("agent") == "main"]) > before)
        assert _wait(lambda: self.session.idle)

    def events(self):
        return self.session.bus.since(0)


def test_models_config_is_cleaned():
    cleaned = clean_models({
        "profiles": {"ok": {"model": " m ", "context_window": "100", "api_key": "", "junk": 1, "reasoning_effort": "max"},
                     "main": {"model": "x"}, "empty": {"model": ""}, "bad": "nope"},
        "reasoning": " ok ", "router": "sometimes",
    })
    assert cleaned == {"profiles": {"ok": {"model": "m", "context_window": 2048}}, "reasoning": "ok",
                       "utility": "", "router": "auto"}
    assert clean_models(None)["profiles"] == {} and clean_models("x")["router"] == "auto"
    config = config_from_dict({"base_url": "u", "api_key": "k", "model": "m", "models": MODELS})
    assert config.models["profiles"]["thinker"]["context_window"] == 16384
    assert config.as_dict()["models"]["reasoning"] == "thinker"


def test_heuristic_router():
    assert heuristic_route(HARD)[0]
    assert heuristic_route("think hard about how to name this")[0]
    assert heuristic_route("fix this\nTraceback (most recent call last):\n  File 'a.py'\nKeyError: 'x'")[0]
    assert heuristic_route("add a --verbose flag " * 60)[0]  # long and detailed
    for text in (EASY, "hello", "rename foo to bar in utils.py", "why"):
        assert not heuristic_route(text)[0], text
    assert is_follow_up("yes, go on") and is_follow_up("fix it") and is_follow_up("now run the tests")
    assert not is_follow_up("why does it crash?") and not is_follow_up(HARD) and not is_follow_up(EASY)


def test_a_profile_has_its_own_server_and_window():
    config = make_config(models=clean_models(MODELS))
    pool = ModelPool(config, FakeLLM(scripted(), config), factory=lambda cfg: FakeLLM(scripted(), cfg))
    cfg, client = pool.resolve("thinker")
    assert (cfg.base_url, cfg.model, cfg.context_window, cfg.reasoning_effort) == \
        ("http://gpu:31002/v1", "big-32b", 16384, "high")
    assert cfg.api_key == "secret" and client.config is cfg  # the key falls back to the main one
    assert pool.resolve("thinker")[1] is client  # reused: it keeps what it learned about its server
    assert pool.resolve("") is None and pool.resolve("main") is None and pool.resolve(config.model) is None
    bare = pool.resolve("other-7b")[0]  # a bare model name: the main server
    assert (bare.base_url, bare.model, bare.context_window) == (config.base_url, "other-7b", config.context_window)
    assert config.model == "qwen2.5-coder" and config.context_window == 32768  # the main config is untouched


def test_hard_tasks_go_to_the_reasoning_model_and_easy_ones_stay():
    rig = Rig()
    try:
        rig.run(EASY)
        assert rig.seen == ["qwen2.5-coder"] and not [e for e in rig.events() if e["type"] == "route"]
        rig.run(HARD)
        assert rig.seen[-1] == "big-32b" and rig.windows[-1] == 16384  # its own context budget for the task
        route = next(e for e in rig.events() if e["type"] == "route")
        assert route["model"] == "big-32b" and route["role"] == "reasoning" and route["reason"]
        usage = [e for e in rig.events() if e["type"] == "usage"][-1]
        assert usage["model"] == "big-32b" and usage["context_window"] == 16384
        # Afterwards the session is back on the main model, with the main window.
        agent = rig.session.agent
        assert agent.llm is rig.session.llm and agent.config is rig.session.config
        assert agent.budget.context_window == 32768
        rig.run("yes, go on")  # a short follow-up continues on the model that was thinking
        assert rig.seen[-1] == "big-32b"
        rig.run(EASY)
        assert rig.seen[-1] == "qwen2.5-coder"
        rig.run("ok")  # ...and a follow-up to an easy task stays on the main model
        assert rig.seen[-1] == "qwen2.5-coder"
    finally:
        rig.session.close()


def test_plans_are_made_and_carried_out_by_the_main_model():
    rig = Rig()
    try:
        rig.session.set_mode("plan")
        rig.run(HARD)
        assert rig.seen == ["qwen2.5-coder"]
        rig.session.resolve_plan("accept_edits")
        assert _wait(lambda: len(rig.seen) == 2 and rig.session.idle)
        assert rig.seen[-1] == "qwen2.5-coder"
        assert any(e["type"] == "user_message" and e["text"] == PLAN_GO for e in rig.events())
    finally:
        rig.session.close()


def test_routing_off_always_and_no_reasoning_model():
    rig = Rig()
    try:
        rig.session.set_model(route="off")
        rig.run(HARD)
        assert rig.seen[-1] == "qwen2.5-coder"
        rig.session.set_model(route="always")
        rig.run(EASY)
        assert rig.seen[-1] == "big-32b"
        rig.session.set_model(route="nonsense")
        assert rig.session.route == "always" and rig.session.state()["route"] == "always"
        assert rig.session.state()["reasoning_model"] == "big-32b"
    finally:
        rig.session.close()
    none = Rig(models={"router": "always"})
    try:
        none.run(HARD)
        assert none.seen == ["qwen2.5-coder"] and none.session.state()["reasoning_model"] == ""
    finally:
        none.session.close()


def test_the_utility_model_sorts_tasks_and_names_the_chat():
    rig = Rig(models=dict(MODELS, utility="tiny"))
    verdicts = {"sorted": [], "titled": []}

    def complete_text(messages, max_tokens=800):
        system = messages[0]["content"]
        if "route tasks" in system:
            verdicts["sorted"].append(messages[1]["content"])
            return "HARD" if "playbook" in messages[1]["content"] else "<think>simple</think>EASY"
        verdicts["titled"].append(messages[1]["content"])
        return "A title"

    try:
        rig.session.models.resolve("tiny")[1].complete_text = complete_text
        rig.run("tidy the playbook")  # no keyword says "hard": the small model's verdict decides
        assert rig.seen == ["big-32b"]
        assert "small-3b judged it hard" in next(e for e in rig.events() if e["type"] == "route")["reason"]
        assert _wait(lambda: verdicts["titled"] == ["tidy the playbook"])  # the title came from it too
        rig.run(HARD.replace("root cause", "reason"))  # ...and it can overrule the keywords
        assert rig.seen[-1] == "qwen2.5-coder"
    finally:
        rig.session.close()


def test_router_falls_back_to_keywords_when_the_utility_model_fails():
    config = make_config(models=clean_models(dict(MODELS, utility="tiny")))

    class Broken(FakeLLM):
        def complete_text(self, messages, max_tokens=800):
            raise RuntimeError("server down")

    pool = ModelPool(config, FakeLLM(scripted(), config), factory=lambda cfg: Broken(scripted(), cfg))
    assert decide(pool, HARD, "auto", False)[0] and not decide(pool, EASY, "auto", False)[0]


def test_a_helper_runs_on_its_profile_with_that_models_window(isolated):
    (isolated / ".dcode" / "agents").mkdir(parents=True)
    (isolated / ".dcode" / "agents" / "scout.md").write_text(
        "---\ndescription: Finds things\nmodel: tiny\n---\nYou find things.\n")

    def answer(messages, tools):
        if "You find things" in messages[0]["content"]:
            return AssistantTurn(content="REPORT: found it")
        if messages[-1]["role"] == "tool":
            return AssistantTurn(content="Done.")
        return AssistantTurn(tool_calls=[call("delegate", json.dumps({"role": "scout", "task": "find x"}))])

    rig = Rig(answer=answer)
    try:
        rig.run("look for x")
        assert rig.seen == ["qwen2.5-coder", "small-3b", "qwen2.5-coder"]
        start = next(e for e in rig.events() if e["type"] == "agent_start")
        assert start["model"] == "small-3b"
        helper_usage = next(e for e in rig.events() if e["type"] == "usage" and e.get("agent") != "main")
        assert helper_usage["context_window"] == 8192
        from dcode import tools

        assert tools.MAX_OUTPUT_CHARS == int(32768 * 3.5 * 0.12)  # the main model's output caps are back
    finally:
        rig.session.close()


def test_profile_keys_are_never_sent_back_and_survive_a_save(stack):
    models = {"profiles": {"thinker": {"model": "big-32b", "base_url": "http://gpu:31002/v1", "api_key": "sk-secret-123456"}},
              "reasoning": "thinker", "router": "always"}
    status, _, _ = req(stack, "PUT", "/api/config", {"models": models})
    assert status == 200
    status, cfg, _ = req(stack, "GET", "/api/config")
    shown = cfg["models"]["profiles"]["thinker"]["api_key"]
    assert "sk-secret-123456" not in json.dumps(cfg) and "•" in shown
    assert _wait(lambda: stack["session"].route == "always")  # the live session picked it up
    assert stack["session"].models.resolve("thinker")[0].api_key == "sk-secret-123456"
    # Saving the form again (masked key, another field changed) keeps the stored key.
    cfg["models"]["profiles"]["thinker"]["context_window"] = 16384
    assert req(stack, "PUT", "/api/config", {"models": cfg["models"]})[0] == 200
    saved = load_raw_config()["models"]["profiles"]["thinker"]
    assert saved["api_key"] == "sk-secret-123456" and saved["context_window"] == 16384
    # Removing the profile removes it.
    assert req(stack, "PUT", "/api/config", {"models": {"profiles": {}}})[0] == 200
    assert load_raw_config()["models"]["profiles"] == {}
    status, _, _ = req(stack, "POST", f"/api/s/{stack['session'].id}/model", {"route": "off"})
    assert status == 200 and stack["session"].route == "off"


def test_models_command_in_the_terminal(isolated):
    from dcode import ui
    from dcode.cli import TerminalApp
    from dcode.config import load_config

    config = make_config()
    session = Session(config, llm=FakeLLM(scripted(), config))
    app = TerminalApp(session)
    try:
        with ui.console.capture() as capture:
            app._command("/models add thinker big-32b http://gpu:31002/v1 16384")
            app._command("/models reasoning thinker")
            app._command("/models helpers thinker")
            app._command("/models route always")
            app._command("/models add main x")
            app._command("/models bogus")
        shown = capture.get()
        assert "big-32b @ http://gpu:31002/v1 · 16,384 tokens" in shown and "always: every task" not in shown
        assert "can't be used" in shown and "Usage: /models" in shown
        saved = load_config()
        assert saved.models["reasoning"] == "thinker" and saved.models["router"] == "always"
        assert saved.models["profiles"] == {"thinker": {"model": "big-32b", "base_url": "http://gpu:31002/v1",
                                                        "context_window": 16384}}
        assert saved.agents["helper_model"] == "thinker" and session.route == "always"
        with ui.console.capture() as capture:
            app._command("/models")
            app._command("/models rm thinker")
        assert "always: every task on the reasoning model" in capture.get()
        assert "[server-url] [context]" in capture.get()  # rich must not eat the brackets
        saved = load_config()
        assert saved.models["profiles"] == {} and saved.models["reasoning"] == "" and saved.agents["helper_model"] == ""
    finally:
        session.close()


def test_the_model_family_in_the_terminal_and_the_settings(isolated, stack):
    from dcode import ui
    from dcode.cli import TerminalApp
    from dcode.config import load_config

    config = make_config(model="my-finetune")
    session = Session(config, llm=FakeLLM(scripted(), config))
    app = TerminalApp(session)
    try:
        with ui.console.capture() as capture:
            app._command("/models")
            app._command("/models family glm")
            app._command("/models family nonsense")
        shown = capture.get()
        assert "family     Other · reasoning: reasoning_effort" in shown
        assert "Main model: GLM (set by you) · reasoning: chat-template switch" in shown
        assert "Families: auto, openai," in shown
        assert load_config().family == "glm"
        app._command("/models family auto")
        assert load_config().family == "auto"
    finally:
        session.close()
    _, data, _ = req(stack, "GET", "/api/config")
    assert {"key": "qwen", "label": "Qwen3"} in data["families"] and data["family"] == "auto"
    assert data["family_detected"].startswith("Other")
    assert req(stack, "PUT", "/api/config", {"family": "deepseek"})[0] == 200
    assert load_raw_config()["family"] == "deepseek"
