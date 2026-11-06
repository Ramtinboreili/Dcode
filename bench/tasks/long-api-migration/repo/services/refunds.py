from httpkit import fetch

URL = "https://api.internal/refunds"


def load():
    """Load refunds."""
    data = fetch(URL, as_json=False, timeout=5)
    return data
