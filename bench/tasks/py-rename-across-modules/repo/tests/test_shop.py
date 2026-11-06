import unittest

from shop.email import greeting
from shop.orders import price_for
from shop.report import tiers


class ShopTest(unittest.TestCase):
    def test_price(self):
        self.assertEqual(price_for(1, 100), 90.0)
        self.assertEqual(price_for(2, 100), 100.0)

    def test_greeting(self):
        self.assertEqual(greeting(1), "Hello ana")

    def test_tiers(self):
        self.assertEqual(tiers([1, 2]), {1: "gold", 2: "basic"})
