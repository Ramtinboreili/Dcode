"""The manager's HTTP server: the gateway (``/v1``), the API (``/api``) and the web UI.

Two ways in: an API key (``Authorization: Bearer dcm-…``) for ``/v1`` and a user's own ``/api/me``
routes (Dcode uses it for ``/usage``), and a web sign-in (an HttpOnly, SameSite=Strict cookie) for
the UI. Changes made with the cookie also need the ``X-Dcode-Manager`` header, which a page on
another site can't send, so a link can't act on the admin's behalf.
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import __version__
from ..families import FAMILIES
from . import auth, discovery, guards, limits, monitoring
from .gateway import Call, Gateway, GatewayError, family_of
from .store import Store, public_model, public_user

WEB = Path(__file__).parent / "web"
COOKIE = "dcm_session"
MAX_BODY = 32 * 1024 * 1024
LOGIN_FAILURES = 8          # per username+address, then a pause
LOGIN_PAUSE = 300.0
MODEL_FIELDS = ("kind", "upstream_url", "upstream_model", "upstream_key", "enabled", "context_length",
                "max_output_tokens", "access", "token_limit", "period", "guard", "deployment", "metrics_url",
                "description", "family")
KINDS = ("chat", "embedding", "rerank", "other")
Route = tuple[str, re.Pattern[str], Callable[..., Any], str]  # method, path, handler, who ("public"/"user"/"admin")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _int_or_none(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be a whole number") from None
    if number < 0:
        raise ApiError(400, f"{name} can't be negative")
    return number


def _period(value: Any, name: str = "period", allow_none: bool = True) -> str | None:
    if value in (None, "") and allow_none:
        return None
    if value not in limits.PERIODS:
        raise ApiError(400, f"{name} must be one of: {', '.join(limits.PERIODS)}")
    return str(value)


class Manager:
    """The app: routes, the store and the gateway. ``serve()`` runs it."""

    def __init__(self, store: Store, public_url: str = "") -> None:
        self.store = store
        self.gateway = Gateway(store)
        self.public_url = public_url.rstrip("/")
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.routes: list[Route] = []
        r = self._route
        r("GET", "/api/info", self.info, "public")
        r("POST", "/api/login", self.login, "public")
        r("POST", "/api/logout", self.logout, "public")
        r("POST", "/api/device/login", self.device_login, "public")
        r("GET", "/api/me", self.me, "user")
        r("GET", "/api/me/usage", self.my_usage, "user")
        r("GET", "/api/me/models", self.my_models, "user")
        r("GET", "/api/me/keys", self.my_keys, "user")
        r("POST", "/api/me/keys", self.new_key, "user")
        r("DELETE", "/api/me/keys/(?P<key_id>\\d+)", self.revoke_key, "user")
        r("POST", "/api/me/password", self.change_password, "user")
        r("GET", "/api/admin/settings", self.get_settings, "admin")
        r("PUT", "/api/admin/settings", self.put_settings, "admin")
        r("GET", "/api/admin/users", self.list_users, "admin")
        r("POST", "/api/admin/users", self.add_user, "admin")
        r("PATCH", "/api/admin/users/(?P<user_id>\\d+)", self.edit_user, "admin")
        r("DELETE", "/api/admin/users/(?P<user_id>\\d+)", self.delete_user, "admin")
        r("GET", "/api/admin/users/(?P<user_id>\\d+)/access", self.user_access, "admin")
        r("PUT", "/api/admin/users/(?P<user_id>\\d+)/access", self.set_user_access, "admin")
        r("GET", "/api/admin/users/(?P<user_id>\\d+)/usage", self.user_usage, "admin")
        r("POST", "/api/admin/users/(?P<user_id>\\d+)/reset-usage", self.reset_user, "admin")
        r("POST", "/api/admin/reset-usage", self.reset_everyone, "admin")
        r("GET", "/api/admin/models", self.list_models, "admin")
        r("POST", "/api/admin/models", self.add_model, "admin")
        r("PATCH", "/api/admin/models/(?P<model_id>[^/]+)", self.edit_model, "admin")
        r("DELETE", "/api/admin/models/(?P<model_id>[^/]+)", self.delete_model, "admin")
        r("POST", "/api/admin/models/(?P<model_id>[^/]+)/test", self.test_model, "admin")
        r("GET", "/api/admin/models/(?P<model_id>[^/]+)/deployment", self.model_deployment, "admin")
        r("POST", "/api/admin/models/(?P<model_id>[^/]+)/scale", self.scale_model, "admin")
        r("POST", "/api/admin/models/(?P<model_id>[^/]+)/max-model-len", self.model_len, "admin")
        r("GET", "/api/admin/discovery", self.discover, "admin")
        r("GET", "/api/admin/monitoring", self.monitor, "admin")
        r("GET", "/api/admin/monitoring/live", self.monitor_live, "admin")
        r("GET", "/api/admin/requests", self.requests_log, "admin")
        r("GET", "/api/admin/clients", self.clients, "admin")
        r("GET", "/api/admin/audit", self.audit_log, "admin")

    def _route(self, method: str, pattern: str, handler: Callable[..., Any], who: str) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), handler, who))

    # --- public -----------------------------------------------------------------------
    def info(self, ctx: Context) -> dict[str, Any]:
        settings = self.store.settings()
        return {"service": "dcode-manager", "name": settings["name"], "version": __version__,
                "gateway": self._base(ctx) + "/v1"}

    def _base(self, ctx: Context) -> str:
        if self.public_url:
            return self.public_url
        proto = ctx.headers.get("X-Forwarded-Proto") or ("https" if ctx.secure else "http")
        host = ctx.headers.get("X-Forwarded-Host") or ctx.headers.get("Host") or "localhost"
        return f"{proto}://{host}"

    def _check_login(self, ctx: Context, body: dict[str, Any]) -> dict[str, Any]:
        username, password = str(body.get("username") or ""), str(body.get("password") or "")
        slot = f"{username.lower()}|{ctx.address}"
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(slot, []) if now - t < LOGIN_PAUSE]
            self._failures[slot] = recent
            if len(recent) >= LOGIN_FAILURES:
                raise ApiError(429, "Too many failed sign-ins. Wait a few minutes and try again.")
        try:
            user = auth.authenticate(self.store, username, password)
        except auth.AuthError as exc:
            with self._lock:
                self._failures.setdefault(slot, []).append(now)
            raise ApiError(401, str(exc)) from None
        with self._lock:
            self._failures.pop(slot, None)
        return user

    def login(self, ctx: Context) -> dict[str, Any]:
        user = self._check_login(ctx, ctx.json())
        hours = float(self.store.settings().get("session_hours") or 12)
        token = auth.start_session(self.store, user["id"], hours)
        ctx.set_cookie = (f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(hours * 3600)}"
                          + ("; Secure" if ctx.secure or ctx.headers.get("X-Forwarded-Proto") == "https" else ""))
        self.store.run("UPDATE users SET last_seen = ? WHERE id = ?", (time.time(), user["id"]))
        return {"user": public_user(user)}

    def logout(self, ctx: Context) -> dict[str, Any]:
        token = ctx.cookie(COOKIE)
        if token:
            auth.end_session(self.store, token)
        ctx.set_cookie = f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        return {"ok": True}

    def device_login(self, ctx: Context) -> dict[str, Any]:
        """Dcode's setup: username + password in, an API key for this device and the models out."""
        body = ctx.json()
        user = self._check_login(ctx, body)
        device = str(body.get("device") or "device").strip()[:60]
        client = str(body.get("client") or ctx.headers.get("User-Agent") or "")[:120]
        key, key_id = auth.create_key(self.store, user["id"], f"Dcode on {device}", client)
        self.store.audit(user["id"], "device_login", device)
        return {"api_key": key, "key_id": key_id, "base_url": self._base(ctx) + "/v1", "user": public_user(user),
                "models": self.gateway.list_models(user)["data"]}

    # --- the signed-in user ------------------------------------------------------------------
    def me(self, ctx: Context) -> dict[str, Any]:
        settings = self.store.settings()
        return {"user": public_user(ctx.user), "name": settings["name"], "gateway": self._base(ctx) + "/v1",
                "version": __version__}

    def my_usage(self, ctx: Context) -> dict[str, Any]:
        return limits.summary(self.store, ctx.user, self.store.settings(), self.store.models())

    def my_models(self, ctx: Context) -> dict[str, Any]:
        return self.gateway.list_models(ctx.user)

    def my_keys(self, ctx: Context) -> dict[str, Any]:
        rows = self.store.all("SELECT * FROM api_keys WHERE user_id = ? AND revoked = 0 ORDER BY created DESC",
                              (ctx.user["id"],))
        return {"keys": [auth.public_key(r) for r in rows]}

    def new_key(self, ctx: Context) -> dict[str, Any]:
        body = ctx.json()
        key, key_id = auth.create_key(self.store, ctx.user["id"], str(body.get("name") or "key"), "web")
        self.store.audit(ctx.user["id"], "key_created", str(body.get("name") or ""))
        return {"api_key": key, "id": key_id, "base_url": self._base(ctx) + "/v1"}

    def revoke_key(self, ctx: Context, key_id: str) -> dict[str, Any]:
        owner = "" if ctx.user["role"] == "admin" else " AND user_id = ?"
        args = [int(key_id)] + ([] if ctx.user["role"] == "admin" else [ctx.user["id"]])
        if not self.store.run(f"UPDATE api_keys SET revoked = 1 WHERE id = ?{owner}", args):
            raise ApiError(404, "No such key")
        self.store.audit(ctx.user["id"], "key_revoked", key_id)
        return {"ok": True}

    def change_password(self, ctx: Context) -> dict[str, Any]:
        body = ctx.json()
        if not auth.check_password(str(body.get("current") or ""), ctx.user["password"]):
            raise ApiError(400, "The current password is wrong.")
        try:
            auth.set_password(self.store, ctx.user["id"], str(body.get("new") or ""))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return {"ok": True}

    # --- admin: settings and users --------------------------------------------------------------
    def get_settings(self, ctx: Context) -> dict[str, Any]:
        return self.store.settings()

    def put_settings(self, ctx: Context) -> dict[str, Any]:
        body = ctx.json()
        changes: dict[str, Any] = {}
        if "name" in body:
            changes["name"] = str(body["name"]).strip()[:80] or "Dcode Manager"
        if "timezone" in body:
            name = str(body["timezone"]).strip() or "UTC"
            if limits.zone(name).tzname(None) is None and name != "UTC":  # pragma: no cover - zoneinfo names have one
                raise ApiError(400, "Unknown time zone")
            changes["timezone"] = name
        if "default_user_limit" in body:
            changes["default_user_limit"] = _int_or_none(body["default_user_limit"], "default_user_limit")
        if "default_user_period" in body:
            changes["default_user_period"] = _period(body["default_user_period"], allow_none=False)
        if "discovery_namespaces" in body:
            value = body["discovery_namespaces"]
            names = value if isinstance(value, list) else re.split(r"[,\s]+", str(value))
            changes["discovery_namespaces"] = [n.strip() for n in names if n.strip()]
        for key in ("models_path", "kubernetes_api"):
            if key in body:
                changes[key] = str(body[key] or "").strip()
        if "session_hours" in body:
            changes["session_hours"] = max(1, _int_or_none(body["session_hours"], "session_hours") or 12)
        self.store.set_settings(changes)
        self.store.audit(ctx.user["id"], "settings", json.dumps(changes)[:400])
        return self.store.settings()

    def list_users(self, ctx: Context) -> dict[str, Any]:
        settings = self.store.settings()
        out = []
        for user in self.store.users():
            total = limits.total_limit(self.store, user, settings)
            keys = self.store.one("SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ? AND revoked = 0", (user["id"],))
            out.append(public_user(user) | {"usage": total.public(), "keys": keys["n"]})
        return {"users": out}

    def add_user(self, ctx: Context) -> dict[str, Any]:
        body = ctx.json()
        try:
            user_id = auth.create_user(self.store, str(body.get("username") or ""), str(body.get("password") or ""),
                                       str(body.get("role") or "user"), str(body.get("name") or ""))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        self._apply_user_limits(user_id, body)
        self.store.audit(ctx.user["id"], "user_added", str(body.get("username")))
        return {"user": public_user(self.store.user(user_id))}

    def _apply_user_limits(self, user_id: int, body: dict[str, Any]) -> None:
        if "token_limit" in body:
            self.store.run("UPDATE users SET token_limit = ? WHERE id = ?",
                           (_int_or_none(body["token_limit"], "token_limit"), user_id))
        if "period" in body:
            self.store.run("UPDATE users SET period = ? WHERE id = ?", (_period(body["period"]), user_id))

    def edit_user(self, ctx: Context, user_id: str) -> dict[str, Any]:
        target = self._user(user_id)
        body = ctx.json()
        if "role" in body:
            if body["role"] not in auth.ROLES:
                raise ApiError(400, f"Role must be one of: {', '.join(auth.ROLES)}")
            if target["role"] == "admin" and body["role"] != "admin":
                self._keep_an_admin(target["id"])
            self.store.run("UPDATE users SET role = ? WHERE id = ?", (body["role"], target["id"]))
        if "enabled" in body:
            if not body["enabled"]:
                if target["id"] == ctx.user["id"]:
                    raise ApiError(400, "You can't disable your own account.")
                if target["role"] == "admin":
                    self._keep_an_admin(target["id"])
                self.store.run("DELETE FROM sessions WHERE user_id = ?", (target["id"],))
            self.store.run("UPDATE users SET enabled = ? WHERE id = ?", (int(bool(body["enabled"])), target["id"]))
        if "name" in body:
            self.store.run("UPDATE users SET name = ? WHERE id = ?", (str(body["name"]).strip()[:80], target["id"]))
        if body.get("password"):
            try:
                auth.set_password(self.store, target["id"], str(body["password"]))
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        self._apply_user_limits(target["id"], body)
        self.store.audit(ctx.user["id"], "user_changed", f"{target['username']}: {sorted(set(body) - {'password'})}")
        return {"user": public_user(self.store.user(target["id"]))}

    def _keep_an_admin(self, leaving: int) -> None:
        others = self.store.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND enabled = 1 AND id != ?",
                                (leaving,))
        if not others["n"]:
            raise ApiError(400, "This is the last admin. Make someone else an admin first.")

    def delete_user(self, ctx: Context, user_id: str) -> dict[str, Any]:
        target = self._user(user_id)
        if target["id"] == ctx.user["id"]:
            raise ApiError(400, "You can't delete your own account.")
        if target["role"] == "admin":
            self._keep_an_admin(target["id"])
        self.store.run("DELETE FROM users WHERE id = ?", (target["id"],))  # usage rows stay, for the history
        self.store.audit(ctx.user["id"], "user_deleted", target["username"])
        return {"ok": True}

    def _user(self, user_id: str | int) -> dict[str, Any]:
        user = self.store.user(int(user_id))
        if user is None:
            raise ApiError(404, "No such user")
        return user

    def user_access(self, ctx: Context, user_id: str) -> dict[str, Any]:
        user = self._user(user_id)
        settings = self.store.settings()
        rows = {r["model_id"]: r for r in self.store.access_rows(user_id=user["id"])}
        out = []
        for model in self.store.models():
            row = rows.get(model["id"])
            usage = limits.model_limit(self.store, user, model, row, settings)
            out.append({"model": model["id"], "model_access": model["access"], "enabled": model["enabled"],
                        "allowed": limits.allowed(model, row, user), "explicit": row is not None,
                        "own_limit": row.get("token_limit") if row else None, "own_period": row.get("period") if row
                        else None, "model_limit": model.get("token_limit"), "model_period": model.get("period"),
                        "usage": usage.public()})
        return {"user": public_user(user), "access": out, "total": limits.total_limit(self.store, user, settings).public()}

    def set_user_access(self, ctx: Context, user_id: str) -> dict[str, Any]:
        """{model_id: {allowed, token_limit, period} | null (back to the model's defaults)}."""
        user = self._user(user_id)
        body = ctx.json()
        with self.store.tx() as db:
            for model_id, value in body.items():
                if self.store.model(model_id) is None:
                    raise ApiError(400, f"No model called {model_id}")
                if value is None:
                    db.execute("DELETE FROM access WHERE user_id = ? AND model_id = ?", (user["id"], model_id))
                    continue
                db.execute(
                    "INSERT INTO access (user_id, model_id, allowed, token_limit, period) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, model_id) DO UPDATE SET allowed = excluded.allowed, "
                    "token_limit = excluded.token_limit, period = excluded.period",
                    (user["id"], model_id, int(bool(value.get("allowed", True))),
                     _int_or_none(value.get("token_limit"), "token_limit"), _period(value.get("period"))))
        self.store.audit(ctx.user["id"], "access_changed", f"{user['username']}: {sorted(body)}")
        return self.user_access(ctx, user_id)

    def user_usage(self, ctx: Context, user_id: str) -> dict[str, Any]:
        return limits.summary(self.store, self._user(user_id), self.store.settings(), self.store.models())

    def reset_user(self, ctx: Context, user_id: str) -> dict[str, Any]:
        user = self._user(user_id)
        self.store.run("UPDATE users SET reset_at = ? WHERE id = ?", (time.time(), user["id"]))
        self.store.audit(ctx.user["id"], "usage_reset", user["username"])
        return {"ok": True}

    def reset_everyone(self, ctx: Context) -> dict[str, Any]:
        self.store.set_settings({"global_reset_at": time.time()})
        self.store.audit(ctx.user["id"], "usage_reset", "everyone")
        return {"ok": True}

    # --- admin: models -------------------------------------------------------------------------------
    def list_models(self, ctx: Context) -> dict[str, Any]:
        counts = {r["model_id"]: r for r in self.store.all(
            "SELECT model_id, COUNT(*) AS users FROM access WHERE allowed = 1 GROUP BY model_id")}
        return {"models": [public_model(m) | {"users": (counts.get(m["id"]) or {}).get("users", 0),
                                              "detected_family": family_of(m).key}
                           for m in self.store.models()],
                "families": [{"key": f.key, "label": f.label} for f in FAMILIES.values()]}

    def _model(self, model_id: str) -> dict[str, Any]:
        model = self.store.model(urllib.parse.unquote(model_id))
        if model is None:
            raise ApiError(404, "No such model")
        return model

    def _model_values(self, body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key in MODEL_FIELDS:
            if key not in body:
                continue
            value = body[key]
            if key in ("context_length", "max_output_tokens", "token_limit"):
                value = _int_or_none(value, key)
            elif key == "period":
                value = _period(value, allow_none=False)
            elif key == "enabled":
                value = int(bool(value))
            elif key == "kind":
                if value not in KINDS:
                    raise ApiError(400, f"kind must be one of: {', '.join(KINDS)}")
            elif key == "access":
                if value not in ("all", "selected"):
                    raise ApiError(400, "access must be all or selected")
            elif key == "family":
                value = str(value or "auto")
                if value != "auto" and value not in FAMILIES:
                    raise ApiError(400, f"Unknown model family {value!r}")
            elif key == "guard":
                if not isinstance(value, dict):
                    raise ApiError(400, "guard must be an object")
                found = guards.problems(value)
                if found:
                    raise ApiError(400, "; ".join(found))
                value = json.dumps(value)
            elif key == "deployment":
                if not isinstance(value, dict):
                    raise ApiError(400, "deployment must be an object")
                value = json.dumps({k: str(value[k]) for k in ("namespace", "name", "container") if value.get(k)})
            elif key == "upstream_url":
                value = str(value or "").strip().rstrip("/")
                if not re.match(r"^https?://[^\s/]+", value):
                    raise ApiError(400, "The server URL must start with http:// or https://")
            elif key == "upstream_key":
                if value in (None, "") and current is not None and body.get("keep_key", True):
                    continue  # an empty field in the form means "keep the saved key"
                value = str(value or "").strip()
            else:
                value = str(value or "").strip()
            values[key] = value
        return values

    def add_model(self, ctx: Context) -> dict[str, Any]:
        body = ctx.json()
        model_id = str(body.get("id") or "").strip()
        if not model_id or len(model_id) > 120 or any(c.isspace() for c in model_id):
            raise ApiError(400, "A model name is one word (letters, digits, - _ . : /), up to 120 characters.")
        if self.store.model(model_id):
            raise ApiError(400, f"There is already a model called {model_id}.")
        values = self._model_values(body)
        if "upstream_url" not in values:
            raise ApiError(400, "The model needs the URL of its server (…/v1).")
        values.setdefault("upstream_model", model_id)
        columns = ["id", "created"] + list(values)
        self.store.run(f"INSERT INTO models ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                       [model_id, time.time(), *values.values()])
        self.store.audit(ctx.user["id"], "model_added", model_id)
        return {"model": public_model(self.store.model(model_id))}

    def edit_model(self, ctx: Context, model_id: str) -> dict[str, Any]:
        model = self._model(model_id)
        body = ctx.json()
        values = self._model_values(body, model)
        new_id = str(body.get("id") or model["id"]).strip()
        if new_id != model["id"]:
            if not new_id or any(c.isspace() for c in new_id) or self.store.model(new_id):
                raise ApiError(400, f"Can't rename to {new_id!r}")
            values["id"] = new_id
        if values:
            sets = ", ".join(f"{k} = ?" for k in values)
            self.store.run(f"UPDATE models SET {sets} WHERE id = ?", [*values.values(), model["id"]])
        what = sorted(set(values) - {"upstream_key"}) + (["upstream_key"] if "upstream_key" in values else [])
        self.store.audit(ctx.user["id"], "model_changed", f"{model['id']}: {what}")
        return {"model": public_model(self.store.model(new_id))}

    def delete_model(self, ctx: Context, model_id: str) -> dict[str, Any]:
        model = self._model(model_id)
        self.store.run("DELETE FROM models WHERE id = ?", (model["id"],))
        self.store.audit(ctx.user["id"], "model_deleted", model["id"])
        return {"ok": True}

    def test_model(self, ctx: Context, model_id: str) -> dict[str, Any]:
        """One tiny request straight to the model's server: does it answer, how fast, with what."""
        model = self._model(model_id)
        started = time.perf_counter()
        listed, error = discovery.probe(model["upstream_url"], model.get("upstream_key", ""))
        out: dict[str, Any] = {"listed": listed[:20], "list_error": error, "list_ms": round(
            (time.perf_counter() - started) * 1000, 1)}
        if model["kind"] == "chat":
            started = time.perf_counter()
            try:
                status, payload = self.gateway._post_json(model, "/chat/completions", {
                    "model": model.get("upstream_model") or model["id"], "max_tokens": 40,
                    "messages": [{"role": "user", "content": "Reply with the word ready."}]}, 60)
                message = ((payload.get("choices") or [{}])[0].get("message") or {}) if status == 200 else {}
                out.update(status=status, answer=(message.get("content") or "")[:200],
                           error="" if status == 200 else json.dumps(payload)[:300], usage=payload.get("usage"))
            except (OSError, GatewayError) as exc:
                out.update(status=0, error=str(exc)[:300])
            out["chat_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if listed and not model.get("context_length"):
            match = next((m for m in listed if m["id"] == (model.get("upstream_model") or model["id"])), None)
            out["server_max_model_len"] = (match or {}).get("max_model_len")
        return out

    # --- admin: the cluster ------------------------------------------------------------------------
    def _kube(self) -> discovery.Kube:
        try:
            return discovery.Kube(self.store.settings().get("kubernetes_api") or "")
        except discovery.KubeError as exc:
            raise ApiError(400, str(exc)) from None

    def _deployment(self, model: dict[str, Any]) -> dict[str, str]:
        link = model.get("deployment") or {}
        if not link.get("namespace") or not link.get("name"):
            raise ApiError(400, f"{model['id']} isn't linked to a deployment. Link it in the model's settings.")
        return link

    def model_deployment(self, ctx: Context, model_id: str) -> dict[str, Any]:
        link = self._deployment(self._model(model_id))
        try:
            return {"link": link} | discovery.deployment_state(self._kube(), link["namespace"], link["name"])
        except discovery.KubeError as exc:
            raise ApiError(502, str(exc)) from None

    def scale_model(self, ctx: Context, model_id: str) -> dict[str, Any]:
        model = self._model(model_id)
        link = self._deployment(model)
        replicas = _int_or_none(ctx.json().get("replicas"), "replicas")
        if replicas is None or replicas > 8:
            raise ApiError(400, "replicas must be 0–8")
        try:
            discovery.scale(self._kube(), link["namespace"], link["name"], replicas)
        except discovery.KubeError as exc:
            raise ApiError(502, str(exc)) from None
        self.store.audit(ctx.user["id"], "model_scaled", f"{model['id']} → {replicas}")
        return {"ok": True, "replicas": replicas}

    def model_len(self, ctx: Context, model_id: str) -> dict[str, Any]:
        """Set the context length the gateway allows; with ``apply`` also the server's --max-model-len."""
        model = self._model(model_id)
        body = ctx.json()
        value = _int_or_none(body.get("value"), "value")
        if not value or value < 512:
            raise ApiError(400, "The context length must be at least 512 tokens")
        out: dict[str, Any] = {"ok": True, "context_length": value}
        if body.get("apply"):
            link = self._deployment(model)
            try:
                out["command"] = discovery.set_max_model_len(self._kube(), link["namespace"], link["name"],
                                                             link.get("container", ""), value)
            except discovery.KubeError as exc:
                raise ApiError(502, str(exc)) from None
            out["restarting"] = True
        self.store.run("UPDATE models SET context_length = ? WHERE id = ?", (value, model["id"]))
        self.store.audit(ctx.user["id"], "model_context", f"{model['id']} → {value}" + (" (deployment)" if
                                                                                        body.get("apply") else ""))
        return out

    def discover(self, ctx: Context) -> dict[str, Any]:
        settings = self.store.settings()
        known = self.store.models()
        out: dict[str, Any] = {"servers": [], "weights": [], "error": "", "namespaces": settings["discovery_namespaces"],
                               "models_path": settings["models_path"]}
        try:
            out["servers"] = discovery.discover(self._kube(), settings["discovery_namespaces"], known)
        except (ApiError, discovery.KubeError) as exc:
            out["error"] = getattr(exc, "message", None) or str(exc)
        out["weights"] = discovery.scan_weights(settings["models_path"], out["servers"])
        if settings["models_path"] and not Path(settings["models_path"]).expanduser().is_dir():
            out["weights_error"] = f"{settings['models_path']} is not a folder the manager can read."
        return out

    def monitor(self, ctx: Context) -> dict[str, Any]:
        return monitoring.overview(self.store, ctx.query.get("range", "24h"))

    def monitor_live(self, ctx: Context) -> dict[str, Any]:
        return {"models": monitoring.live(self.store)}

    def requests_log(self, ctx: Context) -> dict[str, Any]:
        where, args = [], []
        if ctx.query.get("user"):
            where.append("u.username = ?")
            args.append(ctx.query["user"])
        if ctx.query.get("model"):
            where.append("g.model_id = ?")
            args.append(ctx.query["model"])
        if ctx.query.get("problems"):
            where.append("(g.status >= 400 OR g.error != '')")
        sql = ("SELECT g.*, u.username FROM usage g LEFT JOIN users u ON u.id = g.user_id"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY g.id DESC LIMIT ?")
        args.append(min(500, int(ctx.query.get("limit") or 100)))
        return {"requests": self.store.all(sql, args)}

    def clients(self, ctx: Context) -> dict[str, Any]:
        return {"clients": [dict(c) | {"revoked": bool(c["revoked"])} for c in monitoring.clients(self.store)]}

    def audit_log(self, ctx: Context) -> dict[str, Any]:
        return {"audit": self.store.all("SELECT a.*, u.username FROM audit a LEFT JOIN users u ON u.id = a.user_id "
                                        "ORDER BY a.id DESC LIMIT 200")}

    # --- the gateway ----------------------------------------------------------------------------------------
    def gateway_request(self, ctx: Context, handler: BaseHTTPRequestHandler) -> None:
        try:
            user, key = auth.key_user(self.store, ctx.bearer())
        except auth.AuthError as exc:
            raise GatewayError(401, str(exc), "invalid_api_key", "authentication_error") from None
        client = ctx.headers.get("X-Dcode-Client") or ctx.headers.get("User-Agent") or ""
        auth.touch_key(self.store, key, client)
        client = client or key.get("client") or ""
        suffix = ctx.path[len("/v1"):]
        if ctx.method == "GET" and suffix in ("/models", "/models/"):
            ctx.send_json(self.gateway.list_models(user))
            return
        if ctx.method == "GET" and suffix.startswith("/models/"):
            wanted = urllib.parse.unquote(suffix[len("/models/"):])
            item = next((m for m in self.gateway.list_models(user)["data"] if m["id"] == wanted), None)
            if item is None:
                raise GatewayError(404, f"The model `{wanted}` does not exist on this manager.", "model_not_found")
            ctx.send_json(item)
            return
        if ctx.method != "POST":
            raise GatewayError(405, "Use POST for this endpoint.", "method_not_allowed")
        try:
            body = ctx.json()
        except ApiError as exc:
            raise GatewayError(400, exc.message, "invalid_json") from None
        endpoint = {"/chat/completions": "chat", "/completions": "completion", "/embeddings": "embedding"}.get(
            suffix, suffix.strip("/").replace("/", ".")[:40] or "other")
        call = Call(user=user, key=key, model_id=str(body.get("model") or "")[:120], endpoint=endpoint,
                    client=client[:120])
        try:
            model, guard = self.gateway.prepare(user, body, endpoint, call)
            self.gateway.check_with_guard_model(guard, body, user)
            self.gateway.forward(handler, call, model, guard, body, suffix)
        except GatewayError as exc:
            call.status, call.error, call.guard = exc.status, exc.message[:300], exc.guard
            if self.store.model(call.model_id) is not None:  # don't log a row per typo'd model name
                self.gateway.record(call)
            raise
        self.gateway.record(call)


class Context:
    """One request as the route handlers see it."""

    def __init__(self, handler: BaseHTTPRequestHandler, method: str, secure: bool) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        self.handler, self.method, self.secure = handler, method, secure
        self.path = parsed.path
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self.headers = handler.headers
        self.address = handler.client_address[0] if handler.client_address else ""
        self.user: dict[str, Any] = {}
        self.set_cookie = ""
        self._body: bytes | None = None

    def raw(self) -> bytes:
        if self._body is None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "The request is too large.")
            self._body = self.handler.rfile.read(length) if length else b""
        return self._body

    def json(self) -> dict[str, Any]:
        raw = self.raw()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            raise ApiError(400, "The request body is not valid JSON.") from None
        if not isinstance(value, dict):
            raise ApiError(400, "The request body must be a JSON object.")
        return value

    def cookie(self, name: str) -> str:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:  # noqa: BLE001 - a malformed cookie header is just "not signed in"
            return ""
        return jar[name].value if name in jar else ""

    def bearer(self) -> str:
        value = self.headers.get("Authorization") or ""
        return value[7:].strip() if value.lower().startswith("bearer ") else ""

    def send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        h = self.handler
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.send_header("Cache-Control", "no-store")
        if self.set_cookie:
            h.send_header("Set-Cookie", self.set_cookie)
        h.end_headers()
        h.wfile.write(raw)


STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/admin.js": ("admin.js", "text/javascript; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def make_handler(app: Manager, secure: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "dcode-manager"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's name
            pass  # the usage table is the access log for the gateway; no per-request stderr noise

        def _dispatch(self, method: str) -> None:
            ctx = Context(self, method, secure)
            try:
                if ctx.path == "/v1" or ctx.path.startswith("/v1/"):
                    app.gateway_request(ctx, self)
                    return
                if ctx.path == "/health":
                    ctx.send_json({"ok": True, "version": __version__})
                    return
                if method == "GET" and (ctx.path in STATIC or ctx.path == "/theme.css"):
                    self._static(ctx.path)
                    return
                if not ctx.path.startswith("/api/"):
                    if method == "GET":
                        self._static("/")  # the UI routes its own paths (#/models …)
                        return
                    raise ApiError(404, "Not found")
                self._api(ctx, method)
            except GatewayError as exc:
                self._error(exc.status, exc.body(), exc.retry_after)
            except ApiError as exc:
                self._error(exc.status, json.dumps({"error": exc.message}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001 - one bad request must not take the server down
                self._error(500, json.dumps({"error": f"Internal error: {exc}"}).encode())

        def _api(self, ctx: Context, method: str) -> None:
            for route_method, pattern, func, who in app.routes:
                if route_method != method:
                    continue
                match = pattern.match(ctx.path)
                if not match:
                    continue
                if who != "public":
                    ctx.user = self._who(ctx, method) or {}
                    if not ctx.user:
                        raise ApiError(401, "Sign in first.")
                    if who == "admin" and ctx.user["role"] != "admin":
                        raise ApiError(403, "Only admins can do this.")
                elif method != "GET" and ctx.path != "/api/device/login" and not ctx.headers.get("X-Dcode-Manager"):
                    raise ApiError(403, "Missing the X-Dcode-Manager header.")
                ctx.send_json(func(ctx, **match.groupdict()))
                return
            raise ApiError(404 if not any(p.match(ctx.path) for _, p, _, _ in app.routes) else 405, "Not found")

        def _who(self, ctx: Context, method: str) -> dict[str, Any] | None:
            bearer = ctx.bearer()
            if bearer:  # an API key reaches the user's own routes only (Dcode's /usage)
                try:
                    user, _key = auth.key_user(app.store, bearer)
                except auth.AuthError:
                    return None
                return user if ctx.path.startswith("/api/me") and method == "GET" else None
            user = auth.session_user(app.store, ctx.cookie(COOKIE))
            if user and method != "GET" and not ctx.headers.get("X-Dcode-Manager"):
                raise ApiError(403, "Missing the X-Dcode-Manager header.")
            return user

        def _static(self, path: str) -> None:
            if path == "/theme.css":
                from ..theme import css_variables

                raw, kind = css_variables().encode(), "text/css; charset=utf-8"
            else:
                name, kind = STATIC[path]
                raw = (WEB / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, status: int, raw: bytes, retry_after: int | None = None) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if retry_after:
                    self.send_header("Retry-After", str(retry_after))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

    return Handler


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(app: Manager, host: str = "0.0.0.0", port: int = 8080, cert: str = "", key: str = ""
          ) -> ManagerServer:
    server = ManagerServer((host, port), make_handler(app, secure=bool(cert)))
    if cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key or None)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
