import unittest

from utils.bytesize import human_bytes


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(human_bytes(1024) == '1.0 KB')
