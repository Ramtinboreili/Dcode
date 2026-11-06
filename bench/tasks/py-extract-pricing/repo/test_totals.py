import unittest

from cart import cart_total


class Totals(unittest.TestCase):
    def test_cart(self):
        self.assertEqual(cart_total([(50, 2)], "member", "us"), 101.65)
        self.assertEqual(cart_total([(10, 1)], "guest", "eu"), 12.0)
        self.assertEqual(cart_total([(10, 10)], "wholesale", "uk"), 96.0)
