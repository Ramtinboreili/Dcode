from httpkit import fetch

URL = "https://api.internal/alerts"


def load():
    """Load alerts."""
    data = fetch(URL, False)
    return data
