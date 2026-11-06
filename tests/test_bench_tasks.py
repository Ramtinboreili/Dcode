"""The benchmark's task format and YAML subset. Task contents are checked by `make bench-validate`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).parent.parent / "bench"
sys.path[:0] = [str(BENCH), str(BENCH / "lib")]
import common  # noqa: E402
import miniyaml  # noqa: E402
import validate_tasks  # noqa: E402

# --- miniyaml -----------------------------------------------------------------------------------------------------
MANIFEST = """\
# a comment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web   # trailing comment
  labels: {app: web, tier: "front end"}
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: app
        image: "nginx:1.27"
        args: ["--port", "8080", '--x']
        ports:
          - containerPort: 8080
            name: http
        env:
        - {name: A, value: "1"}
      - name: side
        image: busybox:1.36
"""


def test_a_kubernetes_manifest():
    doc = miniyaml.load(MANIFEST)
    assert doc["metadata"] == {"name": "web", "labels": {"app": "web", "tier": "front end"}}
    first, second = doc["spec"]["template"]["spec"]["containers"]
    assert first["image"] == "nginx:1.27" and first["args"] == ["--port", "8080", "--x"]
    assert first["ports"] == [{"containerPort": 8080, "name": "http"}]
    assert first["env"] == [{"name": "A", "value": "1"}]
    assert second == {"name": "side", "image": "busybox:1.36"}


@pytest.mark.parametrize("text,value", [
    ("a: 1", {"a": 1}),
    ("a: -1.5", {"a": -1.5}),
    ("a: '1'", {"a": "1"}),
    ('a: "x\\ny"', {"a": "x\ny"}),
    ("a: 'it''s'", {"a": "it's"}),
    ("a: true\nb: False\nc: null\nd: ~\ne:", {"a": True, "b": False, "c": None, "d": None, "e": None}),
    ("on: push\nyes: no", {"on": "push", "yes": "no"}),            # not YAML 1.1's booleans
    ("0123: 0123", {"0123": "0123"}),                               # not an octal
    ("a: b#c", {"a": "b#c"}),
    ('a: "b # c"', {"a": "b # c"}),
    ("a: http://x.y:80/z", {"a": "http://x.y:80/z"}),
    ("rule: Host(`a.b`) && Path(`/`)", {"rule": "Host(`a.b`) && Path(`/`)"}),
    ("a: {{ .Values.x }}", {"a": "{{ .Values.x }}"}),
    ('"a b": 1', {"a b": 1}),
    ("a: []\nb: {}", {"a": [], "b": {}}),
    ("a: [1, [2, 3], {b: c}]", {"a": [1, [2, 3], {"b": "c"}]}),
    ("- 1\n- a\n- - 2\n  - 3\n- b: 1\n  c: 2", [1, "a", [2, 3], {"b": 1, "c": 2}]),
    ("a:\n- 1\n- 2\nb: 3", {"a": [1, 2], "b": 3}),
    ("a: |\n  x\n    y\n\n  z\nb: 1", {"a": "x\n  y\n\nz\n", "b": 1}),
    ("a: |-\n  x\nb: 1", {"a": "x", "b": 1}),
    ("a: >\n  x\n  y\n\n  z\n", {"a": "x y\nz\n"}),
    ("a:\n  - run: |\n      echo 1\n      echo 2\n    name: t", {"a": [{"run": "echo 1\necho 2\n", "name": "t"}]}),
])
def test_scalars_and_structures(text, value):
    assert miniyaml.load(text) == value


def test_documents():
    assert miniyaml.load_all("---\na: 1\n---\n# nothing\n---\nb: 2\n...\n") == [{"a": 1}, {"b": 2}]
    assert miniyaml.load("") is None
    with pytest.raises(miniyaml.MiniYamlError):
        miniyaml.load("a: 1\n---\nb: 2")


@pytest.mark.parametrize("text", [
    "a: &x 1", "a: *x", "a: !!str 1", "<<: {a: 1}", "a: 1\na: 2", "a: b\n  c: d", "a: 'x", "a: [1, 2", "\ta: 1",
    "a:\n\tb: 1", "just text\nmore: 1", "a:\n  b: 1\n c: 2", "%YAML 1.2\na: 1",
])
def test_what_it_cant_honour_is_an_error_not_a_guess(text):
    with pytest.raises(miniyaml.MiniYamlError):
        miniyaml.load(text)


def test_it_agrees_with_json_on_json():
    data = {"a": [1, 2.5, "x", None, True], "b": {"c": "d e", "f": []}, "g": "h: i"}
    assert miniyaml.load(json.dumps(data)) == data


# --- tasks --------------------------------------------------------------------------------------------------------
def _task(root, name="t1", **meta):
    path = root / "tasks" / name
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "value.txt").write_text("1\n")
    (path / "check").mkdir()
    (path / "check" / "check.sh").write_text('test "$(cat value.txt)" = 2\n')
    (path / "solution.patch").write_text(
        "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-1\n+2\n")
    fields = {"id": name, "category": "coding", "difficulty": "easy", "prompt": "fix it", "timeout_s": 60, "requires": [], "tags": []}
    (path / "task.json").write_text(json.dumps({**fields, **meta}))
    return path


@pytest.mark.parametrize("change,says", [
    ({"id": "other"}, "must match"), ({"category": "x"}, "category"), ({"difficulty": "x"}, "difficulty"),
    ({"prompt": " "}, "prompt"), ({"timeout_s": 0}, "timeout"), ({"requires": "go"}, "requires"), ({"requires": [1]}, "requires"),
    ({"max_steps": 0}, "max_steps"),
])
def test_bad_metadata_is_refused(tmp_path, change, says):
    with pytest.raises(common.TaskError, match=says):
        common.load_task(_task(tmp_path, **change))


@pytest.mark.parametrize("remove", ["repo", "check/check.sh", "solution.patch", "task.json"])
def test_a_missing_part_is_refused(tmp_path, remove):
    path = _task(tmp_path)
    target = path / remove
    if target.is_dir():
        for item in target.iterdir():
            item.unlink()
        target.rmdir()
    else:
        target.unlink()
    with pytest.raises(common.TaskError):
        common.load_task(path)


def test_a_repo_may_not_bring_its_own_check_folder(tmp_path):
    path = _task(tmp_path)
    (path / "repo" / "check").mkdir()
    with pytest.raises(common.TaskError, match="check/"):
        common.load_task(path)


def test_selecting_by_id_glob_and_category_and_a_typo_is_an_error(tmp_path):
    _task(tmp_path, "c1")
    _task(tmp_path, "c2")
    _task(tmp_path, "d1", category="devops")
    tasks = common.load_tasks(tmp_path / "tasks")
    ids = lambda *patterns: [t.id for t in common.select_tasks(tasks, list(patterns))]  # noqa: E731
    assert ids() == ["c1", "c2", "d1"]
    assert ids("c2") == ["c2"] and ids("c*") == ["c1", "c2"] and ids("devops") == ["d1"]
    assert ids("d1,c1") == ["c1", "d1"] and ids("c1", "c*") == ["c1", "c2"]
    with pytest.raises(common.TaskError, match="nope"):
        ids("nope")


def _oracle(tmp_path, *args):
    return validate_tasks.main(["--root", str(tmp_path / "tasks"), *args])


def test_the_oracle_accepts_a_right_task(tmp_path, capsys):
    _task(tmp_path)
    assert _oracle(tmp_path) == 0
    assert "1 ok" in capsys.readouterr().out


def test_the_oracle_catches_a_check_that_always_passes(tmp_path, capsys):
    path = _task(tmp_path)
    (path / "check" / "check.sh").write_text("exit 0\n")
    assert _oracle(tmp_path) == 1
    assert "passes on the untouched repo" in capsys.readouterr().out


def test_the_oracle_catches_a_solution_that_doesnt_solve_it(tmp_path, capsys):
    path = _task(tmp_path)
    (path / "check" / "check.sh").write_text('test "$(cat value.txt)" = 3\n')
    assert _oracle(tmp_path) == 1
    assert "fails with the solution applied" in capsys.readouterr().out


def test_the_oracle_catches_a_patch_that_doesnt_apply(tmp_path, capsys):
    path = _task(tmp_path)
    (path / "solution.patch").write_text("--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-1\n+2\n")
    assert _oracle(tmp_path) == 1
    assert "doesn't apply" in capsys.readouterr().out


def test_a_missing_tool_is_skipped_not_failed(tmp_path, capsys):
    _task(tmp_path, requires=["no-such-binary-here"])
    assert _oracle(tmp_path) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_the_oracle_catches_a_flaky_check(tmp_path, capsys):
    path = _task(tmp_path)
    marker = tmp_path / "ran"
    (path / "check" / "check.sh").write_text(
        f'test "$(cat value.txt)" = 2 || exit 1\nif [ -e {marker} ]; then exit 1; fi\ntouch {marker}\n')
    assert _oracle(tmp_path) == 1
    assert "fails with the solution applied" in capsys.readouterr().out


# --- scoring ------------------------------------------------------------------------------------------------------
def test_scoring_ignores_an_agents_own_check_folder_and_doesnt_touch_the_workspace(tmp_path):
    task = common.load_task(_task(tmp_path))
    workspace = tmp_path / "w"
    common.prepare_workspace(task, workspace)
    (workspace / "check").mkdir()
    (workspace / "check" / "check.sh").write_text("exit 0\n")   # a shortcut that must not work
    result = common.score(workspace, task)
    assert not result["passed"] and result["exit_code"] != 0
    assert (workspace / "check" / "check.sh").read_text() == "exit 0\n"


def test_a_check_that_hangs_is_a_failure_and_leaves_nothing_running(tmp_path):
    path = _task(tmp_path)
    (path / "check" / "check.sh").write_text("sleep 300 &\nwait\n")
    task = common.load_task(path)
    workspace = tmp_path / "w"
    common.prepare_workspace(task, workspace)
    result = common.score(workspace, task, timeout=0.5)
    assert result["timed_out"] and not result["passed"]


def test_a_workspace_is_the_same_commit_every_time(tmp_path):
    task = common.load_task(_task(tmp_path))
    first = common.prepare_workspace(task, tmp_path / "a")
    second = common.prepare_workspace(task, tmp_path / "b")
    assert first == second and len(first) == 40


def test_the_diff_holds_new_files_and_the_agents_own_commits(tmp_path):
    task = common.load_task(_task(tmp_path))
    workspace = tmp_path / "w"
    base = common.prepare_workspace(task, workspace)
    (workspace / "value.txt").write_text("2\n")
    common.git(workspace, "add", "-A")
    common.git(workspace, "commit", "-q", "--no-gpg-sign", "-m", "agent's own")
    (workspace / "new.txt").write_text("hello\n")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "x.pyc").write_text("junk")
    diff = common.capture_diff(workspace, base)
    assert "+2" in diff and "new.txt" in diff and "__pycache__" not in diff
