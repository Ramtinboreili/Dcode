from httpkit import fetch

URL = "https://api.internal/orders"


def load():
    """Load orders."""
    data = fetch(URL)
    return data
