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
from .memory import SCOPES, _parse, slugify, write_memory
from .skills import list_skills, normalize_name, save_skill
from .theme import css_variables

WEB_DIR = Path(__file__).resolve().parent / "web"
HUB_IDLE_GRACE = 6.0       # seconds with zero live sessions before the hub exits
HUB_START_GRACE = 30.0     # max wait for the first session to register
COOKIE = "dcode_token"
SSE_PING = 15.0


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
                if parsed.path == "/events":
                    query = urllib.parse.parse_qs(parsed.query)
                    return outer._stream(self, int((query.get("since") or ["0"])[0] or 0))
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
            elif text:
                session.submit(text, source="web")
            return None
        if path == "/approve":
            ok = session.resolve_approval(str(body.get("id", "")), bool(body.get("approved")), by="web",
                                          scope="session" if body.get("scope") == "session" else "once")
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
            return {"ok": session.resume(str(body.get("thread_id") or ""))}
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
        if path == "/compact":
            session.compact(str(body.get("focus") or ""), source="web")
            return None
        if path == "/undo":
            result = session.undo()
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
                        self.send_response(302)
                        self.send_header(
                            "Set-Cookie", f"{COOKIE}={outer.token}; Path=/; HttpOnly; SameSite=Strict"
                        )
                        self.send_header("Location", "/")
                        self.end_headers()
                        return
                if parsed.path == "/static/logo.svg":
                    return self._static("logo.svg")
                if parsed.path in ("/guide", "/static/guide.html") and self._host_ok():
                    return self._static("guide.html")  # public docs: no token needed
                if parsed.path == "/api/health":
                    return self._json({"ok": True, "app": "dcode", "version": __version__})
                if not self._authorized():
                    return self._deny() if not parsed.path.startswith("/api/") else self._json({"error": "unauthorized"}, 401)
                if parsed.path in ("/", "/index.html"):
                    return self._static("index.html")
                if parsed.path == "/theme.css":
                    return self._raw(css_variables().encode(), "text/css")
                if parsed.path.startswith("/static/"):
                    return self._static(parsed.path[len("/static/"):])
                if parsed.path.startswith("/api/s/"):
                    return outer._proxy(self, "GET", parsed)
                return outer._api_get(self, parsed.path, query)

            def do_POST(self) -> None:
                self._mutate("POST")

            def do_PUT(self) -> None:
                self._mutate("PUT")

            def do_DELETE(self) -> None:
                self._mutate("DELETE")

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
        threading.Thread(target=self._watch, name="dcode-hub-watch", daemon=True).start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
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
            if not self._had_session:
                if now - self.started > HUB_START_GRACE:
                    break
                continue
            self._idle_since = self._idle_since or now
            if now - self._idle_since >= HUB_IDLE_GRACE:
                break
        self.httpd.shutdown()

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
        if path == "/api/artifacts":
            return handler._json(ArtifactStore().list())
        if path.startswith("/api/artifacts/"):
            rest = path[len("/api/artifacts/"):]
            artifact_id, _, action = rest.partition("/")
            record = ArtifactStore().get(urllib.parse.unquote(artifact_id))
            if record is None:
                return handler._json({"error": "not found"}, 404)
            if action == "raw":
                return outer_download(handler, record)
            data = ArtifactStore.meta(record) | {"content": record["content"]}
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
            for section in ("permissions", "experimental", "agents", "memory", "ui", "artifacts"):
                if isinstance(body.get(section), dict):
                    merged = dict(raw.get(section) or {})
                    merged.update(body[section])
                    raw[section] = merged
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
        if path.startswith("/api/artifacts/") and method == "DELETE":
            ArtifactStore().delete(urllib.parse.unquote(path.rsplit("/", 1)[1]))
            return handler._json({"ok": True})
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
        # httpx ships its own CA bundle (certifi); urllib relies on the OS store,
        # which python.org builds on macOS don't have.
        import httpx

        try:
            response = httpx.get(cfg.base_url.rstrip("/") + "/models", timeout=10.0, follow_redirects=True,
                                 headers={"Authorization": f"Bearer {cfg.api_key}"})
            response.raise_for_status()
            payload = response.json()
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
    Hub(port).serve()


# --- client side (used by a starting session) --------------------------------------------------

def hub_healthy(info: dict[str, Any] | None) -> bool:
    if not info or not pid_alive(int(info.get("pid", 0))):
        return False
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=1.5)
        conn.request("GET", "/api/health")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except (OSError, KeyError, ValueError):
        return False


def ensure_hub(port: int = 0, wait: float = 8.0) -> str | None:
    """Return the hub URL, spawning the hub in the background if needed."""
    info = _read_json(hub_file())
    if hub_healthy(info):
        return str(info["url"])  # type: ignore[index]
    if getattr(sys, "frozen", False):
        command = [sys.executable, "hub"]
    else:
        command = [sys.executable, "-m", "dcode", "hub"]
    if port:
        command += ["--port", str(port)]
    log = open(run_dir() / "hub.log", "ab")
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True}
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
        if hub_healthy(info):
            return str(info["url"])  # type: ignore[index]
    return None


def session_url(hub_url: str, session_id: str) -> str:
    return f"{hub_url}/?token={get_token()}#s={session_id}"


__all__ = [
    "SCOPES", "Hub", "SessionServer", "ensure_hub", "get_token", "live_sessions",
    "register_session", "run_hub", "session_url", "unregister_session",
]
