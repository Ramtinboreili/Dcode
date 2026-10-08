"""Localhost web UI: per-session API servers + one per-user hub.

Topology::

    browser ──► hub (127.0.0.1:<per-user port>)  serves UI, settings, history
                 │  proxies /api/s/<id>/...
                 ├──► session A API (127.0.0.1:<ephemeral>)
                 └──► session B API (127.0.0.1:<ephemeral>)

* Every ``dcode`` process runs a tiny API server for its own session and
  registers it in ``~/.dcode/run/sessions/``.
* The first session spawns the hub; later sessions just reuse it. The hub
  exits on its own once no live session remains. History, memory and
  settings live on disk, so the next hub shows everything again.
* Only the standard library is used (air-gapped friendly). Everything binds to
  127.0.0.1 and requires a per-user secret token (0600 file), so other users
  on a shared server can't see or drive your sessions.
"""

from __future__ import annotations

import errno
import hmac
import http.client
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import ArtifactStore, filename_for
from .config import (
    REASONING_EFFORTS,
    apply_env_overrides,
    config_file,
    config_from_dict,
    ensure_app_dirs,
    env_locked_keys,
    list_threads,
    load_raw_config,
    load_thread,
    memory_dir,
    run_dir,
    skills_dir,
    threads_dir,
    write_private_json,
)
from .custom_agents import agents_dir, load_specs, save_spec
from .custom_agents import normalize_name as agent_name
from .hooks import clean as clean_hooks
from .memory import SCOPES, _parse, slugify, write_memory
from .rules import _clean as _clean_rules
from .skills import list_skills, normalize_name, save_skill
from .theme import css_variables

WEB_DIR = Path(__file__).resolve().parent / "web"
HUB_IDLE_GRACE = 6.0       # seconds with zero live sessions before the hub exits
HUB_START_GRACE = 30.0     # max wait for the first session to register
COOKIE = "dcode_token"
SSE_PING = 15.0
DB_SCHEMA_TTL = 120.0      # seconds the hub reuses a database schema while you browse
DB_MAX_GRID_ROWS = 5000    # the most rows one query may send to the browser, whatever the page asks for


# --- runtime files ------------------------------------------------------------------

def _sessions_dir() -> Path:
    path = run_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hub_file() -> Path:
    return run_dir() / "hub.json"


def get_token() -> str:
    """Per-user secret shared by the hub and all of this user's sessions."""
    ensure_app_dirs()
    path = run_dir() / "token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    return token


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def live_sessions() -> list[dict[str, Any]]:
    """Registry entries whose process is still alive; stale files are pruned."""
    items = []
    for path in _sessions_dir().glob("*.json"):
        entry = _read_json(path)
        if not entry or not pid_alive(int(entry.get("pid", 0))):
            path.unlink(missing_ok=True)
            continue
        items.append(entry)
    items.sort(key=lambda e: str(e.get("started_at", "")))
    return items


def register_session(info: dict[str, Any]) -> None:
    write_private_json(_sessions_dir() / f"{info['id']}.json", info)


def unregister_session(session_id: str) -> None:
    (_sessions_dir() / f"{session_id}.json").unlink(missing_ok=True)


def default_port(configured: int = 0) -> int:
    if configured:
        return configured
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return 7300 + (uid % 500)  # stable per OS user, so bookmarks keep working


# --- shared HTTP plumbing -----------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = f"Dcode/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:  # silence default stderr logging
        pass

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 5_000_000:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()


# --- per-session API --------------------------------------------------------------------

