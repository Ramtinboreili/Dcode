"""The manager's database: one SQLite file (WAL), one connection per thread.

Tables: settings, users, sessions (web sign-in), api_keys, models, access (a user's rights and limits
on one model), usage (one row per request: the source of every usage number and chart) and audit.
Secrets never leave through this module's ``public_*`` helpers: password hashes, key hashes and a
model's upstream key stay in the database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL DEFAULT '',
  password TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  enabled INTEGER NOT NULL DEFAULT 1,
  token_limit INTEGER,
  period TEXT,
  reset_at REAL NOT NULL DEFAULT 0,
  created REAL NOT NULL,
  last_seen REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created REAL NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT '',
  prefix TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE,
  created REAL NOT NULL,
  last_used REAL NOT NULL DEFAULT 0,
  client TEXT NOT NULL DEFAULT '',
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'chat',
  upstream_url TEXT NOT NULL,
  upstream_model TEXT NOT NULL DEFAULT '',
  upstream_key TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  context_length INTEGER,
  max_output_tokens INTEGER,
  access TEXT NOT NULL DEFAULT 'all',
  token_limit INTEGER,
  period TEXT NOT NULL DEFAULT 'month',
  guard TEXT NOT NULL DEFAULT '{}',
  deployment TEXT NOT NULL DEFAULT '{}',
  metrics_url TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  learned TEXT NOT NULL DEFAULT '{}',
  family TEXT NOT NULL DEFAULT 'auto',
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  model_id TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE ON UPDATE CASCADE,
  allowed INTEGER NOT NULL DEFAULT 1,
  token_limit INTEGER,
  period TEXT,
  PRIMARY KEY (user_id, model_id)
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  user_id INTEGER,
  model_id TEXT NOT NULL,
  key_id INTEGER,
  endpoint TEXT NOT NULL DEFAULT 'chat',
  status INTEGER NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms REAL NOT NULL DEFAULT 0,
  ttft_ms REAL,
  stream INTEGER NOT NULL DEFAULT 0,
  estimated INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  guard TEXT NOT NULL DEFAULT '',
  client TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS usage_user_ts ON usage (user_id, ts);
CREATE INDEX IF NOT EXISTS usage_model_ts ON usage (model_id, ts);
CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  user_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""

MIGRATIONS = {"models": [("family", "family TEXT NOT NULL DEFAULT 'auto'")]}

DEFAULT_SETTINGS: dict[str, Any] = {
    "name": "Dcode Manager",
    "timezone": "UTC",                 # when days, weeks and months start (usage resets)
    "default_user_limit": None,        # tokens per user across all models (None = unlimited)
    "default_user_period": "month",
    "global_reset_at": 0,              # an admin reset everyone's usage at this time
    "discovery_namespaces": ["vllm"],  # where to look for model servers
    "models_path": "",                 # a folder of model weights to list (mounted into the pod)
    "kubernetes_api": "",              # "" = in-cluster when running in a pod; else e.g. http://127.0.0.1:8001
    "session_hours": 12,
}

USER_FIELDS = ("id", "username", "name", "role", "enabled", "token_limit", "period", "reset_at", "created", "last_seen")
MODEL_SECRET_FIELDS = ("upstream_key",)
JSON_MODEL_FIELDS = ("guard", "deployment", "learned")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write = threading.Lock()  # one writer at a time; readers never wait (WAL)
        with self._write:
            self.conn().executescript(SCHEMA)  # executescript commits by itself: no BEGIN around it
            self._migrate()

    def _migrate(self) -> None:
        """Columns added after a database was made (CREATE TABLE IF NOT EXISTS leaves old tables as they were)."""
        db = self.conn()
        have = {row["name"] for row in db.execute("PRAGMA table_info(models)")}
        for column, ddl in MIGRATIONS["models"]:
            if column not in have:
                db.execute(f"ALTER TABLE models ADD COLUMN {ddl}")

    # --- connections ------------------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                db.execute("PRAGMA journal_mode = WAL")
                db.execute("PRAGMA synchronous = NORMAL")
            self._local.db = db
        return db

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        db = self.conn()
        with self._write:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def all(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn().execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
        row = self.conn().execute(sql, args).fetchone()
        return dict(row) if row else None

    def run(self, sql: str, args: tuple | list = ()) -> int:
        with self.tx() as db:
            cursor = db.execute(sql, args)
            return cursor.lastrowid or cursor.rowcount

    # --- settings ---------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        values = dict(DEFAULT_SETTINGS)
        for row in self.all("SELECT key, value FROM settings"):
            try:
                values[row["key"]] = json.loads(row["value"])
            except ValueError:
                pass
        return values

    def set_settings(self, changes: dict[str, Any]) -> None:
        with self.tx() as db:
            for key, value in changes.items():
                if key in DEFAULT_SETTINGS:
                    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                               "value = excluded.value", (key, json.dumps(value)))

    # --- users --------------------------------------------------------------------------
    def user(self, user_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def user_by_name(self, username: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE username = ?", (username.strip(),))

    def users(self) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM users ORDER BY username")

    # --- models -------------------------------------------------------------------------
    def model(self, model_id: str) -> dict[str, Any] | None:
        return _decode_model(self.one("SELECT * FROM models WHERE id = ?", (model_id,)))

    def models(self) -> list[dict[str, Any]]:
        return [_decode_model(row) for row in self.all("SELECT * FROM models ORDER BY id")]

    def access_rows(self, user_id: int | None = None, model_id: str | None = None) -> list[dict[str, Any]]:
        if user_id is not None:
            return self.all("SELECT * FROM access WHERE user_id = ?", (user_id,))
        if model_id is not None:
            return self.all("SELECT * FROM access WHERE model_id = ?", (model_id,))
        return self.all("SELECT * FROM access")

    def audit(self, user_id: int | None, action: str, detail: str = "") -> None:
        self.run("INSERT INTO audit (ts, user_id, action, detail) VALUES (?, ?, ?, ?)",
                 (time.time(), user_id, action, detail[:500]))


def _decode_model(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for key in JSON_MODEL_FIELDS:
        try:
            row[key] = json.loads(row.get(key) or "{}")
        except ValueError:
            row[key] = {}
    row["enabled"] = bool(row["enabled"])
    return row


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row.get(key) for key in USER_FIELDS}
    out["enabled"] = bool(out["enabled"])
    return out


def public_model(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in row.items() if key not in MODEL_SECRET_FIELDS}
    out["has_upstream_key"] = bool(row.get("upstream_key"))
    return out
