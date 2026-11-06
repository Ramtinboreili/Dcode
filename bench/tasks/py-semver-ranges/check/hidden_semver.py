import unittest

from semver import Version, best_match, satisfies


class Hidden(unittest.TestCase):
    def test_prerelease_order(self):
        order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-rc.1", "1.0.0-rc.2", "1.0.0-rc.10", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        self.assertEqual(sorted(order[::-1], key=Version), order)
        self.assertLess(Version("1.0.0-rc.9"), Version("1.0.0-rc.10"))
        self.assertGreater(Version("1.0.0"), Version("1.0.0-rc.1"))
        self.assertEqual(Version("1.2.3"), Version("1.2.3"))

    def check(self, spec, good, bad):
        for version in good:
            self.assertTrue(satisfies(version, spec), f"{version} should match {spec}")
        for version in bad:
            self.assertFalse(satisfies(version, spec), f"{version} should not match {spec}")

    def test_exact(self):
        self.check("1.2.3", ["1.2.3"], ["1.2.4", "1.2.2"])

    def test_caret(self):
        self.check("^1.2.3", ["1.2.3", "1.9.0"], ["1.2.2", "2.0.0"])
        self.check("^0.2.3", ["0.2.3", "0.2.9"], ["0.3.0", "0.2.2"])
        self.check("^0.0.3", ["0.0.3"], ["0.0.4", "0.0.2"])

    def test_tilde(self):
        self.check("~1.2.3", ["1.2.3", "1.2.9"], ["1.3.0", "1.2.2"])

    def test_comparators_and_or(self):
        self.check(">=1.0.0 <2.0.0", ["1.0.0", "1.9.9"], ["0.9.9", "2.0.0"])
        self.check("<1.0.0 || >=2.0.0", ["0.5.0", "2.0.0"], ["1.5.0"])
        self.check("^1.0.0 || ^3.0.0", ["1.5.0", "3.1.0"], ["2.0.0"])
        self.check(">1.0.0 <=1.5.0", ["1.5.0"], ["1.0.0", "1.5.1"])

    def test_prereleases_are_out_unless_named(self):
        self.check("^1.0.0", ["1.0.0"], ["1.5.0-rc.1", "2.0.0-rc.1"])
        self.check(">=1.0.0-rc.1", ["1.0.0-rc.1", "1.0.0-rc.2", "1.0.0"], ["1.1.0-rc.1"])

    def test_best_match(self):
        versions = ["1.0.0", "1.4.0", "1.5.0-rc.1", "2.0.0", "1.3.9"]
        self.assertEqual(best_match(versions, "^1.0.0"), "1.4.0")
        self.assertEqual(best_match(versions, "~1.3.0"), "1.3.9")
        self.assertEqual(best_match(versions, ">=1.5.0-rc.1 <2.0.0"), "1.5.0-rc.1")
        self.assertIsNone(best_match(versions, "^9.0.0"))
