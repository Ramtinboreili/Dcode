"""Database connections: schema, guarded queries, summaries and exports.

A connection is saved once (``~/.dcode/db/connections.json``, 0600) and then used from
the terminal (``/db``), the web UI (Databases) and by the model (``db_schema`` /
``db_query``). What the connection may do is decided by the database itself: Dcode
logs in as the user you gave it and gets exactly that user's rights.

Guards, because a database can be far bigger than a context window or a disk:

* reads run in a read-only session, one statement at a time, with a statement timeout
* results are streamed and cut at a row limit; cells are truncated
* anything that isn't a read is a *change*: the caller must confirm it first
* exports refuse tables estimated above the size limits and stop at them otherwise

Drivers are pure Python (pg8000, PyMySQL) and imported only when a connection of that
kind is used, so neither the terminal nor the hub pays for them otherwise.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import time
import urllib.parse
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import home_dir, load_raw_config, write_private_json

ENGINES = {"postgres": 5432, "mysql": 3306, "sqlite": 0}
ENGINE_LABELS = {"postgres": "PostgreSQL", "mysql": "MySQL / MariaDB", "sqlite": "SQLite"}
_ENGINE_ALIASES = {"postgresql": "postgres", "pg": "postgres", "pgsql": "postgres", "mariadb": "mysql",
                   "sqlite3": "sqlite"}
_DRIVER_MODULES = {"postgres": "pg8000", "mysql": "pymysql", "sqlite": "sqlite3"}
SSL_MODES = ("auto", "require", "verify", "off")

DEFAULT_LIMITS = {
    "row_limit": 200,             # rows returned by one query
    "timeout": 30,                # seconds per statement
    "export_max_rows": 1_000_000,
    "export_max_mb": 200,
}
EXPORT_TIMEOUT = 900              # seconds for one export statement
MAX_CELL_CHARS = 2000             # one value, as shown in a result grid
MAX_RESULT_CHARS = 2_000_000      # one result, all cells together
MAX_SCHEMA_TABLES = 2000
FETCH_BATCH = 2000
_READ_WORDS = {"select", "with", "show", "explain", "describe", "desc", "values", "table", "pragma"}
_CURSOR_WORDS = {"select", "with", "values", "table"}  # PostgreSQL can stream these through a cursor


class DBError(Exception):
    """A problem worth showing to the user (or the model) as it is."""


class NeedsConfirmation(DBError):
    """The statement changes something; the caller must confirm it and call again."""

    def __init__(self, statement: str) -> None:
        super().__init__("This statement changes the database and needs your confirmation.")
        self.statement = statement


class TooHeavy(DBError):
    """An export that the size guard refused."""


# --- saved connections --------------------------------------------------------------------

def db_dir() -> Path:
    return home_dir() / "db"


def exports_dir() -> Path:
    path = db_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _connections_file() -> Path:
    return db_dir() / "connections.json"


def clean_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-")[:40]


def load_connections() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_connections_file().read_text())
    except (OSError, ValueError):
        return {}
    items = data.get("connections") if isinstance(data, dict) else None
    return {c["name"]: c for c in items or [] if isinstance(c, dict) and c.get("name")}


def get_connection(name: str) -> dict[str, Any]:
    connections = load_connections()
    conn = connections.get(clean_name(name))
    if conn is None:
        known = ", ".join(connections) or "none saved yet"
        raise DBError(f"No database connection named '{name}'. Saved connections: {known}.")
    return conn


def _store(connections: dict[str, dict[str, Any]]) -> None:
    write_private_json(_connections_file(), {"connections": list(connections.values())})


def parse_url(url: str) -> dict[str, Any]:
    """``postgres://user:pass@host:5432/db`` · ``mysql://…`` · ``sqlite:///path/to/file.db``"""
    parts = urllib.parse.urlsplit(url.strip())
    engine = _ENGINE_ALIASES.get(parts.scheme.lower(), parts.scheme.lower())
    if engine not in ENGINES:
        raise DBError("Use a URL like postgres://user:password@host:5432/dbname (or mysql://…, sqlite:///file.db).")
    if engine == "sqlite":
        # sqlite:///relative.db · sqlite:////absolute/path.db
        return {"engine": engine, "database": urllib.parse.unquote((parts.netloc + parts.path)[1:] or parts.netloc)}
    return {
        "engine": engine, "host": parts.hostname or "localhost", "port": parts.port or ENGINES[engine],
        "database": urllib.parse.unquote(parts.path.lstrip("/")),
        "user": urllib.parse.unquote(parts.username or ""), "password": urllib.parse.unquote(parts.password or ""),
    }


def build_connection(data: dict[str, Any]) -> dict[str, Any]:
    """Validate connection settings (from a form or a URL). An empty or masked password keeps the saved one."""
    name = clean_name(data.get("name") or "")
    if not name:
        raise DBError("Give the connection a short name (letters, digits, - and _).")
    engine = str(data.get("engine") or "").strip().lower()
    engine = _ENGINE_ALIASES.get(engine, engine)
    if engine not in ENGINES:
        raise DBError(f"Unknown database kind '{engine}'. Choose from: {', '.join(ENGINES)}.")
    previous = load_connections().get(name, {})
    password = str(data.get("password") or "")
    if not password or "•" in password:
        password = str(previous.get("password") or "")
    try:
        port = int(data.get("port") or ENGINES[engine])
    except (TypeError, ValueError):
        raise DBError("The port must be a number.") from None
    ssl_mode = str(data.get("ssl") or "auto").lower()
    conn = {
        "name": name, "engine": engine,
        "host": str(data.get("host") or "localhost").strip(), "port": port,
        "database": str(data.get("database") or "").strip(),
        "user": str(data.get("user") or "").strip(),
        "password": password, "password_env": str(data.get("password_env") or "").strip(),
        "ssl": ssl_mode if ssl_mode in SSL_MODES else "auto",
        "ssl_ca": str(data.get("ssl_ca") or "").strip(),
        "read_only": bool(data.get("read_only")),
        "note": " ".join(str(data.get("note") or "").split())[:200],
    }
    if engine == "sqlite":
        if not conn["database"]:
            raise DBError("A SQLite connection needs the path of the database file.")
        conn["database"] = str(Path(conn["database"]).expanduser())
    elif not conn["user"]:
        raise DBError("A user name is required: Dcode gets exactly the rights of that database user.")
    return conn


