from httpkit import fetch

URL = "https://api.internal/catalog"


def load():
    """Load catalog."""
    data = fetch(URL, False, 2, 30)
    return data
