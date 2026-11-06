import unittest

from utils.invert import invert


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(invert({'a': 1, 'b': 1}) == {1: ['a', 'b']})
