from __future__ import annotations

import json

import pytest
from conftest import FakeLLM, call, make_config, scripted

from dcode import config
from dcode.artifacts import ArtifactStore, guess_kind, valid_id
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn
from dcode.skills import BUILTIN_DIR, get_skill, list_skills, save_skill, seed_builtin_skills

BUILTINS = {"devops", "developer", "ui-designer", "code-reviewer", "artifact-creator", "tester",
            "security-auditor", "docs-writer", "debugger"}


# --- artifacts ---------------------------------------------------------------------

def test_artifact_save_update_keeps_history():
    store = ArtifactStore()
    meta = store.save("Status Dashboard", "<html><body>v1</body></html>", kind="html")
    assert meta["kind"] == "html" and meta["versions"] == 1 and valid_id(meta["id"])
    updated = store.save("Status Dashboard", "<html><body>v2</body></html>", artifact_id=meta["id"])
    assert updated["id"] == meta["id"] and updated["versions"] == 2
    record = store.get(meta["id"])
    assert "v2" in record["content"] and "v1" in record["history"][0]["content"]
    assert [m["id"] for m in store.list()] == [meta["id"]]
    assert store.delete(meta["id"]) and store.get(meta["id"]) is None


def test_artifact_validation():
    store = ArtifactStore()
    with pytest.raises(ValueError):
        store.save("empty", "   ")
    assert store.get("../../config") is None
    assert not store.delete("../x")
    assert guess_kind("", "<svg viewBox='0 0 1 1'></svg>") == "svg"
    assert guess_kind("md", "# hi") == "markdown"
    assert guess_kind("weird", "print(1)") == "code"


# --- skills ------------------------------------------------------------------------

def test_builtin_skills_are_seeded_once():
    config.ensure_app_dirs()
    names = {s.name for s in list_skills()}
    assert BUILTINS <= names
    assert {p.stem for p in BUILTIN_DIR.glob("*.md")} == BUILTINS
    # A skill the user deleted must not come back.
    (config.skills_dir() / "devops.md").unlink()
    seed_builtin_skills()
    assert "devops" not in {s.name for s in list_skills()}


def test_builtin_skills_are_short_and_described():
    for skill in list_skills():
        assert skill.description, skill.name
        assert len(skill.content) < 1800, f"{skill.name} is too long for small-context models"


def test_save_and_project_skills(isolated):
    skill = save_skill("Ansible Playbooks", "Writing playbooks our way", "- use roles\n- tag every task")
    assert skill.name == "ansible-playbooks" and get_skill("ansible-playbooks").description == "Writing playbooks our way"
    project_dir = isolated / ".dcode" / "skills"
    project_dir.mkdir(parents=True)
    (project_dir / "repo-rules.md").write_text("# Repo rules\n- run make lint\n")
    found = {s.name: s for s in list_skills(isolated)}
    assert found["repo-rules"].scope == "project" and found["repo-rules"].description == "Repo rules"


# --- engine integration ---------------------------------------------------------------

def _agent(router, **cfg):
    config.ensure_app_dirs()
    c = make_config(**cfg)
    return Agent(c, FakeLLM(router, c), NullHost())


def test_artifact_tool_saves_and_compacts_context():
    big = "<html><body>" + "x" * 5000 + "</body></html>"
    agent = _agent(scripted(
        AssistantTurn(tool_calls=[call("artifact_save", json.dumps({"title": "Report", "kind": "html", "content": big}))]),
        AssistantTurn(content="Saved the report."),
    ))
    agent.run_turn("make a report page")
    saved = ArtifactStore().list()
    assert len(saved) == 1 and saved[0]["title"] == "Report"
    assert ArtifactStore().get(saved[0]["id"])["content"] == big
    # The 5K payload must not stay in the (small) context window.
    call_args = next(m for m in agent.messages if m.get("tool_calls"))["tool_calls"][0]["function"]["arguments"]
    assert "x" * 300 not in call_args and "chars" in call_args
    assert any(e["type"] == "artifact_saved" for e in agent.host.events)


def test_skill_index_and_skill_use():
    agent = _agent(scripted(
        AssistantTurn(tool_calls=[call("skill_use", '{"name": "devops"}')]),
        AssistantTurn(content="ok"),
    ))
    prompt = agent.system_prompt()
    assert "- devops:" in prompt and "set -euo pipefail" not in prompt  # index only, not full text
    agent.run_turn("write a deploy script")
    tool = next(m for m in agent.messages if m["role"] == "tool")
    assert "set -euo pipefail" in tool["content"]


def test_pinned_skill_is_inlined():
    agent = _agent(scripted(), enabled_skills=["debugger.md"])
    prompt = agent.system_prompt()
    assert "Pinned skill: debugger" in prompt and "Reproduce first" in prompt


def test_skill_save_tool():
    agent = _agent(scripted(
        AssistantTurn(tool_calls=[call("skill_save", json.dumps(
            {"name": "k8s-deploys", "description": "Deploying to our clusters", "instructions": "- use helm"}))]),
        AssistantTurn(content="done"),
    ))
    agent.run_turn("make a skill for k8s deploys")
    assert get_skill("k8s-deploys").content.endswith("- use helm")
    assert "- k8s-deploys:" in agent.messages[0]["content"]


def test_artifacts_can_be_disabled():
    agent = _agent(scripted(), artifacts={"enabled": False})
    assert "artifact_save" not in agent.tool_names()