def save_connection(data: dict[str, Any]) -> dict[str, Any]:
    """Create or update a saved connection."""
    conn = build_connection(data)
    connections = load_connections()
    connections[conn["name"]] = conn
    _store(connections)
    return conn


def delete_connection(name: str) -> bool:
    connections = load_connections()
    if connections.pop(clean_name(name), None) is None:
        return False
    _store(connections)
    return True


def public(conn: dict[str, Any]) -> dict[str, Any]:
    """A connection as the UI and the model may see it: never the password."""
    data = {k: v for k, v in conn.items() if k != "password"}
    data["has_password"] = bool(conn.get("password") or conn.get("password_env"))
    data["target"] = describe(conn)
    return data


def describe(conn: dict[str, Any]) -> str:
    if conn["engine"] == "sqlite":
        return f"sqlite · {conn['database']}"
    database = conn.get("database") or "(all databases)"
    return f"{conn['engine']} · {conn.get('user')}@{conn.get('host')}:{conn.get('port')}/{database}"


def drivers() -> dict[str, bool]:
    """Which database kinds this Dcode build can talk to (no driver is imported)."""
    from importlib.util import find_spec

    return {engine: find_spec(module) is not None for engine, module in _DRIVER_MODULES.items()}


def limits() -> dict[str, int]:
    """The guards, with the user's overrides from the ``db`` section of the config."""
    merged = dict(DEFAULT_LIMITS)
    section = load_raw_config().get("db")
    for key in merged:
        try:
            merged[key] = max(1, int((section or {}).get(key, merged[key])))
        except (TypeError, ValueError, AttributeError):
            pass
    return merged


# --- SQL text ---------------------------------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split on ``;`` outside strings, quoted names, dollar quotes and comments (comments are dropped)."""
    statements, buf, i, n = [], [], 0, len(sql)
    while i < n:
        ch, pair = sql[i], sql[i:i + 2]
        if pair == "--":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            continue
        if pair == "/*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            buf.append(" ")
            continue
        if ch in "'\"`":
            end = i + 1
            while end < n:
                if sql[end] == "\\" and ch != "`":
                    end += 2
                    continue
                if sql[end] == ch:
                    if sql[end:end + 2] == ch * 2:  # doubled quote = an escaped quote
                        end += 2
                        continue
                    break
                end += 1
            buf.append(sql[i:end + 1])
            i = end + 1
            continue
        if ch == "$":
            tag = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if tag:
                end = sql.find(tag.group(0), i + len(tag.group(0)))
                end = n if end < 0 else end + len(tag.group(0))
                buf.append(sql[i:end])
                i = end
                continue
        if ch == ";":
            statements.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    statements.append("".join(buf).strip())
    return [s for s in statements if s]


def single_statement(sql: str) -> str:
    statements = split_statements(sql or "")
    if not statements:
        raise DBError("The SQL is empty.")
    if len(statements) > 1:
        raise DBError(f"Send one statement at a time (this has {len(statements)}).")
    return statements[0]


def first_word(statement: str) -> str:
    match = re.match(r"[\s(]*([A-Za-z]+)", statement)
    return match.group(1).lower() if match else ""


def classify(statement: str) -> str:
    """'read' or 'write'. Anything not clearly a read counts as a change.

    A wrong 'read' is still safe: reads run in a read-only session, so the database
    itself refuses e.g. ``WITH … DELETE`` or ``SELECT … INTO``.
    """
    return "read" if first_word(statement) in _READ_WORDS else "write"


def quote_name(engine: str, *parts: str) -> str:
    mark = "`" if engine == "mysql" else '"'
    return ".".join(mark + p.replace(mark, mark * 2) + mark for p in parts if p)


# --- one open connection ------------------------------------------------------------------------

def _driver(engine: str) -> Any:
    try:
        if engine == "postgres":
            import pg8000.dbapi as module
        elif engine == "mysql":
            import pymysql as module
        else:
            import sqlite3 as module
    except ImportError:
        raise DBError(f"This Dcode build has no {ENGINE_LABELS[engine]} driver "
                      f"(Python package {_DRIVER_MODULES[engine]}).") from None
    return module


def _password(conn: dict[str, Any]) -> str:
    env = conn.get("password_env")
    if env:
        value = os.environ.get(env)
        if value is None:
            raise DBError(f"The environment variable {env} (the password for '{conn['name']}') is not set.")
        return value
    return str(conn.get("password") or "")


def _ssl_contexts(conn: dict[str, Any]) -> list[Any]:
    """TLS settings to try, in order. ``None`` = no TLS."""
    import ssl

    mode = conn.get("ssl") or "auto"
    if mode == "off":
        return [None]
    if mode == "verify":
        return [ssl.create_default_context(cafile=conn.get("ssl_ca") or None)]
    loose = ssl.create_default_context()
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE  # encrypts, doesn't verify: internal servers use private CAs
    return [loose] if mode == "require" else [loose, None]


