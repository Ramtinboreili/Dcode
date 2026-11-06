import csv
import io
import unittest

from importer import parse, parse_line


class Hidden(unittest.TestCase):
    def test_escaped_quotes(self):
        self.assertEqual(parse_line('"say ""hi""",2'), ['say "hi"', "2"])

    def test_empty_fields(self):
        self.assertEqual(parse_line("a,,c,"), ["a", "", "c", ""])
        self.assertEqual(parse_line('"",x'), ["", "x"])

    def test_quoted_spaces_kept_plain_trimmed(self):
        self.assertEqual(parse_line('" a ", b '), [" a ", "b"])

    def test_hash_inside_a_row(self):
        self.assertEqual(parse("# c\nx,#y\n\n"), [["x", "#y"]])

    def test_same_as_csv_on_tricky_rows(self):
        rows = [["a,b", 'c"d', "e"], ["", "f g", ",,"], ['"', "h", ""]]
        out = io.StringIO()
        csv.writer(out, quoting=csv.QUOTE_MINIMAL, lineterminator="\n").writerows(rows)
        self.assertEqual([parse_line(line) for line in out.getvalue().splitlines()], rows)
