import os
import tarfile
import tempfile

from benchcheck import finish, ok, run

script = os.path.abspath("backup.sh")
ok(run("bash", "-n", script).returncode == 0, "bash -n passes")
with tempfile.TemporaryDirectory() as root:
    src = os.path.join(root, "my files", "project a")
    os.makedirs(src)
    with open(os.path.join(src, "note one.txt"), "w") as handle:
        handle.write("hello")
    dest = os.path.join(root, "back ups", "today")
    result = run("bash", script, src, dest)
    ok(result.returncode == 0, "works with spaces: " + result.stderr[-300:])
    archive = os.path.join(dest, "project a.tar.gz")
    ok(os.path.isfile(archive), "made the archive with the right name")
    if os.path.isfile(archive):
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        ok("project a/note one.txt" in names, f"holds the files under the folder's name: {names}")
    ok(os.path.join(dest, "project a.tar.gz") in result.stdout, "says where it went")

    single = os.path.join(root, "one file.log")
    with open(single, "w") as handle:
        handle.write("x")
    ok(run("bash", script, single, dest).returncode == 0, "a single file works")
    ok(os.path.isfile(os.path.join(dest, "one file.log.tar.gz")), "and is archived")

    relative = run("bash", script, "project a", "../rel out", cwd=os.path.join(root, "my files"))
    ok(relative.returncode == 0, "relative paths work: " + relative.stderr[-300:])
    ok(os.path.isfile(os.path.join(root, "rel out", "project a.tar.gz")), "a relative destination lands in the right place")

    missing = run("bash", script, os.path.join(root, "nope"), dest)
    ok(missing.returncode != 0, "a missing source fails")
    ok(missing.stderr.strip(), "and says why on stderr")
    ok("backup done" not in missing.stdout, "and doesn't claim success")
    ok(not os.path.exists(os.path.join(dest, "nope.tar.gz")), "and leaves no archive")
finish()
