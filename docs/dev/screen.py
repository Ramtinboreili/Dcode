#!/usr/bin/env python3
"""Render exactly what a terminal shows while Dcode runs (layout and colors).

Raw pty captures lie: cursor movement is stripped, so in-place redraws look like
duplicated output. This runs `dcode` in a sized pty, feeds the bytes to a terminal
emulator (pyte) and prints the final screen — optionally as HTML with colors,
which you can screenshot (e.g. headless Chrome) to check styling.

    pip install pyte            # dev-only, not a Dcode dependency
    python docs/dev/screen.py --type "say hello" --type "list files"
    python docs/dev/screen.py --cols 110 --rows 30 --html /tmp/screen.html

Uses the mock model (docs/dev/mock_llm.py) and a throwaway DCODE_HOME.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import os
import pty
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

import pyte

REPO = Path(__file__).resolve().parents[2]
MOCK = Path(__file__).resolve().parent / "mock_llm.py"
NAMED = {"black": "#000", "red": "#cd3131", "green": "#0dbc79", "brown": "#e5e510", "blue": "#2472c8",
         "magenta": "#bc3fbc", "cyan": "#11a8cd", "white": "#e5e5e5"}


def color(value: str, fallback: str) -> str:
    if value in (None, "default"):
        return fallback
    if value in NAMED:
        return NAMED[value]
    return f"#{value}" if len(value) == 6 else fallback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=str(REPO / ".venv" / "bin" / "dcode"))
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--rows", type=int, default=34)
    parser.add_argument("--type", action="append", default=[], help="a message to send (repeatable)")
    parser.add_argument("--wait", type=float, default=4.0, help="seconds to wait after each message")
    parser.add_argument("--html", help="also write the screen with colors to this HTML file")
    args = parser.parse_args()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    tmp = Path(tempfile.mkdtemp(prefix="dcode-screen-"))
    (tmp / "work").mkdir()
    mock = subprocess.Popen([sys.executable, str(MOCK), str(port)], stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    env = dict(os.environ, DCODE_HOME=str(tmp / "home"), DCODE_BASE_URL=f"http://127.0.0.1:{port}/v1",
               DCODE_API_KEY="x", DCODE_MODEL="qwen2.5-coder:7b", DCODE_CONTEXT_WINDOW="32768",
               TERM="xterm-256color", COLORTERM="truecolor", PROMPT_TOOLKIT_NO_CPR="1", BROWSER="true",
               PYTHONPATH=str(REPO))
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(tmp / "work")
        os.execve(args.binary, [args.binary], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", args.rows, args.cols, 0, 0))
    screen = pyte.Screen(args.cols, args.rows)
    stream = pyte.ByteStream(screen)

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    stream.feed(os.read(fd, 65536))
                except OSError:
                    return

    try:
        pump(8)  # the one-file binary unpacks itself and starts the hub
        for message in args.type:
            os.write(fd, message.encode() + b"\r")
            pump(args.wait)
        print("\n".join(line.rstrip() for line in screen.display))
        if args.html:
            rows = []
            for y in range(args.rows):
                cells = []
                for x in range(args.cols):
                    ch = screen.buffer[y][x]
                    fg, bg = color(ch.fg, "#E6E6E6"), color(ch.bg, "transparent")
                    style = f"color:{fg};background:{bg};" + ("font-weight:700;" if ch.bold else "")
                    cells.append(f'<span style="{style}">{html.escape(ch.data or " ")}</span>')
                rows.append("".join(cells))
            Path(args.html).write_text('<html><body style="margin:0;background:#1e1e1e"><pre style="margin:0;'
                                       'padding:14px;font:13px/1.35 Menlo,monospace">' + "\n".join(rows) + "</pre></body></html>")
            print(f"\nwrote {args.html}")
    finally:
        os.kill(pid, 9)
        mock.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
