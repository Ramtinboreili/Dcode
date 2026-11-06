"""Split a list into pages."""


def paginate(items, page, size):
    """Return page number `page` (starting at 1) of `items`, with `size` items per page."""
    start = page * size
    return items[start:start + size]


def page_count(total, size):
    """How many pages `total` items need."""
    return total // size
