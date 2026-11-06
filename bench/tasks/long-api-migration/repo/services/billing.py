from httpkit import fetch

URL = "https://api.internal/billing"


def load():
    """Load billing."""
    data = fetch(URL, as_json=False, timeout=5)
    return data
