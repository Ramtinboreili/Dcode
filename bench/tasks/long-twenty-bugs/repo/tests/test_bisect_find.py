import unittest

from utils.bisect_find import find


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(find([1, 3, 5], 5) == 2)
        self.assertTrue(find([1], 1) == 0)
