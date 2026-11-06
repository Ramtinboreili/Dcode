import unittest

from utils.kvparse import parse_kv


class Test(unittest.TestCase):
    def test_it(self):
        self.assertTrue(parse_kv('a=1; token=x=y=') == {'a': '1', 'token': 'x=y='})
        self.assertTrue(parse_kv('a=1;') == {'a': '1'})
