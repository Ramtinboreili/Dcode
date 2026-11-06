import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path.cwd()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def file(self, text, name="Buildfile"):
        (self.dir / name).write_text(textwrap.dedent(text))

    def mb(self, *args):
        return subprocess.run([sys.executable, "-m", "minibuild", *args], cwd=self.dir, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=60)

    def touch(self, name, when, text="x"):
        path = self.dir / name
        if not path.exists():
            path.write_text(text)
        os.utime(path, (when, when))

    def lines(self, name="log"):
        path = self.dir / name
        return path.read_text().split() if path.exists() else []


CHAIN = """\
    all: app

    app: main.o util.o
        echo app >> log
        cat $^ > $@

    main.o: main.c util.h
        echo main >> log
        cat $< > $@

    util.o: util.c util.h
        echo util >> log
        cat $< > $@
    """


class Rebuild(Base):
    def setUp(self):
        super().setUp()
        self.file(CHAIN)
        self.touch("main.c", 1000)
        self.touch("util.c", 1000)
        self.touch("util.h", 1000)

    def test_builds_everything_in_order(self):
        got = self.mb()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.lines()[-1], "app")
        self.assertEqual(sorted(self.lines()), ["app", "main", "util"])
        self.assertIn("echo app >> log", got.stdout)

    def test_second_run_does_nothing(self):
        self.mb()
        (self.dir / "log").unlink()
        got = self.mb("app")
        self.assertEqual(got.returncode, 0)
        self.assertEqual(self.lines(), [])
        self.assertIn("minibuild: 'app' is up to date", got.stdout)

    def test_only_what_changed(self):
        self.mb()
        for name in ("main.o", "util.o", "app"):
            os.utime(self.dir / name, (2000, 2000))
        (self.dir / "log").unlink()
        os.utime(self.dir / "main.c", (3000, 3000))
        self.assertEqual(self.mb("app").returncode, 0)
        self.assertEqual(self.lines(), ["main", "app"])

    def test_shared_header(self):
        self.mb()
        for name in ("main.o", "util.o", "app"):
            os.utime(self.dir / name, (2000, 2000))
        (self.dir / "log").unlink()
        os.utime(self.dir / "util.h", (3000, 3000))
        self.assertEqual(self.mb("app").returncode, 0)
        self.assertEqual(sorted(self.lines()), ["app", "main", "util"])

    def test_same_time_is_not_newer(self):
        self.mb()
        for name in ("main.c", "util.c", "util.h", "main.o", "util.o", "app"):
            os.utime(self.dir / name, (2000, 2000))
        (self.dir / "log").unlink()
        self.mb("app")
        self.assertEqual(self.lines(), [])

    def test_missing_target_rebuilds_up_the_chain(self):
        self.mb()
        for name in ("main.o", "util.o", "app"):
            os.utime(self.dir / name, (2000, 2000))
        (self.dir / "log").unlink()
        (self.dir / "util.o").unlink()
        self.mb("app")
        self.assertEqual(self.lines(), ["util", "app"])

    def test_dry_run(self):
        got = self.mb("-n", "app")
        self.assertEqual(got.returncode, 0)
        self.assertIn("cat main.c > main.o", got.stdout)
        self.assertFalse((self.dir / "app").exists())
        self.assertFalse((self.dir / "log").exists())