def error_text(exc: BaseException) -> str:
    """One readable line from whatever a driver raised."""
    if isinstance(exc, DBError):
        return str(exc)
    arg = exc.args[0] if exc.args else ""
    if isinstance(arg, dict):  # pg8000: {'S': 'ERROR', 'C': '42P01', 'M': 'relation … does not exist', …}
        text = str(arg.get("M") or arg)
        if arg.get("C") == "57014":
            text = "the statement timed out and was cancelled"
        return text + (f" ({arg['H']})" if arg.get("H") else "")
    if len(exc.args) >= 2 and isinstance(arg, int):  # PyMySQL: (1146, "Table … doesn't exist")
        return f"{exc.args[1]} (error {arg})"
    text = " ".join(str(exc).split())
    if isinstance(exc, TimeoutError) or "timed out" in text.lower():
        return "timed out waiting for the database"
    return text or exc.__class__.__name__


class Link:
    """One open connection. Engine differences live here; everything above is engine-neutral."""

    def __init__(self, conn: dict[str, Any], timeout: float = DEFAULT_LIMITS["timeout"], write: bool = False) -> None:
        self.conn = conn
        self.engine = conn["engine"]
        self.timeout = max(1.0, float(timeout))
        self.write = write
        self.tls = False
        self._deadline = 0.0
        self._cursor: Any = None  # the streaming cursor, if a read is in progress
        self.module = _driver(self.engine)
        try:
            self.handle = getattr(self, f"_open_{self.engine}")()
        except DBError:
            raise
        except Exception as exc:
            raise DBError(f"Could not connect to {describe(conn)}: {error_text(exc)}") from exc

    # --- opening ---
    def _open_postgres(self) -> Any:
        conn, last = self.conn, None
        for context in _ssl_contexts(conn):
            try:
                handle = self.module.connect(
                    user=conn["user"], host=conn["host"], port=int(conn["port"]), database=conn["database"] or None,
                    password=_password(conn), ssl_context=context, timeout=self.timeout + 15,
                    application_name="dcode")
            except Exception as exc:
                last = exc
                if context is not None and "ssl" in str(exc).lower():
                    continue  # the server has no TLS: 'auto' falls back to a plain connection
                raise
            self.tls = context is not None
            handle.autocommit = True
            cursor = handle.cursor()
            cursor.execute(f"SET statement_timeout = {int(self.timeout * 1000)}")
            if not self.write:
                cursor.execute("SET default_transaction_read_only = on")
            return handle
        raise last  # type: ignore[misc]

    def _open_mysql(self) -> Any:
        conn, last = self.conn, None
        for context in _ssl_contexts(conn):
            try:
                handle = self.module.connect(
                    user=conn["user"], password=_password(conn), host=conn["host"], port=int(conn["port"]),
                    database=conn["database"] or None, charset="utf8mb4", connect_timeout=15,
                    read_timeout=self.timeout + 15, write_timeout=self.timeout + 15, autocommit=True,
                    ssl=context, ssl_disabled=context is None)
            except Exception as exc:
                last = exc
                if context is not None and "ssl" in str(exc).lower():
                    continue
                raise
            self.tls = context is not None
            cursor = handle.cursor()
            for statement in (f"SET SESSION max_execution_time = {int(self.timeout * 1000)}",   # MySQL
                              f"SET SESSION max_statement_time = {int(self.timeout)}"):         # MariaDB
                try:
                    cursor.execute(statement)
                except Exception:
                    pass  # each server knows only one of the two
            if not self.write:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            return handle
        raise last  # type: ignore[misc]

    def _open_sqlite(self) -> Any:
        path = Path(self.conn["database"]).expanduser()
        if not path.is_file():
            raise DBError(f"No SQLite file at {path}.")
        mode = "rw" if self.write else "ro"
        handle = self.module.connect(f"file:{urllib.parse.quote(str(path))}?mode={mode}", uri=True, timeout=10)
        if not self.write:
            handle.execute("PRAGMA query_only = ON")
        handle.set_progress_handler(lambda: 1 if time.monotonic() > self._deadline > 0 else 0, 20_000)
        return handle

    def close(self) -> None:
        # A half-read MySQL result would be drained (slowly, or noisily after the socket is gone)
        # when it is garbage-collected; we are closing the connection, so just drop it.
        result = getattr(self._cursor, "_result", None)
        if result is not None and hasattr(result, "unbuffered_active"):
            result.unbuffered_active = False
        try:
            self.handle.close()
        except Exception:
            pass

    def __enter__(self) -> Link:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- running ---
    def all(self, sql: str, args: tuple = ()) -> list[tuple]:
        """A small catalog query, fully fetched."""
        self._deadline = time.monotonic() + self.timeout
        cursor = self.handle.cursor()
        cursor.execute(sql, args) if args else cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]

    def stream(self, statement: str, batch: int = FETCH_BATCH) -> tuple[list[str], Iterator[list[tuple]]]:
        """(column names, batches of rows). Rows are pulled from the server as they are consumed."""
        self._deadline = time.monotonic() + self.timeout
        if self.engine == "postgres" and first_word(statement) in _CURSOR_WORDS:
            return self._stream_postgres(statement, batch)
        cursor = self.handle.cursor(self.module.cursors.SSCursor) if self.engine == "mysql" else self.handle.cursor()
        self._cursor = cursor
        cursor.execute(statement)
        columns = [str(d[0]) for d in cursor.description or []]

        def batches() -> Iterator[list[tuple]]:
            while cursor.description:
                rows = cursor.fetchmany(batch)
                if not rows:
                    return
                yield [tuple(r) for r in rows]

        return columns, batches()

    def _stream_postgres(self, statement: str, batch: int) -> tuple[list[str], Iterator[list[tuple]]]:
        # pg8000 loads a whole result into memory, so big reads go through a server-side cursor.
        cursor = self.handle.cursor()
        cursor.execute("BEGIN")
        cursor.execute(f"DECLARE dcode_cur NO SCROLL CURSOR FOR {statement}")
        cursor.execute(f"FETCH {int(batch)} FROM dcode_cur")
        columns = [str(d[0]) for d in cursor.description or []]
        first = [tuple(r) for r in cursor.fetchall()]

        def batches() -> Iterator[list[tuple]]:
            rows = first
            while rows:
                yield rows
                if len(rows) < batch:
                    return
                cursor.execute(f"FETCH {int(batch)} FROM dcode_cur")
                rows = [tuple(r) for r in cursor.fetchall()]

        return columns, batches()

    def execute(self, statement: str) -> int:
        """Run a change and commit it. Returns the number of affected rows (-1 if unknown)."""
        self._deadline = time.monotonic() + self.timeout
        cursor = self.handle.cursor()
        cursor.execute(statement)
        if self.engine == "sqlite":
            self.handle.commit()
        count = getattr(cursor, "rowcount", -1)
        return int(count) if isinstance(count, int) else -1


