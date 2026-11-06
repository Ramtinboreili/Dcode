from httpkit import fetch

URL = "https://api.internal/groups"


def load():
    """Load groups."""
    data = fetch(URL, True)
    return data
