from __future__ import annotations

from dcode.memory import MemoryStore, project_key


def test_save_list_get_delete(isolated):
    store = MemoryStore(isolated)
    store.save("Build Command", "Run `make test` before committing.", tags=["build"])
    store.save("editor", "User prefers tabs", scope="global")
    names = {m.name for m in store.list()}
    assert names == {"build-command", "editor"}
    assert store.get("build-command").tags == ["build"]
    assert store.get("build-command").description.startswith("Run `make test`")
    assert store.delete("build-command")
    assert store.get("build-command") is None


def test_projects_are_isolated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    MemoryStore(a).save("fact", "only in a")
    assert MemoryStore(b).list(scope="project") == []
    assert project_key(a) != project_key(b)


def test_search_ranks_relevant_first(isolated):
    store = MemoryStore(isolated)
    store.save("db-migrations", "Use alembic for database migrations; never edit applied revisions.")
    store.save("logging", "Use structlog, JSON output in production.")
    store.save("frontend", "React components live in web/src/components.")
    hits = store.search("how do I add a database migration?")
    assert hits and hits[0].name == "db-migrations"
    assert store.search("") == []


def test_recall_respects_budget(isolated):
    store = MemoryStore(isolated)
    store.save("big", "migration " * 500)
    text = store.recall("migration", max_chars=400)
    assert 0 < len(text) <= 420


def test_index_excludes_sessions(isolated):
    store = MemoryStore(isolated)
    store.save("conv", "snake_case everywhere")
    store.save_session_summary("20260101-000000", "- refactored parser")
    index = store.index_text(max_chars=1000)
    assert "conv" in index and "session-" not in index
    assert store.search("parser")[0].name.startswith("session-")
