import httpkit as hk

URL = "https://api.internal/cart"


def load():
    """Load cart."""
    data = hk.fetch(URL)
    return data
