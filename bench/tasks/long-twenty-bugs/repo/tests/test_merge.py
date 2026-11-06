import unittest

from utils.merge import merge_sorted


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(merge_sorted([1, 3, 9], [2]) == [1, 2, 3, 9])
