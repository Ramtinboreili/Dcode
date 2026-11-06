import subprocess
import sys
import unittest

from notifier.app import build


class Hidden(unittest.TestCase):
    def test_without_smtp_only_posts(self):
        app = build({"channel": "c"})
        self.assertEqual(app.notify("x"), ["posted to c: x"])

    def test_with_smtp_still_mails(self):
        app = build({"channel": "c", "smtp": {"host": "h", "port": 1}})
        result = app.notify("x")
        self.assertEqual(len(result), 2)
        self.assertIn("mailed via h:1", result[1])

    def test_staging_starts(self):
        run = subprocess.run([sys.executable, "main.py", "config/staging.json"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("ops-staging", run.stdout)

    def test_staging_config_stays_as_it_was(self):
        with open("config/staging.json") as handle:
            self.assertNotIn("smtp", handle.read())
