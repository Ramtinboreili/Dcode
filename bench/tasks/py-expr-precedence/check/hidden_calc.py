import unittest

from calc import CalcError, evaluate


class Hidden(unittest.TestCase):
    def test_values(self):
        for text, want in [("2+3*4", 14), ("(2+3)*4", 20), ("10-4-3", 3), ("100/10/5", 2), ("2^3^2", 512), ("2*3^2", 18),
                           ("-2^2", -4), ("(-2)^2", 4), ("2^-1", 0.5), ("--3", 3), ("3*-2", -6), ("-(1+2)*3", -9), ("1.5+1.5", 3),
                           ("  1 +  2 ", 3), ("2-3+4", 3), ("+5", 5), ("8/2*4", 16)]:
            self.assertAlmostEqual(evaluate(text), want, msg=text)

    def test_errors(self):
        for text in ("2+", "(1", "1)", "", "*2", "7/0", "1/(2-2)", "2 3", "2 $ 3", "()", "a"):
            with self.assertRaises(CalcError, msg=text):
                evaluate(text)
