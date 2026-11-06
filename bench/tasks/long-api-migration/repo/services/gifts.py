from httpkit import fetch

URL = "https://api.internal/gifts"


def load():
    """Load gifts."""
    data = fetch(URL, False, 2, 30)
    return data
