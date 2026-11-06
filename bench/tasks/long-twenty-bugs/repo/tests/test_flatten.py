import unittest

from utils.flatten import flatten


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(flatten([1, [2, [3, [4]]]]) == [1, 2, 3, 4])
