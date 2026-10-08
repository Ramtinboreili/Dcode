#!/usr/bin/env python3
"""End-to-end test: drive the real `dcode` in a pseudo-terminal against the mock model.

This catches what unit tests can't: prompt_toolkit key handling, the approval menu,
the web hub spawned as a separate process, PyInstaller packaging issues, etc.

    python docs/dev/terminal_e2e.py                              # repo venv (.venv/bin/dcode)
    python docs/dev/terminal_e2e.py --binary /opt/homebrew/bin/dcode   # a built binary

macOS/Linux only (uses pty). Each check prints PASS/FAIL; exit code = number of failures.
Never opens a real browser (BROWSER=true) and uses a throwaway DCODE_HOME.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pty
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MOCK = Path(__file__).resolve().parent / "mock_llm.py"
FAKE_MCP = REPO / "tests" / "fixtures" / "fake_mcp_server.py"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\r")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Terminal:
    def __init__(self, binary: str, env: dict, cwd: Path, args: tuple[str, ...] = ()) -> None:
        self.buf = ""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execve(binary, [binary, *args], env)

    def read(self, seconds: float = 0.3) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    self.buf += os.read(self.fd, 65536).decode(errors="replace")
                except OSError:
                    return

    def text(self) -> str:
        return ANSI.sub("", self.buf)

    def expect(self, pattern: str, timeout: float = 12) -> re.Match | None:
        end = time.time() + timeout
        while time.time() < end:
            self.read(0.2)
            match = re.search(pattern, self.text())
            if match:
                return match
        return None

    def send(self, data: str | bytes) -> None:
        os.write(self.fd, data.encode() if isinstance(data, str) else data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=str(REPO / ".venv" / "bin" / "dcode"))
    parser.add_argument("--verbose", action="store_true", help="print the screen tail when a check fails")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="dcode-e2e-"))
    work = tmp / "work"
    work.mkdir()
    (work / "app.py").write_text("print('hi')\n")
    import sqlite3

    stock = sqlite3.connect(work.parent / "stock.db")  # a real database for the /db checks
    stock.executescript("CREATE TABLE parts (id INTEGER PRIMARY KEY, name TEXT);"
                        "INSERT INTO parts VALUES (1, 'bolt'), (2, 'nut'), (3, 'gear');")
    stock.commit()
    stock.close()
    (work / ".dcode" / "commands").mkdir(parents=True)
    (work / ".dcode" / "commands" / "hello.md").write_text("---\ndescription: Greet someone\n---\nSay hello to $ARGUMENTS")
    editor = tmp / "fake-editor"  # stands in for $EDITOR in the /editor check
    editor.write_text('#!/bin/sh\nprintf "written in the editor" > "$1"\n')
    editor.chmod(0o755)
    port = free_port()
    mock = subprocess.Popen([sys.executable, str(MOCK), str(port)], stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    env = dict(os.environ, DCODE_HOME=str(tmp / "home"), DCODE_BASE_URL=f"http://127.0.0.1:{port}/v1",
               DCODE_API_KEY="local", DCODE_MODEL="mock-coder-7b", DCODE_CONTEXT_WINDOW="16384",
               TERM="xterm-256color", PROMPT_TOOLKIT_NO_CPR="1", BROWSER="true", PYTHONPATH=str(REPO),
               EDITOR=str(editor), VISUAL="")
    failures = 0

    def check(name: str, ok: object) -> None:
        nonlocal failures
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        failures += 0 if ok else 1
        if not ok and args.verbose:
            print("    " + term.text()[-1500:].replace("\n", "\n    "), flush=True)

    term = Terminal(args.binary, env, work)
    try:
        url = term.expect(r"(http://127\.0\.0\.1:\d+/\?token=[\w-]+#s=\w+)", timeout=20)
        check("banner shows the web UI link", url)
        if not url:
            print(term.text()[-3000:])
            return failures + 1
        hub_port = int(re.search(r":(\d+)/", url.group(1)).group(1))
        token = re.search(r"token=([\w-]+)", url.group(1)).group(1)
        session_id = url.group(1).split("#s=")[1]

        def hub(method: str, path: str, body: dict | None = None):
            conn = http.client.HTTPConnection("127.0.0.1", hub_port, timeout=10)
            conn.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers={"Cookie": f"dcode_token={token}", "X-Dcode": "1", "Content-Type": "application/json"})
            response = conn.getresponse()
            data = response.read()
            conn.close()
            return response.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)

        term.read(0.8)
        term.send("say hello\r")
        check("streamed reply, <think> stripped",
              term.expect(r"Echo: say hello") and "hidden reasoning" not in term.text())
        check("end-of-turn line", term.expect(r"Done in \d"))
        term.send("list files\r")
        check("tool call runs", term.expect(r"list_files") and term.expect(r"Done\. Tool .?list_files"))
        term.send("/version\r")
        check("/version", term.expect(r"Dcode\s+\d+\.\d+\.\d+"))
        term.send(b"\x1b[Z")
        check("shift+tab cycles mode", term.expect(r"◆ auto mode", timeout=4))
        for _ in range(3):  # back to accept edits: auto → plan → manual → accept edits
            term.send(b"\x1b[Z")
            term.read(0.3)
        status, page = hub("GET", "/")
        check("hub serves the web UI", status == 200 and b"app.js" in page)
        status, guide = hub("GET", "/guide")
        check("hub serves the bilingual guide", status == 200 and "راهنمای Dcode".encode() in guide)
        hub("POST", f"/api/s/{session_id}/message", {"text": "run it"})
        check("web message echoed in the terminal", term.expect(r"run it.*\(from the web UI\)"))
        check("approval menu in the terminal", term.expect(r"Dcode wants to run a command"))
        _, state = hub("GET", f"/api/s/{session_id}/state")
        approval = state["approvals"][0]
        hub("POST", f"/api/s/{session_id}/approve", {"id": approval["id"], "approved": True})
        check("approval answered from the web", term.expect(r"Approved from the web UI")
              and term.expect(r"Done\. Tool .?run_command"))
        term.send("run it\r")
        term.expect(r"Dcode wants to run a command")
        term.read(0.4)
        term.send("y")
        check("approval answered in the terminal (one decision line)", term.expect(r"✓ Allowed · echo hi-from-tool"))
        term.expect(r"Done in")
        term.read(0.4)
        term.send(b"\x0f")
        check("ctrl+o expands outputs", term.expect(r"run_command\s+echo hi-from-tool"))
        term.send("explore the repo\r")
        check("helper delegation", term.expect(r"◆ explore-\d+") and term.expect(r"explore-\d+ finished"))
        term.send("look at @ap")
        check("@ suggests project files", term.expect(r"@app\.py\s+file", timeout=4))
        term.send("\t")
        term.read(0.3)
        term.send("please\r")
        check("@file is attached to the message", term.expect(r"\+ attached app\.py")
              and term.expect(r"Done\. Tool .?read_file"))
        term.send("make a checklist\r")
        check("todo checklist is drawn", term.expect(r"Checklist\s+1/2 done") and term.expect(r"▸ Write the fix"))
        term.send("ask me something\r")
        check("question menu in the terminal", term.expect(r"\?\s+Which database\?"))
        term.read(0.4)
        term.send("\r")
        check("question answered", term.expect(r"Which database\? →\s*Postgres") and term.expect(r"Done\. Tool .?ask_user"))
        term.send("break the syntax\r")
        check("post-edit check reports the error", term.expect(r"check found 1 problem"))
        term.send('/rules deny "rm -rf *"\r')
        check("/rules saves a rule", term.expect(r"Rule saved: deny command"))
        term.send("/hello bob\r")
        check("custom /command runs", term.expect(r"Echo: Say hello to bob"))
        term.send("@explore find the entry point\r")
        check("@helper runs directly", term.expect(r"◆ explore-\d+ +find the entry point")
              and term.expect(r"explore-\d+ finished") and term.expect(r"✓ Done in [\d.]+s · 1 helper"))
        term.send("!echo own-$((20+3))\r")
        check("!command runs your own shell command, no approval", term.expect(r"╰─ own-23"))
        term.send("/thinking on\r")
        term.expect(r"reasoning is shown")
        term.send("say goodbye\r")
        check("/thinking shows the model's reasoning", term.expect(r"✻ thinking[\s\S]{0,160}hidden reasoning")
              and term.expect(r"Echo: say goodbye"))
        term.expect(r"Done in")
        term.send("/thinking off\r")
        term.send("/details off\r")
        term.expect(r"Tool calls and results are hidden")
        mark = len(term.text())
        term.send("list files\r")
        check("/details off hides tool calls", term.expect(r"Done\. Tool .?list_files[\s\S]*Done in")
              and "◇" not in term.text()[mark:])
        term.send("/details on\r")
        term.expect(r"Tool calls and results are shown")
        term.send("/editor\r")
        check("/editor sends what you wrote in $EDITOR", term.expect(r"Echo: written in the editor"))
        term.expect(r"Done in")
        term.send(f"/db add stock sqlite:///{work.parent / 'stock.db'}\r")
        check("/db add saves and tests a connection", term.expect(r"Saved stock and connected"))
        term.send("/db stock\r")
        check("/db lists tables with sizes", term.expect(r"parts · 2 cols, ~3 rows"))
        term.send("/db stock SELECT name FROM parts ORDER BY id\r")
        check("/db runs a query", term.expect(r"bolt\s+nut\s+gear\s+3 rows"))
        term.send("count the parts\r")
        check("the model queries the database", term.expect(r"db_query\s+stock: SELECT count") and term.expect(r"Done\. Tool .?db_query"))
        term.send(b"\x1b[Z")  # auto mode: even there a database change must ask
        term.expect(r"◆ auto mode", timeout=4)
        term.send("delete the old parts\r")
        check("a database change asks even in auto mode", term.expect(r"Dcode wants to change a database")
              and term.expect(r"asked every time"))
        term.read(0.4)
        term.send("n")
        check("a denied change is not run", term.expect(r"✕ Denied · DELETE FROM parts") and term.expect(r"Done in"))
        _, rows = hub("POST", "/api/db/stock/query", {"sql": "SELECT count(*) FROM parts"})
        check("the row is still there (web API)", rows.get("rows") == [[3]])
        for _ in range(3):  # back to accept edits
            term.send(b"\x1b[Z")
            term.read(0.3)
        term.send(f"/mcp add notes {sys.executable} {FAKE_MCP}\r")
        check("/mcp add starts a local MCP server", term.expect(r"notes\s+ready · 6 tools", timeout=20))
        term.send("use the notebook\r")
        check("an external tool asks first", term.expect(r"Dcode wants to use an external tool")
              and term.expect(r"notes · add"))
        term.read(0.4)
        term.send("y")
        check("the external tool runs", term.expect(r"✓ Allowed · notes · add") and term.expect(r"Done\. Tool .?mcp_call"))
        term.expect(r"Done in")
        term.send("/mcp rm notes\r")
        check("/mcp rm", term.expect(r"Removed the MCP server notes"))
        term.send('/cron add "0 0 1 1 *" echo hello-from-cron\r')
        check("/cron add schedules a job", term.expect(r"Scheduled echo-hello-from-cron: every year on January 1[\s\S]{0,40}next\s+run\s+\d{4}-01-01"))
        term.send("/cron run echo-hello-from-cron\r")
        term.expect(r"Starting echo-hello-from-cron now")
        log = ""
        for _ in range(60):  # the hub's scheduler picks the request up within a few seconds
            _, runs = hub("GET", "/api/cron/echo-hello-from-cron/runs")
            if runs and runs[0]["status"] == "ok":
                _, body = hub("GET", f"/api/cron/echo-hello-from-cron/log?run={runs[0]['id']}")
                log = body["text"]
                break
            time.sleep(0.25)
        check("the hub runs the job and keeps its log", "hello-from-cron" in log and "exit code 0" in log)
        term.send("/cron\r")
        check("/cron shows the last result", term.expect(r"echo-hello-from-cron\s+every year on January 1 at 00:00[\s\S]{0,60}last\s+ok"))
        term.send("/cron rm echo-hello-from-cron\r")  # an enabled job would keep the hub alive (checked below)
        check("/cron rm", term.expect(r"Removed the job echo-hello-from-cron"))
        term.send("tell me about this project and make an artifact\r")
        check("follow-through after an announced-but-skipped artifact",
              term.expect(r"Reminding the model to follow through") and term.expect(r"◆ Artifact"))
        term.send("loop: make the repo report artifact\r")
        check("re-save loop capped, one artifact", term.expect(r"already saved — skipped", timeout=20))
        _, artifacts = hub("GET", "/api/artifacts")
        check("no 'Untitled' artifacts", not any(a["title"] == "Untitled" for a in artifacts))
        term.send("/export html\r")
        exported = term.expect(r"Exported this chat \([\d,]+ characters\) to dcode-[\w-]+\.html")
        page = next(iter(work.glob("dcode-*.html")), None)
        check("/export writes a self-contained page", exported and page is not None
              and "<script" not in page.read_text() and "Echo: say hello" in page.read_text())
        term.send("/compact\r")
        check("/compact", term.expect(r"Compacted the conversation"))
        term.send(b"\x03")
        check("first ctrl+c arms exit", term.expect(r"Press Ctrl\+C again to exit", timeout=3))
        term.send(b"\x03")
        check("second ctrl+c exits", term.expect(r"Bye\.", timeout=8))
        os.waitpid(term.pid, 0)
        hub_file = tmp / "home" / "run" / "hub.json"
        for _ in range(40):
            if not hub_file.exists():
                break
            time.sleep(0.25)
        check("hub shuts down after the last session", not hub_file.exists())
        check("thread saved", any((tmp / "home" / "threads").glob("*.json")))
        term = Terminal(args.binary, env, work, ("--continue",))
        check("dcode --continue picks up this folder's last chat",
              term.expect(r"Continuing “[^”]+”", timeout=20) and term.expect(r"▎ "))
        term.read(0.8)
        term.send("/new\r")
        term.expect(r"Chat new")
        term.send("/sessions\r")
        check("/sessions lists saved chats", term.expect(r"Chats — enter continues one[\s\S]*this folder"))
        term.read(0.4)
        term.send("\r")
        check("/sessions continues the picked chat", term.expect(r"Chats — enter[\s\S]*Continuing “[^”]+”[\s\S]*▎ "))
        term.send(b"\x03")
        term.expect(r"Press Ctrl\+C again to exit", timeout=3)
        term.send(b"\x03")
        term.expect(r"Bye\.", timeout=8)
    finally:
        try:
            os.kill(term.pid, 9)
        except OSError:
            pass
        mock.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'all checks passed' if not failures else f'{failures} check(s) failed'}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