def _cell(value: Any, limit: int = MAX_CELL_CHARS) -> Any:
    """A database value as JSON-friendly data, cut to a sane size."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return "0x" + raw[:32].hex() + (f"… ({len(raw)} bytes)" if len(raw) > 32 else "")
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, default=str, ensure_ascii=False)
    text = str(value)
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


def run_query(conn: dict[str, Any], sql: str, *, row_limit: int | None = None, timeout: float | None = None,
              confirmed: bool = False) -> dict[str, Any]:
    """Run one statement. Reads are read-only and limited; changes need ``confirmed=True``."""
    guards = limits()
    statement = single_statement(sql)
    timeout = float(timeout or guards["timeout"])
    started = time.monotonic()
    if classify(statement) == "write":
        if conn.get("read_only"):
            raise DBError(f"The connection '{conn['name']}' is marked read-only in Dcode, so this change was not run.")
        if not confirmed:
            raise NeedsConfirmation(statement)
        try:
            with Link(conn, timeout=max(timeout, 300), write=True) as link:
                count = link.execute(statement)
        except DBError:
            raise
        except Exception as exc:
            raise DBError(error_text(exc)) from exc
        return {"kind": "write", "rowcount": count, "elapsed": round(time.monotonic() - started, 3),
                "statement": statement}
    limit = max(1, int(row_limit or guards["row_limit"]))
    rows: list[list[Any]] = []
    truncated, chars = False, 0
    try:
        with Link(conn, timeout=timeout) as link:
            columns, batches = link.stream(statement, batch=min(limit + 1, FETCH_BATCH))
            for batch in batches:
                for row in batch:
                    if len(rows) >= limit or chars > MAX_RESULT_CHARS:
                        truncated = True
                        break
                    cells = [_cell(v) for v in row]
                    chars += sum(len(c) for c in cells if isinstance(c, str)) + 8 * len(cells)
                    rows.append(cells)
                if truncated:
                    break
    except DBError:
        raise
    except Exception as exc:
        raise DBError(_read_error(exc, timeout)) from exc
    return {"kind": "read", "columns": columns, "rows": rows, "truncated": truncated, "limit": limit,
            "elapsed": round(time.monotonic() - started, 3)}


def _read_error(exc: BaseException, timeout: float) -> str:
    text = error_text(exc)
    low = text.lower()
    if "timed out" in low or "interrupted" in low or "max_statement_time" in low or "maximum statement execution" in low:
        return (f"Stopped after {int(timeout)}s (the query time limit). Narrow it with WHERE or LIMIT, "
                "or aggregate in SQL.")
    if "read-only" in low or "read only" in low or "readonly" in low:
        return "That statement would change the database, and reads run read-only. " + text
    return text


def test_connection(conn: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    with Link(conn, timeout=15) as link:
        try:
            version = str(link.all("SELECT sqlite_version()" if link.engine == "sqlite" else "SELECT version()")[0][0])
        except Exception as exc:
            raise DBError(error_text(exc)) from exc
        return {"ok": True, "version": version.split(" on ")[0][:80], "tls": link.tls,
                "ms": int((time.monotonic() - started) * 1000)}


# --- schema ---------------------------------------------------------------------------------------

_PG_SKIP = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'"
_MYSQL_SYSTEM = ("information_schema", "mysql", "performance_schema", "sys")


def _estimate(value: Any) -> int | None:
    """A catalog estimate as an int; None when the server doesn't know (never analyzed, a view, …)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


_PG_INDEX = re.compile(r"^CREATE (UNIQUE )?INDEX (\S+) ON \S+ USING (\w+) ")


def _short_index(match: re.Match) -> str:
    """'CREATE INDEX i ON public.t USING btree (a)' -> 'i (a)'; other index methods stay visible."""
    method = "" if match.group(3) == "btree" else match.group(3) + " "
    return f"{match.group(1) or ''}{match.group(2)} {method}"


def _table(schema: str, name: str, kind: str, rows: Any = None, size: Any = None, comment: Any = "") -> dict[str, Any]:
    return {"schema": schema, "name": name, "type": kind, "rows": _estimate(rows), "bytes": _estimate(size),
            "comment": str(comment or ""), "columns": [], "keys": [], "indexes": []}


