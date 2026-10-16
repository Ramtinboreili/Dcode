"""run_command the same for every model: command shapes, commands that need a terminal, how it runs."""

from __future__ import annotations

import json
import time

import pytest

from dcode import shell
from dcode.tools import execute_tool, normalize_call


@pytest.mark.parametrize("args, command", [
    ({"command": ["bash", "-lc", "ls -la"]}, "ls -la"),                  # GPT / Codex
    ({"command": ["git", "commit", "-m", "fix bug"]}, "git commit -m 'fix bug'"),
    ({"command": ["npm ci", "npm test"]}, "npm ci && npm test"),
    ({"cmd": "pwd"}, "pwd"),
    ({"script": "echo hi"}, "echo hi"),
    ({"command": "```bash\nmake test\n```"}, "make test"),
    ({"command": "$ git status\n$ git log -1"}, "git status\ngit log -1"),
    ({"command": "bash -lc 'cd src && ls'"}, "cd src && ls"),
    ({"command": "ls", "workdir": "src"}, "cd src && ls"),
    ({"command": "ls", "cwd": "."}, "ls"),
])
def test_every_command_shape_becomes_one_command_line(args, command):
    assert shell.clean_command(args)["command"] == command


def test_background_timeouts_and_a_trailing_ampersand():
    assert shell.clean_command({"command": "npm run dev &"}) == {"command": "npm run dev", "background": True}
    # both at once (Qwen3-Coder): the & must still go, or bash detaches the server and the job looks finished
    assert shell.clean_command({"command": "python3 -m http.server 8765 &", "background": True}) == {
        "command": "python3 -m http.server 8765", "background": True}
    assert shell.clean_command({"command": "a && b"}) == {"command": "a && b"}
    assert shell.clean_command({"command": "x", "is_background": "true"})["background"] is True
    assert shell.clean_command({"command": "x", "timeout_ms": 30000})["timeout"] == 30
    assert shell.clean_command({"command": "x", "timeout": 9999})["timeout"] == shell.MAX_TIMEOUT
    assert "command" not in shell.clean_command({"path": "a"})  # nothing to run: the tool says what's missing


@pytest.mark.parametrize("command", [
    "tail -f /var/log/syslog", "journalctl -u nginx -f", "kubectl logs -n vllm deploy/vllm-gemma4 -f",
    "docker logs --follow web", "kubectl get pods -w", "watch df -h", "ping 10.0.0.1", "vim a.py",
    "python3", "kubectl edit deploy/web", "cd /srv && tail -F app.log",
])
def test_commands_that_need_a_terminal_are_refused_with_a_way_out(command):
    reason = shell.refusal(command)
    assert reason.startswith("Not run:") and "Instead" in reason
    assert execute_tool("run_command", {"command": command}) == reason


@pytest.mark.parametrize("command", [
    "tail -n 50 app.log", "kubectl logs deploy/web --tail=200", "kubectl apply -f deploy.yaml",
    "ping -c 4 10.0.0.1", "curl -s https://x/install.sh | sh", "git log | less", "python3 -c 'print(1)'",
    "echo 'tail -f is bad'", "top -b -n 1",
])
def test_ordinary_commands_are_not_refused(command):
    assert shell.refusal(command) == ""


def test_commands_never_wait_for_input_and_run_in_bash(isolated):
    out = execute_tool("run_command", {"command": "read -t 5 line; echo \"got:$line\"; [[ 1 == 1 ]] && echo bash"})
    assert "got:" in out and "bash" in out
    assert execute_tool("run_command", {"command": "echo $GIT_PAGER $NO_COLOR"}) == "cat 1"


def test_colour_codes_are_stripped():
    assert execute_tool("run_command", {"command": r"printf '\033[31mred\033[0m plain'"}) == "red plain"


def test_a_timeout_stops_the_children_too():
    started = time.time()
    out = execute_tool("run_command", {"command": "sleep 30 & sleep 30; echo never", "timeout": 1})
    assert time.time() - started < 10
    assert "stopped after 1s" in out and "background=true" in out


@pytest.mark.parametrize("name, args, expected", [
    ("read_file", {"file_path": "a.py", "offset": 10, "limit": 20}, {"path": "a.py", "start_line": 10, "end_line": 29}),
    ("read_file", {"path": "a.py", "view_range": [5, 9]}, {"path": "a.py", "start_line": 5, "end_line": 9}),
    ("edit_file", {"target_file": "a.py", "old_string": "x", "new_string": "y"},
     {"path": "a.py", "old_text": "x", "new_text": "y"}),
    ("write_file", {"filename": "a.py", "contents": "x"}, {"path": "a.py", "content": "x"}),
    ("search_in_files", {"query": "TODO", "include": "*.py"}, {"pattern": "TODO", "glob": "*.py"}),
    ("list_files", {"directory": "src"}, {"path": "src"}),
    ("run_command", {"cmd": ["bash", "-c", "ls"], "cwd": "src"}, {"command": "cd src && ls"}),
])
def test_other_agents_argument_names_are_mapped(name, args, expected):
    call = {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    normalize_call(call)
    assert json.loads(call["function"]["arguments"]) == expected


def test_a_correct_call_is_left_byte_for_byte():
    raw = '{"path": "a.py",  "old_text": "x", "new_text": "y"}'
    call = {"id": "c", "type": "function", "function": {"name": "edit_file", "arguments": raw}}
    normalize_call(call)
    assert call["function"]["arguments"] == raw


def test_frozen_binary_gives_commands_the_hosts_libraries():
    """Reported: every command in the 0.7.0 binary began with "bash: /tmp/_MEI…/libtinfo.so.6:
    no version information available", tokens the model read on every call."""
    from dcode.__main__ import restore_host_libraries

    env = {"LD_LIBRARY_PATH": "/tmp/_MEI123", "LD_LIBRARY_PATH_ORIG": "/opt/lib", "KEEP": "1"}
    restore_host_libraries(env, frozen=True)
    assert env == {"LD_LIBRARY_PATH": "/opt/lib", "KEEP": "1"}
    env = {"LD_LIBRARY_PATH": "/tmp/_MEI123"}
    restore_host_libraries(env, frozen=True)
    assert env == {}  # the host had none
    env = {"LD_LIBRARY_PATH": "/mine"}
    restore_host_libraries(env, frozen=False)
    assert env == {"LD_LIBRARY_PATH": "/mine"}  # from source: untouched
