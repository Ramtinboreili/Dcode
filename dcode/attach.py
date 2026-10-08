"""``dcode attach``: a second terminal on a session that is already running.

The session keeps living in the process that started it. This terminal talks to it through
the hub's HTTP API (the same one the web UI uses): it follows the event stream, sends messages
and answers approvals. Leaving it detaches; nothing stops.

Because the hub only listens on 127.0.0.1, a session on a server is reached through an SSH
tunnel (``ssh -L 7300:127.0.0.1:7300 server``) and the link ``dcode ui`` prints there.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from . import __version__, ui
from .agents import MODE_LABELS, MODES
from .cli import TerminalApp
from .theme import MUTED, PRIMARY

COOKIE = "dcode_token"
POLL_SECONDS = 2.0       # how often the context meter and job list are refreshed
EXPECT_BUSY = 3.0        # after sending: seconds to wait for the session to report it started


class AttachError(Exception):
    """Could not reach or join a session; the message is for the user."""


# --- finding the session ----------------------------------------------------------------------

def parse_target(target: str) -> tuple[str, str, str]:
    """(hub base URL, token, session id) from a web UI link; empty strings for what it lacks."""
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AttachError(f"“{target}” is not a web UI link or a session id.")
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    return (f"{parsed.scheme}://{parsed.netloc}", (query.get("token") or [""])[0], (fragment.get("s") or [""])[0])


def local_hub() -> tuple[str, str]:
    """This machine's hub (base URL, token), or an error that says how to get one."""
    from . import server

    info = server._read_json(server.hub_file())
    if not server.hub_healthy(info):
        raise AttachError("No Dcode session is running on this machine (start one with: dcode). "
                          "For a session elsewhere, pass its web UI link.")
    return str(info["url"]), server.get_token()


