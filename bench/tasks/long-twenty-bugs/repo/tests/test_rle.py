import unittest

from utils.rle import rle


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(rle('aab') == [('a', 2), ('b', 1)])
