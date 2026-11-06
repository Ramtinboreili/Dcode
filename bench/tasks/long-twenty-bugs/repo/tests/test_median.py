import unittest

from utils.median import median


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(median([1, 2, 3, 4]) == 2.5)
