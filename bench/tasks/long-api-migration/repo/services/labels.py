from httpkit import fetch

URL = "https://api.internal/labels"


def load():
    """Load labels."""
    data = fetch(URL, False, 2, 30)
    return data
