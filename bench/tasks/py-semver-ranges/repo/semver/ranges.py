"""Which versions a range allows."""
from .version import Version


def satisfies(version, spec):
    return Version(version) == Version(spec)


def best_match(versions, spec):
    matching = [v for v in versions if satisfies(v, spec)]
    return max(matching, key=Version) if matching else None
