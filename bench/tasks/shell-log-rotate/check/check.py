import os
import re
import stat
import tempfile

from benchcheck import finish, ok, read, run

script = os.path.abspath("rotate.sh")
ok(run("bash", "-n", script).returncode == 0, "bash -n passes")
ok(not re.search(r"\bdate\b", read("rotate.sh")), "doesn't depend on the date any more")


def write(folder, name, text):
    with open(os.path.join(folder, name), "w") as handle:
        handle.write(text)


def content(folder, name):
    with open(os.path.join(folder, name)) as handle:
        return handle.read()


with tempfile.TemporaryDirectory() as root:
    empty = os.path.join(root, "empty dir")
    os.mkdir(empty)
    result = run("bash", script, empty, "3")
    ok(result.returncode == 0, "an empty folder is fine")
    ok(os.listdir(empty) == [], "and stays empty")

    logs = os.path.join(root, "the logs")
    os.mkdir(logs)
    write(logs, "app.log", "current")
    write(logs, "app.log.1", "older")
    write(logs, "app.log.2", "oldest")
    write(logs, "my app.log", "spaced")
    write(logs, "notes.txt", "leave me")
    os.chmod(os.path.join(logs, "app.log"), 0o640)
    result = run("bash", script, logs, "3")
    ok(result.returncode == 0, "rotation works: " + result.stderr[-300:])
    ok(content(logs, "app.log.1") == "current", "the current log became .1")
    ok(content(logs, "app.log.2") == "older", ".1 became .2")
    ok(content(logs, "app.log.3") == "oldest", ".2 became .3")
    ok(content(logs, "app.log") == "", "an empty log is left behind")
    ok(stat.S_IMODE(os.stat(os.path.join(logs, "app.log")).st_mode) == 0o640, "with the same permissions")
    ok(content(logs, "my app.log.1") == "spaced", "names with spaces work")
    ok(content(logs, "my app.log") == "", "and get a fresh log")
    ok(content(logs, "notes.txt") == "leave me" and not os.path.exists(os.path.join(logs, "notes.txt.1")), "other files stay out of it")

    write(logs, "app.log", "second round")
    result = run("bash", script, logs, "3")
    ok(result.returncode == 0, "a second run works")
    ok(content(logs, "app.log.1") == "second round" and content(logs, "app.log.2") == "current", "shifted again")
    ok(content(logs, "app.log.3") == "older", "the third is the next oldest")
    ok(not os.path.exists(os.path.join(logs, "app.log.4")), "nothing past KEEP")

    one = os.path.join(root, "keep one")
    os.mkdir(one)
    write(one, "x.log", "a")
    write(one, "x.log.1", "b")
    ok(run("bash", script, one, "1").returncode == 0, "KEEP of 1 works")
    ok(content(one, "x.log.1") == "a" and not os.path.exists(os.path.join(one, "x.log.2")), "keeps only the newest")
    ok(run("bash", script, os.path.join(root, "no such dir"), "2").returncode != 0, "a missing folder fails")
finish()
