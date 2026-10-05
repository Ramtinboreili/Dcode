from __future__ import annotations

from conftest import FakeLLM, make_config, scripted

from dcode import mentions
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn


def _tree(root):
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# readme\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x")


def test_find_mentions(isolated):
    _tree(isolated)
    text = "fix @src/app.py and see @README.md. mail me at a@b.com, ignore @missing.txt, list @src"
    assert mentions.find(text, isolated) == ["src/app.py", "README.md", "src/"]


def test_mentions_cannot_escape_the_workspace(isolated, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    assert mentions.find("read @../secret.txt", isolated) == []


def test_typing_token():
    assert mentions.typing_token("look at @src/a") == "src/a"
    assert mentions.typing_token("@") == ""
    assert mentions.typing_token("mail a@b") is None
    assert mentions.typing_token("done @x ") is None


def test_fuzzy_completion_ranks_names_first(isolated):
    _tree(isolated)
    mentions._index.clear()
    assert mentions.complete("app", isolated)[0] == "src/app.py"
    assert mentions.complete("sapp", isolated) == ["src/app.py"]  # subsequence
    assert not any("node_modules" in p for p in mentions.complete("", isolated, limit=50))


def test_mentioned_files_arrive_as_read_results(isolated):
    _tree(isolated)
    seen = {}

    def router(messages, tools):
        seen["messages"] = [dict(m) for m in messages]
        return AssistantTurn(content="It prints hi.")

    config = make_config()
    host = NullHost()
    agent = Agent(config, FakeLLM(router, config), host)
    agent.run_turn("what does @src/app.py do?")
    roles = [m["role"] for m in seen["messages"]]
    assert roles[-3:] == ["user", "assistant", "tool"]
    assert seen["messages"][-1]["content"] == "1| print('hi')"
    assert seen["messages"][-2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert next(e for e in host.events if e["type"] == "mentions")["attached"] == ["src/app.py"]


def test_extra_mentions_are_only_named(isolated):
    for i in range(5):
        (isolated / f"f{i}.txt").write_text(str(i))
    config = make_config()
    agent = Agent(config, FakeLLM(scripted(AssistantTurn(content="ok")), config), NullHost())
    agent.run_turn("compare @f0.txt @f1.txt @f2.txt @f3.txt @f4.txt")
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert len(tools) == mentions.MAX_ATTACH
    assert "f3.txt, f4.txt" in agent.messages[-2]["content"]  # before the final answer
