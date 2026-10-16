"""Passwords, web sessions and API keys.

Passwords are scrypt hashes; sessions and API keys are random tokens stored only as SHA-256 hashes,
so a copy of the database gives nobody a working key. An API key is shown once, when it is made.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any

from .store import Store

KEY_PREFIX = "dcm-"
ROLES = ("admin", "user")
_N, _R, _P = 2**14, 8, 1
MIN_PASSWORD = 8


class AuthError(Exception):
    """Wrong credentials, a disabled account, or a key that no longer works."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_problem(password: str) -> str:
    """Why this password can't be used ("" when it can)."""
    if len(password or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return ""


# --- users ------------------------------------------------------------------------------
def create_user(store: Store, username: str, password: str, role: str = "user", name: str = "") -> int:
    username = (username or "").strip()
    if not username or len(username) > 64 or any(c.isspace() for c in username):
        raise ValueError("A username is one word, up to 64 characters.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    if store.user_by_name(username):
        raise ValueError(f"There is already a user called {username}.")
    return store.run("INSERT INTO users (username, name, password, role, created) VALUES (?, ?, ?, ?, ?)",
                     (username, name.strip(), hash_password(password), role, time.time()))


def set_password(store: Store, user_id: int, password: str) -> None:
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    store.run("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), user_id))
    store.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # signed out everywhere


def authenticate(store: Store, username: str, password: str) -> dict[str, Any]:
    user = store.user_by_name(username or "")
    # Hash anyway when the user doesn't exist, so timing doesn't tell which usernames are real.
    if not check_password(password or "", user["password"] if user else hash_password("x" * 12)) or user is None:
        raise AuthError("Wrong username or password.")
    if not user["enabled"]:
        raise AuthError("This account is disabled. Ask an admin.")
    return user


# --- web sessions ------------------------------------------------------------------------
def start_session(store: Store, user_id: int, hours: float) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    store.run("INSERT INTO sessions (hash, user_id, created, expires) VALUES (?, ?, ?, ?)",
              (_digest(token), user_id, now, now + hours * 3600))
    store.run("DELETE FROM sessions WHERE expires < ?", (now,))
    return token


def session_user(store: Store, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    row = store.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.hash = ? AND s.expires > ?",
                    (_digest(token), time.time()))
    return row if row and row["enabled"] else None


def end_session(store: Store, token: str) -> None:
    store.run("DELETE FROM sessions WHERE hash = ?", (_digest(token),))


# --- API keys --------------------------------------------------------------------------------
def create_key(store: Store, user_id: int, name: str = "", client: str = "") -> tuple[str, int]:
    """(the key, shown once; its id)."""
    key = KEY_PREFIX + secrets.token_urlsafe(30)
    key_id = store.run("INSERT INTO api_keys (user_id, name, prefix, hash, created, client) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, (name or "key").strip()[:80], key[:10], _digest(key), time.time(), client[:120]))
    return key, key_id


def key_user(store: Store, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(user, key row) for a working key, or AuthError."""
    if not key or not key.startswith(KEY_PREFIX):
        raise AuthError("Missing or malformed API key. Sign in with `dcode login <manager-url>` to get one.")
    row = store.one("SELECT * FROM api_keys WHERE hash = ?", (_digest(key),))
    if row is None or row["revoked"]:
        raise AuthError("This API key was revoked or never existed. Run `dcode login` again.")
    user = store.user(row["user_id"])
    if user is None or not user["enabled"]:
        raise AuthError("This account is disabled. Ask an admin.")
    return user, row


def touch_key(store: Store, key_row: dict[str, Any], client: str) -> None:
    """Remember when and from what a key was last used (at most once a minute: it's on every request)."""
    now = time.time()
    if now - (key_row.get("last_used") or 0) < 60 and (not client or client == key_row.get("client")):
        return
    store.run("UPDATE api_keys SET last_used = ?, client = CASE WHEN ? = '' THEN client ELSE ? END WHERE id = ?",
              (now, client, client[:120], key_row["id"]))
    store.run("UPDATE users SET last_seen = ? WHERE id = ?", (now, key_row["user_id"]))


def public_key(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "name", "prefix", "created", "last_used", "client")} | {
        "revoked": bool(row["revoked"])}
