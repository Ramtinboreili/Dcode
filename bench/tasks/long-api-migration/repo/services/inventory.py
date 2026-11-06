from httpkit import fetch

URL = "https://api.internal/inventory"


def load():
    """Load inventory."""
    data = fetch(URL, True, 3)
    return data
