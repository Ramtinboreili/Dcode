from httpkit import fetch

URL = "https://api.internal/feeds"


def load():
    """Load feeds."""
    data = fetch(URL, as_json=False, timeout=5)
    return data
