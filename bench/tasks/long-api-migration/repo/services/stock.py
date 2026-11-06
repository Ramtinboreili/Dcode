from httpkit import fetch

URL = "https://api.internal/stock"


def load():
    """Load stock."""
    data = fetch(URL, True, 3)
    return data