class Remote:
    """The hub's HTTP API, with the user's token."""

    def __init__(self, base: str, token: str) -> None:
        parsed = urllib.parse.urlsplit(base)
        self.base = base.rstrip("/")
        self.token = token
        self._secure = parsed.scheme == "https"
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or (443 if self._secure else 80)

    def _connect(self, timeout: float | None) -> http.client.HTTPConnection:
        kind = http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
        return kind(self._host, self._port, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Cookie": f"{COOKIE}={self.token}", "X-Dcode": "1", "Content-Type": "application/json",
                "User-Agent": f"dcode-attach/{__version__}"}

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        conn = self._connect(timeout)
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=self._headers())
            response = conn.getresponse()
            raw = response.read()
        except OSError as exc:
            raise AttachError(f"Can't reach Dcode at {self.base}: {exc}") from exc
        finally:
            conn.close()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if response.status == 401:
            raise AttachError("The access token was refused. Copy the link again from `dcode ui` on that machine.")
        if response.status >= 400:
            raise AttachError(str(data.get("error") if isinstance(data, dict) else "") or f"HTTP {response.status}")
        return data

    def sessions(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/api/sessions")
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def stream(self, session_id: str, since: int):
        """Yield the session's events from ``since`` on. Returns when the stream ends."""
        conn = self._connect(None)
        try:
            conn.request("GET", f"/api/s/{session_id}/events?since={since}", headers=self._headers())
            response = conn.getresponse()
            if response.status != 200:
                raise AttachError("gone" if response.status == 404 else f"HTTP {response.status}")
            for raw in response:
                if raw.startswith(b"data: "):
                    try:
                        yield json.loads(raw[6:])
                    except ValueError:
                        continue
        except OSError as exc:
            raise AttachError(str(exc)) from exc
        finally:
            conn.close()


# --- the session, seen from here ----------------------------------------------------------------

class _Bus:
    def __init__(self) -> None:
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self.listeners.append(listener)


class RemoteSession:
    """What the terminal needs from a session, answered over HTTP instead of in-process."""

    def __init__(self, remote: Remote, session_id: str) -> None:
        self.remote = remote
        self.id = session_id
        self.bus = _Bus()
        self.ended = False
        self.sent: list[str] = []          # messages typed here, so their echo isn't shown twice
        self.jobs = SimpleNamespace(running=lambda: [], list=lambda: [])
        self.agent = SimpleNamespace(specs={})
        self._attention: list[Callable[[], None]] = []
        self._approvals: dict[str, dict[str, Any]] = {}
        self._busy = False
        self._expect_busy = 0.0
        self._state: dict[str, Any] = {}
        self._apply_state(self._get("/state"))
        self.seq = int(self._state.get("seq") or 0)
        for approval in self._state.get("approvals") or []:
            self._approvals[approval["id"]] = approval
        threading.Thread(target=self._follow, name="dcode-attach-events", daemon=True).start()
        threading.Thread(target=self._poll, name="dcode-attach-state", daemon=True).start()

    # --- plumbing
    def _get(self, path: str) -> Any:
        return self.remote.request("GET", f"/api/s/{self.id}{path}")

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.remote.request("POST", f"/api/s/{self.id}{path}", {**(body or {}), "source": "attach"})

    def _apply_state(self, state: dict[str, Any]) -> None:
        self._state = state
        self._busy = bool(state.get("busy"))
        self.cwd = str(state.get("cwd") or "")
        self.mode = str(state.get("mode") or "accept_edits")
        self.thread_name = str(state.get("thread_name") or "")
        self.plan_pending = bool(state.get("plan_pending"))
        self.git = state.get("git") or {"is_repo": False}
        self.config = SimpleNamespace(
            model=str(state.get("model") or ""), base_url="", reasoning_effort=str(state.get("reasoning_effort") or ""),
            context_window=int(state.get("context_window") or 0), agents={"enabled": bool(state.get("agents_enabled", True))},
            experimental={}, ui=self._prefs())

    @staticmethod
    def _prefs() -> dict[str, Any]:
        """How *this* terminal shows things: its own machine's /thinking, /details and /notify."""
        from .config import DEFAULT_UI, load_raw_config

        try:
            return {**DEFAULT_UI, **(load_raw_config().get("ui") or {})}
        except Exception:
            return dict(DEFAULT_UI)

    def _wake(self) -> None:
        for callback in list(self._attention):
            try:
                callback()
            except Exception:
                pass

    def _follow(self) -> None:
        while not self.ended:
            try:
                for event in self.remote.stream(self.id, self.seq):
                    self.seq = max(self.seq, int(event.get("seq") or 0))
                    self._on_event(event)
            except AttachError as exc:
                if str(exc) == "gone":
                    break
            # The stream dropped (hub restart, tunnel hiccup): is the session still there?
            time.sleep(1.0)
            if self._gone():
                break
        self.ended = True
        self._wake()

    def _gone(self) -> bool:
        """True when the hub answers and no longer lists this session (unreachable is not gone)."""
        try:
            return not any(s.get("id") == self.id for s in self.remote.sessions())
        except AttachError:
            return False

    def _poll(self) -> None:
        while not self.ended:
            time.sleep(POLL_SECONDS)
            try:
                state = self._get("/state")
            except AttachError:
                if self._gone():  # the stream can stay open after the session's process is gone
                    self.ended = True
                    self._wake()
                continue
            self._state = state
            if time.time() > self._expect_busy and bool(state.get("busy")) != self._busy:
                self._busy = bool(state.get("busy"))  # an event was missed (the stream reconnected)
                self._wake()

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        wake = False
        if kind == "status":
            self._busy = bool(event.get("busy"))
            self._expect_busy = 0.0
            wake = True
        elif kind == "approval_request":
            self._approvals[event["id"]] = event
        elif kind == "approval_resolved":
            self._approvals.pop(event.get("id"), None)
            wake = True
        elif kind == "mode":
            self.mode = str(event.get("mode") or self.mode)
        elif kind == "plan_ready":
            self.plan_pending, wake = True, True
        elif kind == "plan_resolved":
            self.plan_pending, wake = False, True
        elif kind == "thread":
            self.thread_name = str(event.get("name") or self.thread_name)
        elif kind == "config":
            self.config.model = str(event.get("model") or self.config.model)
            self.config.context_window = int(event.get("context_window") or self.config.context_window)
        elif kind == "user_message" and event.get("source") != "attach":
            wake = True  # someone else started a task: leave the input box and watch it
        for listener in list(self.bus.listeners):
            try:
                listener(event)
            except Exception:
                pass
        if wake:
            self._wake()

    # --- what TerminalApp asks of a session
    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def idle(self) -> bool:
        return not self._busy and time.time() > self._expect_busy

    def pending_approvals(self) -> list[dict[str, Any]]:
        return list(self._approvals.values())

    def on_attention(self, callback: Callable[[], None]) -> None:
        self._attention.append(callback)

    def custom_commands(self) -> dict[str, Any]:
        return {c["name"]: SimpleNamespace(name=c["name"], description=c.get("description", ""), scope=c.get("scope", ""))
                for c in self._state.get("commands") or [] if isinstance(c, dict) and c.get("name")}

    def context(self) -> tuple[int, int]:
        return int(self._state.get("context_used") or 0), int(self._state.get("context_window") or 0)

    def _safely(self, path: str, body: dict[str, Any] | None = None, echo: str = "", starts_task: bool = False) -> Any:
        """POST, and show a failure as an error line instead of ending the terminal."""
        if echo:
            self.sent.append(echo)
            del self.sent[:-20]
        if starts_task:
            self._expect_busy = time.time() + EXPECT_BUSY
        try:
            return self._post(path, body)
        except AttachError as exc:
            self._expect_busy = 0.0
            for listener in list(self.bus.listeners):
                listener({"type": "error", "text": f"Not sent: {exc}"})
            return {}

    def _send(self, text: str) -> None:
        self._safely("/message", {"text": text}, echo=text, starts_task=True)

    def submit(self, text: str, source: str = "attach") -> None:
        self._send(text.strip())

    def run_shell(self, command: str, source: str = "attach") -> None:
        self._send("!" + command.strip())

    def review(self, source: str = "attach") -> None:
        self._send("/review")

    def interrupt(self, source: str = "attach") -> None:
        self._safely("/interrupt")

    def resolve_approval(self, approval_id: str, approved: bool, by: str = "attach", scope: str = "once",
                         answer: str = "") -> bool:
        self._approvals.pop(approval_id, None)
        return bool(self._safely("/approve", {"id": approval_id, "approved": approved, "scope": scope, "answer": answer}).get("ok"))

    def set_mode(self, mode: str, source: str = "attach") -> None:
        self.mode = str(self._post("/mode", {"mode": mode}).get("mode") or mode)

    def cycle_mode(self, source: str = "attach") -> str:
        wanted = MODES[(MODES.index(self.mode) + 1) % len(MODES)] if self.mode in MODES else MODES[0]
        self.mode = str(self._safely("/mode", {"mode": wanted}).get("mode") or self.mode)  # runs inside a key handler
        return self.mode

    def resolve_plan(self, decision: str, source: str = "attach") -> None:
        self.plan_pending = False
        self._safely("/plan", {"decision": decision}, starts_task=decision != "keep",
                     echo="" if decision == "keep" else "The plan is approved. Implement it now, step by step.")

    def command(self, path: str, body: dict[str, Any] | None = None, echo: str = "") -> Any:
        """A slash command; ``echo`` is the line the session will announce for it (a task starts)."""
        if echo:
            self.sent.append(echo)
            self._expect_busy = time.time() + EXPECT_BUSY
        return self._post(path, body)


# --- the terminal -----------------------------------------------------------------------------

ATTACH_COMMANDS = {
    "/help": "Show commands",
    "/mode": "Switch mode: manual · accept edits · auto · plan",
    "/review": "Review the git diff (sub-agent)",
    "/compact": "Summarize the session to free context: /compact [focus]",
    "/undo": "Undo Dcode's file changes from the last task",
    "/redo": "Bring back the changes the last /undo removed",
    "/init": "Write AGENTS.md for the project",
    "/new": "Save and start a new chat in that session",
    "/rename": "Rename the chat",
    "/export": "Save the chat here as a file: /export [md|html|json] [file]",
    "/thinking": "Show or hide the model's reasoning (this terminal)",
    "/details": "Show or hide tool calls and results (this terminal)",
    "/ui": "Print the web UI link",
    "/exit": "Detach (the session keeps running)",
}


class AttachApp(TerminalApp):
    surface = "attach"

    def __init__(self, session: RemoteSession, ui_url: str) -> None:
        super().__init__(session, ui_url)  # type: ignore[arg-type]

    def _mine(self, event: dict[str, Any], key: str) -> bool:
        if event.get("type") == "user_message":  # another attached terminal has the same tag: compare the text
            sent = self.session.sent
            if event.get("source") == "attach" and event.get("text") in sent:
                sent.remove(event["text"])
                return True
            return False
        return event.get(key) == "attach"

    def run(self, recap: bool = False) -> None:
        ui.COMMANDS, saved = dict(ATTACH_COMMANDS), ui.COMMANDS
        try:
            super().run()
        finally:
            ui.COMMANDS = saved

    def _draw_home(self) -> None:
        session = self.session
        ui.console.print()
        ui.console.print(f"  [bold {PRIMARY}]❯[/] [bold]Dcode[/] [{MUTED}]v{__version__} · attached to session {session.id}[/]")
        ui.console.print(f"  [{MUTED}]{ui._escape(ui.pretty_thread(session.thread_name))} · {ui._escape(session.config.model)} · "
                         f"{ui._escape(ui.short_path(session.cwd))}[/]")
        ui.console.print(f"  [{PRIMARY}]▎[/] [{MUTED}]Mode: {MODE_LABELS.get(session.mode, session.mode)} · what you type runs in that "
                         f"session · /exit or Ctrl+C twice detaches, nothing stops[/]")
        if session.busy:
            ui.notice("Dcode is working there right now; you'll see it continue.")

    def status_text(self) -> str:
        used, window = self.session.context()
        pct = min(100, int(100 * used / max(1, window)))
        filled = round(8 * pct / 100)
        left = (f"attached · {ui.pretty_thread(self.session.thread_name)[:32]}  ·  {self.session.config.model[:28]}  ·  "
                f"ctx {'▰' * filled}{'▱' * (8 - filled)} {pct}%")
        return self._status_line(left, ["shift+tab mode", "ctrl+c ×2 detach", "ctrl+o expand"])

    def _command(self, text: str) -> str | None:
        command, _, arg = text.partition(" ")
        arg = arg.strip()
        session: RemoteSession = self.session  # type: ignore[assignment]
        try:
            if command == "/exit":
                return "exit"
            if command == "/help":
                ui.show_help()
            elif command == "/mode":
                wanted = arg.lower().replace(" ", "_").replace("-", "_")
                wanted = {"accept": "accept_edits", "edits": "accept_edits"}.get(wanted, wanted)
                if not wanted:
                    labels = [MODE_LABELS[m] for m in MODES]
                    picked = ui.select_one("Mode", labels, MODE_LABELS.get(session.mode))
                    wanted = next((m for m in MODES if MODE_LABELS[m] == picked), session.mode)
                session.set_mode(wanted)
                ui.notice(f"Mode: {MODE_LABELS.get(session.mode, session.mode)}", "success")
            elif command == "/review":
                session.review()
                self._await_start()
            elif command == "/compact":
                session.command("/compact", {"focus": arg}, echo="/compact" + (f" {arg}" if arg else ""))
                self._await_start()
            elif command == "/init":
                done = session.command("/init", echo="/init")
                ui.notice(f"Dcode will look around the project and write {done.get('file', 'AGENTS.md')}.")
                self._await_start()
            elif command in ("/undo", "/redo"):
                done = session.command(command)
                if not done.get("ok"):
                    ui.notice(f"Nothing to {command[1:]}.")
            elif command == "/new":
                session.command("/new")
            elif command == "/rename":
                name = arg or ui.prompt_input("Chat name", default=session.thread_name)
                if name:
                    session.command("/rename", {"name": name})
            elif command == "/export":
                self._export(arg)
            elif command in ("/thinking", "/details"):
                key = "show_thinking" if command == "/thinking" else "show_details"
                prefs = session.config.ui
                prefs[key] = arg == "on" if arg in ("on", "off") else not prefs.get(key, key == "show_details")
                what = "The model's reasoning is" if key == "show_thinking" else "Tool calls and results are"
                ui.notice(f"{what} {'shown' if prefs[key] else 'hidden'} in this terminal.", "success")
            elif command == "/ui":
                ui.console.print(f"{ui.PAD}[{MUTED}]Web UI[/]  {ui.link(self.ui_url or '')}", soft_wrap=True)
            elif command[1:].lower() in session.custom_commands():
                session.command("/command", {"name": command[1:], "args": arg}, echo=command + (f" {arg}" if arg else ""))
                self._await_start()
            else:
                ui.notice(f"{command} isn't available from an attached terminal. /help lists what is; "
                          "the rest is in the session's own terminal or the web UI.", "warning")
        except AttachError as exc:
            ui.notice(str(exc), "error")
        except (KeyboardInterrupt, ui.PromptInterrupted):
            pass
        return None

    def _export(self, arg: str) -> None:
        from pathlib import Path

        from .config import slugify_thread_name
        from .transcripts import FORMATS, export_name

        fmt, _, target = arg.partition(" ")
        fmt = fmt.lower().lstrip(".") or "md"
        if fmt not in FORMATS:
            ui.notice(f"Usage: /export [{'|'.join(FORMATS)}] [file]", "warning")
            return
        session: RemoteSession = self.session  # type: ignore[assignment]
        thread_id = urllib.parse.quote(slugify_thread_name(session.thread_name))
        conn = session.remote._connect(30)
        try:
            conn.request("GET", f"/api/threads/{thread_id}/export?format={fmt}", headers=session.remote._headers())
            response = conn.getresponse()
            data = response.read()
        except OSError as exc:
            ui.notice(f"Couldn't export: {exc}", "error")
            return
        finally:
            conn.close()
        if response.status != 200:
            ui.notice("Nothing to export yet: the chat is saved after its first task.", "warning")
            return
        path = Path(target.strip()).expanduser() if target.strip() else Path(export_name(session.thread_name, fmt))
        path.write_bytes(data)
        ui.notice(f"Exported the chat ({len(data):,} bytes) to {path}.", "success")


# --- entry point ------------------------------------------------------------------------------

def _label(entry: dict[str, Any]) -> str:
    state = "needs an answer" if entry.get("approvals") else "working" if entry.get("busy") else "idle"
    return (f"{entry.get('id', ''):<9} {ui.pretty_thread(str(entry.get('thread_name') or ''))[:36]:<36}  "
            f"{ui.short_path(str(entry.get('cwd') or ''))[:34]:<34}  {state}")


def resolve(target: str = "") -> tuple[Remote, str]:
    """Which hub and which session ``dcode attach [target]`` means (asks when several match)."""
    base = token = wanted = ""
    if "://" in target:
        base, token, wanted = parse_target(target)
        if not token:
            raise AttachError("That link has no access token. Use the full link `dcode ui` prints (it contains ?token=…).")
    else:
        base, token = local_hub()
        wanted = target.strip()
    remote = Remote(base, token)
    sessions = remote.sessions()
    if not sessions:
        raise AttachError("No session is running there.")
    if wanted:
        matches = [s for s in sessions if str(s.get("id", "")).startswith(wanted)]
        if len(matches) != 1:
            known = ", ".join(str(s.get("id")) for s in sessions)
            raise AttachError(f"No session “{wanted}” there. Running: {known}.")
        return remote, str(matches[0]["id"])
    if len(sessions) == 1:
        return remote, str(sessions[0]["id"])
    labels = [_label(s) for s in sessions]
    picked = ui._selector("Attach to which session?", labels, cancel_result=None)
    if not isinstance(picked, int):
        raise AttachError("Nothing picked.")
    return remote, str(sessions[picked]["id"])


def main(target: str = "") -> int:
    try:
        remote, session_id = resolve(target)
        session = RemoteSession(remote, session_id)
    except AttachError as exc:
        ui.console.print(f"[{MUTED}]dcode attach:[/] {ui._escape(str(exc))}")
        return 1
    url = f"{remote.base}/?token={remote.token}#s={session_id}"
    try:
        AttachApp(session, url).run()
    except AttachError as exc:
        ui.console.print(f"[{MUTED}]dcode attach:[/] {ui._escape(str(exc))}")
    finally:
        ended, session.ended = session.ended, True
    ui.console.print(f"[{PRIMARY}]{'The session ended.' if ended else 'Detached.'}[/] "
                     + ("" if ended else "The session keeps running."))
    return 0
