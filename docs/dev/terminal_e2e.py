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
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\r")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Terminal:
    def __init__(self, binary: str, env: dict, cwd: Path) -> None:
        self.buf = ""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execve(binary, [binary], env)

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
    (work / ".dcode" / "commands").mkdir(parents=True)
    (work / ".dcode" / "commands" / "hello.md").write_text("---\ndescription: Greet someone\n---\nSay hello to $ARGUMENTS")
    port = free_port()
    mock = subprocess.Popen([sys.executable, str(MOCK), str(port)], stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    env = dict(os.environ, DCODE_HOME=str(tmp / "home"), DCODE_BASE_URL=f"http://127.0.0.1:{port}/v1",
               DCODE_API_KEY="local", DCODE_MODEL="mock-coder-7b", DCODE_CONTEXT_WINDOW="16384",
               TERM="xterm-256color", PROMPT_TOOLKIT_NO_CPR="1", BROWSER="true", PYTHONPATH=str(REPO))
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
        term.send("tell me about this project and make an artifact\r")
        check("follow-through after an announced-but-skipped artifact",
              term.expect(r"Reminding the model to follow through") and term.expect(r"◆ Artifact"))
        term.send("loop: make the repo report artifact\r")
        check("re-save loop capped, one artifact", term.expect(r"already saved — skipped", timeout=20))
        _, artifacts = hub("GET", "/api/artifacts")
        check("no 'Untitled' artifacts", not any(a["title"] == "Untitled" for a in artifacts))
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
