import ast
import pathlib
import unittest

from cart import cart_total
from invoice import invoice_total
from pricing import total
from quote import quote_total


class Hidden(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total(100, "member", "us"), 101.65)
        self.assertEqual(total(99, "member", "none"), 99)
        self.assertEqual(total(100, "wholesale", "eu"), 96.0)
        self.assertEqual(total(10, "guest", "uk"), 12.0)

    def test_all_three_agree(self):
        for amount in (10, 99, 100, 250.5):
            for kind in ("guest", "member", "wholesale"):
                for region in ("eu", "us", "uk", "jp"):
                    want = total(amount, kind, region)
                    self.assertEqual(cart_total([(amount, 1)], kind, region), want)
                    self.assertEqual(invoice_total([{"amount": amount}], kind, region), want)
                    self.assertEqual(quote_total(amount, kind, region), want)

    def test_the_logic_lives_in_one_place(self):
        for name in ("cart.py", "invoice.py", "quote.py"):
            source = pathlib.Path(name).read_text()
            tree = ast.parse(source)
            imported = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
            self.assertTrue(any("pricing" in ast.dump(node) for node in imported), name)
            self.assertNotIn("0.95", source, name)
            self.assertNotIn("wholesale", source, name)
            self.assertNotIn("0.07", source, name)
