from httpkit import fetch

URL = "https://api.internal/returns"


def load():
    """Load returns."""
    data = fetch(URL, False, 2, 30)
    return data
