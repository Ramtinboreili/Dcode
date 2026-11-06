import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


def run(*args):
    return subprocess.run([sys.executable, "du.py", *args], capture_output=True, text=True)


class Hidden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = cls.dir.name
        os.mkdir(os.path.join(root, "sub"))
        for name, size in (("small", 10), ("sub/big", 5000), ("mid", 2048)):
            with open(os.path.join(root, name), "wb") as handle:
                handle.write(b"x" * size)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_json_output(self):
        result = run(self.dir.name, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([row["path"] for row in data], [os.path.join("sub", "big"), "mid", "small"])
        self.assertEqual(data[0], {"path": os.path.join("sub", "big"), "bytes": 5000, "human": "4.9KB"})
        self.assertEqual(data[2]["human"], "10B")

    def test_top_with_json(self):
        data = json.loads(run(self.dir.name, "--json", "--top", "1").stdout)
        self.assertEqual(len(data), 1)

    def test_table_unchanged(self):
        result = run(self.dir.name, "--top", "2")
        self.assertEqual(result.stdout.splitlines()[0].split(), ["4.9KB", os.path.join("sub", "big")])
        self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_error_unchanged(self):
        self.assertEqual(run("/no/such/folder", "--json").returncode, 2)

    def test_they_added_a_test_for_it(self):
        found = [p for p in pathlib.Path(".").rglob("*.py")
                 if "check" not in p.parts and p.name != "du.py" and "json" in p.read_text().lower() and "test" in p.name]
        self.assertTrue(found, "no test mentioning the JSON output was added")
