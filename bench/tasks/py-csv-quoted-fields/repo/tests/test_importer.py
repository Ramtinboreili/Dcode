import unittest

from importer import parse_line, to_dicts


class ImporterTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_line("a, b ,c"), ["a", "b", "c"])

    def test_quoted_comma(self):
        self.assertEqual(parse_line('1,"Smith, Jo",3'), ["1", "Smith, Jo", "3"])

    def test_dicts(self):
        text = '# export\nid,name\n1,"Smith, Jo"\n'
        self.assertEqual(to_dicts(text), [{"id": "1", "name": "Smith, Jo"}])
