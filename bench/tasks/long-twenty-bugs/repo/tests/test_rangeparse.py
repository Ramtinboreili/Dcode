import unittest

from utils.rangeparse import parse_ranges


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(parse_ranges('1-3,7') == [1, 2, 3, 7])