class SessionServer:
    """Exposes one :class:`~dcode.session.Session` on an ephemeral localhost port."""

    def __init__(self, session: Any, token: str) -> None:
        self.session = session
        self.token = token
        outer = self

        class Handler(_Handler):
            def _authorized(self) -> bool:
                return hmac.compare_digest(self.headers.get("X-Dcode-Token", ""), outer.token)

            def do_GET(self) -> None:
                if not self._authorized():
                    return self._json({"error": "unauthorized"}, 401)
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/state":
                    return self._json(outer.session.state())
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/events":
                    return outer._stream(self, int((query.get("since") or ["0"])[0] or 0))
                if parsed.path == "/files":  # @mention completion
                    from .mentions import complete

                    q = (query.get("q") or [""])[0]
                    session = outer.session
                    helpers = ([{"name": n, "description": spec.description} for n, spec in session.agent.specs.items()
                                if n.startswith(q.lower())] if session.config.agents.get("enabled", True) else [])
                    return self._json({"files": complete(q, Path(session.cwd)), "helpers": helpers})
                return self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:
                if not self._authorized():
                    return self._json({"error": "unauthorized"}, 401)
                try:
                    result = outer._command(urllib.parse.urlsplit(self.path).path, self._body())
                except RuntimeError as exc:
                    return self._json({"error": str(exc)}, 409)
                return self._json(result if result is not None else {"ok": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="dcode-session-api", daemon=True)

    def start(self) -> SessionServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _stream(self, handler: _Handler, since: int) -> None:
        handler._sse_headers()
        bus = self.session.bus
        try:
            while True:
                events = bus.wait_since(since, timeout=SSE_PING)
                if not events:
                    handler.wfile.write(b": ping\n\n")
                for event in events:
                    since = event["seq"]
                    handler.wfile.write(f"id: {since}\ndata: {json.dumps(event, default=str)}\n\n".encode())
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _command(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        session = self.session
        if path == "/message":
            text = str(body.get("text") or "").strip()
            if text == "/review":
                session.review(source="web")
            elif text.startswith("!"):
                session.run_shell(text[1:], source="web")
            elif text:
                session.submit(text, source="web")
            return None
        if path == "/approve":
            ok = session.resolve_approval(str(body.get("id", "")), bool(body.get("approved")), by="web",
                                          scope="session" if body.get("scope") == "session" else "once",
                                          answer=str(body.get("answer") or ""))
            return {"ok": ok}
        if path == "/interrupt":
            session.interrupt(source="web")
            return None
        if path == "/new":
            session.new_thread()
            return None
        if path == "/rename":
            session.rename(str(body.get("name") or ""))
            return None
        if path == "/resume":
            return {"ok": session.resume(str(body.get("thread_id") or ""), source="web")}
        if path == "/model":
            effort = body.get("reasoning_effort")
            session.set_model(body.get("model") or None, effort if effort in REASONING_EFFORTS else None)
            return None
        if path == "/reload":
            return {"ok": session.reload_config()}
        if path == "/mode":
            try:
                session.set_mode(str(body.get("mode") or ""), source="web")
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            return {"ok": True, "mode": session.mode}
        if path == "/plan":
            session.resolve_plan(str(body.get("decision") or "keep"), source="web")
            return None
        if path == "/command":
            session.run_custom_command(str(body.get("name") or ""), str(body.get("args") or ""), source="web")
            return None
        if path == "/init":
            return {"ok": True, "file": session.init_project(source="web")}
        if path == "/compact":
            session.compact(str(body.get("focus") or ""), source="web")
            return None
        if path == "/undo":
            result = session.undo()
            return {"ok": bool(result), "files": result[1] if result else []}
        if path == "/redo":
            result = session.redo()
            return {"ok": bool(result), "files": result[1] if result else []}
        raise RuntimeError(f"unknown command {path}")


# --- hub ----------------------------------------------------------------------------------

def _session_request(entry: dict[str, Any], method: str, path: str, token: str,
                     body: bytes | None = None, timeout: float = 5.0) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", int(entry["port"]), timeout=timeout)
    try:
        headers = {"X-Dcode-Token": token, "Content-Type": "application/json"}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _get_json(url: str, api_key: str, timeout: float = 10.0) -> dict[str, Any]:
    """GET a JSON document from the model server.

    Plain urllib, so the hub doesn't load a whole HTTP client stack (several MB) for one request.
    python.org builds on macOS have no CA store, so HTTPS uses certifi's bundle when it's installed.
    """
    context = None
    if url.lower().startswith("https:"):
        import ssl

        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}", "Accept": "application/json", "User-Agent": f"dcode/{__version__}"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, dict) else {}


def _mask(key: str) -> str:
    return ("•" * 8 + key[-4:]) if key and len(key) > 4 else ("•" * len(key))


def _memory_scopes() -> dict[str, Path]:
    root = memory_dir()
    scopes = {"global": root / "global"}
    projects = root / "projects"
    if projects.is_dir():
        for path in sorted(projects.iterdir()):
            if path.is_dir():
                scopes[f"projects/{path.name}"] = path
    return scopes


class Hub:
    def __init__(self, port: int = 0) -> None:
        ensure_app_dirs()
        self.token = get_token()
        self.started = time.time()
        self._had_session = False
        self._idle_since: float | None = None
        self._schemas: dict[str, tuple[float, dict[str, Any], str]] = {}  # database schemas, briefly cached
        self.scheduler: Any = None  # scheduled jobs; started in serve()
        outer = self

        class Handler(_Handler):
            # --- auth -------------------------------------------------------------
            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0]
                return host in ("127.0.0.1", "localhost")  # blocks DNS-rebinding

            def _cookie_token(self) -> str:
                cookie = SimpleCookie(self.headers.get("Cookie") or "")
                return cookie[COOKIE].value if COOKIE in cookie else ""

            def _authorized(self) -> bool:
                return self._host_ok() and hmac.compare_digest(self._cookie_token(), outer.token)

            def _deny(self) -> None:
                body = (
                    b"<!doctype html><meta charset=utf-8><title>Dcode</title>"
                    b"<body style='background:#0A1020;color:#E4ECFA;font:15px system-ui;padding:40px'>"
                    b"<h2 style='color:#3D8BFF'>Dcode</h2><p>Open the link printed in your terminal "
                    b"(it contains your access token).</p></body>"
                )
                self.send_response(401)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            # --- routes -----------------------------------------------------------
            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/" and query.get("token") and self._host_ok():
                    if hmac.compare_digest(query["token"][0], outer.token):
                        nxt = (query.get("next") or ["/"])[0]
                        if not nxt.startswith("/") or nxt.startswith("//"):
                            nxt = "/"  # never redirect off this host
                        self.send_response(302)
                        self.send_header(
                            "Set-Cookie",
                            f"{COOKIE}={outer.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={30 * 24 * 3600}"
                        )
                        self.send_header("Location", nxt)
                        self.end_headers()
                        return
                if parsed.path == "/static/logo.svg":
                    return self._static("logo.svg")
                if parsed.path in ("/guide", "/static/guide.html") and self._host_ok():
                    return self._static("guide.html")  # public docs: no token needed
                if parsed.path == "/api/health":
                    # "assets" catches a hub whose bundled files vanished (e.g. a deleted unpack dir).
                    return self._json({"ok": True, "app": "dcode", "version": __version__,
                                       "assets": (WEB_DIR / "index.html").is_file()})
                if not self._authorized():
                    return self._deny() if not parsed.path.startswith("/api/") else self._json({"error": "unauthorized"}, 401)
                if parsed.path in ("/", "/index.html"):
                    return self._static("index.html")
                if parsed.path == "/theme.css":
                    return self._raw(css_variables().encode(), "text/css")
                if parsed.path.startswith("/static/"):
                    return self._static(parsed.path[len("/static/"):])
                if parsed.path.startswith("/a/"):
                    return self._static("artifact.html")  # standalone artifact page
                if parsed.path.startswith("/api/s/"):
                    return outer._proxy(self, "GET", parsed)
                return outer._api_get(self, parsed.path, query)

            def do_POST(self) -> None:
                self._mutate("POST")

            def do_PUT(self) -> None:
                self._mutate("PUT")

            def do_DELETE(self) -> None:
                self._mutate("DELETE")

            def do_PATCH(self) -> None:
                self._mutate("PATCH")

            def _mutate(self, method: str) -> None:
                # Custom header + SameSite cookie: a cross-site page can't forge this.
                if not self._authorized() or self.headers.get("X-Dcode") != "1":
                    return self._json({"error": "unauthorized"}, 401)
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path.startswith("/api/s/"):
                    return outer._proxy(self, method, parsed)
                try:
                    return outer._api_mutate(self, method, parsed.path, self._body())
                except (ValueError, OSError) as exc:
                    return self._json({"error": str(exc)}, 400)

            def _raw(self, data: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            def _static(self, name: str) -> None:
                path = (WEB_DIR / name).resolve()
                if WEB_DIR not in path.parents or not path.is_file():
                    return self._json({"error": "not found"}, 404)
                types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                         ".svg": "image/svg+xml"}
                self._raw(path.read_bytes(), types.get(path.suffix, "application/octet-stream"))

        preferred = default_port(port)
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", preferred), Handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # --- lifecycle ------------------------------------------------------------------------
    def serve(self) -> None:
        write_private_json(hub_file(), {"pid": os.getpid(), "port": self.port, "url": self.url,
                                        "version": __version__, "started": self.started})
        from .schedules import Scheduler

        self.scheduler = Scheduler(env=_clean_child_env()).start()
        threading.Thread(target=self._watch, name="dcode-hub-watch", daemon=True).start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self.scheduler.stop()
            self.httpd.server_close()
            info = _read_json(hub_file())
            if info and info.get("pid") == os.getpid():
                hub_file().unlink(missing_ok=True)

    def _watch(self) -> None:
        while True:
            time.sleep(1.0)
            count = len(live_sessions())
            now = time.time()
            if count:
                self._had_session = True
                self._idle_since = None
                continue
            if self._has_scheduled_work():
                self._idle_since = None  # scheduled jobs keep the hub alive with no terminal open
                continue
            if not self._had_session:
                if now - self.started > HUB_START_GRACE:
                    break
                continue
            self._idle_since = self._idle_since or now
            if now - self._idle_since >= HUB_IDLE_GRACE:
                break
        self.httpd.shutdown()

    def _has_scheduled_work(self) -> bool:
        from .schedules import has_enabled_jobs

        try:
            return bool(self.scheduler and (self.scheduler.busy() or has_enabled_jobs()))
        except Exception:
            return False

    # --- API --------------------------------------------------------------------------------
    def _sessions_with_state(self) -> list[dict[str, Any]]:
        out = []
        for entry in live_sessions():
            item = dict(entry)
            try:
                status, body = _session_request(entry, "GET", "/state", self.token, timeout=1.5)
                if status == 200:
                    item.update(json.loads(body))
                    item["live"] = True
            except (OSError, ValueError):
                item["live"] = False
            out.append(item)
        return out

    def _api_get(self, handler: _Handler, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/hub":
            return handler._json({"version": __version__, "user": _username(), "home": str(run_dir().parent)})
        if path == "/api/sessions":
            return handler._json(self._sessions_with_state())
        if path == "/api/threads":
            return handler._json([
                {k: v for k, v in t.items() if k not in ("messages", "path")} | {"count": len(t["messages"])}
                for t in list_threads()
            ])
        if path.startswith("/api/threads/"):
            from .session import transcript_from_messages

            path_obj = threads_dir() / f"{slugify_id(path.rsplit('/', 1)[1])}.json"
            if not path_obj.exists():
                return handler._json({"error": "not found"}, 404)
            payload = load_thread(path_obj)
            return handler._json({
                "name": payload.get("name"), "cwd": payload.get("cwd"), "model": payload.get("model"),
                "created_at": payload.get("created_at"), "updated_at": payload.get("updated_at"),
                "items": transcript_from_messages(payload.get("messages") or []),
            })
        if path == "/api/config":
            raw = load_raw_config()
            cfg = config_from_dict(apply_env_overrides(dict(raw)))
            data = cfg.as_dict() if cfg else dict(raw)
            # Env vars win over the file; the UI shows those fields read-only.
            data["env_locked"] = env_locked_keys()
            data["has_api_key"] = bool(data.get("api_key"))
            data["api_key"] = _mask(str(data.get("api_key") or ""))
            data["reasoning_efforts"] = REASONING_EFFORTS
            return handler._json(data)
        if path == "/api/models":
            return handler._json(self._models())
        if path == "/api/memory":
            out = []
            for key, folder in _memory_scopes().items():
                if not folder.is_dir():
                    continue
                for file in sorted(folder.glob("*.md")):
                    memory = _parse(file, "global" if key == "global" else "project")
                    if memory:
                        out.append(memory.as_dict() | {"scope_key": key})
            out.sort(key=lambda m: str(m.get("updated", "")), reverse=True)
            return handler._json(out)
        if path == "/api/skills":
            raw = load_raw_config()
            pinned = {normalize_name(n) for n in raw.get("enabled_skills") or []}
            return handler._json([
                {"name": s.name, "filename": s.filename, "description": s.description, "content": s.content,
                 "pinned": s.name in pinned, "builtin": s.builtin, "scope": s.scope}
                for s in list_skills()
            ])
        if path == "/api/agents":
            # Project helpers too, but only for a folder a live session is working in.
            cwd = query.get("cwd", [""])[0]
            workspace = Path(cwd) if cwd and any(e.get("cwd") == cwd for e in live_sessions()) else None
            return handler._json([spec.as_dict() for spec in load_specs(workspace).values()])
        if path == "/api/db" or path.startswith("/api/db/"):
            return self._db_get(handler, path, query)
        if path == "/api/cron" or path.startswith("/api/cron/"):
            return self._cron(handler, "GET", path, query, {})
        if path == "/api/artifacts":
            return handler._json(ArtifactStore().list())
        if path.startswith("/api/artifacts/"):
            rest = path[len("/api/artifacts/"):]
            artifact_id, _, action = rest.partition("/")
            record = ArtifactStore().get(urllib.parse.unquote(artifact_id))
            if record is None:
                return handler._json({"error": "not found"}, 404)
            store = ArtifactStore()
            if action == "raw":
                return outer_download(handler, record)
            version = query.get("version", [""])[0]
            content = record["content"]
            if version.isdigit():
                content = store.content_at(record, int(version))
                if content is None:
                    return handler._json({"error": "no such version"}, 404)
            data = ArtifactStore.meta(record) | {"content": content, "history": store.versions(record),
                                                 "shown_version": int(version) if version.isdigit() else None}
            return handler._json(data)
        return handler._json({"error": "not found"}, 404)

    def _api_mutate(self, handler: _Handler, method: str, path: str, body: dict[str, Any]) -> None:
        if path == "/api/config" and method == "PUT":
            raw = load_raw_config()
            for key in ("base_url", "model", "reasoning_effort", "context_window", "max_output_tokens", "stream"):
                if key in body:
                    raw[key] = body[key]
            if body.get("api_key") and "•" not in str(body["api_key"]):
                raw["api_key"] = body["api_key"]
            for section in ("permissions", "experimental", "agents", "memory", "ui", "artifacts", "checks", "db"):
                if isinstance(body.get(section), dict):
                    merged = dict(raw.get(section) or {})
                    merged.update(body[section])
                    raw[section] = merged
            if isinstance(body.get("rules"), dict):  # replaced whole, so deleting a pattern works
                raw["rules"] = {
                    "commands": _clean_rules(body["rules"].get("commands")),
                    "files": _clean_rules(body["rules"].get("files")),
                    "outside_workspace": str(body["rules"].get("outside_workspace") or "ask"),
                }
            if isinstance(body.get("hooks"), list):
                raw["hooks"] = clean_hooks(body["hooks"])
            if isinstance(body.get("enabled_skills"), list):
                raw["enabled_skills"] = [str(s) for s in body["enabled_skills"]]
            if config_from_dict(apply_env_overrides(dict(raw))) is None:
                raise ValueError("base_url, api_key and model are required")
            write_private_json(config_file(), raw)
            reloaded = self._broadcast("/reload")
            return handler._json({"ok": True, "sessions_reloaded": reloaded})
        if path == "/api/memory" and method == "POST":
            scope_key = str(body.get("scope_key") or "global")
            folder = _memory_scopes().get(scope_key)
            if folder is None:
                raise ValueError("unknown memory scope")
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("name is required")
            write_memory(folder, "global" if scope_key == "global" else "project", name,
                         str(body.get("content") or ""), str(body.get("description") or ""),
                         str(body.get("tags") or "").split(","))
            return handler._json({"ok": True})
        if path.startswith("/api/memory/") and method == "DELETE":
            scope_key, _, name = path[len("/api/memory/"):].rpartition("/")
            folder = _memory_scopes().get(urllib.parse.unquote(scope_key))
            if folder is None:
                raise ValueError("unknown memory scope")
            (folder / f"{slugify(urllib.parse.unquote(name))}.md").unlink(missing_ok=True)
            return handler._json({"ok": True})
        if path.startswith("/api/threads/") and method == "DELETE":
            (threads_dir() / f"{slugify_id(path.rsplit('/', 1)[1])}.json").unlink(missing_ok=True)
            return handler._json({"ok": True})
        if path == "/api/skills" and method == "POST":
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("skill name is required")
            skill = save_skill(name, str(body.get("description") or ""), str(body.get("content") or ""))
            return handler._json({"ok": True, "name": skill.name})
        if path.startswith("/api/skills/") and method == "DELETE":
            name = normalize_name(urllib.parse.unquote(path.rsplit("/", 1)[1]))
            (skills_dir() / f"{name}.md").unlink(missing_ok=True)
            return handler._json({"ok": True})
        if path == "/api/agents" and method == "POST":
            name = agent_name(str(body.get("name") or ""))
            if not name or not str(body.get("description") or "").strip():
                raise ValueError("a helper needs a name and a description")
            save_spec(name, str(body.get("description")), str(body.get("prompt") or ""),
                      str(body.get("tools") or ""), str(body.get("model") or ""))
            return handler._json({"ok": True, "name": name})
        if path.startswith("/api/agents/") and method == "DELETE":
            name = agent_name(urllib.parse.unquote(path.rsplit("/", 1)[1]))
            if name:
                (agents_dir() / f"{name}.md").unlink(missing_ok=True)
            return handler._json({"ok": True})
        if path == "/api/db" or path.startswith("/api/db/"):
            return self._db_mutate(handler, method, path, body)
        if path == "/api/cron" or path.startswith("/api/cron/"):
            return self._cron(handler, method, path, {}, body)
        if path == "/api/artifacts" and method == "POST":
            meta = ArtifactStore().save(
                str(body.get("title") or "Untitled"), str(body.get("content") or ""),
                kind=str(body.get("kind") or ""), language=str(body.get("language") or ""),
            )
            return handler._json(meta)
        if path.startswith("/api/artifacts/"):
            artifact_id, _, action = urllib.parse.unquote(path[len("/api/artifacts/"):]).partition("/")
            store = ArtifactStore()
            if store.get(artifact_id) is None:
                return handler._json({"error": "not found"}, 404)
            if method == "DELETE" and not action:
                store.delete(artifact_id)
                return handler._json({"ok": True})
            if method == "PATCH" and not action:
                return handler._json(store.rename(artifact_id, str(body.get("title") or "")))
            if method == "PUT" and action == "content":
                record = store.get(artifact_id) or {}
                return handler._json(store.save(record.get("title", ""), str(body.get("content") or ""),
                                                artifact_id=artifact_id))
            if method == "POST" and action == "restore":
                return handler._json(store.restore(artifact_id, int(body.get("version") or 0)))
            if method == "POST" and action == "duplicate":
                return handler._json(store.duplicate(artifact_id))
        return handler._json({"error": "not found"}, 404)

    # --- scheduled jobs -----------------------------------------------------------------------
    def _cron(self, handler: _Handler, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> None:
        from . import schedules

        parts = [urllib.parse.unquote(p) for p in path[len("/api/cron"):].strip("/").split("/") if p]
        try:
            if not parts and method == "GET":
                return handler._json({"jobs": [schedules.describe_job(j) for j in schedules.load_jobs().values()],
                                      "modes": schedules.PROMPT_MODES, "now": time.strftime("%Y-%m-%dT%H:%M")})
            if parts == ["preview"]:  # check a schedule while it is being typed
                expression = (query.get("schedule") or [str(body.get("schedule") or "")])[0]
                schedules.parse(expression)
                upcoming, moment = [], None
                for _ in range(3):
                    moment = schedules.next_run(expression, moment)
                    if moment is None:
                        break
                    upcoming.append(moment.isoformat(timespec="minutes"))
                return handler._json({"text": schedules.explain(expression), "next": upcoming})
            if not parts and method == "POST":
                return handler._json(schedules.describe_job(schedules.save_job(body)))
            job = schedules.load_jobs().get(schedules.clean_id(parts[0])) if parts else None
            if job is None:
                return handler._json({"error": "no such job"}, 404)
            action = parts[1] if len(parts) > 1 else ""
            if method == "DELETE" and not action:
                if self.scheduler:
                    self.scheduler.stop_job(job["id"])
                return handler._json({"ok": schedules.delete_job(job["id"])})
            if method == "GET" and action == "runs":
                return handler._json(list(reversed(schedules.load_runs(job["id"]))))
            if method == "GET" and action == "log":
                return handler._json({"text": schedules.read_log(job["id"], (query.get("run") or [""])[0])})
            if method == "POST" and action == "toggle":
                return handler._json(schedules.describe_job(schedules.update_job(job["id"], enabled=bool(body.get("enabled")))))
            if method == "POST" and action == "run":
                if self.scheduler is None or self.scheduler.is_running(job["id"]):
                    return handler._json({"error": "This job is already running."}, 409)
                self.scheduler.launch(job, "manual")
                return handler._json({"ok": True})
            if method == "POST" and action == "stop":
                return handler._json({"ok": bool(self.scheduler and self.scheduler.stop_job(job["id"]))})
        except schedules.ScheduleError as exc:
            return handler._json({"error": str(exc)}, 400)
        return handler._json({"error": "not found"}, 404)

    # --- databases --------------------------------------------------------------------------
    def _db_schema(self, name: str, refresh: bool = False) -> dict[str, Any]:
        """A connection's schema, kept for a couple of minutes so browsing doesn't re-read the catalog."""
        from . import db

        conn = db.get_connection(name)
        cached = self._schemas.get(conn["name"])
        if cached and not refresh and time.time() - cached[0] < DB_SCHEMA_TTL and cached[2] == db.describe(conn):
            return cached[1]
        schema = db.read_schema(conn)
        self._schemas[conn["name"]] = (time.time(), schema, db.describe(conn))
        return schema

    def _db_get(self, handler: _Handler, path: str, query: dict[str, list[str]]) -> None:
        from . import db

        parts = [urllib.parse.unquote(p) for p in path[len("/api/db"):].strip("/").split("/") if p]
        try:
            if not parts:
                return handler._json({"connections": [db.public(c) for c in db.load_connections().values()],
                                      "drivers": db.drivers(), "limits": db.limits(),
                                      "engines": db.ENGINE_LABELS, "ports": db.ENGINES})
            if parts[0] == "exports":
                if len(parts) == 1:
                    files = sorted(db.exports_dir().iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
                    return handler._json([{"file": f.name, "bytes": f.stat().st_size, "size": db.human_size(f.stat().st_size),
                                           "modified": f.stat().st_mtime} for f in files if f.is_file()][:200])
                return _send_file(handler, db.exports_dir() / Path(parts[1]).name)
            if len(parts) == 2 and parts[1] == "schema":
                return handler._json(self._db_schema(parts[0], refresh=bool(query.get("refresh"))))
            if len(parts) == 2 and parts[1] == "summary":
                return handler._json(db.summarize(self._db_schema(parts[0], refresh=bool(query.get("refresh")))))
        except db.DBError as exc:
            return handler._json({"error": str(exc)}, 400)
        return handler._json({"error": "not found"}, 404)

    def _db_mutate(self, handler: _Handler, method: str, path: str, body: dict[str, Any]) -> None:
        from . import db

        parts = [urllib.parse.unquote(p) for p in path[len("/api/db"):].strip("/").split("/") if p]
        try:
            if not parts and method == "POST":
                conn = db.save_connection(body)
                self._schemas.pop(conn["name"], None)
                return handler._json(db.public(conn))
            if parts == ["test"] and method == "POST":
                # Test what is in the form before saving it (an untouched password = the saved one).
                trial = db.build_connection({**body, "name": body.get("name") or "new"})
                return handler._json(db.test_connection(trial))
            if len(parts) == 2 and parts[0] == "exports" and method == "DELETE":
                (db.exports_dir() / Path(parts[1]).name).unlink(missing_ok=True)
                return handler._json({"ok": True})
            if len(parts) == 1 and method == "DELETE":
                self._schemas.pop(db.clean_name(parts[0]), None)
                return handler._json({"ok": db.delete_connection(parts[0])})
            if len(parts) == 2 and parts[1] == "query" and method == "POST":
                conn = db.get_connection(parts[0])
                try:
                    limit = min(int(body.get("limit") or 0), DB_MAX_GRID_ROWS) or None
                    result = db.run_query(conn, str(body.get("sql") or ""), row_limit=limit,
                                          confirmed=body.get("confirmed") is True)
                except db.NeedsConfirmation as ask:
                    # Nothing ran. The page shows the statement and sends it again with confirmed=true.
                    return handler._json({"needs_confirmation": True, "statement": ask.statement,
                                          "target": db.describe(conn)})
                if result["kind"] == "write":
                    self._schemas.pop(conn["name"], None)
                return handler._json(result)
            if len(parts) == 2 and parts[1] == "export" and method == "POST":
                conn = db.get_connection(parts[0])
                what = str(body.get("what") or "table")
                fmt = "jsonl" if str(body.get("format")) in ("json", "jsonl") else "csv"
                try:
                    if what == "schema":
                        result = db.export_schema(self._db_schema(conn["name"]), db.export_name(conn["name"], "schema", "md"))
                    elif what == "database":
                        result = db.export_database(conn, self._db_schema(conn["name"]),
                                                    db.export_name(conn["name"], "all-tables", "zip"), force=body.get("force") is True)
                    elif what == "query":
                        result = db.export_query(conn, str(body.get("sql") or ""), db.export_name(conn["name"], "query", fmt), fmt)
                    else:
                        table = str(body.get("table") or "")
                        result = db.export_table(conn, self._db_schema(conn["name"]), table,
                                                 db.export_name(conn["name"], table, fmt), fmt, force=body.get("force") is True)
                except db.TooHeavy as heavy:
                    return handler._json({"too_heavy": True, "message": str(heavy)})
                result["url"] = "/api/db/exports/" + urllib.parse.quote(result["file"])
                return handler._json(result)
        except db.DBError as exc:
            return handler._json({"error": str(exc)}, 400)
        return handler._json({"error": "not found"}, 404)

    def _broadcast(self, path: str) -> int:
        count = 0
        for entry in live_sessions():
            try:
                status, _ = _session_request(entry, "POST", path, self.token, body=b"{}", timeout=3)
                count += status == 200
            except OSError:
                pass
        return count

    def _models(self) -> dict[str, Any]:
        cfg = config_from_dict(apply_env_overrides(load_raw_config()))
        if cfg is None:
            return {"models": [], "error": "not configured"}
        try:
            payload = _get_json(cfg.base_url.rstrip("/") + "/models", cfg.api_key)
        except Exception as exc:
            return {"models": [], "error": str(exc)}
        models = sorted({str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)})
        return {"models": models}

    def _proxy(self, handler: _Handler, method: str, parsed: urllib.parse.SplitResult) -> None:
        parts = parsed.path.split("/", 4)  # ['', 'api', 's', '<id>', 'rest']
        session_id = parts[3] if len(parts) > 3 else ""
        rest = "/" + (parts[4] if len(parts) > 4 else "state")
        entry = next((e for e in live_sessions() if e.get("id") == session_id), None)
        if entry is None:
            return handler._json({"error": "session not found (it may have ended)"}, 404)
        target = rest + (f"?{parsed.query}" if parsed.query else "")
        body = handler.rfile.read(int(handler.headers.get("Content-Length") or 0)) if method != "GET" else None
        streaming = rest == "/events"
        conn = http.client.HTTPConnection("127.0.0.1", int(entry["port"]), timeout=None if streaming else 30)
        try:
            conn.request(method, target, body=body,
                         headers={"X-Dcode-Token": self.token, "Content-Type": "application/json"})
            response = conn.getresponse()
            if not streaming:
                data = response.read()
                handler.send_response(response.status)
                handler.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
                return
            handler._sse_headers()
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as exc:
            try:
                handler._json({"error": f"session unreachable: {exc}"}, 502)
            except OSError:
                pass
        finally:
            conn.close()


def outer_download(handler: _Handler, record: dict[str, Any]) -> None:
    """Serve an artifact as a file download (never rendered on the hub origin)."""
    data = record["content"].encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename_for(record)}"')
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _send_file(handler: _Handler, path: Path) -> None:
    """Stream a file as a download (exports can be hundreds of MB: never read it whole)."""
    if not path.is_file():
        return handler._json({"error": "not found"}, 404)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
    handler.send_header("Content-Length", str(path.stat().st_size))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    try:
        with path.open("rb") as source:
            while chunk := source.read(256 * 1024):
                handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def slugify_id(value: str) -> str:
    """Thread ids are file stems; reject anything that could traverse paths."""
    value = urllib.parse.unquote(value)
    return "".join(c for c in value if c.isalnum() or c in "-_") or "_"


def _username() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""


def run_hub(port: int = 0) -> None:
    """Entry point for the background hub process (``dcode hub``)."""
    ensure_app_dirs()
    lock_path = run_dir() / "hub.lock"
    lock_handle = open(lock_path, "w")
    try:
        import fcntl

        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass  # non-POSIX: rely on hub.json health checks instead
    except OSError:
        return  # another hub already owns this user's UI

    def _terminate(signum, frame):  # replaced by a newer hub: exit cleanly (removes hub.json)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)
    Hub(port).serve()


def hub_main(argv: list[str]) -> None:
    """``dcode hub [--port N]`` without the terminal app's imports.

    The hub lives as long as any session does, so it shouldn't carry click, rich and
    prompt_toolkit (about 12 MB) that only the terminal uses.
    """
    port = 0
    for index, arg in enumerate(argv):
        value = arg.split("=", 1)[1] if arg.startswith("--port=") else (
            argv[index + 1] if arg == "--port" and index + 1 < len(argv) else "")
        if value.isdigit():
            port = int(value)
    run_hub(port)


# --- client side (used by a starting session) --------------------------------------------------

def hub_health(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """The hub's /api/health payload, or None if it isn't reachable."""
    if not info or not pid_alive(int(info.get("pid", 0))):
        return None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=1.5)
        conn.request("GET", "/api/health")
        response = conn.getresponse()
        body = json.loads(response.read() or b"{}") if response.status == 200 else None
        conn.close()
        return body if isinstance(body, dict) else None
    except (OSError, KeyError, ValueError):
        return None


def hub_healthy(info: dict[str, Any] | None) -> bool:
    return hub_health(info) is not None


def hub_usable(info: dict[str, Any] | None) -> bool:
    """Healthy, same Dcode version, and its web files are present."""
    health = hub_health(info)
    return bool(health) and health.get("version") == __version__ and health.get("assets", True) is not False


def _retire_hub(info: dict[str, Any] | None, wait: float = 4.0) -> None:
    """Stop an outdated or broken hub (sessions keep running; a new hub takes over)."""
    pid = int((info or {}).get("pid", 0))
    if not pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + wait
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)


