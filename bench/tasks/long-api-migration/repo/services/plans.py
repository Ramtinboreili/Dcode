from httpkit import fetch

URL = "https://api.internal/plans"


def load():
    """Load plans."""
    data = fetch(URL, False)
    return data
