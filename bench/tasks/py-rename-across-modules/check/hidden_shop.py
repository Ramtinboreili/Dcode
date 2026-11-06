import pathlib
import subprocess
import sys
import unittest

import shop.users
from shop.email import greeting
from shop.orders import discount_for, price_for
from shop.report import lookup_by_name, tiers


class Hidden(unittest.TestCase):
    def test_new_names(self):
        self.assertEqual(shop.users.fetch_user(2).name, "bo")
        self.assertEqual(shop.users.UserRecord(3, "c").tier, "basic")
        self.assertEqual(discount_for(shop.users.UserRecord(3, "c", "gold")), 0.1)
        self.assertEqual(lookup_by_name("x"), shop.users.fetch_user)

    def test_old_names_gone_from_the_code(self):
        self.assertFalse(hasattr(shop.users, "get_user_data"))
        self.assertFalse(hasattr(shop.users, "UserData"))

    def test_old_names_gone_everywhere(self):
        for path in pathlib.Path(".").rglob("*"):
            if path.is_file() and "check" not in path.parts and path.suffix in (".py", ".md"):
                text = path.read_text()
                self.assertNotIn("get_user_data", text, path)
                self.assertNotIn("UserData", text, path)

    def test_behaviour_kept(self):
        self.assertEqual(price_for(1, 50), 45.0)
        self.assertEqual(greeting(2), "Hello bo")
        self.assertEqual(tiers([1]), {1: "gold"})
        with self.assertRaises(KeyError):
            shop.users.fetch_user(99)

    def test_visible_tests_still_pass(self):
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