def _schema_postgres(link: Link) -> tuple[list[dict[str, Any]], str]:
    kinds = {"r": "table", "p": "table", "v": "view", "m": "view", "f": "table"}
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    # Only what this database user may actually read: the database's own permissions decide.
    for schema, name, kind, rows, size, comment in link.all(
            "SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint, "
            "CASE WHEN c.relkind IN ('r','m') THEN pg_total_relation_size(c.oid) END, obj_description(c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relkind IN ('r','p','v','m','f') AND {_PG_SKIP} "
            "AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT') "
            f"ORDER BY 1, 2 LIMIT {MAX_SCHEMA_TABLES + 1}"):
        tables[(schema, name)] = _table(schema, name, kinds.get(kind, "table"), rows, size, comment)
    for schema, name, column, kind, nullable, default in link.all(
            "SELECT table_schema, table_name, column_name, "
            "CASE WHEN data_type IN ('USER-DEFINED', 'ARRAY') THEN udt_name ELSE data_type END, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY table_schema, table_name, ordinal_position"):
        if (schema, name) in tables:
            tables[(schema, name)]["columns"].append(
                {"name": column, "type": kind, "nullable": nullable == "YES", "default": default, "pk": False})
    for schema, name, kind, definition in link.all(
            "SELECT n.nspname, c.relname, con.contype, pg_get_constraintdef(con.oid) "
            "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE con.contype IN ('p', 'f', 'u') AND {_PG_SKIP} ORDER BY con.contype DESC"):
        table = tables.get((schema, name))
        if table is None:
            continue
        table["keys"].append(definition)
        if kind == "p":
            _mark_pk(table, re.findall(r'"([^"]+)"|(\w+)', definition.split("(", 1)[-1].rsplit(")", 1)[0]))
    for schema, name, definition in link.all(
            "SELECT schemaname, tablename, indexdef FROM pg_indexes "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2, 3"):
        if (schema, name) in tables:
            tables[(schema, name)]["indexes"].append(_PG_INDEX.sub(_short_index, definition))
    return list(tables.values()), "public"


def _mark_pk(table: dict[str, Any], names: list[Any]) -> None:
    wanted = {(n[0] or n[1]) if isinstance(n, tuple) else n for n in names}
    for column in table["columns"]:
        if column["name"] in wanted:
            column["pk"] = True


def _schema_mysql(link: Link) -> tuple[list[dict[str, Any]], str]:
    database = link.conn.get("database") or ""
    scope = "= %s" if database else "NOT IN (%s, %s, %s, %s)"
    args = (database,) if database else _MYSQL_SYSTEM
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    for schema, name, kind, rows, size, comment in link.all(
            "SELECT table_schema, table_name, table_type, table_rows, data_length + index_length, table_comment "
            f"FROM information_schema.tables WHERE table_schema {scope} ORDER BY 1, 2 LIMIT {MAX_SCHEMA_TABLES + 1}", args):
        is_view = "VIEW" in str(kind).upper()
        tables[(schema, name)] = _table(schema, name, "view" if is_view else "table", None if is_view else rows,
                                        None if is_view else size, "" if comment == "VIEW" else comment)
    for schema, name, column, kind, nullable, default, key, extra in link.all(
            "SELECT table_schema, table_name, column_name, column_type, is_nullable, column_default, column_key, extra "
            f"FROM information_schema.columns WHERE table_schema {scope} ORDER BY table_schema, table_name, ordinal_position",
            args):
        if (schema, name) in tables:
            tables[(schema, name)]["columns"].append(
                {"name": column, "type": str(kind) + (f" {extra}" if extra else ""), "nullable": nullable == "YES",
                 "default": default, "pk": key == "PRI"})
    for table in tables.values():
        pk = [c["name"] for c in table["columns"] if c["pk"]]
        if pk:
            table["keys"].append(f"PRIMARY KEY ({', '.join(pk)})")
    foreign: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    for schema, name, constraint, column, ref_schema, ref_table, ref_column in link.all(
            "SELECT table_schema, table_name, constraint_name, column_name, referenced_table_schema, "
            "referenced_table_name, referenced_column_name FROM information_schema.key_column_usage "
            f"WHERE referenced_table_name IS NOT NULL AND table_schema {scope} ORDER BY 1, 2, 3, ordinal_position", args):
        target = ref_table if ref_schema == schema else f"{ref_schema}.{ref_table}"
        foreign.setdefault((schema, name, constraint), []).append((column, target, ref_column))
    for (schema, name, _), parts in foreign.items():
        if (schema, name) in tables:
            tables[(schema, name)]["keys"].append(
                f"FOREIGN KEY ({', '.join(p[0] for p in parts)}) REFERENCES {parts[0][1]}({', '.join(p[2] for p in parts)})")
    indexes: dict[tuple[str, str, str], list[Any]] = {}
    for schema, name, index, unique, column in link.all(
            "SELECT table_schema, table_name, index_name, non_unique, column_name FROM information_schema.statistics "
            f"WHERE table_schema {scope} ORDER BY 1, 2, 3, seq_in_index", args):
        indexes.setdefault((schema, name, index), [not unique]).append(column)
    for (schema, name, index), parts in indexes.items():
        if (schema, name) in tables and index != "PRIMARY":
            tables[(schema, name)]["indexes"].append(
                f"{'UNIQUE ' if parts[0] else ''}{index} ({', '.join(str(p) for p in parts[1:])})")
    return list(tables.values()), database


SQLITE_COUNT_MAX_BYTES = 256 * 1024 * 1024  # count rows only in files where that is quick


def _schema_sqlite(link: Link) -> tuple[list[dict[str, Any]], str]:
    tables = []
    countable = Path(link.conn["database"]).expanduser().stat().st_size <= SQLITE_COUNT_MAX_BYTES
    for name, kind in link.all("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
                               f"AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name LIMIT {MAX_SCHEMA_TABLES + 1}"):
        table = _table("", name, kind)
        quoted = quote_name("sqlite", name)
        pk = []
        for _, column, ctype, notnull, default, is_pk in link.all(f"PRAGMA table_info({quoted})"):
            table["columns"].append({"name": column, "type": ctype or "", "nullable": not notnull, "default": default,
                                     "pk": bool(is_pk)})
            if is_pk:
                pk.append(column)
        if pk:
            table["keys"].append(f"PRIMARY KEY ({', '.join(pk)})")
        for row in link.all(f"PRAGMA foreign_key_list({quoted})"):
            table["keys"].append(f"FOREIGN KEY ({row[3]}) REFERENCES {row[2]}({row[4]})")
        for row in link.all(f"PRAGMA index_list({quoted})"):
            columns = ", ".join(str(c[2]) for c in link.all(f"PRAGMA index_info({quote_name('sqlite', row[1])})"))
            table["indexes"].append(f"{'UNIQUE ' if row[2] else ''}{row[1]} ({columns})")
        if kind == "table" and countable:  # SQLite keeps no estimates
            table["rows"] = _estimate(link.all(f"SELECT count(*) FROM {quoted}")[0][0])
        tables.append(table)
    return tables, ""


def read_schema(conn: dict[str, Any]) -> dict[str, Any]:
    """Every table and view this database user can read, with columns, keys, indexes and size estimates."""
    try:
        with Link(conn, timeout=max(30, limits()["timeout"])) as link:
            tables, default_schema = globals()[f"_schema_{link.engine}"](link)
            if link.engine == "sqlite":
                version = "SQLite " + str(link.all("SELECT sqlite_version()")[0][0])
                size = Path(conn["database"]).expanduser().stat().st_size
            elif link.engine == "postgres":
                version = str(link.all("SELECT version()")[0][0]).split(" on ")[0]
                size = link.all("SELECT pg_database_size(current_database())")[0][0]
            else:
                version = str(link.all("SELECT version()")[0][0])
                version = ("" if "maria" in version.lower() else "MySQL ") + version
                size = sum(t["bytes"] or 0 for t in tables)
    except DBError:
        raise
    except Exception as exc:
        raise DBError(error_text(exc)) from exc
    truncated = len(tables) > MAX_SCHEMA_TABLES
    tables = tables[:MAX_SCHEMA_TABLES]
    many_schemas = len({t["schema"] for t in tables}) > 1
    for table in tables:
        qualified = bool(table["schema"]) and (many_schemas or table["schema"] != default_schema)
        table["label"] = f"{table['schema']}.{table['name']}" if qualified else table["name"]
    return {"connection": conn["name"], "engine": conn["engine"], "target": describe(conn), "version": version,
            "bytes": int(size or 0), "tables": tables, "truncated": truncated,
            "read_at": datetime.now(timezone.utc).isoformat()}


def find_table(schema: dict[str, Any], wanted: str) -> dict[str, Any]:
    key = wanted.strip().strip('"`').lower().replace('"', "").replace("`", "")
    for table in schema["tables"]:
        if key in (table["label"].lower(), f"{table['schema']}.{table['name']}".lower()):
            return table
    matches = [t for t in schema["tables"] if t["name"].lower() == key]
    if len(matches) == 1:
        return matches[0]
    near = ", ".join(t["label"] for t in schema["tables"] if key and key in t["label"].lower())[:300]
    raise DBError(f"No table or view named '{wanted}' in {schema['connection']}."
                  + (f" Similar: {near}." if near else " Call db_schema without a table to list them."))


def table_sql_name(schema: dict[str, Any], table: dict[str, Any]) -> str:
    return quote_name(schema["engine"], table["schema"], table["name"])


def human_size(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def human_count(count: int | None) -> str:
    if count is None:
        return "?"
    for limit, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= limit:
            return f"{count / limit:.1f}{unit}"
    return str(count)


def summarize(schema: dict[str, Any]) -> dict[str, Any]:
    """Facts about a database's size and shape, from the schema estimates (no table is scanned)."""
    tables = [t for t in schema["tables"] if t["type"] == "table"]
    guards = limits()
    biggest = sorted(tables, key=lambda t: (t["bytes"] or 0, t["rows"] or 0), reverse=True)[:10]
    heavy = [t["label"] for t in tables
             if (t["bytes"] or 0) > guards["export_max_mb"] * 1024 * 1024 or (t["rows"] or 0) > guards["export_max_rows"]]
    return {
        "connection": schema["connection"], "target": schema["target"], "version": schema["version"],
        "bytes": schema["bytes"], "size": human_size(schema["bytes"]),
        "tables": len(tables), "views": len(schema["tables"]) - len(tables),
        "columns": sum(len(t["columns"]) for t in schema["tables"]),
        "rows_estimate": sum(t["rows"] or 0 for t in tables),
        "foreign_keys": sum(1 for t in tables for k in t["keys"] if k.startswith("FOREIGN")),
        "without_primary_key": [t["label"] for t in tables if not any(c["pk"] for c in t["columns"])][:50],
        "largest": [{"table": t["label"], "rows": t["rows"], "bytes": t["bytes"], "size": human_size(t["bytes"])}
                    for t in biggest],
        "heavy": heavy[:50], "truncated": schema["truncated"],
    }


# --- text for the model (and the terminal) ----------------------------------------------------------

def _column_text(column: dict[str, Any], references: dict[str, str]) -> str:
    text = f"{column['name']} {column['type']}".strip()
    if column["pk"]:
        text += " pk"
    elif not column["nullable"]:
        text += " not null"
    if column["name"] in references:
        text += f" -> {references[column['name']]}"
    return text


def _references(table: dict[str, Any]) -> dict[str, str]:
    refs = {}
    for key in table["keys"]:
        match = re.match(r"FOREIGN KEY \(\"?([\w$]+)\"?\) REFERENCES ([^\s(]+)\s*\(([^)]+)\)", key)
        if match:
            refs[match.group(1)] = f"{match.group(2)}.{match.group(3)}".replace('"', "")
    return refs


def _table_line(table: dict[str, Any]) -> str:
    facts = [f"{len(table['columns'])} cols"]
    if table["rows"] is not None:
        facts.append(f"~{human_count(table['rows'])} rows")
    if table["bytes"] is not None:
        facts.append(human_size(table["bytes"]))
    return f"{table['label']}{' (view)' if table['type'] == 'view' else ''} · {', '.join(facts)}"


def table_lines(schema: dict[str, Any]) -> list[str]:
    """One line per table: name, column count, estimated rows and size."""
    return [_table_line(t) for t in schema["tables"]]


def schema_text(schema: dict[str, Any], table: str = "", max_chars: int = 6000) -> str:
    """Compact schema for a small context: every column if it fits, else table names only."""
    if table:
        t = find_table(schema, table)
        refs = _references(t)
        lines = [_table_line(t)] + [f"  {_column_text(c, refs)}" for c in t["columns"]]
        lines += [f"  {k}" for k in t["keys"] if not k.startswith("PRIMARY")]
        lines += [f"  index {i}" for i in t["indexes"][:12]]
        if t["comment"]:
            lines.append(f"  note: {t['comment'][:200]}")
        return "\n".join(lines)[:max_chars]
    head = f"{schema['target']} · {schema['version']} · {human_size(schema['bytes'])} · {len(schema['tables'])} tables/views"
    if not schema["tables"]:
        return head + "\n(no tables this user can read)"
    full = [f"{_table_line(t)}\n    " + ", ".join(_column_text(c, _references(t)) for c in t["columns"])
            for t in schema["tables"]]
    body = "\n".join(full)
    if len(body) > max_chars:
        body = "\n".join(_table_line(t) for t in schema["tables"])
        if len(body) > max_chars:
            body = body[:max_chars].rsplit("\n", 1)[0] + f"\n… (list cut; {len(schema['tables'])} tables in total)"
        body += "\n(Columns left out to save context: call db_schema with a table name.)"
    return head + "\n" + body


def result_text(result: dict[str, Any], max_chars: int = 4000, cell: int = 120) -> str:
    """A query result as compact text."""
    if result["kind"] == "write":
        count = result["rowcount"]
        return f"Done ({count} row{'s' if count != 1 else ''} affected)." if count >= 0 else "Done."
    columns, rows = result["columns"], result["rows"]
    if not rows:
        return f"(no rows) columns: {', '.join(columns)}"
    lines = [" | ".join(columns)]
    shown = 0
    for row in rows:
        line = " | ".join("NULL" if v is None else str(v).replace("\n", "\\n")[:cell] for v in row)
        if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
            break
        lines.append(line)
        shown += 1
    more = result["truncated"] or shown < len(rows)
    tail = f"({shown} row{'s' if shown != 1 else ''}"
    tail += "; more exist — use WHERE/LIMIT or aggregate in SQL, or save_as to write them to a file)" if more else ")"
    return "\n".join(lines + [tail])


# --- export -----------------------------------------------------------------------------------------

def _export_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return value


def _json_cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _export_cell(value) if isinstance(value, (bytes, bytearray, memoryview, dict, list, tuple)) else str(value)


def _byte_cap(dest: Path, max_mb: int) -> int:
    """The export size limit, lowered when the disk is nearly full (always leave 200 MB free)."""
    free = shutil.disk_usage(dest.parent).free - 200 * 1024 * 1024
    if free <= 0:
        raise TooHeavy(f"Not enough free disk space in {dest.parent} to export safely.")
    return min(max_mb * 1024 * 1024, free)


def export_query(conn: dict[str, Any], sql: str, dest: Path, fmt: str = "csv") -> dict[str, Any]:
    """Stream a read statement into a CSV or JSON-lines file, stopping at the export limits."""
    statement = single_statement(sql)
    if classify(statement) != "read":
        raise DBError("Only a read statement (SELECT …) can be exported.")
    fmt = "jsonl" if fmt in ("json", "jsonl", "ndjson") else "csv"
    guards = limits()
    dest.parent.mkdir(parents=True, exist_ok=True)
    cap = _byte_cap(dest, guards["export_max_mb"])
    rows = written = 0
    stopped = ""
    started = time.monotonic()
    try:
        with Link(conn, timeout=EXPORT_TIMEOUT) as link, dest.open("w", encoding="utf-8", newline="") as handle:
            columns, batches = link.stream(statement)
            writer = csv.writer(handle)
            if fmt == "csv":
                writer.writerow(columns)
            for batch in batches:
                if rows + len(batch) > guards["export_max_rows"]:
                    batch = batch[:guards["export_max_rows"] - rows]
                    stopped = f"the {guards['export_max_rows']:,}-row export limit"
                if fmt == "csv":
                    writer.writerows([_export_cell(v) for v in row] for row in batch)
                else:
                    handle.writelines(json.dumps({c: _json_cell(v) for c, v in zip(columns, row, strict=False)},
                                                 ensure_ascii=False) + "\n" for row in batch)
                rows += len(batch)
                written = handle.tell()
                if written > cap and not stopped:
                    stopped = f"the {human_size(cap)} export size limit"
                if stopped:
                    break
    except DBError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise DBError(_read_error(exc, EXPORT_TIMEOUT)) from exc
    return {"path": str(dest), "file": dest.name, "rows": rows, "bytes": written, "size": human_size(written),
            "format": fmt, "truncated": bool(stopped), "stopped": stopped,
            "elapsed": round(time.monotonic() - started, 2)}


def check_table_weight(table: dict[str, Any]) -> str:
    """'' if the table is fine to export whole, else why it is too heavy."""
    guards = limits()
    too_big = (table["bytes"] or 0) > guards["export_max_mb"] * 1024 * 1024
    too_long = (table["rows"] or 0) > guards["export_max_rows"]
    if not (too_big or too_long):
        return ""
    return (f"{table['label']} is heavy: about {human_count(table['rows'])} rows, {human_size(table['bytes'])}. "
            f"The export limit is {guards['export_max_rows']:,} rows / {guards['export_max_mb']} MB.")


def export_table(conn: dict[str, Any], schema: dict[str, Any], table_name: str, dest: Path, fmt: str = "csv",
                 force: bool = False) -> dict[str, Any]:
    """Export one table. A table estimated above the limits is refused unless ``force`` (then it is cut at them)."""
    table = find_table(schema, table_name)
    heavy = check_table_weight(table)
    if heavy and not force:
        raise TooHeavy(heavy + " Export a part of it with a query (WHERE / LIMIT), or confirm to export only "
                               "the first rows up to the limit.")
    result = export_query(conn, f"SELECT * FROM {table_sql_name(schema, table)}", dest, fmt)
    result["table"] = table["label"]
    return result


def export_schema(schema: dict[str, Any], dest: Path) -> dict[str, Any]:
    """The structure (no data) as a Markdown document."""
    lines = [f"# {schema['connection']} — schema", "", f"{schema['target']} · {schema['version']} · "
             f"{human_size(schema['bytes'])} · read {schema['read_at'][:19]}Z", ""]
    for table in schema["tables"]:
        refs = _references(table)
        lines += [f"## {table['label']}{' (view)' if table['type'] == 'view' else ''}", ""]
        if table["comment"]:
            lines += [table["comment"], ""]
        lines += [f"Rows ≈ {human_count(table['rows'])} · size {human_size(table['bytes'])}", "",
                  "| column | type | null | default | key |", "|---|---|---|---|---|"]
        for c in table["columns"]:
            key = "PK" if c["pk"] else (f"→ {refs[c['name']]}" if c["name"] in refs else "")
            default = "" if c["default"] is None else str(c["default"]).replace("|", "\\|")[:60]
            lines.append(f"| {c['name']} | {c['type']} | {'yes' if c['nullable'] else 'no'} | {default} | {key} |")
        lines += [""] + [f"- {k}" for k in table["keys"]] + [f"- index {i}" for i in table["indexes"]] + [""]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines), encoding="utf-8")
    size = dest.stat().st_size
    return {"path": str(dest), "file": dest.name, "rows": len(schema["tables"]), "bytes": size, "size": human_size(size),
            "format": "markdown", "truncated": schema["truncated"], "stopped": ""}


