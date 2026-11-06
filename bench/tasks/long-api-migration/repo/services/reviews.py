from httpkit import fetch

URL = "https://api.internal/reviews"


def load():
    """Load reviews."""
    data = fetch(URL, True)
    return data
