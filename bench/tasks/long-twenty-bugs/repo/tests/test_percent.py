import unittest

from utils.percent import percent


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(percent(0, 0) == 0)
