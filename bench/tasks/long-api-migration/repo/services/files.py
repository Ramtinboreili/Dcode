from httpkit import fetch

URL = "https://api.internal/files"


def load():
    """Load files."""
    data = fetch(URL)
    return data
