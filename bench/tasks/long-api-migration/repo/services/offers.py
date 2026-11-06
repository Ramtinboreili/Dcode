from httpkit import fetch

URL = "https://api.internal/offers"


def load():
    """Load offers."""
    data = fetch(URL, as_json=False, timeout=5)
    return data