def _clean_child_env() -> dict[str, str]:
    """Environment for the hub process.

    A frozen (PyInstaller one-file) app passes variables to its children that make them
    reuse the parent's unpack folder. The hub outlives the session that starts it, so it
    must unpack its own copy — otherwise its web files vanish when that session exits.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI_", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def ensure_hub(port: int = 0, wait: float = 8.0) -> str | None:
    """Return the hub URL, spawning the hub in the background if needed."""
    info = _read_json(hub_file())
    if hub_usable(info):
        return str(info["url"])  # type: ignore[index]
    if hub_healthy(info):
        _retire_hub(info)  # another Dcode version, or its files are gone: replace it
    if getattr(sys, "frozen", False):
        command = [sys.executable, "hub"]
    else:
        command = [sys.executable, "-m", "dcode", "hub"]
    if port:
        command += ["--port", str(port)]
    log = open(run_dir() / "hub.log", "ab")
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True,
                              "env": _clean_child_env()}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # survive the terminal that started it
    else:
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(command, **kwargs)
    log.close()
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.15)
        info = _read_json(hub_file())
        if hub_usable(info):
            return str(info["url"])  # type: ignore[index]
    return None


def session_url(hub_url: str, session_id: str) -> str:
    return f"{hub_url}/?token={get_token()}#s={session_id}"


def artifact_url(hub_url: str, artifact_id: str) -> str:
    """Link to an artifact's own page; logs the browser in on first use."""
    return f"{hub_url}/?token={get_token()}&next=/a/{urllib.parse.quote(artifact_id)}"


__all__ = [
    "SCOPES", "Hub", "SessionServer", "ensure_hub", "get_token", "live_sessions",
    "register_session", "run_hub", "session_url", "unregister_session",
]