def export_database(conn: dict[str, Any], schema: dict[str, Any], dest: Path, force: bool = False) -> dict[str, Any]:
    """Every table as CSV plus the schema, in one zip. Refused when the database is above the size limit."""
    import tempfile
    import zipfile

    guards = limits()
    tables = [t for t in schema["tables"] if t["type"] == "table"]
    total = sum(t["bytes"] or 0 for t in tables)
    heavy = [t["label"] for t in tables if check_table_weight(t)]
    if (heavy or total > guards["export_max_mb"] * 1024 * 1024) and not force:
        which = f" Heavy tables: {', '.join(heavy[:8])}." if heavy else ""
        raise TooHeavy(f"This database is heavy: about {human_size(total)} in {len(tables)} tables; the export limit "
                       f"is {guards['export_max_rows']:,} rows per table and {guards['export_max_mb']} MB.{which} "
                       "Export single tables or queries instead, or confirm to export it cut at the limits.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cap = _byte_cap(dest, guards["export_max_mb"])
    files, cut, rows, used = [], [], 0, 0
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        note = Path(tmp) / "schema.md"
        export_schema(schema, note)
        archive.write(note, "schema.md")
        for table in tables:
            if used > cap:
                cut.append(table["label"] + " (skipped: size limit reached)")
                continue
            part = Path(tmp) / (re.sub(r"[^\w.-]+", "_", table["label"]) + ".csv")
            result = export_query(conn, f"SELECT * FROM {table_sql_name(schema, table)}", part)
            archive.write(part, part.name)
            part.unlink()
            used += result["bytes"]
            rows += result["rows"]
            files.append(part.name)
            if result["truncated"]:
                cut.append(f"{table['label']} (cut at {result['stopped']})")
    size = dest.stat().st_size
    return {"path": str(dest), "file": dest.name, "rows": rows, "bytes": size, "size": human_size(size),
            "format": "zip", "tables": len(files), "truncated": bool(cut), "stopped": "; ".join(cut)[:600]}


def export_name(conn_name: str, what: str, extension: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w.-]+", "_", what).strip("_")[:60] or "query"
    path = exports_dir() / f"{conn_name}-{slug}-{stamp}.{extension}"
    count = 1
    while path.exists():  # two exports in the same second must not overwrite each other
        count += 1
        path = exports_dir() / f"{conn_name}-{slug}-{stamp}-{count}.{extension}"
    return path
