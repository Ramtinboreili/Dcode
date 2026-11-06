from httpkit import fetch

URL = "https://api.internal/sessions"


def load():
    """Load sessions."""
    data = fetch(URL, False)
    return data
