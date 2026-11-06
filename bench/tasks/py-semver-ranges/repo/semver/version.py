"""Versions like 1.2.3 and 1.2.3-rc.1."""
from functools import total_ordering


@total_ordering
class Version:
    def __init__(self, text):
        self.text = text
        core, _, pre = text.partition("-")
        self.major, self.minor, self.patch = (int(part) for part in core.split("."))
        self.pre = pre

    def _key(self):
        return (self.major, self.minor, self.patch, self.pre)

    def __eq__(self, other):
        return self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return f"Version({self.text!r})"
