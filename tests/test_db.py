from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest
from conftest import FakeLLM, call, make_config, scripted
from test_modes import ModeHost
from test_session import _session, _wait

from dcode import db
from dcode.config import home_dir
from dcode.engine import Agent, NullHost
from dcode.llm import AssistantTurn


@pytest.fixture
def shop(isolated):
    """A small SQLite database saved as the connection 'shop'."""
    path = isolated.parent / "shop.db"
    handle = sqlite3.connect(path)
    handle.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                             total REAL, status TEXT DEFAULT 'new', blobby BLOB);
        CREATE INDEX orders_customer_idx ON orders(customer_id);
        CREATE VIEW paid AS SELECT * FROM orders WHERE status = 'paid';
    """)
    handle.executemany("INSERT INTO customers VALUES (?, ?, ?)", [(i, f"u{i}@example.com", f"User {i}") for i in range(1, 51)])
    handle.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
                       [(i, 1 + i % 50, i + 0.5, "paid" if i % 3 == 0 else "new", b"\xde\xad") for i in range(1, 501)])
    handle.commit()
    handle.close()
    return db.save_connection({"name": "Shop", "engine": "sqlite3", "database": str(path)})


def _count(conn, table="orders", where=""):
    return db.run_query(conn, f"SELECT count(*) FROM {table} {where}")["rows"][0][0]


# --- SQL text -------------------------------------------------------------------------------

def test_statements_are_split_outside_strings_and_comments():
    sql = "SELECT ';' AS a, \"x;y\" FROM t -- no; split here\n; /* nor; here */ UPDATE t SET a = $tag$ ; $tag$;"
    assert db.split_statements(sql) == ["SELECT ';' AS a, \"x;y\" FROM t", "UPDATE t SET a = $tag$ ; $tag$"]
    assert db.single_statement("  SELECT 1 ;\n") == "SELECT 1"
    with pytest.raises(db.DBError, match="one statement at a time"):
        db.single_statement("SELECT 1; DROP TABLE t")
    with pytest.raises(db.DBError, match="empty"):
        db.single_statement(" -- nothing\n")


def test_anything_that_is_not_clearly_a_read_counts_as_a_change():
    for sql in ("select 1", "  (SELECT 1)", "WITH x AS (SELECT 1) SELECT * FROM x", "EXPLAIN SELECT 1", "show tables"):
        assert db.classify(sql) == "read", sql
    for sql in ("UPDATE t SET a=1", "delete from t", "DROP TABLE t", "TRUNCATE t", "create index i on t(a)",
                "GRANT ALL ON t TO u", "CALL p()", "SET x = 1", "vacuum", "COPY t TO '/tmp/x'", ""):
        assert db.classify(sql) == "write", sql


# --- connections ------------------------------------------------------------------------------

def test_connections_are_private_and_never_show_the_password(isolated):
    conn = db.save_connection({"name": "Prod DB!", "engine": "PostgreSQL", "host": "db.internal", "database": "app",
                               "user": "readonly", "password": "s3cret"})
    assert conn["name"] == "prod-db" and conn["engine"] == "postgres" and conn["port"] == 5432
    path = home_dir() / "db" / "connections.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    shown = db.public(db.get_connection("prod-db"))
    assert "password" not in shown and shown["has_password"] and "s3cret" not in json.dumps(shown)
    # Saving again with an empty or masked password keeps the stored one.
    db.save_connection({"name": "prod-db", "engine": "postgres", "host": "db2.internal", "user": "readonly", "password": "••••"})
    again = db.get_connection("prod-db")
    assert again["password"] == "s3cret" and again["host"] == "db2.internal"
    with pytest.raises(db.DBError, match="user name"):
        db.save_connection({"name": "x", "engine": "mysql", "host": "h"})
    with pytest.raises(db.DBError, match="No database connection named 'nope'"):
        db.get_connection("nope")
    assert db.delete_connection("prod-db") and not db.load_connections()


def test_connection_urls():
    assert db.parse_url("postgresql://ana:p%40ss@db.internal:6432/shop") == {
        "engine": "postgres", "host": "db.internal", "port": 6432, "database": "shop", "user": "ana", "password": "p@ss"}
    assert db.parse_url("mariadb://root@10.0.0.5/app")["port"] == 3306
    assert db.parse_url("sqlite:////var/data/app.db") == {"engine": "sqlite", "database": "/var/data/app.db"}
    with pytest.raises(db.DBError):
        db.parse_url("redis://x")


def test_password_can_come_from_the_environment(monkeypatch):
    conn = {"name": "x", "password_env": "MY_DB_PW", "password": ""}
    with pytest.raises(db.DBError, match="MY_DB_PW"):
        db._password(conn)
    monkeypatch.setenv("MY_DB_PW", "from-env")
    assert db._password(conn) == "from-env"


# --- schema, queries, guards ----------------------------------------------------------------------

def test_schema_summary_and_model_text(shop):
    schema = db.read_schema(shop)
    assert [t["label"] for t in schema["tables"]] == ["customers", "orders", "paid"]
    orders = db.find_table(schema, "ORDERS")
    assert [c["name"] for c in orders["columns"] if c["pk"]] == ["id"] and orders["rows"] == 500
    assert "FOREIGN KEY (customer_id) REFERENCES customers(id)" in orders["keys"]
    text = db.schema_text(schema)
    assert "customer_id INTEGER not null -> customers.id" in text and "paid (view)" in text
    # Too big for the context: table names only, with the way to get the columns.
    short = db.schema_text(schema, max_chars=120)
    assert "customer_id" not in short and "db_schema with a table name" in short
    assert "index orders_customer_idx (customer_id)" in db.schema_text(schema, "orders")
    with pytest.raises(db.DBError, match="No table or view named 'order'.*Similar: orders"):
        db.find_table(schema, "order")
    summary = db.summarize(schema)
    assert (summary["tables"], summary["views"], summary["rows_estimate"], summary["foreign_keys"]) == (2, 1, 550, 1)
    assert summary["largest"][0]["table"] == "orders" and summary["heavy"] == []


def test_reads_are_row_limited_and_cells_are_cut(shop):
    result = db.run_query(shop, "SELECT * FROM orders ORDER BY id", row_limit=20)
    assert len(result["rows"]) == 20 and result["truncated"] and result["columns"][0] == "id"
    assert result["rows"][0][4] == "0xdead"  # bytes are shown as hex, never raw
    wide = db.run_query(shop, "SELECT printf('%.*c', 5000, 'x') AS big")
    assert len(wide["rows"][0][0]) < 2100 and "+3000 chars" in wide["rows"][0][0]
    text = db.result_text(result, max_chars=200)
    assert text.splitlines()[0].startswith("id | customer_id") and "more exist" in text.splitlines()[-1]
    assert db.result_text(db.run_query(shop, "SELECT * FROM orders WHERE id < 0")).startswith("(no rows)")


def test_a_change_needs_confirmation_and_reads_cannot_write(shop):
    with pytest.raises(db.NeedsConfirmation) as caught:
        db.run_query(shop, "DELETE FROM orders WHERE id = 1;")
    assert caught.value.statement == "DELETE FROM orders WHERE id = 1" and _count(shop) == 500
    # Classified as a read, but it writes: the read-only session refuses it.
    with pytest.raises(db.DBError):
        db.run_query(shop, "PRAGMA user_version = 7")
    done = db.run_query(shop, "DELETE FROM orders WHERE id <= 10", confirmed=True)
    assert done["kind"] == "write" and done["rowcount"] == 10 and _count(shop) == 490
    assert db.result_text(done) == "Done (10 rows affected)."


def test_a_read_only_connection_never_writes(shop):
    locked = db.save_connection({**shop, "read_only": True})
    with pytest.raises(db.DBError, match="marked read-only"):
        db.run_query(locked, "DELETE FROM orders", confirmed=True)
    assert _count(locked) == 500


def test_a_runaway_query_is_stopped(shop):
    endless = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    with pytest.raises(db.DBError, match="Stopped after 1s"):
        db.run_query(shop, endless, timeout=1)


# --- export -------------------------------------------------------------------------------------------

def _set_limits(**values):
    (home_dir() / "config.json").write_text(json.dumps({"db": values}))


def test_export_table_query_schema_and_database(shop, isolated):
    schema = db.read_schema(shop)
    out = db.export_table(shop, schema, "orders", isolated / "orders.csv")
    lines = (isolated / "orders.csv").read_text().splitlines()
    assert out["rows"] == 500 and not out["truncated"] and lines[0] == "id,customer_id,total,status,blobby"
    assert lines[1] == "1,2,1.5,new,0xdead"
    out = db.export_query(shop, "SELECT id, name FROM customers WHERE id <= 2", isolated / "c.jsonl", "json")
    assert [json.loads(x) for x in (isolated / "c.jsonl").read_text().splitlines()] == [
        {"id": 1, "name": "User 1"}, {"id": 2, "name": "User 2"}]
    with pytest.raises(db.DBError, match="Only a read"):
        db.export_query(shop, "DELETE FROM orders", isolated / "x.csv")
    db.export_schema(schema, isolated / "schema.md")
    assert "| customer_id | INTEGER | no |  | → customers.id |" in (isolated / "schema.md").read_text()
    import zipfile

    packed = db.export_database(shop, schema, isolated / "all.zip")
    assert sorted(zipfile.ZipFile(isolated / "all.zip").namelist()) == ["customers.csv", "orders.csv", "schema.md"]
    assert packed["rows"] == 550 and not packed["truncated"]


def test_heavy_exports_are_refused_or_cut_at_the_limits(shop, isolated):
    _set_limits(export_max_rows=100, export_max_mb=1)
    schema = db.read_schema(shop)
    assert db.summarize(schema)["heavy"] == ["orders"]
    with pytest.raises(db.TooHeavy, match="orders is heavy: about 500 rows"):
        db.export_table(shop, schema, "orders", isolated / "o.csv")
    assert not (isolated / "o.csv").exists()
    forced = db.export_table(shop, schema, "orders", isolated / "o.csv", force=True)
    assert forced["rows"] == 100 and forced["truncated"] and "100-row export limit" in forced["stopped"]
    assert len((isolated / "o.csv").read_text().splitlines()) == 101
    # A free-form query has no estimate, so it is cut the same way.
    assert db.export_query(shop, "SELECT * FROM orders", isolated / "q.csv")["truncated"]
    with pytest.raises(db.TooHeavy, match="database is heavy.*Heavy tables: orders"):
        db.export_database(shop, schema, isolated / "all.zip")
    packed = db.export_database(shop, schema, isolated / "all.zip", force=True)
    assert packed["truncated"] and "orders (cut at" in packed["stopped"] and packed["rows"] == 150


# --- the model's tools ------------------------------------------------------------------------------------

def _agent(router, host=None):
    config = make_config()
    return Agent(config, FakeLLM(router, config), host or NullHost())


def _tool_output(agent, index=0):
    return [m["content"] for m in agent.messages if m["role"] == "tool"][index]


def test_db_tools_exist_only_when_a_connection_is_saved(isolated):
    agent = _agent(scripted())
    assert "db_query" not in agent.tool_names() and "Databases (" not in agent.system_prompt()
    db.save_connection({"name": "shop", "engine": "sqlite", "database": str(isolated / "x.db")})
    agent.refresh_system_message()
    assert {"db_schema", "db_query"} <= set(agent.tool_names())
    assert "Databases (shop [sqlite])" in agent.messages[0]["content"]
    off = Agent(make_config(db={"enabled": False}), FakeLLM(scripted()), NullHost())
    assert "db_query" not in off.tool_names()


def test_model_reads_schema_and_rows(shop):
    agent = _agent(scripted(
        AssistantTurn(tool_calls=[call("db_schema", "{}", "c1")]),  # one connection: no name needed
        AssistantTurn(tool_calls=[call("db_query", json.dumps(
            {"connection": "shop", "sql": "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY status"}), "c2")]),
        AssistantTurn(content="334 new, 166 paid."),
    ))
    agent.run_turn("how many orders per status?")
    assert "orders · 5 cols, ~500 rows" in _tool_output(agent, 0)
    assert _tool_output(agent, 1) == "status | n\nnew | 334\npaid | 166\n(2 rows)"
    bad = agent._dispatch("db_query", {"connection": "shop", "sql": "SELECT nope FROM orders"})
    assert bad.startswith("Database error:") and "nope" in bad


@pytest.mark.parametrize("mode", ["auto", "accept_edits", "manual"])
def test_a_database_change_always_asks_first(shop, mode):
    delete = call("db_query", json.dumps({"connection": "shop", "sql": "DELETE FROM orders WHERE id <= 5"}))
    host = ModeHost(mode, approve=False)
    agent = Agent(make_config(), FakeLLM(scripted(AssistantTurn(tool_calls=[delete]), AssistantTurn(content="ok"))), host)
    agent.run_turn("clean up old orders")
    assert host.asked == ["db_write"] and "cancelled by the user" in _tool_output(agent) and _count(shop) == 500
    host = ModeHost(mode, approve=True)
    agent = Agent(make_config(), FakeLLM(scripted(AssistantTurn(tool_calls=[delete]), AssistantTurn(content="ok"))), host)
    agent.run_turn("clean up old orders")
    assert host.asked == ["db_write"] and "Done (5 rows affected)" in _tool_output(agent) and _count(shop) == 495


def test_plan_mode_and_read_only_connections_block_changes(shop):
    host = ModeHost("plan", approve=True)
    agent = Agent(make_config(), FakeLLM(scripted()), host)
    assert "Plan mode" in agent._dispatch("db_query", {"connection": "shop", "sql": "DROP TABLE orders"})
    assert "Plan mode" in agent._dispatch("db_query", {"connection": "shop", "sql": "SELECT 1", "save_as": "x.csv"})
    assert agent._dispatch("db_query", {"connection": "shop", "sql": "SELECT count(*) AS n FROM orders"}).startswith("n\n500")
    db.save_connection({**shop, "read_only": True})
    agent = _agent(scripted(), ModeHost("auto", approve=True))
    assert "read-only in Dcode" in agent._dispatch("db_query", {"connection": "shop", "sql": "DROP TABLE orders"})
    assert host.asked == [] and _count(shop) == 500


def test_save_as_writes_inside_the_workspace_only(shop, isolated):
    agent = _agent(scripted())
    out = agent._dispatch("db_query", {"connection": "shop", "sql": "SELECT id, total FROM orders", "save_as": "out/orders.csv"})
    assert out == "Saved 500 rows (5 KB, csv) to out/orders.csv."
    assert (isolated / "out" / "orders.csv").read_text().splitlines()[1] == "1,1.5"
    outside = agent._dispatch("db_query", {"connection": "shop", "sql": "SELECT 1", "save_as": "/etc/dcode-x.csv"})
    assert "outside the workspace" in outside and not os.path.exists("/etc/dcode-x.csv")


def test_database_changes_cannot_be_allowed_for_the_session(shop):
    delete = call("db_query", json.dumps({"connection": "shop", "sql": "DELETE FROM orders WHERE id = 1"}))
    session = _session(scripted(AssistantTurn(tool_calls=[delete]), AssistantTurn(content="done")))
    try:
        session.set_mode("auto")
        session.submit("delete order 1")
        _wait(lambda: session.pending_approvals())
        pending = session.pending_approvals()[0]
        assert pending["kind"] == "db_write" and "DELETE FROM orders WHERE id = 1" in pending["detail"]
        session.resolve_approval(pending["id"], True, scope="session")
        _wait(lambda: session.idle)
        assert "db_write" not in session.allowed_kinds and _count(shop) == 499
    finally:
        session.close()
