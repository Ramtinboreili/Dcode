import unittest

from utils.roman import to_roman


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(to_roman(4) == 'IV')
        self.assertTrue(to_roman(1994) == 'MCMXCIV')
