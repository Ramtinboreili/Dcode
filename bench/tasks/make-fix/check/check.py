import os

from benchcheck import exists, finish, ok, read, run

os.chmod("run_tests.sh", 0o755)
result = run("make", "build")
ok(result.returncode == 0, "make build works: " + result.stderr[-300:])
ok(read("out/bundle.txt") == "hello\nworld\n", "bundles src/*.txt in order")
again = run("make", "build")
ok(again.returncode == 0 and "cat " not in again.stdout, "a second build does nothing")
for _ in range(2):
    test = run("make", "test")
    ok(test.returncode == 0 and "tests-ran" in test.stdout, "make test runs the tests each time")
ok(exists("test/keep.txt"), "the test folder is still there")
ok(run("make", "clean").returncode == 0, "make clean works")
ok(not exists("out"), "and removes the output")
ok(exists("src/a.txt"), "and only the output")
rebuilt = run("make")
ok(rebuilt.returncode == 0 and exists("out/bundle.txt"), "plain make still builds")
finish()
