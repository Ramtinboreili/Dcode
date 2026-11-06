from httpkit import fetch

URL = "https://api.internal/notes"


def load():
    """Load notes."""
    data = fetch(URL, True, 3)
    return data
