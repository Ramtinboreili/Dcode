from httpkit import fetch

URL = "https://api.internal/quotes"


def load():
    """Load quotes."""
    data = fetch(URL, True, 3)
    return data