class Syntax(Base):
    def test_automatic_and_user_variables(self):
        self.file("""\
            A = 1
            B = $(A)-$(C)
            C = 3
            out: x y
                @echo "$@|$<|$^|$(B)|$(NOPE)|$$HOME_LIT" > res
            x:
                @touch x
            y:
                @touch y
            """)
        self.assertEqual(self.mb("out").returncode, 0)
        self.assertEqual((self.dir / "res").read_text().split("|")[:5], ["out", "x", "x y", "1-3", ""])

    def test_literal_dollar(self):
        self.file("""\
            out:
                @echo 'a$$b' > res
            """)
        self.mb()
        self.assertEqual((self.dir / "res").read_text().strip(), "a$b")

    def test_command_line_override(self):
        self.file("""\
            V = file
            out:
                @echo $(V) > res
            """)
        self.mb("V=cli")
        self.assertEqual((self.dir / "res").read_text().strip(), "cli")

    def test_at_hides_echo(self):
        self.file("""\
            out:
                @echo hidden-cmd > a
                echo shown-cmd > b
            """)
        got = self.mb()
        self.assertNotIn("hidden-cmd", got.stdout)
        self.assertIn("echo shown-cmd > b", got.stdout)
        got = self.mb("-n")
        self.assertIn("echo hidden-cmd > a", got.stdout)

    def test_dash_ignores_failure(self):
        self.file("""\
            out:
                -false
                touch out
            """)
        self.assertEqual(self.mb().returncode, 0)
        self.assertTrue((self.dir / "out").exists())

    def test_first_target_is_the_default(self):
        self.file("""\
            # a comment

            one:
                @touch one
            two:
                @touch two
            """)
        self.mb()
        self.assertTrue((self.dir / "one").exists())
        self.assertFalse((self.dir / "two").exists())

    def test_tabs_and_file_option(self):
        self.file("t:\n\t@touch from_other\n", "other.build")
        self.assertEqual(self.mb("-f", "other.build").returncode, 0)
        self.assertTrue((self.dir / "from_other").exists())

    def test_syntax_errors(self):
        self.file("X = 1\n\n    echo orphan\nt:\n    @touch ran\n")
        got = self.mb("t")
        self.assertEqual(got.returncode, 2)
        self.assertIn("minibuild: Buildfile:3:", got.stderr)
        self.assertFalse((self.dir / "ran").exists())
        self.file("t:\n    @touch ran\nthis is not anything\n")
        got = self.mb("t")
        self.assertEqual(got.returncode, 2)
        self.assertIn("minibuild: Buildfile:3:", got.stderr)
        self.assertFalse((self.dir / "ran").exists())


class Phony(Base):
    def test_phony_always_runs(self):
        self.file("""\
            .phony: clean
            clean:
                echo cleaned >> log
            """)
        (self.dir / "clean").write_text("a file with that name")
        self.mb("clean")
        self.mb("clean")
        self.assertEqual(self.lines(), ["cleaned", "cleaned"])

    def test_diamond_runs_once(self):
        self.file("""\
            .phony: top a b base
            top: a b
            a: base
            b: base
            base:
                echo base >> log
            """)
        self.assertEqual(self.mb("top").returncode, 0)
        self.assertEqual(self.lines(), ["base"])


class Errors(Base):
    def test_missing_dependency(self):
        self.file("""\
            app: nope.c
                @touch ran
            """)
        got = self.mb()
        self.assertEqual(got.returncode, 2)
        self.assertIn("minibuild: no rule to make 'nope.c' (needed by 'app')", got.stderr)
        self.assertFalse((self.dir / "ran").exists())

    def test_cycle_found_before_running(self):
        self.file("""\
            start: first a
            first:
                @touch ran
            a: b
            b: c
            c: a
            """)
        got = self.mb()
        self.assertEqual(got.returncode, 2)
        self.assertIn("minibuild: cycle: a -> b -> c -> a", got.stderr)
        self.assertFalse((self.dir / "ran").exists())

    def test_failure_stops(self):
        self.file("""\
            .phony: all
            all: a b
            a:
                sh -c 'exit 3'
            b:
                touch b
            """)
        got = self.mb()
        self.assertEqual(got.returncode, 1)
        self.assertIn("minibuild: target 'a' failed (exit 3)", got.stderr)
        self.assertFalse((self.dir / "b").exists())

    def test_keep_going(self):
        self.file("""\
            .phony: all
            all: bad after other
            bad:
                false
            after: bad
                touch after
            other:
                touch other
            """)
        got = self.mb("-k")
        self.assertEqual(got.returncode, 1)
        self.assertTrue((self.dir / "other").exists())
        self.assertFalse((self.dir / "after").exists())


class Parallel(Base):
    def test_runs_at_the_same_time(self):
        self.file("""\
            .phony: all a b c d
            all: a b c d
            a:
                @sleep 1
            b:
                @sleep 1
            c:
                @sleep 1
            d:
                @sleep 1
            """)
        started = time.time()
        self.assertEqual(self.mb("-j", "4").returncode, 0)
        self.assertLess(time.time() - started, 3.5)

    def test_respects_dependencies(self):
        self.file("""\
            .phony: all first second third
            all: third
            third: second
                @cat second_done >> log && echo third >> log
            second: first
                @sleep 0.2; cat first_done >> log && echo second >> log; touch second_done
            first:
                @sleep 0.2; echo first >> log; touch first_done
            """)
        got = self.mb("-j", "4")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.lines(), ["first", "second", "third"])

    def test_failure_with_jobs(self):
        self.file("""\
            .phony: all a b
            all: a b
            a:
                false
            b: a
                touch b
            """)
        got = self.mb("-j", "3")
        self.assertEqual(got.returncode, 1)
        self.assertFalse((self.dir / "b").exists())
