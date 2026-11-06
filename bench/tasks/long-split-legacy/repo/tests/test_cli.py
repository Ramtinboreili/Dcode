import subprocess
import sys
import unittest


class Test(unittest.TestCase):
    def test_summary(self):
        out = subprocess.run([sys.executable, "legacy_inventory.py", "tests/stock.csv", "summary"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("W1:", out.stdout)
