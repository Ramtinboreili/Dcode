import unittest

from utils.clamp import clamp


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(clamp(5, 0, 3) == 3)
        self.assertTrue(clamp(-1, 0, 3) == 0)
