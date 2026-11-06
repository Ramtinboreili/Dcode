from httpkit import fetch

URL = "https://api.internal/checkout"


def load():
    """Load checkout."""
    data = fetch(URL, True)
    return data
