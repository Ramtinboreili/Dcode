from httpkit import fetch

URL = "https://api.internal/accounts"


def load():
    """Load accounts."""
    data = fetch(URL, True)
    return data
