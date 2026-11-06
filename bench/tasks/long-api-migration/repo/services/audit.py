from httpkit import fetch

URL = "https://api.internal/audit"


def load():
    """Load audit."""
    data = fetch(URL, True, 3)
    return data
