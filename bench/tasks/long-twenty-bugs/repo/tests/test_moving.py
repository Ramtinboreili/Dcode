import unittest

from utils.moving import moving_average


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5])
